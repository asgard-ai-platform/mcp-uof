"""Login.aspx authentication backed by an in-memory httpx cookie jar."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Optional

from .base import AuthMode, AuthProvider


from .._log import eprint as _eprint  # 診斷一律走 stderr（共用，勿在各檔複製）


CREDENTIALS_DIR = Path(os.path.expanduser("~")) / ".uof"
DEFAULT_SESSION_TTL = 20 * 60


def _ensure_dir() -> None:
    if not CREDENTIALS_DIR.exists():
        CREDENTIALS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)


class SessionAuthProvider(AuthProvider):
    mode = AuthMode.SESSION

    def __init__(self) -> None:
        self._last_validated: float = 0.0
        self._identity_cached: Optional[str] = None
        # Logged-in user's display info, populated after successful login.
        self.logged_in_display_name: Optional[str] = None

    # ── Identity helpers ────────────────────────────────────────────
    def _identity_key(self) -> str:
        return "|".join(
            [
                os.getenv("UOF_BASE_URL", ""),
                os.getenv("UOF_ACCOUNT", ""),
            ]
        )

    def credentials_file(self) -> str:
        """Path stub for this identity (cookies live in-memory via httpx.Client)."""
        account = os.getenv("UOF_ACCOUNT", "anonymous")
        safe_account = re.sub(r"[^A-Za-z0-9_.-]", "_", account) or "anonymous"
        digest = hashlib.sha256(self._identity_key().encode("utf-8")).hexdigest()[:8]
        return str(CREDENTIALS_DIR / f"identity-{safe_account}-{digest}.json")

    def required_env_help(self) -> str:
        return (
            "- `UOF_BASE_URL`\n"
            "- `UOF_ACCOUNT`\n"
            "- `UOF_PASSWORD`"
        )

    # ── AuthProvider surface ────────────────────────────────────────
    def ensure_valid(self) -> None:
        """Verify or re-establish the httpx session (login if cookie expired)."""
        # Avoid hammering the server: re-validate at most once per 30s.
        if time.time() - self._last_validated < 30 and self._identity_cached == self._identity_key():
            return
        self._validate_env()
        from ..ops.http_web import get_http_session
        get_http_session()._ensure_logged_in()
        self.logged_in_display_name = os.getenv("UOF_ACCOUNT", "")
        self._last_validated = time.time()
        self._identity_cached = self._identity_key()

    def status_report(self) -> str:
        account = os.getenv("UOF_ACCOUNT", "(未設定)")
        base_url = os.getenv("UOF_BASE_URL", "(未設定)")
        try:
            self.ensure_valid()
        except RuntimeError as e:
            from .base import auth_failure_message
            return auth_failure_message(str(e))
        display = self.logged_in_display_name or account
        return (
            f"✅ Session 有效，目前以 **{account}**（{display}）的身份操作"
            f"（認證：httpx web session）。\n"
            f"🔗 Base URL: {base_url}\n"
        )

    def clear(self, all_identities: bool = False) -> None:
        from ..ops.http_web import reset_http_session
        reset_http_session()
        self._last_validated = 0.0
        self._identity_cached = None

    # ── Internal ────────────────────────────────────────────────────
    def _validate_env(self) -> None:
        missing = [
            k for k in ("UOF_BASE_URL", "UOF_ACCOUNT", "UOF_PASSWORD")
            if not os.getenv(k)
        ]
        if missing:
            raise RuntimeError(
                "UOF_BASE_URL、UOF_ACCOUNT、UOF_PASSWORD 必須全部設定。"
                f"目前缺少: {', '.join(missing)}"
            )

    # ── Optional identity metadata ──────────────────────────────────
    def write_metadata(self) -> None:
        """Write optional identity metadata; this does not persist session cookies."""
        _ensure_dir()
        meta_path = self.credentials_file() + ".meta"
        data = {
            "account": os.getenv("UOF_ACCOUNT", ""),
            "base_url": os.getenv("UOF_BASE_URL", ""),
            "identity": self._identity_key(),
            "logged_in_at": time.time(),
            "display_name": self.logged_in_display_name or "",
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.chmod(meta_path, stat.S_IRUSR | stat.S_IWUSR)
