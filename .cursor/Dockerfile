FROM ubuntu

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHON_VERSION=3.9.11
ENV VENV_NAME=django-ergo_env
ENV VIRTUAL_ENV=/home/ubuntu/.venv

# Update package list and install basic dependencies first
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    lsb-release \
    ca-certificates \
    apt-transport-https \
    && rm -rf /var/lib/apt/lists/*

# Add PostgreSQL 17 repository
RUN sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list' && \
    wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | apt-key add -

# Update package list and install system dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    git \
    curl \
    vim \
    nano \
    tree \
    htop \
    unzip \
    sudo \
    software-properties-common \
    libssl-dev \
    libffi-dev \
    libreadline-dev \
    libsqlite3-dev \
    libbz2-dev \
    libncurses5-dev \
    libncursesw5-dev \
    xz-utils \
    tk-dev \
    libxml2-dev \
    libxmlsec1-dev \
    liblzma-dev \
    postgresql-17 \
    postgresql-server-dev-17 \
    postgresql-17-pgvector \
    && rm -rf /var/lib/apt/lists/*

# Initialize PostgreSQL 17 and configure pgvector
RUN mkdir -p /var/run/postgresql && chown -R postgres:postgres /var/run/postgresql && \
    rm -rf /var/lib/postgresql/17/main && \
    su - postgres -c "/usr/lib/postgresql/17/bin/initdb -D /var/lib/postgresql/17/main" && \
    echo "host all  all    0.0.0.0/0  md5" >> /var/lib/postgresql/17/main/pg_hba.conf && \
    echo "listen_addresses='*'" >> /var/lib/postgresql/17/main/postgresql.conf && \
    systemctl enable postgresql

# Set up UV and Python environment for ubuntu user
RUN usermod -aG sudo ubuntu && \
    echo "ubuntu ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers.d/ubuntu
USER ubuntu
WORKDIR /home/ubuntu

# Install UV for ubuntu user
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/home/ubuntu/.local/bin:$PATH"

# Create virtual environment
RUN export VENV_ROOT="/home/ubuntu/.venv" && \
    export PATH="$VENV_ROOT/bin:$PATH" && \
    uv venv --python ${PYTHON_VERSION} ${VENV_ROOT}

# Set up shell environment
RUN echo 'export PATH="/home/ubuntu/.local/bin:$PATH"' >> ~/.bashrc && \
    echo 'export PYTHONPATH="/home/ubuntu:$PYTHONPATH"' >> ~/.bashrc && \
    echo 'export VENV_ROOT="/home/ubuntu/.venv"' >> ~/.bashrc && \
    echo 'export VIRTUAL_ENV="/home/ubuntu/.venv"' >> ~/.bashrc && \
    echo 'export PATH="$VENV_ROOT/bin:$PATH"' >> ~/.bashrc

# Upgrade pip and install wheel
RUN uv pip install --upgrade pip setuptools wheel

# Install all the development dependencies that will be needed
RUN uv pip install \
    twine \
    wheel \
    build \
    "django>=4.2.9,<5.0" \
    django-environ \
    django-extensions \
    pip-freezer \
    pytest \
    pytest-mock \
    ruff \
    coverage \
    djlint \
    pre-commit \
    factory-boy \
    django-coverage-plugin \
    pytest-django \
    psycopg[binary]

# Expose common development ports
EXPOSE 8000 3000 5432 6379
