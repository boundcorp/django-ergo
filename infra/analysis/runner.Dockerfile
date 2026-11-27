# Analysis job runner. Built by .github/workflows/analysis-images.yml with
# PYTHON_BASE from base-images.env; the build fails unless it is digest-pinned.
# Dependencies come only from the hash-locked runner.requirements.txt.
ARG PYTHON_BASE
FROM ${PYTHON_BASE}
ARG PYTHON_BASE
RUN case "${PYTHON_BASE}" in *@sha256:????????????????????????????????????????????????????????????????) ;; \
      *) echo "PYTHON_BASE must be pinned by sha256 digest" >&2; exit 1 ;; esac
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /opt/analysis
COPY infra/analysis/runner.requirements.txt ./requirements.txt
RUN pip install --require-hashes --only-binary=:all: -r requirements.txt
COPY pyproject.toml README.md HISTORY.md ./
COPY src ./src
# The executor imports only the standard library and the locked extras, not Django.
RUN pip install --no-deps --no-build-isolation --no-index . \
    && rm -rf build src/*.egg-info && chmod -R a-w /opt/analysis
ENV HOME=/tmp MPLBACKEND=Agg OPENBLAS_NUM_THREADS=1
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python", "-I", "-m", "django_ergo.analysis.executor"]
