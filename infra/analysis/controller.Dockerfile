# Analysis controller. Never mounts workspace PVCs. Built by
# .github/workflows/analysis-images.yml with PYTHON_BASE from base-images.env;
# the build fails unless it is digest-pinned. Dependencies come only from the
# hash-locked controller.requirements.txt.
ARG PYTHON_BASE
FROM ${PYTHON_BASE}
ARG PYTHON_BASE
RUN case "${PYTHON_BASE}" in *@sha256:????????????????????????????????????????????????????????????????) ;; \
      *) echo "PYTHON_BASE must be pinned by sha256 digest" >&2; exit 1 ;; esac
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /opt/analysis
COPY infra/analysis/controller.requirements.txt ./requirements.txt
RUN pip install --require-hashes --only-binary=:all: -r requirements.txt
COPY pyproject.toml README.md HISTORY.md ./
COPY src ./src
RUN pip install --no-deps --no-build-isolation --no-index . \
    && rm -rf build src/*.egg-info && chmod -R a-w /opt/analysis
USER 10002:10002
ENTRYPOINT ["python", "-I", "-m", "django_ergo.analysis.controller"]
