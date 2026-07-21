"""Process-wide operations facade."""
from __future__ import annotations

from typing import Optional

from .base import OpsBackend


_backend: Optional[OpsBackend] = None


def get_backend() -> OpsBackend:
    """Return the process-wide OpsRouter."""
    global _backend
    if _backend is None:
        from .router import OpsRouter
        _backend = OpsRouter()
    return _backend


def reset_backend_for_tests() -> None:
    global _backend
    _backend = None


__all__ = ["OpsBackend", "get_backend", "reset_backend_for_tests"]
