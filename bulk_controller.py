import asyncio
import re
import logging
from fastapi import APIRouter, HTTPException
from services.fetch import get_secret_from_gcp
from models.bulk_models import BulkPANRequest
from services.pan_service import fetch_pan_details_async
from services.utils import generate_case_id
from slowapi import Limiter
from slowapi.util import get_remote_address
from fastapi import Request, Header
from services.rate_limiter import limiter

logger = logging.getLogger(__name__)

router = APIRouter()

project_id = "hsbc-12597193-ingatewyuat-dev"
manager_name = "USER_firestartGWSSU"

PAN_REGEX = r'^[A-Z]{5}\d{4}[A-Z]{1}$'

# Only 1 bulk request at a time
GLOBAL_REQUEST_LOCK = asyncio.Lock()


# CALL KARZA WITH RETRY
async def call_karza(pan: str, case_id: str, name: str):

    MAX_RETRIES = 3

    for attempt in range(MAX_RETRIES):
        try:
            logger.info(f"Calling Karza for PAN (attempt {attempt + 1}/{MAX_RETRIES})")

            result = await fetch_pan_details_async(
                pan=pan,
                consent="Y",
                lite="N",
                case_id=case_id,
                panstatus="Y",
                name=name
            )

            # Success - return immediately
            if isinstance(result, dict) and "error" not in result:
                return result

            # Rate limit - retry
            if isinstance(result, dict) and result.get("error") == "Rate limit exceeded":
                logger.warning(f"Karza rate limit for PAN {pan}, retrying...")
                await asyncio.sleep(2 ** attempt)
                continue

            # Timeout or connection error - retry
            if isinstance(result, dict) and result.get("error") in ["Timeout", "Connection error"]:
                logger.warning(f"Karza {result.get('error')} for PAN {pan}, retrying...")
                await asyncio.sleep(2 ** attempt)
                continue

            # Any other error - return actual error
            logger.error(f"Karza error for PAN {pan}: {result}")
            return result

        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed for PAN {pan}: {str(e)}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                return {"error": str(e)}

    return {"error": f"Failed after {MAX_RETRIES} retries"}


# PROCESS SINGLE PAN
async def process_single_pan(pan: str, name: str):
    case_id = generate_case_id()
    pan = pan.upper()
    logger.info(f"Processing PAN Request")

    # Validate PAN format
    if len(pan) != 10 or not re.match(PAN_REGEX, pan):
        logger.warning("Invalid PAN Format Received")
        return {
            "pan": pan,
            "error": "Invalid PAN format"
        }

    # Call Karza - waits for full response before returning
    response = await call_karza(pan, case_id, name)

    # Success
    if isinstance(response, dict) and "result" in response:
        profile_match = response["result"].get("profileMatch", [])
        match_score = profile_match[0].get("matchScore") if profile_match else None
        match_result = profile_match[0].get("matchResult") if profile_match else None
        return {
            "pan": pan,
            "name": response["result"].get("name"),
            "matchScore": match_score,
            "matchResult": match_result,
            "PANStatus": response["result"].get("status")
        }

    # 402 Insufficient Credits
    if isinstance(response, dict) and "402" in str(response):
        logger.error(f"Karza insufficient credits for PAN {pan}")
        raise HTTPException(status_code=402, detail="Insufficient Credits")

    # Return actual error from Karza instead of generic message
    actual_error = response.get("error", str(response)) if isinstance(response, dict) else str(response)
    logger.error(f"Karza returned error for PAN {pan}: {actual_error}")
    return {
        "pan": pan,
        "error": actual_error
    }


# BULK ENDPOINT
@router.post("/bulk_pan")
@limiter.limit("90/minute")
async def bulk_pan_verification(
        request: Request,
        req: BulkPANRequest,
        x_api_key: str = Header(None)
):
    logger.info("BULK PAN Verification request received")

    # Validate API key from header
    if not x_api_key:
        logger.warning("Missing API key")
        raise HTTPException(status_code=401, detail="Missing API key")

    try:
        stored_api_key = get_secret_from_gcp(manager_name, project_id)
    except Exception:
        logger.error(f"Secret Unauthorized")
        raise HTTPException(status_code=401, detail="Unauthorized")

    if x_api_key != stored_api_key:
        logger.warning("Invalid API Key Used")
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Validate panList
    if not req.panList:
        logger.warning("Empty Pan List Received")
        raise HTTPException(status_code=400, detail="panList cannot be empty")

    if len(req.panList) > 10:
        logger.warning("More Pan Received than Expected")
        raise HTTPException(status_code=400, detail="List of PAN are more per request")

    # Strict Sequential Processing - one at a time
    async with GLOBAL_REQUEST_LOCK:

        results = []

        for item in req.panList:
            logger.info(f"Processing next PAN in batch")
            result = await process_single_pan(item.pan, item.name)
            results.append(result)
            logger.info(f"Response received, moving to next PAN")

        logger.info(f"BULK REQUEST COMPLETED TOTAL:{len(results)}")
        return {
            "total": len(results),
            "results": results
        }
