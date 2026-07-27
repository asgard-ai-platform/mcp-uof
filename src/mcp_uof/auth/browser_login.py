"""瀏覽器登入：在 127.0.0.1 起反向代理，讓使用者在真實的 UOF 登入頁完成登入。

為什麼是代理而不是讀瀏覽器的 cookie：瀏覽器裡的 cookie 本程序拿不到，而 UOF 的 session cookie
是 HttpOnly，頁面 JS 也讀不到。代理把方向反過來——畫面是真的登入頁，但請求由本程序的 httpx
client 發出，`Set-Cookie` 自然落在我們的 cookie jar，也不會踩到 session 綁 UA/IP 的問題。

安全邊界（每條都是刻意的）：
- 只綁 127.0.0.1 並檢查 Host；只轉發與 UOF_BASE_URL 同 host 的請求。
- 一次性 token 換 localhost cookie 才放行，否則同機任何程序都能藉這個 port 冒用身份。
- 不把上游 Set-Cookie 轉給瀏覽器，也剝掉瀏覽器上行的 Cookie：session 只存在我們的 jar。
- 成功或逾時即自動關閉，不長駐。
"""
from __future__ import annotations

import os
import posixpath
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import unquote, unquote_plus, urlparse

from .._log import eprint as _eprint

_DONE_PATH = "/__uof_login_done"
_TOKEN_PARAM = "_uof_token"
_TOKEN_COOKIE = "uof_proxy_token"

# 需要改寫網址的內容型別（純文字類）。二進位資源原樣轉發。
_TEXTUAL = (
    "text/", "application/javascript", "application/x-javascript",
    "application/json", "application/xml", "image/svg",
)

_HOP_BY_HOP_REQ = frozenset([
    "host", "cookie", "accept-encoding", "connection", "keep-alive",
    "proxy-connection", "proxy-authorization", "upgrade", "te", "trailer",
    "content-length", "transfer-encoding",
])
_HOP_BY_HOP_RESP = frozenset([
    "connection", "keep-alive", "proxy-authenticate", "transfer-encoding",
    "trailer", "upgrade", "content-encoding", "content-length",
    "set-cookie",  # 刻意不外流給瀏覽器
])


def wait_seconds() -> float:
    """uof_custom_login 同步等待上限；預設 45s，避免撞到 MCP client 的 tool timeout。"""
    try:
        return float(os.getenv("UOF_LOGIN_WAIT_SECONDS", "45"))
    except ValueError:
        return 45.0


def timeout_seconds() -> float:
    """代理背景存活上限。"""
    try:
        return float(os.getenv("UOF_LOGIN_TIMEOUT_SECONDS", "600"))
    except ValueError:
        return 600.0


def _timer(delay: float, fn) -> None:
    """延後執行。**一定要 daemon**：non-daemon 的 Timer 會讓整個程序在結束時卡到計時器到期。"""
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def open_in_browser(url: str) -> bool:
    """開使用者的預設瀏覽器。

    刻意不用 `webbrowser`：它在部分平台會讓子程序繼承 stdout，而 stdio MCP 只要有一個 byte
    混進 stdout 就會破壞 JSON-RPC。這裡一律把子程序的 stdout/stderr 導到 devnull。
    """
    if sys.platform == "darwin":
        cmd = ["open", url]
    elif sys.platform == "win32":
        cmd = ["cmd", "/c", "start", "", url]
    else:
        cmd = ["xdg-open", url]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        return True
    except Exception as ex:
        _eprint(f"[auth.browser_login] ⚠️ 無法自動開啟瀏覽器（{type(ex).__name__}: {ex}）")
        return False


