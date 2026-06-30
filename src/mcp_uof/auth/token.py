"""
TokenAuthProvider — RSA + SOAP GetToken.

This is the original mcp-uof authentication path, preserved verbatim and adapted to the
AuthProvider interface. Token is cached in memory and persisted to
`~/.uof/credentials-<account>-<hash>.json` keyed by identity so multiple deployments don't
poison each other's cache.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import time
from typing import Optional

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from .base import AuthMode, AuthProvider


from .._log import eprint as _eprint  # 診斷一律走 stderr（共用，勿在各檔複製）


CREDENTIALS_DIR = os.path.join(os.path.expanduser("~"), ".uof")
DEFAULT_TOKEN_TTL = 14 * 24 * 60 * 60  # 14 days

# Pre-package single-file credential layout (kept for cleanup compatibility).
_LEGACY_CREDENTIALS_FILE = os.path.join(CREDENTIALS_DIR, "credentials.json")


def _ensure_credentials_dir() -> None:
    if not os.path.exists(CREDENTIALS_DIR):
        os.makedirs(CREDENTIALS_DIR, mode=0o700, exist_ok=True)
        _eprint(f"[auth.token] 已建立憑證目錄: {CREDENTIALS_DIR}")


def _rsa_encrypt(public_key_base64: str, plaintext: str) -> str:
    public_key_xml = base64.b64decode(public_key_base64).decode("utf-8")
    key = _import_rsa_xml_key(public_key_xml)
    cipher = PKCS1_v1_5.new(key)
    encrypted = cipher.encrypt(plaintext.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def _import_rsa_xml_key(xml_string: str) -> RSA.RsaKey:
    from lxml import etree

    root = etree.fromstring(xml_string.encode("utf-8"))
    modulus_elem = root.find("Modulus")
    exponent_elem = root.find("Exponent")
    if modulus_elem is None or exponent_elem is None:
        raise ValueError("RSA XML 格式錯誤：缺少 Modulus 或 Exponent 元素")
    n = int.from_bytes(base64.b64decode(modulus_elem.text.strip()), byteorder="big")
    e = int.from_bytes(base64.b64decode(exponent_elem.text.strip()), byteorder="big")
    return RSA.construct((n, e))


class TokenAuthProvider(AuthProvider):
    mode = AuthMode.TOKEN

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._identity: Optional[str] = None

    # ── Identity helpers ────────────────────────────────────────────
    def _identity_key(self) -> str:
        return "|".join(
            [
                os.getenv("UOF_BASE_URL", ""),
                os.getenv("UOF_APP_NAME", ""),
                os.getenv("UOF_ACCOUNT", ""),
            ]
        )

    def credentials_file(self) -> str:
        account = os.getenv("UOF_ACCOUNT", "anonymous")
        safe_account = re.sub(r"[^A-Za-z0-9_.-]", "_", account) or "anonymous"
        digest = hashlib.sha256(self._identity_key().encode("utf-8")).hexdigest()[:8]
        return os.path.join(CREDENTIALS_DIR, f"credentials-{safe_account}-{digest}.json")

    def required_env_help(self) -> str:
        return (
            "- `UOF_BASE_URL`\n"
            "- `UOF_APP_NAME`\n"
            "- `UOF_RSA_PUBLIC_KEY`\n"
            "- `UOF_ACCOUNT`\n"
            "- `UOF_PASSWORD`"
        )

    # ── AuthProvider surface ────────────────────────────────────────
    def ensure_valid(self) -> None:
        self.fetch_token()

    def status_report(self) -> str:
        account = os.getenv("UOF_ACCOUNT", "(未設定)")
        base_url = os.getenv("UOF_BASE_URL", "(未設定)")
        # 實際向 UOF 重新取得一次 Token 來「真正驗證」登入，而非只看本地快取 TTL——
        # UOF Token 的伺服器端有效期可能短於本地 TTL，只看快取會誤報為有效。
        try:
            self.fetch_token(force_refresh=True)
            return (
                f"✅ 已成功登入 UOF，目前以「{account}」的身份操作。\n"
                f"🔗 連線目標：{base_url}\n"
                f"Token 由系統自動管理，過期時會在工具呼叫時自動更新，使用者不需手動處理。\n"
                f"💡 要改以其他人的身份操作，請切換 MCP 設定（一份設定 = 一個身份）。"
            )
        except RuntimeError as e:
            from .base import auth_failure_message
            return auth_failure_message(str(e))

    def clear(self, all_identities: bool = False) -> None:
        targets = []
        if all_identities:
            if os.path.isdir(CREDENTIALS_DIR):
                for name in os.listdir(CREDENTIALS_DIR):
                    if name.startswith("credentials") and name.endswith(".json"):
                        targets.append(os.path.join(CREDENTIALS_DIR, name))
        else:
            targets.append(self.credentials_file())
            if os.path.exists(_LEGACY_CREDENTIALS_FILE):
                targets.append(_LEGACY_CREDENTIALS_FILE)
        for path in targets:
            if os.path.exists(path):
                os.remove(path)
                _eprint(f"[auth.token] 🗑️ 已清除憑證: {path}")
        self._token = None
        self._expires_at = 0.0
        self._identity = None

    # ── Token-specific API ──────────────────────────────────────────
    def fetch_token(self, force_refresh: bool = False) -> str:
        """取得有效 Token。

        force_refresh=True 時略過記憶體與磁碟快取，強制重新向 UOF 取得——用於
        「快取 Token 已被伺服器端判定失效」時的刷新重試（UOF Token 伺服器端有效期
        可能短於本地 TTL，失效時 Wkf.asmx 回 HTTP 500 而無明確過期訊息）。
        """
        identity = self._identity_key()
        if not force_refresh:
            # 1. memory cache
            if (
                self._token
                and self._identity == identity
                and time.time() < (self._expires_at - 60)
            ):
                return self._token
            # 2. disk cache
            creds = self.read_credentials()
            if creds:
                self._token = creds["token"]
                self._expires_at = creds["expires_at"]
                self._identity = identity
                return self._token
        # 3. SOAP GetToken
        _eprint(
            f"[auth.token] 🔄 正在以 {os.getenv('UOF_ACCOUNT', '?')} 身份呼叫 GetToken..."
        )
        token = self._call_get_token()
        ttl_env = os.getenv("UOF_TOKEN_TTL")
        ttl = int(ttl_env) if ttl_env else DEFAULT_TOKEN_TTL
        self._write_credentials(token, ttl)
        self._token = token
        self._expires_at = time.time() + ttl
        self._identity = identity
        return token

    def _call_get_token(self) -> str:
        from ..soap_client import uof_client
        from ..domains.system.endpoints import AUTH_ENDPOINT

        app_name = os.getenv("UOF_APP_NAME", "")
        public_key = os.getenv("UOF_RSA_PUBLIC_KEY", "")
        account = os.getenv("UOF_ACCOUNT", "")
        password = os.getenv("UOF_PASSWORD", "")
        if not all([app_name, public_key, account, password]):
            raise RuntimeError(
                "UOF_APP_NAME、UOF_RSA_PUBLIC_KEY、UOF_ACCOUNT、UOF_PASSWORD "
                "必須全部設定。請檢查 .env 檔案。"
            )
        encrypted_account = _rsa_encrypt(public_key, account)
        encrypted_password = _rsa_encrypt(public_key, password)
        # 登入失敗的回應型態不一致：有時回空 GetTokenResponse（200），有時直接 HTTP 500
        # （如密碼錯誤導致伺服器端 RSADecrypt 失敗）。兩者都收斂為 RuntimeError，
        # 讓上層以固定訊息回報，不把原始 500 堆疊丟給介接 AI。
        try:
            result = uof_client.call(
                endpoint_path=AUTH_ENDPOINT,
                method_name="GetToken",
                params={
                    "appName": app_name,
                    "account": encrypted_account,
                    "password": encrypted_password,
                },
            )
        except Exception as e:
            brief = " ".join(str(e).split()).split("For more information")[0].strip()[:160]
            raise RuntimeError(
                f"呼叫 GetToken 失敗（帳號「{account}」）：{brief or e.__class__.__name__}。"
                "可能是帳號或密碼錯誤、RSA 公私鑰不相符，或 UOF 服務暫時無法連線。"
            )
        if not result:
            raise RuntimeError(
                f"GetToken 未回傳憑證（帳號「{account}」）：可能是帳號或密碼錯誤、密碼已變更、"
                "帳號被停用，或 RSA 公私鑰不相符。"
            )
        return result.strip()

    # ── Disk persistence ─────────────────────────────────────────────
    def read_credentials(self) -> Optional[dict]:
        path = self.credentials_file()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            _eprint(f"[auth.token] ❌ 讀取憑證檔失敗: {e}")
            return None
        token = data.get("token")
        expires_at = data.get("expires_at", 0)
        if not token:
            return None
        if data.get("identity") and data.get("identity") != self._identity_key():
            return None
        if time.time() >= (expires_at - 60):
            _eprint(
                f"[auth.token] ⏰ Token 已過期 "
                f"(expires_at={expires_at}, now={time.time():.0f})"
            )
            return None
        return data

    def _write_credentials(self, token: str, ttl: int) -> None:
        _ensure_credentials_dir()
        path = self.credentials_file()
        now = time.time()
        data = {
            "token": token,
            "expires_at": now + ttl,
            "ttl": ttl,
            "issued_at": now,
            "account": os.getenv("UOF_ACCOUNT", ""),
            "app_name": os.getenv("UOF_APP_NAME", ""),
            "base_url": os.getenv("UOF_BASE_URL", ""),
            "identity": self._identity_key(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        _eprint(f"[auth.token] ✅ 憑證已寫入 {path} (ttl={ttl}s)")
