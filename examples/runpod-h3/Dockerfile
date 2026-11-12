# Preparation only: no checkpoint is downloaded at build time.
FROM nvidia/cuda:13.0.0-cudnn-devel-ubuntu24.04@sha256:e3fd439288969c0c78d52768cda4a10ab20f408180d6fa2933f94f7af7810cb0
ENV DEBIAN_FRONTEND=noninteractive HF_HOME=/workspace/hf-cache
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-venv python3-pip git curl ffmpeg ca-certificates \
    build-essential pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*
RUN python3.12 -m venv /opt/venv
ENV PATH=/opt/venv/bin:/root/.cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup.sh && \
    sh /tmp/rustup.sh -y --profile minimal --default-toolchain 1.93.0 && \
    rm /tmp/rustup.sh
RUN pip install --no-cache-dir uv && \
    git clone https://github.com/sgl-project/sglang.git /opt/sglang && \
    git -C /opt/sglang checkout 2511743bd784e69e5a81ca3d926a000711dae4ab && \
    uv pip install -e '/opt/sglang/python[diffusion]' --prerelease=allow && \
    uv pip install huggingface_hub==1.28.0 comfy-kitchen && \
    uv pip freeze > /opt/h3-environment.txt
COPY examples/runpod-h3/launch.py /opt/h3-launch.py
ENTRYPOINT ["python", "/opt/h3-launch.py"]
