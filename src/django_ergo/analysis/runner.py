"""Fail-closed isolated-runner contract.

No production backend is supplied. A future controller must enforce the policy
independently; merely implementing this protocol does not establish isolation.
"""

from dataclasses import dataclass
from typing import Protocol


class RunnerUnavailableError(RuntimeError):
    """No approved isolated runner is available."""


@dataclass(frozen=True)
class Limits:
    timeout_seconds: int = 60
    memory_bytes: int = 512 * 1024 * 1024
    disk_bytes: int = 1024 * 1024 * 1024
    output_bytes: int = 64 * 1024
    pids: int = 64
    cpu_millis: int = 1000

    def __post_init__(self):
        for name, ceiling in (
            ("timeout_seconds", 300),
            ("memory_bytes", 2**30),
            ("disk_bytes", 2**31),
            ("output_bytes", 2**20),
            ("pids", 128),
            ("cpu_millis", 2000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                msg = f"Invalid {name}"
                raise ValueError(msg)


class IsolatedController(Protocol):
    """Future trusted controller; handles must be authorized by the caller and controller."""

    def execute(
        self, *, session_id: str, user_id: str, code: str, limits: Limits
    ) -> dict:
        """Run in a separately enforced boundary and reap the job before returning."""


def execute(
    *, session_id: str, user_id: str, code: str, limits: Limits | None = None
) -> dict:
    """No backend is wired in: execution is unavailable, without fallback."""
    msg = "Isolated analysis controller is not configured"
    raise RunnerUnavailableError(msg)
