"""Authentication contract, provider singleton, and MCP tool guard."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from functools import wraps
from typing import Any, Callable, Optional


class BrowserLoginRequired(RuntimeError):
    """尚未登入，需要使用者親自在瀏覽器完成登入。

    與 `auth_failure_message`（設定錯了，對話中解不了）刻意分開——這個是使用者按幾下就好，
    兩者給介接 AI 的指示完全不同。
    """


class AuthProvider(ABC):
    """Abstract auth provider; the web session implementation lives behind this."""

    @abstractmethod
    def ensure_valid(self) -> None:
        """Refresh credentials if needed. Raises RuntimeError on hard auth failure."""

    @abstractmethod
    def clear(self, all_identities: bool = False) -> None:
        """Clear authentication state (memory session, login flow, stored session file)."""


def browser_login_required_message(detail: str = "") -> str:
    """尚未登入時給介接 AI 的固定訊息：明確要它呼叫 `uof_custom_login`，而不是要使用者改設定，
    也不要自行猜測帳號密碼。"""
    base_url = os.getenv("UOF_BASE_URL", "(未設定)")
    detail_line = f"（{detail}）\n" if detail else ""
    return (
        "🔑 尚未登入 UOF，需要使用者親自在瀏覽器完成登入。\n"
        f"{detail_line}"
        f"連線目標：{base_url}\n\n"
        "請立刻呼叫 `uof_custom_login` 工具：它會在使用者的預設瀏覽器開啟 UOF 登入頁，\n"
        "使用者輸入帳密（或公司 AD）登入後，登入狀態會自動交回本 MCP，接著就能繼續操作。\n\n"
        "請直接告訴使用者：「需要你在瀏覽器登入 UOF，我現在幫你開啟登入頁」。\n"
        "不要向使用者索取帳號密碼、不要沿用先前對話記得的帳密、也不要假設已經登入成功。"
    )


def auth_failure_message(detail: str = "") -> str:
    """設定層級失敗時給介接 AI 的固定訊息：說清楚這要由設定的人處理，不讓 AI 自行臆測，
    也不沿用對話記憶中的帳號資訊。"""
    account = os.getenv("UOF_ACCOUNT", "") or "(未設定，走瀏覽器登入)"
    base_url = os.getenv("UOF_BASE_URL", "(未設定)")
    detail_line = f"原因：{detail}\n" if detail else ""
    return (
        f"🔒 UOF 登入失敗：無法以帳號「{account}」取得有效憑證。\n"
        f"{detail_line}"
        f"連線目標：{base_url}\n\n"
        "這是設定層級的問題，需要由設定這個 MCP 的人處理，無法在對話中自行解決。常見原因：\n"
        "- UOF_BASE_URL 連線設定不正確，或站台無法連線\n"
        "- 有設定 UOF_ACCOUNT / UOF_PASSWORD 但內容不正確，或密碼已在 UOF 變更\n"
        "- 該帳號在 UOF 被停用，或未設定部門/職級\n\n"
        "請直接、明確告訴使用者：「UOF 登入失敗，請檢查 MCP 設定中的連線與帳號設定是否正確」。\n"
        "不要猜測其他原因、不要沿用先前對話記得的帳號或密碼、也不要假設已經解決。"
    )


_session_provider: Optional[AuthProvider] = None


def get_session_provider() -> AuthProvider:
    """Return the process-wide Login.aspx cookie-session provider."""
    global _session_provider
    if _session_provider is None:
        from .session import SessionAuthProvider
        _session_provider = SessionAuthProvider()
    return _session_provider


def reset_provider_for_tests() -> None:
    """Test hook — drop cached provider so the next getter rebuilds it."""
    global _session_provider
    _session_provider = None


def require_auth(func: Callable[..., Any]) -> Callable[..., Any]:
    """工具入口的認證閘——驗證集中在這一道，工具內部不再重複驗證。

    失敗回固定字串而非 raise，避免 MCP client 收到 isError：`BrowserLoginRequired` → 🔑 指示去
    呼叫 `uof_custom_login`，其餘 → 🔒 設定層級說明。工具本體也攔 `BrowserLoginRequired`（session
    可能通過閘門後才過期）；其他例外原樣拋出。裝飾期 fail-loud 驗證 op 已在 BINDING 登錄。
    """
    op = func.__name__.removeprefix("uof_custom_")
    from ..ops.router import mechanisms_for as _validate_op
    _validate_op(op)

    @wraps(func)
    def wrapper(*args, **kwargs):
        from ..ops.http_web.session import session_lifecycle
        with session_lifecycle().operation():
            try:
                get_session_provider().ensure_valid()
            except BrowserLoginRequired as e:
                return browser_login_required_message(" ".join(str(e).split())[:160])
            except Exception as e:
                return auth_failure_message(" ".join(str(e).split())[:160])
            try:
                return func(*args, **kwargs)
            except BrowserLoginRequired as e:
                return browser_login_required_message(" ".join(str(e).split())[:160])
    return wrapper
