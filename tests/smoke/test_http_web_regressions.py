"""Offline regression checks for http_web internals touched by PR review fixes."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

_common.ensure_src_on_path()

from mcp_uof.ops.http_web import (  # noqa: E402
    HttpSession,
    _FORM_CACHE_TTL_SECONDS,
    _map_row_to_columns,
    _mark_filled,
)


class _Resp:
    def __init__(self, url: str, text: str = ""):
        self.url = url
        self.text = text


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = 0

    def post(self, url, data):
        self.posts += 1
        return self.responses.pop(0)


def _session_with_client(client: _Client) -> HttpSession:
    s = HttpSession.__new__(HttpSession)
    s._base = "https://uof.example"
    s._vpath = ""
    s._client = client
    return s


def main() -> int:
    failures = 0

    filled = {}
    fb = {"code": "003", "label": "客戶名稱"}
    _mark_filled(filled, "客戶名稱", fb, "測試客戶")
    failures += _common.check(
        "filled 同時支援 caller key / code / label",
        filled.get("客戶名稱") == "測試客戶" and filled.get("003") == "測試客戶",
        str(filled),
    )

    no_retry = _session_with_client(_Client([_Resp("https://uof.example/Login.aspx")]))
    no_retry._relogin_if_still_expired = lambda: (_ for _ in ()).throw(AssertionError("不應重登"))
    resp = no_retry.post("/write.aspx", {"x": "1"}, retry_on_login=False)
    failures += _common.check(
        "寫入 POST 遇 Login.aspx 不自動重送",
        "Login.aspx" in str(resp.url) and no_retry._client.posts == 1,
        f"posts={no_retry._client.posts}, url={resp.url}",
    )

    retry = _session_with_client(_Client([
        _Resp("https://uof.example/Login.aspx"),
        _Resp("https://uof.example/ok.aspx"),
    ]))
    relogins = []
    retry._relogin_if_still_expired = lambda: relogins.append("login")
    resp2 = retry.post("/query.aspx", {"x": "1"})
    failures += _common.check(
        "查詢 POST 預設仍會重登後重送",
        str(resp2.url).endswith("/ok.aspx") and retry._client.posts == 2 and relogins == ["login"],
        f"posts={retry._client.posts}, relogins={relogins}, url={resp2.url}",
    )

    cache_s = _session_with_client(_Client([]))
    cache_s._form_cache_at = time.monotonic()
    failures += _common.check("表單快取 TTL 內有效", cache_s._form_cache_valid())
    cache_s._form_cache_at = time.monotonic() - _FORM_CACHE_TTL_SECONDS - 1
    failures += _common.check("表單快取 TTL 後失效", not cache_s._form_cache_valid())

    columns = [
        {"index": 0, "label": "品名"},
        {"index": 1, "label": "數量"},
    ]
    mapped, unmatched = _map_row_to_columns({"品名": "筆", "數量": 2}, columns)
    failures += _common.check(
        "明細列欄名可映射到欄位 index",
        mapped == {0: "筆", 1: 2} and unmatched == [],
        f"mapped={mapped}, unmatched={unmatched}",
    )

    print("=" * 50)
    print("http_web 回歸測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
