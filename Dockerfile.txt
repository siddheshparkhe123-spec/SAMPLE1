FROM nexus3.systems.uk.hsbc:18096/com/hsbc/group/itid/es/dc/ubuntu/gcr-ubuntu-2404:latest

ARG API_NAME
ARG API_VERSION
ARG PIP_NAME
ARG PIP_LOC
ENV PIP_ACCOUNT="${PIP_NAME}:${PIP_LOC}"

ENV GUNICORN_CMD_ARGS="--bind=0.0.0.0:8080 --workers=1"
ENV SCRIPT_NAME="/${API_NAME}/v${API_VERSION}"
ENV API_NAME=$API_NAME

ENV NEXUS_URL="nexus302.systems.uk.hsbc:8081/nexus/repository/pypi-proxy_n3p/simple"
ENV NEXUS_HOST="nexus302.systems.uk.hsbc:8081"

USER root

#1. INIT SETUP
COPY ["target/${API_NAME}-${API_VERSION}.tar.gz", "/tmp/"]
RUN mkdir -p /opt/apps/${API_NAME}-${API_VERSION} \
    && tar -xf /tmp/${API_NAME}-${API_VERSION}.tar.gz -C /opt/apps/${API_NAME}-${API_VERSION}/ \
    && rm -rf /tmp/*

WORKDIR /opt/apps/${API_NAME}-${API_VERSION}/

RUN --mount=type=secret,id=auth,target=/etc/apt/auth.conf \
    apt update --fix-missing -y || true \
    && apt-get dist-upgrade -y \
    && apt-get install -y --no-install-recommends \
        python3.12 \
        python3.12-venv \
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1

#  CREATE VENV
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip --version \
    && apt-get purge -y python3-pip-whl || true \
    # USE VENV
    && /opt/venv/bin/pip install --upgrade pip \
    \
    # INSTALL DEPENDENCIES FROM NEXUS (INSIDE VENV)
    && /opt/venv/bin/pip install -r requirements.txt \
        --index-url https://${PIP_ACCOUNT}@${NEXUS_URL} \
        --trusted-host ${NEXUS_HOST} \
    \
    # ENSURE UVICORN INSTALLED IN VENV
    && /opt/venv/bin/pip install uvicorn \
        --index-url https://${PIP_ACCOUNT}@${NEXUS_URL} \
        --trusted-host ${NEXUS_HOST} \
    \
    # (OPTIONAL) GUNICORN
    && /opt/venv/bin/pip show gunicorn -q || \
        /opt/venv/bin/pip install gunicorn==20.1.0 \
            --index-url https://${PIP_ACCOUNT}@${NEXUS_URL} \
            --trusted-host ${NEXUS_HOST} \
    \
    # CLEANUP
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

#  FORCE ALL COMMANDS TO USE VENV
ENV PATH="/opt/venv/bin:$PATH"

# 3. DEBUG (optional but useful)
RUN which python && which pip && which uvicorn

# 4. CREATE USER
RUN groupadd -g 2000 apiadmgroup \
    && useradd -u 2000 -g 2000 -ms /bin/bash apiadm \
    && chown -R apiadm:apiadmgroup /opt/

RUN chmod u+s /usr/bin/rm \
    && chmod u+s /usr/bin/find

RUN rm -f /opt/apps/${API_NAME}-${API_VERSION}/docker.sh \
    && find /opt/apps/${API_NAME}-${API_VERSION} -name "docker.sh" -type f -delete

# 5. START API
EXPOSE 8080

USER apiadm

#  ALWAYS RUN FROM VENV
CMD ["/opt/venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
