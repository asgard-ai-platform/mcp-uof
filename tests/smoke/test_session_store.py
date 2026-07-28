"""Offline smoke checks for the ~/.uof session cookie store (auth/store.py).

離線：不連 UOF、不起子程序。用 tempdir 覆寫 HOME，驗證存取 round-trip、檔案權限、
身份隔離（不同帳號不得互相載到對方的 session）與過期 cookie 不還原。

執行：uv run python tests/smoke/test_session_store.py
"""
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

_common.ensure_src_on_path()

import httpx

from mcp_uof.auth import store


BASE = "https://uof.example.com/UOF"


def _client_with(name: str, value: str, *, domain: str = "uof.example.com",
                 expires=None) -> httpx.Client:
    c = httpx.Client()
    from http import cookiejar

    c.cookies.jar.set_cookie(cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True, secure=True, expires=expires,
        discard=expires is None, comment=None, comment_url=None, rest={}, rfc2109=False,
    ))
    return c


def main() -> int:
    failures = 0
    tmp = tempfile.mkdtemp(prefix="uof-store-test-")
    os.environ["HOME"] = tmp
    os.environ["UOF_BASE_URL"] = BASE
    os.environ.pop("UOF_SESSION_PERSIST", None)

    # ── 1) round-trip：存進去的 cookie 要能原樣載回 ──────────────────
    os.environ["UOF_ACCOUNT"] = "alice"
    src = _client_with("ASP.NET_SessionId", "sess-alice")
    path = store.save_session(src, source="browser")
    failures += _common.check("save_session 有寫出檔案", bool(path) and Path(path).exists(),
                              f"path={path}")

    dst = httpx.Client()
    meta = store.load_session(dst)
    got = {c.name: c.value for c in dst.cookies.jar}
    failures += _common.check("load_session 還原 cookie", got.get("ASP.NET_SessionId") == "sess-alice",
                              f"got={got}")
    failures += _common.check("meta 記錄 source", (meta or {}).get("source") == "browser",
                              str(meta))
    failures += _common.check("secure 旗標保留",
                              all(c.secure for c in dst.cookies.jar),
                              str([(c.name, c.secure) for c in dst.cookies.jar]))

    # ── 2) 檔案權限必須是 0600（內容等同登入態）─────────────────────
    mode = stat.S_IMODE(Path(path).stat().st_mode)
    failures += _common.check("session 檔權限 0600", mode == 0o600, oct(mode))
    dir_mode = stat.S_IMODE(store.credentials_dir().stat().st_mode)
    failures += _common.check("~/.uof 目錄權限 0700", dir_mode == 0o700, oct(dir_mode))

    # ── 3) 權限被放寬時要修回，但既有內容視為可能遭竄改、不得載入 ─────
    os.chmod(path, 0o644)
    fixer = httpx.Client()
    meta = store.load_session(fixer)
    mode = stat.S_IMODE(Path(path).stat().st_mode)
    failures += _common.check("權限過寬會自動改回 0600", mode == 0o600, oct(mode))
    failures += _common.check(
        "權限曾過寬的 session 本次拒絕載入",
        meta is None and not list(fixer.cookies.jar),
        f"meta={meta}, cookies={list(fixer.cookies.jar)}",
    )

    # ── 4) 身份隔離：換帳號不得載到別人的 session ────────────────────
    # mounted 測試三個帳號共用同一個 HOME，這條錯了會變成拿別人身份操作。
    alice_path = store.session_path()
    os.environ["UOF_ACCOUNT"] = "bob"
    bob_path = store.session_path()
    failures += _common.check("不同帳號 → 不同存檔路徑", alice_path != bob_path,
                              f"{alice_path.name} vs {bob_path.name}")
    other = httpx.Client()
    failures += _common.check("bob 載不到 alice 的 session", store.load_session(other) is None,
                              str([c.name for c in other.cookies.jar]))

    # ── 5) 過期 cookie 不還原 ──────────────────────────────────────
    os.environ["UOF_ACCOUNT"] = "carol"
    store.save_session(_client_with("stale", "x", expires=int(time.time()) - 3600),
                       source="password")
    expired = httpx.Client()
    failures += _common.check("過期 cookie 不還原（整份視為無效）",
                              store.load_session(expired) is None,
                              str([c.name for c in expired.cookies.jar]))

    # ── 6) UOF_SESSION_PERSIST=false → 完全不落地 ───────────────────
    os.environ["UOF_ACCOUNT"] = "dave"
    os.environ["UOF_SESSION_PERSIST"] = "false"
    failures += _common.check("persist 關閉時 save_session 回 None",
                              store.save_session(_client_with("x", "y")) is None)
    failures += _common.check("persist 關閉時不產生檔案",
                              not store.session_path().exists())
    failures += _common.check("persist 關閉時 load_session 回 None",
                              store.load_session(httpx.Client()) is None)
    os.environ.pop("UOF_SESSION_PERSIST")

    # ── 6.5) UOF_SESSION_DIR 可改存放位置（含 ~ 展開）───────────────
    custom = Path(tmp) / "custom-session-dir"
    os.environ["UOF_SESSION_DIR"] = str(custom)
    os.environ["UOF_ACCOUNT"] = "heidi"
    failures += _common.check("UOF_SESSION_DIR 改變存放目錄",
                              store.credentials_dir() == custom, str(store.credentials_dir()))
    store.save_session(_client_with("s", "1"))
    failures += _common.check("存檔確實落在指定目錄",
                              store.session_path().parent == custom and store.session_path().exists())
    failures += _common.check("指定目錄權限仍是 0700",
                              stat.S_IMODE(custom.stat().st_mode) == 0o700,
                              oct(stat.S_IMODE(custom.stat().st_mode)))
    os.environ["UOF_SESSION_DIR"] = "~/expanded-uof-dir"
    failures += _common.check("UOF_SESSION_DIR 支援 ~ 展開",
                              str(store.credentials_dir()).startswith(tmp)
                              and "~" not in str(store.credentials_dir()),
                              str(store.credentials_dir()))
    os.environ.pop("UOF_SESSION_DIR")
    failures += _common.check("未設定時回到預設 ~/.uof",
                              store.credentials_dir() == Path(tmp) / ".uof",
                              str(store.credentials_dir()))

    # ── 6.6) UOF_SESSION_FILE 固定檔名 ────────────────────────────────
    fixed_dir = Path(tmp) / "fixed-name-dir"
    os.environ["UOF_SESSION_DIR"] = str(fixed_dir)
    os.environ["UOF_SESSION_FILE"] = "auth.json"
    failures += _common.check("固定檔名生效", store.session_path().name == "auth.json",
                              store.session_path().name)
    failures += _common.check(
        "站台與身份都不再參與命名",
        store.session_path(account="alice").name
        == store.session_path("https://other.example/X", "bob").name == "auth.json",
    )
    os.environ.pop("UOF_ACCOUNT", None)
    store.save_session(_client_with("ASP.NET_SessionId", "fixed-1"), account="alice",
                       source="browser")
    failures += _common.check("存檔寫到固定檔名", (fixed_dir / "auth.json").exists())
    resolved = store.resolve_session_file()
    failures += _common.check("resolve_session_file 定位到固定檔名",
                              resolved is not None and resolved.name == "auth.json",
                              str(resolved))
    loaded = httpx.Client()
    failures += _common.check("load_session 讀得回固定檔名的存檔",
                              store.load_session(loaded) is not None
                              and loaded.cookies.get("ASP.NET_SessionId") == "fixed-1")
    # 固定檔名不符 `session-*.json` glob，漏刪會留下可重放的登入態。
    failures += _common.check("clear_all_sessions 刪得到固定檔名",
                              store.clear_all_sessions() == 1
                              and not (fixed_dir / "auth.json").exists())

    # 路徑分隔或 Windows drive 可能逃出 session 目錄，必須拒絕並退回推導檔名。
    for bad in (
        "../escape.json",
        "sub/dir.json",
        r"..\escape.json",
        r"sub\dir.json",
        "C:escape.json",
        r"C:\escape.json",
        "..",
    ):
        os.environ["UOF_SESSION_FILE"] = bad
        name = store.session_path().name
        failures += _common.check(
            f"拒絕逃逸的 UOF_SESSION_FILE={bad!r}",
            name.startswith("session-") and "/" not in name and "\\" not in name
            and ".." not in name,
            name,
        )
    os.environ.pop("UOF_SESSION_FILE")
    failures += _common.check("移除後回到推導檔名",
                              store.session_path().name.startswith("session-"),
                              store.session_path().name)
    os.environ.pop("UOF_SESSION_DIR")

    # ── 7) clear ──────────────────────────────────────────────────
    os.environ["UOF_ACCOUNT"] = "alice"
    failures += _common.check("clear_session 刪掉本身份存檔", store.clear_session() is True)
    failures += _common.check("clear_session 對不存在的檔回 False", store.clear_session() is False)
    store.clear_all_sessions()  # 歸零，前面步驟留下的存檔不影響下面的計數
    os.environ["UOF_ACCOUNT"] = "eve"
    store.save_session(_client_with("a", "1"))
    os.environ["UOF_ACCOUNT"] = "frank"
    store.save_session(_client_with("b", "2"))
    failures += _common.check("clear_all_sessions 清掉所有身份", store.clear_all_sessions() == 2)

    # ── 8) 壞檔／版本不符要被安全忽略，不可拋例外 ────────────────────
    os.environ["UOF_ACCOUNT"] = "grace"
    p = store.session_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    failures += _common.check("壞掉的存檔被忽略（不拋例外）", store.load_session(httpx.Client()) is None)
    p.write_text('{"version": 999, "cookies": []}', encoding="utf-8")
    failures += _common.check("版本不符的存檔被忽略", store.load_session(httpx.Client()) is None)

    failures += _identity_resolution()
    failures += _namespace_path_consistency()
    failures += _directory_hardening()

    print("=" * 50)
    print("session store 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


def _identity_resolution() -> int:
    """啟動時「我是誰」的判定：不可以在有歧義時猜，也不可以載到別人的 session。"""
    failures = 0
    home = tempfile.mkdtemp(prefix="uof-store-identity-")
    os.environ["HOME"] = home
    os.environ["UOF_BASE_URL"] = BASE
    for k in ("UOF_ACCOUNT", "UOF_SESSION_NAMESPACE", "UOF_SESSION_DIR"):
        os.environ.pop(k, None)

    # 兩個人在同一台機器、同一站台各自登入
    store.save_session(_client_with("s", "alice-sess"), account="alice", source="browser")
    failures += _common.check("以實際帳號命名，不再共用 anonymous",
                              store.session_path(account="alice").exists()
                              and not store.session_path(account="").exists())
    store.save_session(_client_with("s", "bob-sess"), account="bob", source="browser")
    failures += _common.check("第二個人不覆蓋第一個人",
                              store.session_path(account="alice").exists()
                              and store.session_path(account="bob").exists())

    # 沒有 namespace 又有兩份 → 不可以猜，寧可要求重新登入
    failures += _common.check("有歧義時不沿用任何一份",
                              store.resolve_session_file() is None)
    failures += _common.check("有歧義時 load_session 回 None",
                              store.load_session(httpx.Client()) is None)

    # 設了 namespace 就能明確定位
    os.environ["UOF_SESSION_NAMESPACE"] = "bob"
    c = httpx.Client()
    meta = store.load_session(c)
    failures += _common.check("UOF_SESSION_NAMESPACE 明確定位到該身份",
                              (meta or {}).get("actual_account") == "bob"
                              and {x.value for x in c.cookies.jar} == {"bob-sess"},
                              str(meta))
    os.environ.pop("UOF_SESSION_NAMESPACE")

    # 只剩一份時可以安全沿用（單人單 entry 的常態）
    store.clear_session(account="bob")
    failures += _common.check("只剩一份時沿用該份",
                              store.resolve_session_file() == store.session_path(account="alice"))

    # 設了 UOF_ACCOUNT，存檔卻屬於別人 → 絕不沿用（否則會安靜地換身份）
    os.environ["UOF_ACCOUNT"] = "carol"
    store.save_session(_client_with("s", "x"), account="dave", source="browser")
    os.environ["UOF_SESSION_NAMESPACE"] = "dave"
    failures += _common.check("存檔屬於別人時拒絕沿用",
                              store.load_session(httpx.Client()) is None)
    os.environ.pop("UOF_SESSION_NAMESPACE")
    os.environ.pop("UOF_ACCOUNT")
    return failures


def _namespace_path_consistency() -> int:
    """設了 UOF_SESSION_NAMESPACE 時，存檔（帶實際帳號呼叫）與載入（用 namespace 定位）
    必須指向同一個檔；否則存了卻找不回，重啟每次都要重登。也驗 _safe_name 不會撞檔名。"""
    failures = 0
    home = tempfile.mkdtemp(prefix="uof-store-ns-")
    os.environ["HOME"] = home
    os.environ["UOF_BASE_URL"] = BASE
    os.environ.pop("UOF_ACCOUNT", None)
    os.environ.pop("UOF_SESSION_DIR", None)
    os.environ["UOF_SESSION_NAMESPACE"] = "entry-a"
    try:
        # 這個 entry 綁 namespace=entry-a，實際在瀏覽器登入 alice
        store.save_session(_client_with("s", "alice-sess"), account="alice", source="browser")
        c = httpx.Client()
        meta = store.load_session(c)
        failures += _common.check("namespace 設定時存/讀指向同一檔（持久化不失效）",
                                  meta is not None
                                  and {x.value for x in c.cookies.jar} == {"alice-sess"},
                                  str(meta))
        failures += _common.check("payload 仍記錄實際登入帳號 alice 供顯示",
                                  (meta or {}).get("actual_account") == "alice", str(meta))
    finally:
        os.environ.pop("UOF_SESSION_NAMESPACE", None)

    # storage key 只負責定位檔案，絕對不能在無法擷取瀏覽器帳號時冒充 actual account。
    os.environ["UOF_SESSION_NAMESPACE"] = "entry-unknown"
    os.environ["UOF_ACCOUNT"] = "configured-carol"
    try:
        store.save_session(_client_with("s", "unknown-sess"), account="", source="browser")
        c = httpx.Client()
        meta = store.load_session(c)
        failures += _common.check(
            "無法辨識瀏覽器帳號時 metadata 不拿 namespace/UOF_ACCOUNT 冒充",
            meta is not None
            and meta.get("storage_key") == "entry-unknown"
            and meta.get("actual_account") == "",
            str(meta),
        )
        from mcp_uof.ops.http_web import HttpSession
        session = HttpSession()
        failures += _common.check(
            "重啟載入未知身份 session 時仍維持未辨識",
            session.session_account == "",
            repr(session.session_account),
        )
    finally:
        os.environ.pop("UOF_SESSION_NAMESPACE", None)
        os.environ.pop("UOF_ACCOUNT", None)

    # _safe_name：有損字元不同的帳號不得撞同一檔名；一般 ASCII 帳號維持穩定（不加雜湊）。
    failures += _common.check("_safe_name 對有損字元不同的帳號不撞檔名",
                              store._safe_name("a/b") != store._safe_name("a?b"),
                              f'{store._safe_name("a/b")} vs {store._safe_name("a?b")}')
    failures += _common.check("_safe_name 對一般 ASCII 帳號維持穩定",
                              store._safe_name("alice") == "alice", store._safe_name("alice"))
    return failures


def _directory_hardening() -> int:
    """session 目錄必須是本使用者獨占，且既有目錄也要被收緊。"""
    failures = 0
    home = tempfile.mkdtemp(prefix="uof-store-dir-")
    os.environ["HOME"] = home
    os.environ["UOF_BASE_URL"] = BASE
    os.environ.pop("UOF_ACCOUNT", None)

    # 既有目錄權限過寬 → 要被主動收緊（mkdir(mode=) 只在建立時生效，擋不到這種）
    loose = Path(home) / "loose-dir"
    loose.mkdir(mode=0o777)
    os.chmod(loose, 0o777)
    os.environ["UOF_SESSION_DIR"] = str(loose)
    store.save_session(_client_with("s", "v"), account="erin")
    saved = store.session_path(account="erin")
    mode = stat.S_IMODE(loose.stat().st_mode)
    failures += _common.check("既有目錄權限過寬會被收緊為 0700", mode == 0o700, oct(mode))

    # 暫存檔不得殘留（也不得使用可預測的名稱）
    leftovers = list(loose.glob("*.tmp"))
    failures += _common.check("寫入後沒有殘留暫存檔", not leftovers, str(leftovers))

    # load 也必須先驗目錄；不能只有 save 才收緊。session file 保持 0600，避免檔案 mode
    # 本身的修正剛好掩蓋「讀取前沒檢查目錄」。
    os.chmod(loose, 0o777)
    loaded = httpx.Client()
    meta = store.load_session(loaded, account="erin")
    failures += _common.check(
        "load_session 讀取前會先收緊 session 目錄",
        meta is not None and stat.S_IMODE(loose.stat().st_mode) == 0o700,
        f"meta={meta}, mode={oct(stat.S_IMODE(loose.stat().st_mode))}",
    )

    # 目錄屬於別人 → 拒絕存放（模擬不同 uid）
    real_getuid = os.getuid
    os.getuid = lambda: real_getuid() + 12345
    try:
        raised = None
        try:
            store._ensure_dir()
        except Exception as e:
            raised = e
        failures += _common.check("目錄非本人所有 → 拋 SessionDirUnsafe",
                                  isinstance(raised, store.SessionDirUnsafe), repr(raised))
        failures += _common.check("目錄非本人所有 → save_session 安全放棄（不拋例外）",
                                  store.save_session(_client_with("s", "v")) is None)
        failures += _common.check(
            "目錄非本人所有 → load_session 也拒絕讀取",
            store.load_session(httpx.Client(), account="erin") is None,
        )
    finally:
        os.getuid = real_getuid

    # session file symlink 即使指向可讀的有效 JSON 也不得跟隨。
    real_file = loose / "real-session.json"
    saved.replace(real_file)
    saved.symlink_to(real_file)
    failures += _common.check(
        "load_session 拒絕 session file symlink",
        store.load_session(httpx.Client(), account="erin") is None,
    )

    # 整個 session directory 也不可為 symlink。
    linked_dir = Path(home) / "linked-dir"
    linked_dir.symlink_to(loose, target_is_directory=True)
    os.environ["UOF_SESSION_DIR"] = str(linked_dir)
    failures += _common.check(
        "load_session 拒絕 symlink session 目錄",
        store.load_session(httpx.Client(), account="erin") is None,
    )
    os.environ.pop("UOF_SESSION_DIR")
    return failures


if __name__ == "__main__":
    sys.exit(main())
