"""Offline contract tests for the replaceable WebForms runtime."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_common.ensure_src_on_path()

from mcp_uof.ops.http_web.runtime import (  # noqa: E402
    EvidenceKind,
    ReplayPolicy,
    WebFormsRuntime,
)


class _Response:
    def __init__(self, url: str, text: str = "", status_code: int = 200):
        self.url = url
        self.text = text
        self.status_code = status_code


class _ScriptedAdapter:
    """Fail-loud adapter: each call consumes exactly one scripted response."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, path):
        self.requests.append(("GET", path, None))
        return self.responses.pop(0)

    def post(self, path, data):
        self.requests.append(("POST", path, dict(data)))
        return self.responses.pop(0)


def main() -> int:
    failures = 0

    html = """
    <html><body><form>
      <input type="hidden" name="__VIEWSTATE" value="state-1">
      <input type="text" name="ctl00$ContentPlaceHolder1$Amount" value="10">
      <input type="submit" name="ctl00$ContentPlaceHolder1$Save" value="儲存">
    </form></body></html>
    """
    adapter = _ScriptedAdapter([_Response("https://uof.example/Form.aspx", html)])
    runtime = WebFormsRuntime(adapter)
    page = runtime.hydrate(adapter.get("/Form.aspx"))
    failures += _common.check(
        "hydrate 集中解析 tree、完整 form state 與正常 evidence",
        page.state == {
            "__VIEWSTATE": "state-1",
            "ctl00$ContentPlaceHolder1$Amount": "10",
        }
        and page.evidence.kind is EvidenceKind.OK
        and page.tree.xpath("string(//form/input[@name='__VIEWSTATE']/@value)") == "state-1",
        str(page.state),
    )

    posted = _Response("https://uof.example/Form.aspx", html.replace("state-1", "state-2"))
    adapter = _ScriptedAdapter([posted])
    runtime = WebFormsRuntime(adapter)
    next_page = runtime.control_postback(
        "/Form.aspx",
        page,
        "Save",
        values={"ctl00$ContentPlaceHolder1$Amount": "20"},
        replay=ReplayPolicy.NEVER,
    )
    sent = adapter.requests[0][2]
    failures += _common.check(
        "control postback 從 hydrated state 組 payload 並由 runtime 回傳下一頁",
        sent == {
            "__VIEWSTATE": "state-1",
            "ctl00$ContentPlaceHolder1$Amount": "20",
            "ctl00$ContentPlaceHolder1$Save": "儲存",
        }
        and next_page.state["__VIEWSTATE"] == "state-2",
        str(sent),
    )

    relogins = []
    adapter = _ScriptedAdapter([
        _Response("https://uof.example/Login.aspx"),
        _Response("https://uof.example/Query.aspx", html),
    ])
    runtime = WebFormsRuntime(adapter, reauthenticate=lambda: relogins.append("login"))
    replayed = runtime.post("/Query.aspx", {"q": "one"}, replay=ReplayPolicy.SAFE)
    failures += _common.check(
        "runtime 是安全 POST 登入後 replay 的唯一 owner",
        replayed.url.endswith("/Query.aspx")
        and len(adapter.requests) == 2
        and relogins == ["login"],
        f"requests={adapter.requests}, relogins={relogins}",
    )

    relogins = []
    adapter = _ScriptedAdapter([_Response("https://uof.example/Login.aspx")])
    runtime = WebFormsRuntime(adapter, reauthenticate=lambda: relogins.append("login"))
    not_replayed = runtime.post("/Write.aspx", {"x": "1"}, replay=ReplayPolicy.NEVER)
    failures += _common.check(
        "NEVER policy 保留 Login evidence 且 adapter 不自行重送",
        runtime.evidence(not_replayed).kind is EvidenceKind.LOGIN
        and len(adapter.requests) == 1
        and relogins == [],
        f"requests={adapter.requests}, relogins={relogins}",
    )

    error = _Response("https://uof.example/ErrorReport.aspx", status_code=500)
    runtime = WebFormsRuntime(_ScriptedAdapter([]))
    failures += _common.check(
        "ErrorReport evidence 集中由 runtime 分類",
        runtime.evidence(error).kind is EvidenceKind.ERROR_REPORT,
    )

    print("=" * 50)
    print("WebForms runtime 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
