"""Offline smoke checks for the localhost browser-login proxy (auth/browser_login.py).

離線：起一個**假的 UOF upstream**（模擬 Login.aspx / Homepage.aspx 的 cookie 行為），
再用 httpx 扮演「使用者的瀏覽器」打代理，驗證安全邊界與登入成功偵測。不連真實 UOF。

執行：uv run python tests/smoke/test_browser_login.py
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

_common.ensure_src_on_path()

import httpx

SESSION_COOKIE = "ASP.NET_SessionId"
VPATH = "/UOF"


# ── 假的 UOF upstream ────────────────────────────────────────────────

class _FakeUof(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _logged_in(self) -> bool:
        return SESSION_COOKIE in (self.headers.get("Cookie") or "")

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == VPATH + "/Login.aspx":
            # 登入頁：發一個非 session 的 cookie（測試它不可以外流到瀏覽器），
            # 並在 HTML 內放一個指向自己的絕對網址（測試網址改寫）。
            origin = f"http://127.0.0.1:{self.server.server_address[1]}"
            body = (
                f'<html><body><form method="post" action="{VPATH}/Login.aspx">'
                f'<link href="{origin}{VPATH}/style.css">'
                f'<input name="txtAccount"><input name="txtPwd">'
                f"</form></body></html>"
            ).encode()
            return self._send(200, body, extra=[("Set-Cookie", "CSRF=abc123; Path=/")])
        if path == VPATH + "/Homepage.aspx":
            if self._logged_in():
                return self._send(200, b"<html><body>home</body></html>")
            return self._send(302, b"", extra=[("Location", VPATH + "/Login.aspx")])
        return self._send(404, b"nope")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if path == VPATH + "/Login.aspx":
            return self._send(302, b"", extra=[
                ("Set-Cookie", f"{SESSION_COOKIE}=sess-xyz; Path=/; HttpOnly"),
                ("Location", VPATH + "/Homepage.aspx"),
            ])
        return self._send(404, b"nope")


def main() -> int:
    failures = 0

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUof)
    upstream.daemon_threads = True
    up_port = upstream.server_address[1]
    upstream_origin = f"http://127.0.0.1:{up_port}"
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    os.environ["UOF_BASE_URL"] = upstream_origin + VPATH
    os.environ["UOF_SESSION_PERSIST"] = "false"   # 測試不落地
    os.environ.pop("UOF_ACCOUNT", None)
    os.environ.pop("UOF_PASSWORD", None)

    from mcp_uof.auth.browser_login import BrowserLoginFlow
    from mcp_uof.ops.http_web import HttpSession

    session = HttpSession()
    flow = BrowserLoginFlow(session)
    url = flow.start()
    local_origin = flow.local_origin

    try:
        # ── 1) 沒有 token / cookie → 403（擋掉同機其他程序）────────────
        anon = httpx.Client(follow_redirects=False)
        r = anon.get(f"{local_origin}{VPATH}/Login.aspx")
        failures += _common.check("無 token/cookie → 403", r.status_code == 403, str(r.status_code))

        # ── 2) 錯的 token → 403 ────────────────────────────────────
        r = anon.get(f"{local_origin}{VPATH}/Login.aspx?_uof_token=wrong-token")
        failures += _common.check("錯誤 token → 403", r.status_code == 403, str(r.status_code))

        # ── 3) Host header 不是 localhost → 403（擋 DNS rebinding）──
        r = anon.get(f"{local_origin}{VPATH}/Login.aspx", headers={"Host": "evil.example.com"})
        failures += _common.check("非 localhost Host → 403", r.status_code == 403, str(r.status_code))

        # ── 4) 正確 token → 302 換發 cookie，且網址不再帶 token ──────
        browser = httpx.Client(follow_redirects=False)
        r = browser.get(url)
        set_cookie = r.headers.get("set-cookie", "")
        failures += _common.check("帶 token → 302 換發 cookie",
                                  r.status_code == 302 and "uof_proxy_token=" in set_cookie,
                                  f"{r.status_code} / {set_cookie[:60]}")
        failures += _common.check("換發後網址不再帶 token",
                                  "_uof_token" not in r.headers.get("location", ""),
                                  r.headers.get("location", ""))

        # ── 5) 代理登入頁：內容有回來、絕對網址被改寫到 localhost ─────
        r = browser.get(f"{local_origin}{VPATH}/Login.aspx")
        failures += _common.check("代理登入頁回 200", r.status_code == 200, str(r.status_code))
        failures += _common.check("上游絕對網址被改寫成 localhost",
                                  local_origin in r.text and upstream_origin not in r.text,
                                  r.text[:120])
        failures += _common.check("表單欄位原樣送達瀏覽器", "txtAccount" in r.text, r.text[:120])

        # ── 6) 上游的 Set-Cookie 不得外流給瀏覽器 ────────────────────
        failures += _common.check("上游 Set-Cookie 不外流",
                                  "CSRF" not in r.headers.get("set-cookie", ""),
                                  r.headers.get("set-cookie", ""))
        failures += _common.check("瀏覽器端沒拿到上游 cookie",
                                  "CSRF" not in dict(browser.cookies),
                                  str(dict(browser.cookies)))
        # 但我們自己的 jar 要拿到
        failures += _common.check("上游 cookie 落在 MCP 的 cookie jar",
                                  "CSRF" in {c.name for c in session._client.cookies.jar},
                                  str({c.name for c in session._client.cookies.jar}))

        # ── 7) 送出登入 → 偵測成功 → 導向本地完成頁 ──────────────────
        failures += _common.check("登入前 flow.success 為 False", flow.success is False)
        r = browser.post(f"{local_origin}{VPATH}/Login.aspx",
                         data={"txtAccount": "alice", "txtPwd": "pw"})
        failures += _common.check("登入成功 → 302 導向完成頁",
                                  r.status_code == 302 and r.headers.get("location") == "/__uof_login_done",
                                  f"{r.status_code} / {r.headers.get('location')}")
        failures += _common.check("flow 標記為成功", flow.success is True)
        # mark_success 必須同時標記來源；否則逾時後才完成登入時 session_source 會留 None，
        # 過期時誤用環境變數帳密自動重登、悄悄換身份。
        failures += _common.check("mark_success 同時標記 session_source=browser",
                                  session.session_source == "browser", repr(session.session_source))
        failures += _common.check("wait() 立即回 True", flow.wait(1) is True)
        failures += _common.check("session cookie 進了 MCP 的 cookie jar",
                                  SESSION_COOKIE in {c.name for c in session._client.cookies.jar},
                                  str({c.name for c in session._client.cookies.jar}))

        # ── 8) 完成頁可讀，之後代理自關 ─────────────────────────────
        r = browser.get(f"{local_origin}/__uof_login_done")
        failures += _common.check("完成頁回 200 HTML",
                                  r.status_code == 200 and "登入完成" in r.text, str(r.status_code))

        # ── 9) 探測函式對「已登入」回 True ───────────────────────────
        failures += _common.check("probe_logged_in() 回 True", flow.probe_logged_in() is True)

        # ── 10) 帳號擷取：只解帳號欄位、支援 ASP.NET 編碼過的欄位名 ────
        from mcp_uof.auth.browser_login import _account_from_login_post as _acct
        failures += _common.check("擷取單純表單的帳號",
                                  _acct(b"txtAccount=alice&txtPwd=pw") == "alice")
        failures += _common.check("帳號值會 urldecode",
                                  _acct(b"txtAccount=al%40ice&txtPwd=pw") == "al@ice")
        # ASP.NET 的欄位名常帶 $（編碼成 %24）；欄位名要先解碼才比對得到帳號欄位。
        failures += _common.check("支援編碼過的欄位名（ctl00%24txtAccount）",
                                  _acct(b"ctl00%24txtAccount=bob&ctl00%24txtPwd=secret") == "bob")
        # 密碼欄位裡就算塞了看起來像參數的內容，也不該汙染帳號擷取（只逐 pair 比對欄位名）。
        failures += _common.check("密碼欄位內容不影響帳號擷取",
                                  _acct(b"txtAccount=carol&txtPwd=a%3Db%26txtAccount%3Devil") == "carol")
        failures += _common.check("沒有帳號欄位 → 回空字串", _acct(b"__VIEWSTATE=x&txtPwd=pw") == "")

        anon.close()
        browser.close()
    finally:
        flow.shutdown()
        upstream.shutdown()
        upstream.server_close()

    print("=" * 50)
    print("瀏覽器登入代理測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
