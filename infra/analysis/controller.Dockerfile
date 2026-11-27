# Proposal only. Controller image never mounts workspace PVCs.
ARG PYTHON_BASE
FROM ${PYTHON_BASE}
WORKDIR /opt/analysis
COPY pyproject.toml README.md HISTORY.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[analysis-controller]' && chmod -R a-w /opt/analysis
USER 10002:10002
ENTRYPOINT ["python", "-m", "django_ergo.analysis.controller"]
