"""Authentication contract, provider singleton, and MCP tool guard."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from enum import Enum
from functools import wraps
from typing import Any, Callable, Optional


class AuthMode(str, Enum):
    """認證機制描述符：session = Login.aspx cookie（web 機制用）。"""
    SESSION = "session"


class AuthProvider(ABC):
    """Abstract auth provider; the web session implementation lives behind this."""

    mode: AuthMode  # set by subclass

    @abstractmethod
    def ensure_valid(self) -> None:
        """Refresh credentials if needed. Raises RuntimeError on hard auth failure."""

    @abstractmethod
    def status_report(self) -> str:
        """Human-readable check_auth output. Triggers a refresh if needed."""

    @abstractmethod
    def credentials_file(self) -> str:
        """Reserved identity-specific metadata path; session cookies are not persisted."""

    @abstractmethod
    def clear(self, all_identities: bool = False) -> None:
        """Clear in-memory authentication state."""

    def required_env_help(self) -> str:
        """Bullet list of env vars this provider requires — shown when auth fails."""
        return (
            "- `UOF_BASE_URL`\n"
            "- `UOF_ACCOUNT`\n"
            "- `UOF_PASSWORD`"
        )


def auth_failure_message(detail: str = "") -> str:
    """
    登入／憑證失敗時給介接 AI 的固定訊息。

    目的：帳號密碼失效或登入有誤時，要讓介接的 AI 對使用者清楚、固定地說明這是設定層級
    的登入問題，而不是讓 AI 自行猜測或沿用對話記憶中的帳號資訊臆測。
    """
    account = os.getenv("UOF_ACCOUNT", "(未設定)")
    base_url = os.getenv("UOF_BASE_URL", "(未設定)")
    detail_line = f"原因：{detail}\n" if detail else ""
    return (
        f"🔒 UOF 登入失敗：無法以帳號「{account}」取得有效憑證。\n"
        f"{detail_line}"
        f"連線目標：{base_url}\n\n"
        "這是設定層級的問題，需要由設定這個 MCP 的人處理，無法在對話中自行解決。常見原因：\n"
        "- UOF_ACCOUNT 或 UOF_PASSWORD 不正確，或密碼已在 UOF 變更\n"
        "- 該帳號在 UOF 被停用，或未設定部門/職級\n"
        "- UOF_BASE_URL 連線設定不正確\n\n"
        "請直接、明確告訴使用者：「UOF 登入失敗，請檢查 MCP 設定中的帳號、密碼與連線設定是否正確」。\n"
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


def get_provider() -> AuthProvider:
    """Return the current authentication provider."""
    return get_session_provider()


def reset_provider_for_tests() -> None:
    """Test hook — drop cached provider so the next getter rebuilds it."""
    global _session_provider
    _session_provider = None


def require_auth(func: Callable[..., Any]) -> Callable[..., Any]:
    """工具入口的認證閘——驗證 web session 認證有效才放行。

    驗證集中在這一道入口閘，工具內部不再重複驗證；session 失效重登由 backend 自行處理。
    失敗回固定字串而非 raise，避免 MCP client 收到 isError。裝飾期會 fail-loud 驗證 op 已在
    BINDING 登錄（漏綁 / 工具改名 / 裝飾順序錯誤都會在 import server 時立刻爆）。
    """
    op = func.__name__.removeprefix("uof_custom_")
    from ..ops.router import mechanisms_for as _validate_op
    _validate_op(op)

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            get_session_provider().ensure_valid()
        except Exception as e:
            return auth_failure_message(" ".join(str(e).split())[:160])
        return func(*args, **kwargs)
    return wrapper
