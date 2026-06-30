"""共用診斷輸出：一律走 stderr。

stdio MCP 下 stdout 專供 JSON-RPC，任何混入 stdout 的文字都會破壞協定。診斷/log 訊息一律經
本模組輸出到 stderr。這條規則只在這裡有單一實作——各模組 `from .._log import eprint`，不要再各自
複製一份（複製就會在某處漏改而污染 stdout）。
"""
from __future__ import annotations

import sys


def eprint(*args, **kwargs) -> None:
    """print 到 stderr（呼叫端可覆寫 file）。"""
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)
