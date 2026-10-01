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
RUN useradd --create-home --uid 1000 ergonaut \
    && mkdir -p /data /bot \
    && chown ergonaut:ergonaut /data /bot
USER ergonaut

ENV DATA_DIR=/data \
    ERGONAUT_BOTS=/bot \
    PORT=8000
VOLUME ["/data"]
EXPOSE 8000
CMD ["ergonaut", "up"]
