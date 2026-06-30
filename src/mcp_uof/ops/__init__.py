"""
UOF MCP Server — Operations package.

對外只有一個操作面 `OpsRouter`（見 router.py）：實作全部工具，每個工具於開發期靜態綁定到一種
**機制**——`SoapBackend`（SOAP/PublicAPI）或 `WebBackend`（Playwright 驅動網頁）。使用者看不到機制，
也不選機制；`get_backend()` 永遠回傳同一個 router。
"""
from __future__ import annotations

from typing import Optional

from .base import OpsBackend


_backend: Optional[OpsBackend] = None


def get_backend() -> OpsBackend:
    """Return the per-process OpsRouter（單例）。工具層只呼叫 get_backend().<method>()；
    用哪種機制由 router 的 BINDING 決定，對使用者透明。"""
    global _backend
    if _backend is None:
        from .router import OpsRouter
        _backend = OpsRouter()
    return _backend


def reset_backend_for_tests() -> None:
    global _backend
    _backend = None


__all__ = ["OpsBackend", "get_backend", "reset_backend_for_tests"]
