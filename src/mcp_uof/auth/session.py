"""Login.aspx authentication backed by an in-memory httpx cookie jar.

session 依序從三處取得：存檔（`store.py`）→ 環境變數帳密 → 瀏覽器登入（`browser_login.py`，
前兩者不成立時拋 `BrowserLoginRequired`）。細節見 docs/architecture.md。
"""
from __future__ import annotations

import os
import time
from typing import Optional

from . import store
from .base import AuthProvider


from .._log import eprint as _eprint  # 診斷一律走 stderr（共用，勿在各檔複製）


def has_password_credentials() -> bool:
    """是否具備「環境變數帳密自動登入」的條件（第 2 段備援）。"""
    return bool(os.getenv("UOF_ACCOUNT") and os.getenv("UOF_PASSWORD"))


class SessionAuthProvider(AuthProvider):

    def __init__(self) -> None:
        self._last_validated: float = 0.0
        self._identity_cached: Optional[str] = None

    # ── Identity helpers ────────────────────────────────────────────
    def _identity_key(self) -> str:
        return store.identity_key()

    # ── AuthProvider surface ────────────────────────────────────────
    def ensure_valid(self) -> None:
        """Verify or re-establish the httpx session (login if cookie expired)."""
        from ..ops.http_web import get_http_session
        session = get_http_session()
        # browser login pending 必須優先於 30 秒 validation cache；否則 force 換身份剛清完
        # cookie 時，舊身份留下的 cache 會讓受保護工具略過認證閘。
        if session.session_source == "browser_pending":
            from .base import BrowserLoginRequired
            raise BrowserLoginRequired("瀏覽器登入仍在等待使用者完成")
        # Avoid hammering the server: re-validate at most once per 30s.
        if time.time() - self._last_validated < 30 and self._identity_cached == self._identity_key():
            return
        self._validate_env()
        session._ensure_logged_in()
        self._last_validated = time.time()
        self._identity_cached = self._identity_key()

    def clear(self, all_identities: bool = False) -> None:
        """清除認證狀態：記憶體 session、背景登入流程，以及磁碟上的 session 存檔。"""
        from ..ops.http_web import current_http_session, reset_http_session
        from .browser_login import shutdown_flow

        # 先取實際登入帳號再 reset：瀏覽器身份可能與 UOF_ACCOUNT 不同，否則會刪錯檔。
        sess = current_http_session()
        account = (sess.session_account if sess else "") or ""

        shutdown_flow()
        reset_http_session()
        if all_identities:
            store.clear_all_sessions()
        else:
            store.clear_session(account=account)
        self._last_validated = 0.0
        self._identity_cached = None

    # ── Internal ────────────────────────────────────────────────────
    def _validate_env(self) -> None:
        """只有連線位址是硬性必要；帳密改為選填（沒有就走瀏覽器登入）。"""
        if not os.getenv("UOF_BASE_URL"):
            raise RuntimeError("UOF_BASE_URL 必須設定（UOF 站台網址，含虛擬路徑、不含尾斜線）。")
        account, password = os.getenv("UOF_ACCOUNT"), os.getenv("UOF_PASSWORD")
        if bool(account) != bool(password):
            # 只設一半通常是打錯或漏設，靜默退回瀏覽器登入會讓人以為備援生效了。
            missing = "UOF_PASSWORD" if account else "UOF_ACCOUNT"
            _eprint(
                f"[auth.session] ⚠️ UOF_ACCOUNT / UOF_PASSWORD 只設定了一半（缺 {missing}），"
                "帳密自動登入不會生效，將改用瀏覽器登入。"
            )
