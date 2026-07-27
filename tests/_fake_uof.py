"""假的 UOF upstream，供離線測試使用（登入代理 / 三段認證流程共用）。

只模擬認證相關行為：Login.aspx 的表單與 cookie 發放、Homepage.aspx 的登入態判斷。
不模擬表單或簽核端點——那些由 tests/mounted 對真實環境驗證。
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

SESSION_COOKIE = "ASP.NET_SessionId"
VPATH = "/UOF"
VALID_ACCOUNT = "alice"
OTHER_ACCOUNT = "bob"      # 用來驗證「設定是 A、實際登入 B」的身份情境
VALID_PASSWORD = "pw"


class FakeUofHandler(BaseHTTPRequestHandler):
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

    def _login_page(self) -> bytes:
        # 含一個指向自己的絕對網址，用來驗證代理的網址改寫。
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        return (
            f'<html><body><form method="post" action="{VPATH}/Login.aspx">'
            f'<link href="{origin}{VPATH}/style.css">'
            f'<input type="hidden" name="__VIEWSTATE" value="vs-1">'
            f'<input name="txtAccount"><input name="txtPwd">'
            f"</form></body></html>"
        ).encode()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == VPATH + "/Login.aspx":
            return self._send(200, self._login_page(),
                              extra=[("Set-Cookie", "CSRF=abc123; Path=/")])
        if path == VPATH + "/Homepage.aspx":
            if self._logged_in():
                return self._send(200, b"<html><body>home</body></html>")
            return self._send(302, b"", extra=[("Location", VPATH + "/Login.aspx")])
        return self._send(404, b"nope")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        if path != VPATH + "/Login.aspx":
            return self._send(404, b"nope")
        form = parse_qs(raw)
        account = (form.get("txtAccount") or [""])[0]
        password = (form.get("txtPwd") or [""])[0]
        if account in (VALID_ACCOUNT, OTHER_ACCOUNT) and password == VALID_PASSWORD:
            return self._send(302, b"", extra=[
                ("Set-Cookie", f"{SESSION_COOKIE}=sess-xyz; Path=/; HttpOnly"),
                ("Location", VPATH + "/Homepage.aspx"),
            ])
        # 帳密錯誤：留在登入頁（真實 UOF 的行為）
        return self._send(200, self._login_page())


def start_fake_uof():
    """起假 UOF，回傳 (server, base_url)。呼叫端負責 shutdown()/server_close()。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUofHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}{VPATH}"
    return server, base_url