class BrowserLoginFlow:
    """一次瀏覽器登入流程：起代理 → 使用者登入 → 取得 cookie → 自關。"""

    def __init__(self, session) -> None:
        self._session = session
        self._token = secrets.token_urlsafe(24)
        self._done = threading.Event()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._shutdown_lock = threading.Lock()
        self._closed = False
        self.success = False
        self.url = ""
        self.local_origin = ""
        # 使用者實際登入的帳號，由登入 POST 取得（見 _account_from_login_post）。
        # 這是本程序唯一能知道「現在是誰」的途徑，session 存檔與身份顯示都靠它。
        self.account = ""

    # ── 生命週期 ────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._httpd is not None and not self._closed

    def start(self) -> str:
        """起代理並回傳要在瀏覽器打開的網址（已含一次性 token）。"""
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True  # 不 join 請求執行緒，避免關閉時自我死鎖
        port = self._httpd.server_address[1]
        self.local_origin = f"http://127.0.0.1:{port}"
        login_path = self._session._vpath + "/Login.aspx"
        self.url = f"{self.local_origin}{login_path}?{_TOKEN_PARAM}={self._token}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True,
                                        name="uof-browser-login")
        self._thread.start()
        _timer(timeout_seconds(), self._on_timeout)
        _eprint(f"[auth.browser_login] 代理已啟動於 {self.local_origin}（逾時 {timeout_seconds():.0f}s）")
        return self.url

    def wait(self, seconds: Optional[float] = None) -> bool:
        """等到登入成功或逾時；回傳是否成功。逾時不代表失敗，代理仍在背景等使用者。"""
        self._done.wait(wait_seconds() if seconds is None else seconds)
        return self.success

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._closed:
                return
            self._closed = True
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            # 一律在別的執行緒關閉：從請求執行緒直接呼叫 shutdown() 會等 serve_forever 收工。
            threading.Thread(target=_close_server, args=(httpd,), daemon=True).start()
        self._done.set()

    def _on_timeout(self) -> None:
        if not self.success and self.running:
            _eprint("[auth.browser_login] ⏱️ 登入逾時，關閉代理")
        self.shutdown()

    # ── 登入成功判定 ────────────────────────────────────────────────
    def probe_logged_in(self) -> bool:
        """探測 Homepage：沒被導回 Login.aspx 就算已登入（判定邏輯在 HttpSession，這裡不重複）。"""
        return self._session.is_logged_in()

    def mark_success(self) -> None:
        if self.success:
            return
        self.success = True
        _eprint(f"[auth.browser_login] ✅ 登入成功，session cookie 已取得"
                f"（帳號：{self.account or '未能辨識'}）")
        # source 在此定案，涵蓋逾時後才完成登入（login() 已 return，只剩本方法會跑）的情況。
        self._session.session_account = self.account
        self._session.session_source = "browser"
        try:
            from . import store
            store.save_session(self._session._client, account=self.account, source="browser")
        except Exception as ex:
            _eprint(f"[auth.browser_login] ⚠️ session 存檔失敗（{type(ex).__name__}: {ex}）")
        self._done.set()
        # 使用者若沒載入成功頁，這個後備計時器仍會收掉代理。
        _timer(20.0, self.shutdown)


def _close_server(httpd: ThreadingHTTPServer) -> None:
    try:
        httpd.shutdown()
        httpd.server_close()
    except Exception:
        pass


_DONE_HTML = """<!doctype html>
<html lang="zh-TW"><head><meta charset="utf-8"><title>UOF 登入完成</title>
<style>
 body{font-family:-apple-system,"Noto Sans TC",sans-serif;background:#f6f7f9;color:#1a1a1a;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 .card{background:#fff;padding:40px 48px;border-radius:12px;text-align:center;
       box-shadow:0 2px 16px rgba(0,0,0,.08);max-width:420px}
 h1{font-size:20px;margin:0 0 12px} p{color:#555;line-height:1.7;margin:0;font-size:14px}
 .tick{font-size:44px}
</style></head><body><div class="card">
<div class="tick">✅</div><h1>UOF 登入完成</h1>
<p>登入資訊已交給 MCP Server，可以關閉這個分頁，回到對話繼續操作。</p>
</div></body></html>
"""


