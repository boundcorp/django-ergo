# Proposal only. Supply a reviewed digest, e.g. python:3.12-slim@sha256:... .
ARG PYTHON_BASE
FROM ${PYTHON_BASE}
WORKDIR /opt/analysis
COPY pyproject.toml README.md HISTORY.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[analysis-runner]' && chmod -R a-w /opt/analysis
ENV HOME=/tmp MPLBACKEND=Agg OPENBLAS_NUM_THREADS=1
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "django_ergo.analysis.executor"]
