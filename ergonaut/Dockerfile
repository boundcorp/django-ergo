# Build from the django-ergo repo root so Ergo is installed from source:
#   docker build -f ergonaut/Dockerfile .
ARG IMAGE_VERSION=python:3.12-slim

#
# Base Stage
FROM ${IMAGE_VERSION} AS base

ENV PATH=/app/.venv/bin:$PATH \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

RUN mkdir /.cache && chmod -R 777 /.cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    libjpeg-dev \
    libcurl4-openssl-dev \
    bash \
    libxml2-dev \
    libxslt-dev \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools uv

# Install Node.js for Vite build
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

#
# Builder Stage
FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libffi-dev \
    libssl-dev \
    libcairo2-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY ergonaut/requirements.freeze.txt /app/
RUN uv venv /app/.venv
RUN uv pip install --no-cache-dir -r requirements.freeze.txt

# Ergo, from this repo
COPY pyproject.toml setup.py README.md HISTORY.md /ergo/
COPY src/ /ergo/src/
RUN uv pip install --no-cache-dir "/ergo[legacy,bots]"

# Build frontend
COPY ergonaut/frontend/package.json ergonaut/frontend/package-lock.json* /app/frontend/
RUN cd /app/frontend && npm ci
COPY ergonaut/frontend/ /app/frontend/
RUN cd /app/frontend && npm run build

COPY ergonaut/ergonaut/ /app/ergonaut
COPY ergonaut/infra/ /app/infra
COPY ergonaut/manage.py /app/
COPY ergonaut/pyproject.toml ergonaut/README.md /app/
RUN uv pip install --no-deps -e .

#
# Developer Stage
FROM base AS dev

ENV PYTHONPATH=/app:$PYTHONPATH \
    VIRTUAL_ENV=/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    libxml2-dev \
    libxslt-dev \
    libcurl4-openssl-dev \
    libssl-dev \
    libffi-dev \
    libcairo2-dev \
    pkg-config \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools uv

WORKDIR /app

RUN uv venv /venv
COPY ergonaut/requirements.freeze.txt /app/
RUN uv pip install --no-cache-dir -r requirements.freeze.txt

ENV PATH="/venv/bin:/usr/local/bin:${PATH}"

# Dev mounts the repo at /ergo and installs Ergo editable from it on start.
COPY ergonaut/ergonaut/ /app/ergonaut
COPY ergonaut/infra/ /app/infra

#
# Release Stage
FROM base AS release

RUN apt-get update && apt-get install -y --no-install-recommends \
    libfreetype6-dev \
    libjpeg62-turbo-dev \
    libpng-dev \
    && rm -rf /var/lib/apt/lists/*

# Agent toolchain: bots shell out to git/ssh/gh, kubectl, the Claude Code and omp
# CLIs, and the Orca CLI. Installed here so both `release` and `aio` have it.
ARG ORCA_VERSION=1.4.180
ARG ORCA_LINUX_APPIMAGE_SHA256=af3b6e6a67fcc3cd034ca469ff8ee1d224b1a6e2df6215fd8452afd0fd8e92ab

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    ca-certificates \
    gnupg \
    kubernetes-client \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# omp runs on Bun.
RUN npm install -g bun @anthropic-ai/claude-code @oh-my-pi/pi-coding-agent \
    && npm cache clean --force

# Orca: pinned Linux AppImage, checksum verified before extraction. Extracted
# (no FUSE needed) and exposed through its bundled CLI. Libraries are the
# Electron runtime dependencies of the extracted app.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdrm2 \
    libgbm1 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libpulse0 \
    libsndfile1 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL \
        "https://github.com/stablyai/orca/releases/download/v${ORCA_VERSION}/orca-linux.AppImage" \
        -o /tmp/orca.AppImage \
    && echo "${ORCA_LINUX_APPIMAGE_SHA256}  /tmp/orca.AppImage" | sha256sum --check \
    && chmod 0755 /tmp/orca.AppImage \
    && cd /tmp && ./orca.AppImage --appimage-extract >/dev/null \
    && mv /tmp/squashfs-root /opt/orca \
    && chmod -R a+rX /opt/orca \
    && rm -f /tmp/orca.AppImage \
    && ln -s /opt/orca/resources/bin/orca-ide /usr/local/bin/orca

COPY --from=builder /app /app
WORKDIR /app

RUN mkdir -p /app/static/uploads && chmod 777 /app/static/uploads
ENV PATH=/app/.venv/bin:$PATH

RUN python manage.py collectstatic --noinput

#
# All-in-one Stage: `ergonaut up` runs Postgres (pgserver), Redis and Garage
# inside the container unless DATABASE_URL, CELERY_BROKER_URL or
# S3_ENDPOINT_URL point elsewhere.
#   docker run -v "$PWD:/bot" -v ergonaut-data:/data -p 8000:8000 ghcr.io/boundcorp/ergonaut
FROM release AS aio

RUN apt-get update && apt-get install -y --no-install-recommends redis-server \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dxflrs/garage:v1.1.0 /garage /usr/local/bin/garage

# Postgres refuses to run as root.
RUN useradd --create-home --uid 10001 ergonaut \
    && mkdir -p /data /bot \
    && chown ergonaut:ergonaut /data /bot
USER ergonaut

ENV DATA_DIR=/data \
    ERGONAUT_BOTS=/bot \
    PORT=8000
VOLUME ["/data"]
EXPOSE 8000
CMD ["ergonaut", "up"]
