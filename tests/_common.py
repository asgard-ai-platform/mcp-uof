"""兩層測試共用的路徑、環境載入與斷言工具。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # mcp-uof/
SRC = ROOT / "src"
PYTHON = sys.executable

_REQUIRED_ENV = ("UOF_BASE_URL", "UOF_ACCOUNT", "UOF_PASSWORD")


def ensure_src_on_path() -> None:
    """把 src 加到 sys.path，讓 `import mcp_uof.*` 在以腳本方式執行時也可用。"""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))


def load_env() -> dict:
    """載入 mcp-uof/.env 到 os.environ（不覆寫既有），並回傳其 dict（給子程序 env 注入用）。"""
    from dotenv import dotenv_values, load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    return {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}


def missing_env() -> list:
    """回傳缺少的真實環境設定鍵（空 list 代表齊備）。"""
    return [k for k in _REQUIRED_ENV if not os.getenv(k)]


def has_live_env() -> bool:
    return not missing_env()


def ok(text: str) -> bool:
    """回應視為成功：非空、且不含失敗標記 ❌ / 🔒。"""
    return bool(text) and "❌" not in text and "🔒" not in text


def check(label: str, cond: bool, detail: str = "") -> int:
    """印出一行 ✅/❌ 並回傳失敗數（0 或 1）；呼叫端用 `failures += check(...)` 累加。

    呼叫端用 `failures += check(...)` 累加結果。
    """
    if cond:
        print(f"  ✅ {label}")
        return 0
    print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
    return 1
