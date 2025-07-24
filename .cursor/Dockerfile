FROM ubuntu

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHON_VERSION=3.9.11
ENV VENV_NAME=django-ergo_env

# Update package list and install system dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    git \
    curl \
    wget \
    vim \
    nano \
    tree \
    htop \
    unzip \
    software-properties-common \
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release \
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
    && rm -rf /var/lib/apt/lists/*

# Set up UV and Python environment for ubuntu user
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
    pytest-django

# Expose common development ports
EXPOSE 8000 3000 5432 6379

# Default command
CMD ["/bin/bash"]