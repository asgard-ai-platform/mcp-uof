"""Offline smoke checks for the three-tier authentication priority (auth/session.py).

離線：對假的 UOF upstream 驗證認證來源優先序——
  1. ~/.uof 存檔 session   2. 環境變數帳密自動登入   3. 瀏覽器登入（拋 BrowserLoginRequired）
並驗證 check_auth / logout 的行為與文案分流。

執行：uv run python tests/smoke/test_auth_flow.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common
import _fake_uof

_common.ensure_src_on_path()


def _reset_process_state():
    """把模組級單例歸零，模擬「新的 MCP server 程序」。"""
    import mcp_uof.auth.base as ab
    from mcp_uof.ops import reset_backend_for_tests
    from mcp_uof.ops.http_web import reset_http_session

    reset_http_session()
    reset_backend_for_tests()
    ab.reset_provider_for_tests()


def _login_tool_end_to_end() -> int:
    """走完 uof_custom_login 的整條路徑：起代理 → 假瀏覽器登入 → 工具回報成功 → session 落地。

    `open_in_browser` 被換掉，測試不會真的開啟使用者的瀏覽器。
    """
    import threading
    import time

    import httpx

    import mcp_uof.auth.browser_login as bl
    from mcp_uof.auth import store
    from mcp_uof.ops import get_backend

    failures = 0
    opened = {}
    real_open = bl.open_in_browser
    bl.open_in_browser = lambda url: bool(opened.setdefault("url", url)) or True

    os.environ.pop("UOF_ACCOUNT", None)
    os.environ.pop("UOF_PASSWORD", None)
    os.environ["UOF_LOGIN_WAIT_SECONDS"] = "15"
    _reset_process_state()
    store.clear_all_sessions()

    try:
        result = {}
        worker = threading.Thread(target=lambda: result.update(out=get_backend().login()))
        worker.start()

        flow = None
        for _ in range(200):                      # 等代理起來（最多 10 秒）
            flow = bl.current_flow()
            if flow is not None and flow.url:
                break
            time.sleep(0.05)
        failures += _common.check("login 起了代理並產生登入網址", bool(flow and flow.url))
        failures += _common.check("login 有嘗試開啟瀏覽器", opened.get("url") == (flow.url if flow else None),
                                  str(opened))
        failures += _common.check("登入網址只綁 127.0.0.1",
                                  bool(flow) and flow.url.startswith("http://127.0.0.1:"),
                                  flow.url if flow else "")

        # 扮演使用者的瀏覽器：開登入頁 → 送出帳密
        browser = httpx.Client(follow_redirects=True)
        browser.get(flow.url)
        page = browser.post(
            f"{flow.local_origin}{_fake_uof.VPATH}/Login.aspx",
            data={"txtAccount": _fake_uof.VALID_ACCOUNT, "txtPwd": _fake_uof.VALID_PASSWORD},
        )
        failures += _common.check("送出後看到本地完成頁", "登入完成" in page.text, page.text[:80])
        browser.close()

        worker.join(20)
        out = result.get("out", "")
        failures += _common.check("login 工具回報成功", "✅" in out and "登入完成" in out, out[:100])
        # P1-a：存檔要落在「實際登入帳號」名下，而不是共用的 anonymous
        failures += _common.check("代理取得實際登入帳號",
                                  flow.account == _fake_uof.VALID_ACCOUNT, repr(flow.account))
        failures += _common.check("session 以實際帳號存檔（非 anonymous）",
                                  store.session_path(account=_fake_uof.VALID_ACCOUNT).exists()
                                  and not store.session_path(account="").exists(),
                                  str(store.session_path(account=_fake_uof.VALID_ACCOUNT)))
        failures += _common.check("login 回報實際登入帳號",
                                  _fake_uof.VALID_ACCOUNT in out, out[:100])
        failures += _common.check("check_auth 回報來源與實際帳號",
                                  "瀏覽器登入" in get_backend().check_auth()
                                  and _fake_uof.VALID_ACCOUNT in get_backend().check_auth(),
                                  get_backend().check_auth()[:120])
        # 已登入時再呼叫 login 應直接回報，不再開代理
        failures += _common.check("已登入時 login 直接回報不重開代理",
                                  "已經是登入狀態" in get_backend().login(),
                                  get_backend().login()[:60])
    finally:
        bl.open_in_browser = real_open
        bl.shutdown_flow()
        os.environ.pop("UOF_LOGIN_WAIT_SECONDS", None)
    return failures


def _browser_identity_wins_over_env() -> int:
    """設定是 A、實際在瀏覽器登入 B 時，身份必須始終是 B。

    這是登入身份安全問題：舊行為會顯示成 A（設定值），且 session 過期時用 A 的帳密自動重登，
    讓操作身份在程序中途從 B 悄悄變成 A。
    """
    import threading
    import time

    import httpx

    import mcp_uof.auth.browser_login as bl
    from mcp_uof.auth import store
    from mcp_uof.auth.base import BrowserLoginRequired
    from mcp_uof.ops import get_backend
    from mcp_uof.ops.http_web import get_http_session

    failures = 0
    real_open = bl.open_in_browser
    bl.open_in_browser = lambda url: True

    # 設定成 alice（含密碼，備援可用），但等一下在瀏覽器登入 bob
    os.environ["UOF_ACCOUNT"] = _fake_uof.VALID_ACCOUNT       # alice
    os.environ["UOF_PASSWORD"] = _fake_uof.VALID_PASSWORD
    os.environ["UOF_LOGIN_WAIT_SECONDS"] = "15"
    _reset_process_state()
    store.clear_all_sessions()

    try:
        result = {}
        worker = threading.Thread(target=lambda: result.update(out=get_backend().login(True)))
        worker.start()
        flow = None
        for _ in range(200):
            flow = bl.current_flow()
            if flow is not None and flow.url:
                break
            time.sleep(0.05)

        browser = httpx.Client(follow_redirects=True)
        browser.get(flow.url)
        browser.post(f"{flow.local_origin}{_fake_uof.VPATH}/Login.aspx",
                     data={"txtAccount": _fake_uof.OTHER_ACCOUNT, "txtPwd": _fake_uof.VALID_PASSWORD})
        browser.close()
        worker.join(20)

        out = result.get("out", "")
        # 身份欄位本身必須是 bob；警告句會提到 alice（那是設定值），所以只比對身份欄位。
        failures += _common.check("login 的身份欄位是實際登入者 bob，不是設定值 alice",
                                  f"已取得（{_fake_uof.OTHER_ACCOUNT}）" in out
                                  and f"已取得（{_fake_uof.VALID_ACCOUNT}）" not in out, out[:140])
        failures += _common.check("login 對身份不一致提出警告", "⚠️" in out, out[:140])

        msg = get_backend().check_auth()
        failures += _common.check("check_auth 顯示實際登入者並警告不一致",
                                  _fake_uof.OTHER_ACCOUNT in msg and "⚠️" in msg, msg[:160])

        # session 過期 → 不可以用 alice 的環境變數帳密自動重登
        session = get_http_session()
        session._client.cookies.clear()
        raised = None
        try:
            session._do_login()
        except Exception as e:
            raised = e
        failures += _common.check("瀏覽器身份下 session 失效不會用環境變數帳密自動重登",
                                  isinstance(raised, BrowserLoginRequired), repr(raised))
        failures += _common.check("存檔歸屬於實際登入者",
                                  store.session_path(account=_fake_uof.OTHER_ACCOUNT).exists())

        # ③ logout 必須刪掉「實際登入者 bob」的存檔，而不是去找設定值 alice 的檔（找不到、
        # 把 bob 這份可重放的 session 留在磁碟）。
        get_backend().logout()
        failures += _common.check("logout 刪掉實際登入者 bob 的存檔（非設定值 alice）",
                                  not store.session_path(account=_fake_uof.OTHER_ACCOUNT).exists(),
                                  str(store.session_path(account=_fake_uof.OTHER_ACCOUNT)))
    finally:
        bl.open_in_browser = real_open
        bl.shutdown_flow()
        os.environ.pop("UOF_LOGIN_WAIT_SECONDS", None)
    return failures


def _failed_login_then_switch_account() -> int:
    """先用錯密碼登入 alice（失敗），再用正確密碼登入 bob（成功）：身份必須是 bob。

    舊行為在第一次 POST 就把 alice latch 住（`not flow.account` 只認第一次），改用 bob 成功
    登入後仍記成 alice，session 也存錯檔。
    """
    import threading
    import time

    import httpx

    import mcp_uof.auth.browser_login as bl
    from mcp_uof.auth import store
    from mcp_uof.ops import get_backend

    failures = 0
    real_open = bl.open_in_browser
    bl.open_in_browser = lambda url: True

    os.environ.pop("UOF_ACCOUNT", None)
    os.environ.pop("UOF_PASSWORD", None)
    os.environ["UOF_LOGIN_WAIT_SECONDS"] = "15"
    _reset_process_state()
    store.clear_all_sessions()

    try:
        result = {}
        worker = threading.Thread(target=lambda: result.update(out=get_backend().login()))
        worker.start()
        flow = None
        for _ in range(200):
            flow = bl.current_flow()
            if flow is not None and flow.url:
                break
            time.sleep(0.05)

        browser = httpx.Client(follow_redirects=True)
        browser.get(flow.url)
        # 第一次：alice + 錯密碼 → 停在登入頁，不成功
        browser.post(f"{flow.local_origin}{_fake_uof.VPATH}/Login.aspx",
                     data={"txtAccount": _fake_uof.VALID_ACCOUNT, "txtPwd": "wrong-password"})
        failures += _common.check("錯密碼那次不算登入成功", flow.success is False, repr(flow.success))
        # 第二次：bob + 正確密碼 → 成功
        browser.post(f"{flow.local_origin}{_fake_uof.VPATH}/Login.aspx",
                     data={"txtAccount": _fake_uof.OTHER_ACCOUNT, "txtPwd": _fake_uof.VALID_PASSWORD})
        browser.close()
        worker.join(20)

        failures += _common.check("身份記成第二次成功登入的 bob，而非第一次失敗的 alice",
                                  flow.account == _fake_uof.OTHER_ACCOUNT, repr(flow.account))
        failures += _common.check("session 存到 bob 名下、不是 alice",
                                  store.session_path(account=_fake_uof.OTHER_ACCOUNT).exists()
                                  and not store.session_path(account=_fake_uof.VALID_ACCOUNT).exists(),
                                  str(store.session_path(account=_fake_uof.OTHER_ACCOUNT)))
    finally:
        bl.open_in_browser = real_open
        bl.shutdown_flow()
        os.environ.pop("UOF_LOGIN_WAIT_SECONDS", None)
    return failures


def _force_relogin_clears_old_identity_file() -> int:
    """login(force=True) 換身份時，必須刪掉目前實際登入者的存檔（即使與 UOF_ACCOUNT 不同）。"""
    import threading
    import time

    import httpx

    import mcp_uof.auth.browser_login as bl
    from mcp_uof.auth import store
    from mcp_uof.ops import get_backend

    failures = 0
    real_open = bl.open_in_browser
    bl.open_in_browser = lambda url: True

    os.environ["UOF_ACCOUNT"] = _fake_uof.VALID_ACCOUNT       # alice
    os.environ["UOF_PASSWORD"] = _fake_uof.VALID_PASSWORD
    os.environ["UOF_LOGIN_WAIT_SECONDS"] = "15"
    _reset_process_state()
    store.clear_all_sessions()

    def _login_worker(result):
        result.update(out=get_backend().login(True))

    def _wait_for_flow(prev=None):
        for _ in range(200):
            f = bl.current_flow()
            if f is not None and f.url and f is not prev:
                return f
            time.sleep(0.05)
        return None

    try:
        # 設定是 alice，但實際在瀏覽器登入 bob
        r1 = {}
        w1 = threading.Thread(target=_login_worker, args=(r1,))
        w1.start()
        flow = _wait_for_flow()
        browser = httpx.Client(follow_redirects=True)
        browser.get(flow.url)
        browser.post(f"{flow.local_origin}{_fake_uof.VPATH}/Login.aspx",
                     data={"txtAccount": _fake_uof.OTHER_ACCOUNT, "txtPwd": _fake_uof.VALID_PASSWORD})
        browser.close()
        w1.join(20)
        failures += _common.check("前置：bob 的存檔存在",
                                  store.session_path(account=_fake_uof.OTHER_ACCOUNT).exists())

        # force 重登：起新流程前的清理應先刪掉舊身份 bob 的存檔
        r2 = {}
        w2 = threading.Thread(target=_login_worker, args=(r2,))
        w2.start()
        flow2 = _wait_for_flow(prev=flow)
        failures += _common.check("force 重登刪掉舊身份 bob 的存檔（非設定值 alice）",
                                  flow2 is not None
                                  and not store.session_path(account=_fake_uof.OTHER_ACCOUNT).exists(),
                                  str(store.session_path(account=_fake_uof.OTHER_ACCOUNT)))
        bl.shutdown_flow()   # 放掉還在等待登入的 worker
        w2.join(20)
    finally:
        bl.open_in_browser = real_open
        bl.shutdown_flow()
        os.environ.pop("UOF_LOGIN_WAIT_SECONDS", None)
        os.environ.pop("UOF_ACCOUNT", None)
        os.environ.pop("UOF_PASSWORD", None)
    return failures


def _force_relogin_pending_blocks_password_fallback() -> int:
    """force 換身份等待期間，即使 env 帳密存在且 provider cache 還有效也不能自動登入。"""
    import mcp_uof.auth.base as ab
    import mcp_uof.auth.browser_login as bl
    from mcp_uof.ops import get_backend
    from mcp_uof.ops.http_web import get_http_session

    failures = 0
    real_open = bl.open_in_browser
    bl.open_in_browser = lambda url: True
    os.environ["UOF_ACCOUNT"] = _fake_uof.VALID_ACCOUNT
    os.environ["UOF_PASSWORD"] = _fake_uof.VALID_PASSWORD
    os.environ["UOF_LOGIN_WAIT_SECONDS"] = "0.01"
    _reset_process_state()

    try:
        provider = ab.get_session_provider()
        provider.ensure_valid()  # 先建立有效 env session 與 30 秒 validation cache
        backend = get_backend()
        out = backend.login(force=True)
        session = get_http_session()
        failures += _common.check(
            "force relogin 等待期間標成 browser_pending",
            session.session_source == "browser_pending" and "等待" in out,
            f"source={session.session_source!r}, out={out[:100]}",
        )

        login_calls = []
        real_do_login = session._do_login
        session._do_login = lambda: login_calls.append("password fallback")
        try:
            raised = None
            try:
                provider.ensure_valid()
            except Exception as ex:
                raised = ex
            failures += _common.check(
                "browser_pending 優先於舊的 30 秒 validation cache",
                isinstance(raised, ab.BrowserLoginRequired),
                repr(raised),
            )

            def uof_custom_get_form_list():
                return "TOOL RAN"

            guarded = ab.require_auth(uof_custom_get_form_list)
            guarded_out = guarded()
            failures += _common.check(
                "browser_pending 期間受保護工具回登入提示且不執行",
                "🔑" in guarded_out and "TOOL RAN" not in guarded_out and not login_calls,
                f"out={guarded_out[:100]}, login_calls={login_calls}",
            )
        finally:
            session._do_login = real_do_login
    finally:
        bl.open_in_browser = real_open
        bl.shutdown_flow()
        os.environ.pop("UOF_LOGIN_WAIT_SECONDS", None)
        os.environ.pop("UOF_ACCOUNT", None)
        os.environ.pop("UOF_PASSWORD", None)
        _reset_process_state()
    return failures


def main() -> int:
    failures = 0
    server, base_url = _fake_uof.start_fake_uof()
    home = tempfile.mkdtemp(prefix="uof-auth-flow-")
    os.environ["HOME"] = home
    os.environ["UOF_BASE_URL"] = base_url
    os.environ.pop("UOF_SESSION_PERSIST", None)

    import mcp_uof.auth.base as ab
    from mcp_uof.auth import store
    from mcp_uof.auth.base import BrowserLoginRequired
    from mcp_uof.ops import get_backend

    try:
        # ── 第 3 段：無帳密、無存檔 → 要求瀏覽器登入 ─────────────────
        os.environ.pop("UOF_ACCOUNT", None)
        os.environ.pop("UOF_PASSWORD", None)
        _reset_process_state()

        raised = None
        try:
            ab.get_session_provider().ensure_valid()
        except Exception as e:
            raised = e
        failures += _common.check("無帳密無存檔 → 拋 BrowserLoginRequired",
                                  isinstance(raised, BrowserLoginRequired), repr(raised))

        def stub(*a, **k):
            return "TOOL RAN"
        stub.__name__ = "uof_custom_get_form_list"
        guarded = ab.require_auth(stub)
        out = guarded()
        failures += _common.check("工具閘回「需要瀏覽器登入」而非設定錯誤",
                                  "🔑" in out and "uof_custom_login" in out, out[:80])
        failures += _common.check("未登入時工具本體不會被執行", "TOOL RAN" not in out, out[:60])

        msg = get_backend().check_auth()
        failures += _common.check("check_auth 指路到 uof_custom_login",
                                  "🔑" in msg and "uof_custom_login" in msg, msg[:80])

        # ── 第 2 段：設定帳密 → 自動登入並落地存檔 ───────────────────
        os.environ["UOF_ACCOUNT"] = _fake_uof.VALID_ACCOUNT
        os.environ["UOF_PASSWORD"] = _fake_uof.VALID_PASSWORD
        _reset_process_state()

        ab.get_session_provider().ensure_valid()   # 不應拋錯
        failures += _common.check("帳密備援 → ensure_valid 通過", True)
        failures += _common.check("帳密登入後有寫出 session 存檔",
                                  store.session_path().exists(), str(store.session_path()))
        msg = get_backend().check_auth()
        failures += _common.check("check_auth 回報已登入且來源為帳密",
                                  "✅" in msg and "環境變數帳密" in msg, msg[:100])

        # ── 第 1 段：拿掉帳密，新程序應直接沿用存檔 session ───────────
        # 只拿掉密碼：存檔是以 alice 的身份鍵寫的，要同一個身份鍵才找得到。
        os.environ.pop("UOF_PASSWORD", None)
        _reset_process_state()

        from mcp_uof.ops.http_web import get_http_session
        s = get_http_session()
        failures += _common.check("重啟後直接沿用存檔 session（免重登）",
                                  s.is_logged_in() is True)
        failures += _common.check("認證來源標記為 password（存檔寫入時的來源）",
                                  s.session_source == "password", str(s.session_source))

        # 沒有密碼可用，但既有 session 有效 → 不該拋 BrowserLoginRequired
        raised = None
        try:
            ab.get_session_provider().ensure_valid()
        except Exception as e:
            raised = e
        failures += _common.check("既有 session 有效時不要求重新登入", raised is None, repr(raised))

        # ── logout：記憶體與磁碟都要清乾淨 ──────────────────────────
        out = get_backend().logout()
        failures += _common.check("logout 回報成功", "✅" in out, out[:60])
        failures += _common.check("logout 刪除 session 存檔",
                                  not store.session_path().exists(), str(store.session_path()))
        raised = None
        try:
            ab.get_session_provider().ensure_valid()
        except Exception as e:
            raised = e
        failures += _common.check("logout 後回到「需要瀏覽器登入」",
                                  isinstance(raised, BrowserLoginRequired), repr(raised))

        # ── 帳密錯誤 → 設定層級失敗訊息（不是要求瀏覽器登入）─────────
        os.environ["UOF_ACCOUNT"] = _fake_uof.VALID_ACCOUNT
        os.environ["UOF_PASSWORD"] = "wrong-password"
        _reset_process_state()
        out = ab.require_auth(stub)()
        failures += _common.check("帳密錯誤 → 回設定層級失敗訊息（🔒 而非 🔑）",
                                  "🔒" in out and "🔑" not in out, out[:80])

        # ── uof_custom_login 全程整合（假瀏覽器，不會真的開視窗）──────
        failures += _login_tool_end_to_end()
        failures += _browser_identity_wins_over_env()
        failures += _failed_login_then_switch_account()
        failures += _force_relogin_clears_old_identity_file()
        failures += _force_relogin_pending_blocks_password_fallback()

    finally:
        server.shutdown()
        server.server_close()

    print("=" * 50)
    print("認證優先序測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
