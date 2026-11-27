"""Inputs of the analysis runner/controller images (blocker B4) stay reviewed.

The images are built by .github/workflows/analysis-images.yml. These checks keep
the hash locks in step with pyproject.toml, the bases digest-pinned and the
runner entrypoint equal to the job pod command that admission pins.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

from django_ergo.analysis.kubernetes import KubernetesRuntime

ROOT = Path(__file__).parents[1]
INFRA = ROOT / "infra" / "analysis"
EXTRAS = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
    "optional-dependencies"
]


def requirements(path):
    lines = (line.split("#")[0].strip() for line in path.read_text().splitlines())
    return [line for line in lines if line]


@pytest.mark.parametrize("image", ["runner", "controller"])
def test_lock_inputs_match_pyproject_extras(image):
    wanted = requirements(INFRA / f"{image}.requirements.in")
    assert sorted(wanted) == sorted(
        [*EXTRAS["analysis-" + image], "setuptools", "wheel"]
    )


@pytest.mark.parametrize("image", ["runner", "controller"])
def test_locks_pin_every_package_with_hashes(image):
    lock = (INFRA / f"{image}.requirements.txt").read_text()
    entries = re.split(r"\n(?=[a-z0-9])", lock.split("\n", 1)[1].strip())
    pinned = {}
    for entry in entries:
        if entry.startswith("#"):
            continue
        match = re.match(r"([a-z0-9._-]+)==(\S+)", entry)
        assert match, entry[:80]
        assert "--hash=sha256:" in entry, match.group(1)
        pinned[match.group(1)] = match.group(2)
    for spec in requirements(INFRA / f"{image}.requirements.in"):
        name = re.match(r"[A-Za-z0-9._-]+", spec).group(0).lower()
        assert name in pinned, name
    assert "django" not in pinned  # the images never import Django


def test_base_images_are_digest_pinned():
    values = dict(
        line.split("=", 1)
        for line in (INFRA / "base-images.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    for key in ("PYTHON_BASE", "NGINX_UNPRIVILEGED"):
        assert re.fullmatch(
            r"docker\.io/[a-z/._-]+:[\w.-]+@sha256:[0-9a-f]{64}", values[key]
        )
    assert re.fullmatch(r"\d{8}\.\d+", values["GVISOR_RELEASE"])
    assert re.fullmatch(r"[0-9a-f]{128}", values["GVISOR_SHA512"])


@pytest.mark.parametrize("image", ["runner", "controller"])
def test_dockerfiles_install_only_locked_dependencies(image):
    text = (INFRA / f"{image}.Dockerfile").read_text()
    assert f"COPY infra/analysis/{image}.requirements.txt" in text
    assert "--require-hashes" in text
    assert "pip install --no-deps --no-build-isolation --no-index ." in text
    assert "PYTHON_BASE must be pinned by sha256 digest" in text
    installs = re.findall(r"pip install[^\n]*", text)
    assert len(installs) == 2


def test_runner_entrypoint_is_the_admitted_pod_command():
    text = (INFRA / "runner.Dockerfile").read_text()
    entrypoint = re.search(r"^ENTRYPOINT (\[.*\])$", text, re.MULTILINE).group(1)
    runtime = KubernetesRuntime(
        core=None,
        namespace="analysis",
        image="registry/runner@sha256:" + "a" * 64,
        storage_class="hard-quota",
        runtime_class="gvisor",
    )
    container = runtime.pod_spec("j", "w", {"op": "list"})["spec"]["containers"][0]
    assert json.loads(entrypoint) == container["command"]
    assert "USER 10001:10001" in text


def test_workflow_pins_actions_and_drops_checkout_token():
    workflow = (ROOT / ".github" / "workflows" / "analysis-images.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)", workflow)
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
    checkouts = workflow.count("uses: actions/checkout@")
    assert workflow.count("persist-credentials: false") == checkouts
    # Publishing (and the registry login) only from main, never on PRs.
    assert "pull_request_target" not in workflow
    assert (
        "PUBLISH: ${{ github.event_name == 'push' && "
        "github.ref == 'refs/heads/main' }}" in workflow
    )
    assert workflow.count("if: env.PUBLISH == 'true'") == 2
    assert not re.search(r"\|\s*(sudo\s+)?(ba)?sh\b", workflow)
