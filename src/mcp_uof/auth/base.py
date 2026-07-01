"""
Auth provider base + the two per-mechanism providers.

`AuthProvider` defines the minimal surface every concrete provider must satisfy. There are
two: `get_token_provider()` (SOAP/PublicAPI — RSA + GetToken) and `get_session_provider()`
(web — Login.aspx cookie). Each is cached per process so it can hold long-lived state
(token cache, browser session). `require_auth` validates whichever the tool's bound
mechanism needs; `get_provider()` remains as a token-provider alias for compat shims.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from enum import Enum
from functools import wraps
from typing import Any, Callable, Optional


class AuthMode(str, Enum):
    """認證機制描述符：token = RSA + GetToken（SOAP 機制用）；session = Login.aspx cookie（web 機制用）。

    這只是各 UOF 機制標記自己用哪種認證，不是使用者可選的模式。
    """
    TOKEN = "token"
    SESSION = "session"


class AuthProvider(ABC):
    """Abstract auth provider; both token + session implementations live behind this."""

    mode: AuthMode  # set by subclass

    @abstractmethod
    def ensure_valid(self) -> None:
        """Refresh credentials if needed. Raises RuntimeError on hard auth failure."""

    @abstractmethod
    def status_report(self) -> str:
        """Human-readable check_auth output. Triggers a refresh if needed."""

    @abstractmethod
    def credentials_file(self) -> str:
        """Path to the on-disk credential/cookie cache for this identity."""

    @abstractmethod
    def clear(self, all_identities: bool = False) -> None:
        """Wipe cached credentials (memory + disk)."""

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
        "- 此 UOF 站台未提供 PublicAPI，SOAP 認證無法運作\n\n"
        "請直接、明確告訴使用者：「UOF 登入失敗，請檢查 MCP 設定中的帳號、密碼與連線設定是否正確」。\n"
        "不要猜測其他原因、不要沿用先前對話記得的帳號或密碼、也不要假設已經解決。"
    )


_token_provider: Optional[AuthProvider] = None
_session_provider: Optional[AuthProvider] = None


def get_token_provider() -> AuthProvider:
    """SOAP 機制用的認證（RSA 帳密 → GetToken token）。惰性建立、單例。"""
    global _token_provider
    if _token_provider is None:
        from .token import TokenAuthProvider
        _token_provider = TokenAuthProvider()
    return _token_provider


def get_session_provider() -> AuthProvider:
    """web 機制用的認證（Login.aspx 登入 → cookie / storage state）。惰性建立、單例。"""
    global _session_provider
    if _session_provider is None:
        from .session import SessionAuthProvider
        _session_provider = SessionAuthProvider()
    return _session_provider


def get_provider() -> AuthProvider:
    """相容入口 = SOAP 身份（token）的認證 provider。

    現在機制是每工具綁定的（見 ops.router）：SOAP 工具用 token、web 工具用 session，各自取得
    所需 provider。此函式保留為 token provider 的別名，給仍以單一身份語意呼叫的相容 shim
    （get_token / credentials_file / domains.system）。"""
    return get_token_provider()


def reset_provider_for_tests() -> None:
    """Test hook — drop cached providers so the next getter rebuilds them."""
    global _token_provider, _session_provider
    _token_provider = None
    _session_provider = None


def _provider_for(mechanism: str) -> AuthProvider:
    """機制 → 它需要的認證 provider。soap→token、web/http_web→session。"""
    if mechanism in ("web", "http_web"):
        return get_session_provider()
    return get_token_provider()  # "soap" 與未知值都用 token（與舊行為一致）


def _mechanisms_for_call(op: str, args: tuple, kwargs: dict) -> list:
    """依本次呼叫參數決定認證機制。

    `BINDING` 是工具層預設；起單相關工具還有表單層分派：registry 命中的表單會走 web_apply，
    因此入口認證也要驗 session，不能先被 SOAP token 擋下。
    """
    from ..ops.router import mechanisms_for

    key = None
    if op in ("get_form_structure_by_id",):
        key = kwargs.get("form_id") if "form_id" in kwargs else (args[0] if args else None)
    elif op in ("get_form_structure", "preview_workflow", "apply_form"):
        key = kwargs.get("form_version_id") if "form_version_id" in kwargs else (args[0] if args else None)

    if key:
        from ..ops import web_apply
        if web_apply.resolve(str(key)):
            return ["web"]
        if op in ("get_form_structure", "preview_workflow", "apply_form"):
            return ["web"]
    return mechanisms_for(op)


def require_auth(func: Callable[..., Any]) -> Callable[..., Any]:
    """工具入口的認證閘——依「該工具設計上綁定的機制」驗證對應認證，而非一律驗 SOAP token。

    一個工具可走多條機制時（fallback，SOAP 優先）採 **OR**：任一機制的認證通過就放行；
    只有當它所有可走機制的認證**都**不過，才回固定的失敗訊息（對使用者只有「通過與否」）。
    驗證集中在這一道入口閘，工具內部不再重複驗證；機制本身的失效重試（token 自動刷新、
    web session 重登）仍各自在 backend 處理。失敗回字串而非 raise，避免 MCP client 收到 isError。
    """
    op = func.__name__.removeprefix("uof_custom_")
    # fail-loud（裝飾期）：op 必須在 BINDING 有登錄機制綁定。漏綁、工具改名、或裝飾順序錯誤
    # （@require_auth 被放到 @mcp.tool 外層導致 __name__ 變 wrapper）都會讓 op 解析不到綁定，
    # 在 import server 時就立刻爆，而非讓 web 工具在執行期靜默回歸成被 SOAP token 擋。
    from ..ops.router import mechanisms_for as _validate_op
    _validate_op(op)

    @wraps(func)
    def wrapper(*args, **kwargs):
        errors = []
        for mech in _mechanisms_for_call(op, args, kwargs):
            try:
                _provider_for(mech).ensure_valid()
            except Exception as e:
                errors.append(f"{mech}: {' '.join(str(e).split())[:120]}")
                continue
            return func(*args, **kwargs)   # 任一機制認證通過即放行；工具本體例外不包成登入失敗
        # 所有可走機制都認證不過 → 才是真正不可用
        return auth_failure_message("；".join(errors))
    return wrapper
