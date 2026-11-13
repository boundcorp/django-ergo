"""Hardware requirements and GPU identity as host policy data, not code.

A profile's ``config.hardware`` declares what the image needs; the account's
optional ``gpu_catalog`` extends or overrides :data:`DEFAULT_GPU_CATALOG`.
Changing GPUs, regions, caps or identities is a policy edit, never a deploy.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from decimal import InvalidOperation

# Provider catalog ID -> NVML display names, compute capability and VRAM (GB).
# Names that are not listed still pass the in-Pod check when the observed
# compute capability is allowed and VRAM/driver meet the profile's minimums.
DEFAULT_GPU_CATALOG = {
    "NVIDIA A100-SXM4-80GB": {
        "nvml_names": ["NVIDIA A100-SXM4-80GB", "A100-SXM4-80GB"],
        "compute_capability": "8.0",
        "vram_gb": 80,
    },
    "NVIDIA A100 80GB PCIe": {
        "nvml_names": ["NVIDIA A100 80GB PCIe"],
        "compute_capability": "8.0",
        "vram_gb": 80,
    },
    "NVIDIA H100 80GB HBM3": {
        "nvml_names": ["NVIDIA H100 80GB HBM3"],
        "compute_capability": "9.0",
        "vram_gb": 80,
    },
    "NVIDIA H100 PCIe": {
        "nvml_names": ["NVIDIA H100 PCIe"],
        "compute_capability": "9.0",
        "vram_gb": 80,
    },
    "NVIDIA H100 NVL": {
        "nvml_names": ["NVIDIA H100 NVL"],
        "compute_capability": "9.0",
        "vram_gb": 94,
    },
    "NVIDIA H200": {
        "nvml_names": ["NVIDIA H200"],
        "compute_capability": "9.0",
        "vram_gb": 141,
    },
    "NVIDIA B200": {
        "nvml_names": ["NVIDIA B200"],
        "compute_capability": "10.0",
        "vram_gb": 180,
    },
    "NVIDIA L40S": {
        "nvml_names": ["NVIDIA L40S"],
        "compute_capability": "8.9",
        "vram_gb": 48,
    },
    "NVIDIA L40": {
        "nvml_names": ["NVIDIA L40"],
        "compute_capability": "8.9",
        "vram_gb": 48,
    },
    "NVIDIA RTX 6000 Ada Generation": {
        "nvml_names": ["NVIDIA RTX 6000 Ada Generation"],
        "compute_capability": "8.9",
        "vram_gb": 48,
    },
    "NVIDIA RTX A6000": {
        "nvml_names": ["NVIDIA RTX A6000"],
        "compute_capability": "8.6",
        "vram_gb": 48,
    },
    "NVIDIA A40": {
        "nvml_names": ["NVIDIA A40"],
        "compute_capability": "8.6",
        "vram_gb": 48,
    },
    "NVIDIA RTX PRO 6000 Blackwell Server Edition": {
        "nvml_names": ["NVIDIA RTX PRO 6000 Blackwell Server Edition"],
        "compute_capability": "12.0",
        "vram_gb": 96,
    },
    "NVIDIA RTX PRO 6000 Blackwell Workstation Edition": {
        "nvml_names": ["NVIDIA RTX PRO 6000 Blackwell Workstation Edition"],
        "compute_capability": "12.0",
        "vram_gb": 96,
    },
}

DEFAULT_HARDWARE = {
    "gpus": [],
    "any_gpu": False,
    "gpu_count": 1,
    "min_vram_gb": 0,
    "min_ram_per_gpu_gb": 8,
    "min_vcpu_per_gpu": 2,
    "min_cuda_version": None,
    "min_driver_major": 0,
    "compute_capabilities": [],
    "cloud_type": "SECURE",
    "countries": [],
    "data_centers": "any",
    "exclude_data_centers": [],
    "container_disk_gb": 200,
    "stock_poll_seconds": 60,
    "stock_wait_seconds": 1800,
    "max_create_attempts": 10,
}

DEFAULT_RENTAL_CAP_USD = "3.00"
MAX_STOCK_POLL_SECONDS = 600
MAX_STOCK_WAIT_SECONDS = 86400

_CAPABILITY = re.compile(r"\d+\.\d+")
_DATA_CENTER = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)+")
_COUNTRY = re.compile(r"[A-Z]{2}")
_CUDA = re.compile(r"\d+(?:\.\d+)?")


def _fail(field):
    msg = f"Invalid hardware requirement: {field}"
    raise ValueError(msg)


def _strings(value, field, pattern=None):
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        _fail(field)
    if pattern is not None and any(not pattern.fullmatch(item) for item in value):
        _fail(field)
    if len(set(value)) != len(value):
        _fail(field)
    return list(value)


def _integer(value, field, minimum=0, maximum=None):
    if type(value) is not int or value < minimum:
        _fail(field)
    if maximum is not None and value > maximum:
        _fail(field)
    return value


def _number(value, field):
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        _fail(field)
    return value


def requirements(config):
    """Validated ``config.hardware`` with defaults; the envelope a launch may choose in."""
    raw = config.get("hardware")
    if not isinstance(raw, dict):
        msg = "Profile config.hardware requirements are required"
        raise ValueError(msg)  # noqa: TRY004 -- operator-facing policy error
    unknown = set(raw) - set(DEFAULT_HARDWARE)
    if unknown:
        msg = "Unknown hardware requirement: " + ", ".join(sorted(unknown))
        raise ValueError(msg)
    hw = {**DEFAULT_HARDWARE, **raw}
    hw["gpus"] = _strings(hw["gpus"], "gpus")
    if type(hw["any_gpu"]) is not bool:
        _fail("any_gpu")
    if not hw["gpus"] and not hw["any_gpu"]:
        msg = "Hardware requirements need a gpus preference list or any_gpu"
        raise ValueError(msg)
    _integer(hw["gpu_count"], "gpu_count", 1, 8)
    _number(hw["min_vram_gb"], "min_vram_gb")
    _integer(hw["min_ram_per_gpu_gb"], "min_ram_per_gpu_gb", 1)
    _integer(hw["min_vcpu_per_gpu"], "min_vcpu_per_gpu", 1)
    if hw["min_cuda_version"] is not None and (
        not isinstance(hw["min_cuda_version"], str)
        or not _CUDA.fullmatch(hw["min_cuda_version"])
    ):
        _fail("min_cuda_version")
    _integer(hw["min_driver_major"], "min_driver_major")
    hw["compute_capabilities"] = _strings(
        hw["compute_capabilities"], "compute_capabilities", _CAPABILITY
    )
    if hw["cloud_type"] not in ("SECURE", "COMMUNITY"):
        _fail("cloud_type")
    hw["countries"] = _strings(hw["countries"], "countries", _COUNTRY)
    if hw["data_centers"] != "any":
        hw["data_centers"] = _strings(hw["data_centers"], "data_centers", _DATA_CENTER)
        if not hw["data_centers"]:
            _fail("data_centers")
    hw["exclude_data_centers"] = _strings(
        hw["exclude_data_centers"], "exclude_data_centers", _DATA_CENTER
    )
    _integer(hw["container_disk_gb"], "container_disk_gb", 1)
    _integer(hw["stock_poll_seconds"], "stock_poll_seconds", 5, MAX_STOCK_POLL_SECONDS)
    _integer(hw["stock_wait_seconds"], "stock_wait_seconds", 0, MAX_STOCK_WAIT_SECONDS)
    _integer(hw["max_create_attempts"], "max_create_attempts", 1, 100)
    return hw


def catalog(overrides=None):
    """Default catalog with the account's ``gpu_catalog`` entries added/replaced."""
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        msg = "gpu_catalog must be a mapping"
        raise TypeError(msg)
    merged = json.loads(json.dumps(DEFAULT_GPU_CATALOG))
    for gpu, entry in overrides.items():
        if entry is None:
            merged.pop(gpu, None)
            continue
        if (
            not isinstance(gpu, str)
            or not gpu
            or not isinstance(entry, dict)
            or set(entry) != {"nvml_names", "compute_capability", "vram_gb"}
            or not isinstance(entry["compute_capability"], str)
            or not _CAPABILITY.fullmatch(entry["compute_capability"])
        ):
            msg = f"Invalid gpu_catalog entry: {gpu}"
            raise ValueError(msg)
        _strings(entry["nvml_names"], f"gpu_catalog {gpu} nvml_names")
        _number(entry["vram_gb"], f"gpu_catalog {gpu} vram_gb")
        merged[gpu] = entry
    return merged


def min_vram_mib(hw):
    """NVML reports usable memory slightly below the marketed size (48 GB -> 45000)."""
    return int(hw["min_vram_gb"] * 1000 * 15 // 16)


def identity(gpu_type, hw, gpu_catalog):
    """In-Pod acceptance rule bound into the create request for one catalog GPU."""
    entry = gpu_catalog.get(gpu_type) or {}
    return {
        "gpu_count": hw["gpu_count"],
        "nvml_names": list(entry.get("nvml_names", [])),
        "compute_capabilities": list(hw["compute_capabilities"]),
        "min_vram_mib": min_vram_mib(hw),
        "min_driver_major": hw["min_driver_major"],
    }


def rental_cap(policy):
    """Per-rental planned USD ceiling from host policy (default 3.00)."""
    try:
        cap = Decimal(str(policy.get("max_rental_usd", DEFAULT_RENTAL_CAP_USD)))
    except (InvalidOperation, ValueError):
        msg = "Invalid max_rental_usd"
        raise ValueError(msg) from None
    if not cap.is_finite() or cap <= 0:
        msg = "Invalid max_rental_usd"
        raise ValueError(msg)
    return cap
