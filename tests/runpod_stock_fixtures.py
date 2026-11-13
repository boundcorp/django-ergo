"""RunPod v2 catalog responses (GET /v2/catalog/gpus?include=AVAILABILITY&product=POD).

Shapes follow https://docs.runpod.io/api-reference-v2/catalog/list-gpu-types;
prices and stock levels are representative, not live quotes.
"""

import copy
import io
import json
from urllib.error import HTTPError

NO_CAPACITY_BODY = {
    "error": (
        "create pod: There are no longer any instances available with the "
        "requested specifications. Please refresh and try again."
    ),
    "status": 500,
}


def _gpu(gpu_id, name, memory, secure, centers, community=None):  # noqa: PLR0913
    return {
        "id": gpu_id,
        "name": name,
        "pool": None,
        "manufacturer": "NVIDIA",
        "memory": memory,
        "secure": True,
        "community": community is not None,
        "price": {"secure": secure, "community": community or 0},
        "maxCount": {"secure": 8, "community": 0},
        "availability": max(
            (level for _, level in centers),
            key=["NONE", "LOW", "MEDIUM", "HIGH"].index,
            default="NONE",
        ),
        **(
            {
                "dataCenters": [
                    {"id": center, "name": center, "availability": level}
                    for center, level in centers
                ],
                "minVcpuCount": 16,
                "minRam": 188,
            }
            if centers
            else {}
        ),
    }


STOCK = [
    _gpu(
        "NVIDIA A100-SXM4-80GB",
        "A100 SXM",
        80,
        1.19,
        [("US-WA-1", "LOW"), ("US-KS-2", "MEDIUM"), ("EU-RO-1", "HIGH")],
    ),
    _gpu(
        "NVIDIA H100 80GB HBM3",
        "H100 SXM",
        80,
        2.69,
        [("US-KS-2", "HIGH")],
    ),
    _gpu(
        "NVIDIA RTX PRO 6000 Blackwell Server Edition",
        "RTX PRO 6000",
        96,
        1.09,
        [("US-MD-1", "HIGH"), ("EU-RO-1", "LOW")],
    ),
    _gpu("NVIDIA L40S", "L40S", 48, 0.86, [("US-MD-1", "HIGH")], community=0.79),
    _gpu("NVIDIA GeForce RTX 4090", "RTX 4090", 24, 0.69, [("EU-RO-1", "HIGH")]),
    _gpu("NVIDIA H200", "H200", 141, 3.99, []),
]


def stock():
    return copy.deepcopy(STOCK)


def respond(payload):
    return io.BytesIO(json.dumps(payload).encode())


def no_capacity(url):
    return HTTPError(url, 500, "Internal Server Error", {}, respond(NO_CAPACITY_BODY))


def hw(container_disk_gb=200, **changes):
    """Validated H3-like requirements for direct configure_create tests."""
    from django_ergo.plugins.runpod.hardware import requirements

    return requirements(
        {
            "hardware": {
                "gpus": ["NVIDIA A100-SXM4-80GB"],
                "min_vram_gb": 48,
                "min_ram_per_gpu_gb": 167,
                "min_vcpu_per_gpu": 10,
                "min_driver_major": 580,
                "compute_capabilities": ["8.0", "9.0"],
                "container_disk_gb": container_disk_gb,
                **changes,
            }
        }
    )