def _is_textual(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return any(ct.startswith(t) or t in ct for t in _TEXTUAL)


# 登入表單的帳號欄位名（與 ops.http_web._do_login 送出的欄位一致）。
_ACCOUNT_FIELDS = ("txtAccount", "txtUserId", "txtUserName")


def _account_from_login_post(body: Optional[bytes]) -> str:
    """從登入 POST 取出**帳號**，標示這個 session 屬於誰。

    只 urldecode 帳號欄位的值；密碼欄位（txtPwd）以未解碼的原始子字串被略過，不使用、不落地。
    欄位名不符時回空字串，交由 UOF_SESSION_NAMESPACE 區分。
    """
    if not body:
        return ""
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        return ""
    for pair in text.split("&"):
        if "=" not in pair:
            continue
        raw_name, _, raw_value = pair.partition("=")
        field = unquote_plus(raw_name).rsplit("$", 1)[-1]
        if field not in _ACCOUNT_FIELDS:
            continue
        value = unquote_plus(raw_value).strip()
        if value:
            return value
    return ""


def _make_handler(flow: BrowserLoginFlow):
    session = flow._session
    upstream_origin = session._base  # scheme://host

    class _ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "uof-login-proxy"

        # BaseHTTPRequestHandler 預設把 access log 印到 stderr；預設靜音，需要時開 debug。
        def log_message(self, fmt, *args):
            if os.getenv("UOF_LOGIN_DEBUG"):
                _eprint("[auth.browser_login] " + fmt % args)

        # ── 共用流程 ────────────────────────────────────────────
        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_HEAD(self):
            self._handle("HEAD")

        def send_response(self, *args, **kwargs):
            self._responded = True
            super().send_response(*args, **kwargs)

        def _handle(self, method: str):
            self._responded = False
            try:
                self._dispatch(method)
            except BrokenPipeError:
                pass  # 使用者關掉分頁
            except Exception as ex:
                _eprint(f"[auth.browser_login] ❌ 代理錯誤：{type(ex).__name__}: {ex}")
                if not self._responded:      # 已送出 header 就不能再送一次，否則回應會壞掉
                    self._send_simple(502, f"代理錯誤：{type(ex).__name__}")

        def _dispatch(self, method: str):
            # 一定要先把 request body 讀完再處理：HTTP/1.1 keep-alive 下，早退（403/409）若留著
            # 未讀的 body，剩下的 bytes 會被當成下一個請求解析，整條連線就錯位了。
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None

            if not self._host_is_local():
                return self._send_simple(403, "forbidden")

            path, query = self._split_path()

            # 一次性 token → 換成 localhost 網域的 cookie，並把 token 從網址移除
            token = self._token_from_query(query)
            if token is not None:
                if not secrets.compare_digest(token, flow._token):
                    return self._send_simple(403, "forbidden")
                clean_query = self._strip_token(query)
                target = path + (f"?{clean_query}" if clean_query else "")
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header(
                    "Set-Cookie",
                    f"{_TOKEN_COOKIE}={flow._token}; Path=/; HttpOnly; SameSite=Lax",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if not self._cookie_authorized():
                return self._send_simple(403, "forbidden — 請重新執行 uof_custom_login")

            if path == _DONE_PATH:
                # 只有真的登入成功才算數：不能因為有人直接開這個網址就標記完成。
                if not flow.success and flow.probe_logged_in():
                    flow.mark_success()
                if not flow.success:
                    return self._send_simple(409, "尚未偵測到登入成功，請回到登入頁完成登入")
                self._send_html(200, _DONE_HTML)
                _timer(2.0, flow.shutdown)
                return

            if path == "/favicon.ico":
                return self._send_simple(404, "")

            if not self._path_is_allowed(path):
                return self._send_simple(403, "forbidden — path outside configured UOF virtual path")

            self._proxy(method, path, query, body)

        # ── 代理本體 ────────────────────────────────────────────
        def _proxy(self, method: str, path: str, query: str, body):
            url = upstream_origin + path + (f"?{query}" if query else "")
            resp = session._client.request(
                method, url, content=body,
                headers=self._forward_headers(),
                follow_redirects=False,
            )

            # 登入送出（POST）或任何轉址之後，探一次是否已經登入成功。
            # 帳號每個帶帳號的 POST 都更新、成功時才定案，避免第一次失敗的帳號被 latch 住。
            if not flow.success and (method == "POST" or 300 <= resp.status_code < 400):
                if method == "POST":
                    acct = _account_from_login_post(body)
                    if acct:
                        flow.account = acct
                if flow.probe_logged_in():
                    flow.mark_success()
                    self.send_response(302)
                    self.send_header("Location", _DONE_PATH)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

            self._send_upstream(resp, head_only=(method == "HEAD"))

        def _send_upstream(self, resp, *, head_only: bool):
            content_type = resp.headers.get("content-type", "")
            no_body = head_only or resp.status_code in (204, 304)
            payload = b"" if no_body else resp.content
            if payload and _is_textual(content_type):
                payload = self._rewrite_body(payload)

            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() in _HOP_BY_HOP_RESP:
                    continue
                if k.lower() == "location":
                    v = self._rewrite_location(v)
                self.send_header(k, v)
            # 204/304 依規範不得帶 body，也不該宣告 Content-Length。
            if resp.status_code not in (204, 304):
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _forward_headers(self) -> dict:
            """轉發瀏覽器的 header，但拿掉 Cookie（改用 jar）與 hop-by-hop。"""
            out = {}
            for k, v in self.headers.items():
                lk = k.lower()
                if lk in _HOP_BY_HOP_REQ:
                    continue
                if lk in ("referer", "origin"):
                    v = v.replace(flow.local_origin, upstream_origin)
                out[k] = v
            return out

        # ── 網址改寫（bytes 層，避免踩到頁面編碼）────────────────
        def _rewrite_body(self, raw: bytes) -> bytes:
            up = upstream_origin.encode()
            local = flow.local_origin.encode()
            raw = raw.replace(up, local)
            # 協定相對網址 //host 與 JS 字串中的跳脫寫法 https:\/\/host
            up_host = urlparse(upstream_origin).netloc.encode()
            raw = raw.replace(b"//" + up_host, b"//" + urlparse(flow.local_origin).netloc.encode())
            raw = raw.replace(up.replace(b"/", b"\\/"), local.replace(b"/", b"\\/"))
            return raw

        def _rewrite_location(self, loc: str) -> str:
            if loc.startswith(upstream_origin):
                return flow.local_origin + loc[len(upstream_origin):]
            host = urlparse(upstream_origin).netloc
            if loc.startswith("//" + host):
                return "//" + urlparse(flow.local_origin).netloc + loc[len("//" + host):]
            return loc

        # ── 小工具 ──────────────────────────────────────────────
        def _host_is_local(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _split_path(self):
            raw = self.path
            if not raw.startswith("/"):
                return "/", ""
            path, _, query = raw.partition("?")
            return path, query

        def _path_is_allowed(self, path: str) -> bool:
            """只代理 configured UOF virtual path；完成頁/favicon 已在呼叫前個別處理。"""
            vpath = session._vpath
            if not vpath:
                return path.startswith("/")
            # IIS/ASP.NET 可能把反斜線視為 path separator，也可能在不同層各 decode 一次；
            # allowlist 前先 canonicalize，避免 `%5c` / double-encoded `..` 逃出 virtual path。
            decoded = path
            for _ in range(3):
                expanded = unquote(decoded)
                if expanded == decoded:
                    break
                decoded = expanded
            decoded = decoded.replace("\\", "/")
            normalized = posixpath.normpath(decoded)
            return normalized == vpath or normalized.startswith(vpath + "/")

        def _token_from_query(self, query: str):
            for part in query.split("&"):
                k, _, v = part.partition("=")
                if k == _TOKEN_PARAM:
                    return v
            return None

        def _strip_token(self, query: str) -> str:
            keep = [p for p in query.split("&")
                    if p and not p.startswith(_TOKEN_PARAM + "=")]
            return "&".join(keep)

        def _cookie_authorized(self) -> bool:
            raw = self.headers.get("Cookie") or ""
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == _TOKEN_COOKIE and secrets.compare_digest(v, flow._token):
                    return True
            return False

        def _send_simple(self, status: int, text: str):
            payload = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _send_html(self, status: int, html: str):
            payload = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _ProxyHandler


# ── 程序層單例（同時只允許一個登入流程）──────────────────────────────

_flow: Optional[BrowserLoginFlow] = None
_flow_lock = threading.Lock()


def current_flow() -> Optional[BrowserLoginFlow]:
    return _flow


def start_login_flow(session, *, reuse: bool = True) -> BrowserLoginFlow:
    """啟動（或沿用）瀏覽器登入流程。已有代理在跑時預設沿用同一個，不重複起 port。"""
    global _flow
    with _flow_lock:
        if reuse and _flow is not None and _flow.running and not _flow.success:
            return _flow
        if _flow is not None:
            _flow.shutdown()
        _flow = BrowserLoginFlow(session)
        _flow.start()
        return _flow


def shutdown_flow() -> None:
    """收掉目前的登入流程（登出／重新登入時呼叫）。"""
    global _flow
    with _flow_lock:
        if _flow is not None:
            _flow.shutdown()
        _flow = None
