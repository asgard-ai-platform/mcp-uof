"""Session cookie 落地：把 httpx cookie jar 存到磁碟，讓 process 重啟後免重登。

存的是可重放的 session cookie、等同登入態，所以目錄與檔案都收緊到 0700 / 0600（既有目錄也會
主動檢查擁有者與權限），暫存檔用 `mkstemp` 建立以避免被搶先建立符號連結。

**檔名以「站台 + 實際登入帳號」命名**：同一台機器上不同人各自一份，不會互相覆蓋或誤載。
瀏覽器登入的帳號在登入時取得（見 `browser_login`）；啟動時還不知道自己是誰，因此：
`UOF_SESSION_NAMESPACE` / `UOF_ACCOUNT` 有設就直接定位，都沒設時只在「該站台剛好只有一份存檔」
才沿用，有多份就寧可要求重新登入，也不猜。

設定：`UOF_SESSION_DIR` 指定目錄（預設 `~/.uof`）、`UOF_SESSION_PERSIST=false` 完全不落地。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from http import cookiejar as _cookiejar
from pathlib import Path
from typing import Optional

from .._log import eprint as _eprint

STORE_VERSION = 1


DEFAULT_DIR_NAME = ".uof"


def credentials_dir() -> Path:
    """session 存放目錄：`UOF_SESSION_DIR` 優先，否則 `~/.uof`。

    每次呼叫都重讀環境變數，不在模組載入時定死——mounted 測試會覆寫 HOME，且部署端可能在
    程序啟動後才注入設定。支援 `~` 展開，方便寫成 `~/somewhere/uof-session`。
    """
    override = os.getenv("UOF_SESSION_DIR", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return Path(os.path.expanduser("~")) / DEFAULT_DIR_NAME


def persist_enabled() -> bool:
    return os.getenv("UOF_SESSION_PERSIST", "true").strip().lower() not in ("false", "0", "no")


def session_namespace() -> str:
    """在啟動時（還不知道登入者是誰）用來定位存檔的鍵；空字串代表「不確定」。"""
    return (os.getenv("UOF_SESSION_NAMESPACE", "").strip()
            or os.getenv("UOF_ACCOUNT", "").strip())


class SessionDirUnsafe(RuntimeError):
    """session 目錄不是本使用者獨占，拒絕在裡面存放等同登入態的資料。"""


def _ensure_dir() -> Path:
    """建立（或收緊）session 目錄。

    `mkdir(mode=...)` 只在建立當下生效且受 umask 影響，既有目錄完全不會被檢查——所以這裡
    每次都驗擁有者並主動 chmod。目錄若屬於別人就直接拒絕：那代表對方能讀走 session。
    """
    d = credentials_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = d.stat()
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise SessionDirUnsafe(
            f"{d} 的擁有者不是目前使用者（uid {st.st_uid}），拒絕在此存放 session。"
            "請改用 UOF_SESSION_DIR 指到自己的目錄，或設 UOF_SESSION_PERSIST=false。"
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        _eprint(f"[auth.store] ⚠️ {d} 權限過寬（{oct(mode)}），已收緊為 0700")
        os.chmod(d, 0o700)
    return d


def _safe_name(value: str) -> str:
    value = value or ""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", value) or "anonymous"
    if value and safe != value:
        # 轉換有損時（a/b 與 a?b 都變 a_b）補原字串雜湊；一般 ASCII 帳號不加，檔名維持穩定。
        return f"{safe}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"
    return safe


def _storage_key(account: str = "") -> str:
    """存檔檔名採用的身份鍵，存與讀共用；顯式 namespace 優先，否則實際帳號，再退回 UOF_ACCOUNT。"""
    ns = os.getenv("UOF_SESSION_NAMESPACE", "").strip()
    return ns or account or os.getenv("UOF_ACCOUNT", "").strip()


def _base_digest(base_url: str = "") -> str:
    base_url = base_url or os.getenv("UOF_BASE_URL", "")
    return hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:8]


def identity_key(base_url: str = "", account: str = "") -> str:
    base_url = base_url or os.getenv("UOF_BASE_URL", "")
    return f"{base_url}|{account or session_namespace()}"


def session_path(base_url: str = "", account: str = "") -> Path:
    """本身份的存檔路徑；檔名鍵由 `_storage_key` 決定，存與讀共用同一規則。"""
    who = _storage_key(account)
    return credentials_dir() / f"session-{_base_digest(base_url)}-{_safe_name(who)}.json"


def _candidates_for_base(base_url: str = "") -> list:
    """同一站台底下所有身份的存檔（用於「不知道自己是誰」時判斷有無歧義）。"""
    d = credentials_dir()
    if not d.exists():
        return []
    return sorted(d.glob(f"session-{_base_digest(base_url)}-*.json"))


# ── cookie ↔ dict ────────────────────────────────────────────────────

def _cookie_to_dict(c: "_cookiejar.Cookie") -> dict:
    return {
        "name": c.name,
        "value": c.value or "",
        "domain": c.domain or "",
        "path": c.path or "/",
        "secure": bool(c.secure),
        "expires": c.expires,
    }


def _dict_to_cookie(d: dict) -> "_cookiejar.Cookie":
    domain = d.get("domain") or ""
    expires = d.get("expires")
    return _cookiejar.Cookie(
        version=0,
        name=d["name"],
        value=d.get("value") or "",
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=d.get("path") or "/",
        path_specified=True,
        secure=bool(d.get("secure")),
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


# ── 存取 ──────────────────────────────────────────────────────────────

def save_session(client, *, base_url: str = "", account: str = "", source: str = "browser") -> Optional[Path]:
    """把 client 的 cookie jar 寫入磁碟；關閉持久化或沒有 cookie 時回 None。

    `account` 是**實際登入的帳號**（瀏覽器登入時由代理取得），決定存檔歸誰所有。
    """
    if not persist_enabled():
        return None
    cookies = [_cookie_to_dict(c) for c in client.cookies.jar]
    if not cookies:
        return None
    try:
        directory = _ensure_dir()
    except SessionDirUnsafe as ex:
        _eprint(f"[auth.store] ⚠️ 不落地：{ex}")
        return None
    path = session_path(base_url, account)
    payload = {
        "version": STORE_VERSION,
        "base_url": base_url or os.getenv("UOF_BASE_URL", ""),
        "account": account or session_namespace(),
        "source": source,
        "saved_at": time.time(),
        "cookies": cookies,
    }
    # mkstemp：名稱不可預測、O_EXCL、0600 —— 固定或帶 pid 的暫存檔名可被搶先建成符號連結。
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".session-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    _eprint(f"[auth.store] session 已存檔（{len(cookies)} cookies, source={source}, "
            f"account={payload['account'] or '未知'}）")
    return path


def resolve_session_file(base_url: str = "") -> Optional[Path]:
    """啟動時決定要載入哪一份存檔。

    有 namespace（`UOF_SESSION_NAMESPACE` / `UOF_ACCOUNT`）就直接定位。都沒設時本程序還不知道
    自己是誰——此時只有「該站台剛好只有一份存檔」才沿用；有多份代表同機有多個身份，猜錯就是拿
    別人的身份操作，所以寧可要求重新登入。
    """
    if session_namespace():
        path = session_path(base_url)
        return path if path.exists() else None
    found = _candidates_for_base(base_url)
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    _eprint(
        f"[auth.store] ⚠️ 此站台有 {len(found)} 份 session 存檔，無法判斷本程序屬於哪一個身份，"
        "不沿用任何一份。請為每個 server entry 設定 UOF_SESSION_NAMESPACE 以區分身份。"
    )
    return None


def load_session(client, *, base_url: str = "", account: str = "") -> Optional[dict]:
    """把磁碟上的 cookie 灌回 client；成功回 meta dict，無檔或無可用 cookie 回 None。"""
    if not persist_enabled():
        return None
    path = session_path(base_url, account) if account else resolve_session_file(base_url)
    if path is None or not path.exists():
        return None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            _eprint(f"[auth.store] ⚠️ {path} 權限過寬（{oct(mode)}），已改回 0600")
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as ex:
        _eprint(f"[auth.store] ⚠️ 讀取 session 存檔失敗，忽略（{type(ex).__name__}: {ex}）")
        return None
    if payload.get("version") != STORE_VERSION:
        _eprint(f"[auth.store] session 存檔版本不符（{payload.get('version')}），忽略")
        return None
    # 設了 UOF_ACCOUNT 就代表本程序該是那個人；存檔屬於別人時絕不沿用，否則會安靜地換身份。
    expected = os.getenv("UOF_ACCOUNT", "").strip()
    stored = (payload.get("account") or "").strip()
    if expected and stored and stored.lower() != expected.lower():
        _eprint(f"[auth.store] ⚠️ 存檔屬於 {stored!r}，但本程序設定為 {expected!r}，不予沿用")
        return None
    now = time.time()
    loaded = 0
    for d in payload.get("cookies", []):
        exp = d.get("expires")
        if exp is not None and exp <= now:
            continue  # 已過期，不要灌回去
        try:
            client.cookies.jar.set_cookie(_dict_to_cookie(d))
            loaded += 1
        except Exception as ex:
            _eprint(f"[auth.store] ⚠️ cookie {d.get('name')!r} 還原失敗：{ex}")
    if not loaded:
        return None
    _eprint(f"[auth.store] 已載入存檔 session（{loaded} cookies, source={payload.get('source')}）")
    return payload


def clear_session(base_url: str = "", account: str = "") -> bool:
    """刪除本身份正在用的 session 存檔；有刪到回 True。

    定位方式與載入時一致（`resolve_session_file`），否則 logout 會刪錯檔或刪不到。
    """
    path = session_path(base_url, account) if account else resolve_session_file(base_url)
    if path is None:
        return False
    try:
        path.unlink()
        _eprint(f"[auth.store] 已刪除 session 存檔 {path.name}")
        return True
    except FileNotFoundError:
        return False


def clear_all_sessions() -> int:
    """刪除本機所有身份的 session 存檔，回刪除數量。"""
    d = credentials_dir()
    if not d.exists():
        return 0
    n = 0
    for p in d.glob("session-*.json"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    if n:
        _eprint(f"[auth.store] 已刪除 {n} 份 session 存檔")
    return n
