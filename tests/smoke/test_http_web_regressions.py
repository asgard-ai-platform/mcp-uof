"""Offline regression checks for http_web internals touched by PR review fixes."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

_common.ensure_src_on_path()

from mcp_uof.ops.http_web import HttpSession  # noqa: E402
from mcp_uof.ops.http_web.constants import _FORM_CACHE_TTL_SECONDS  # noqa: E402
from mcp_uof.ops.http_web.rendering import _parse_filled_form_fields  # noqa: E402
from mcp_uof.ops.http_web.validation import (  # noqa: E402
    _map_row_to_columns,
    _mark_filled,
    _resolve_checkbox_value,
    _uof_row_date,
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


def _form_rows_html(rows) -> str:
    rendered = ['<input type="hidden" name="__VIEWSTATE" value="vs">']
    for i, (task_id, apply_time) in enumerate(rows):
        cls = "GridItem" if i % 2 == 0 else "GridItemAlternating"
        rendered.append(
            f'<tr class="{cls}">'
            f'<td>F-{i}</td><td>測試表單</td><td>摘要</td><td>申請者</td><td>處理中</td>'
            f'<td>{apply_time}</td><td></td>'
            f"<td><a onclick=\"openForm('TASK_ID={task_id}')\">開啟</a></td></tr>"
        )
    return "<html><body><form><table>" + "".join(rendered) + "</table></form></body></html>"


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
    resp = no_retry.post("/write.aspx", {"x": "1"})
    failures += _common.check(
        "POST 預設禁止 replay，寫入遇 Login.aspx 不自動重送",
        "Login.aspx" in str(resp.url) and no_retry._client.posts == 1,
        f"posts={no_retry._client.posts}, url={resp.url}",
    )

    retry = _session_with_client(_Client([
        _Resp("https://uof.example/Login.aspx"),
        _Resp("https://uof.example/ok.aspx"),
    ]))
    relogins = []
    retry._relogin_if_still_expired = lambda: relogins.append("login")
    resp2 = retry.post("/query.aspx", {"x": "1"}, retry_on_login=True)
    failures += _common.check(
        "已知安全的查詢 POST 明確 opt in 後才會重登重送",
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

    rendered_grid = """
    <table id="ctl00_tbFieldCollection"><tr><td>
      <span class="TitleFont">合成複合欄位</span><span class="FieldHide">(BLOCK)</span>
      <table id="ctl00_Grid1">
        <tr><th>項次</th><th>任意額外欄</th></tr>
        <tr><td>1</td><td>保留值</td></tr>
      </table>
    </td></tr></table>
    """
    parsed_grid = _parse_filled_form_fields(
        HttpSession.__new__(HttpSession)._parse(_Resp("https://uof.example/View.aspx", rendered_grid))
    )
    failures += _common.check(
        "get_task_data Grid parser 保留頁面渲染的任意額外欄位",
        parsed_grid[0]["grid"][0]["rows"]
        == [["項次", "任意額外欄"], ["1", "保留值"]],
        str(parsed_grid),
    )

    checked, posted, err = _resolve_checkbox_value(
        [{"value": "否", "label": "否"}], "否"
    )
    unchecked, _, false_err = _resolve_checkbox_value(
        [{"value": "否", "label": "否"}], False
    )
    _, _, bad_checkbox = _resolve_checkbox_value(
        [{"value": "否", "label": "否"}], "不轉"
    )
    _, _, opposite_checkbox = _resolve_checkbox_value(
        [{"value": "否", "label": "否"}], "是"
    )
    failures += _common.check(
        "checkbox 合法選項「否」優先於布林 false 語意且未知字串會擋下",
        checked and posted == "否" and err is None
        and not unchecked and false_err is None
        and bad_checkbox is not None and opposite_checkbox is not None,
        f"checked={checked}, posted={posted}, err={err}, "
        f"bad={bad_checkbox}, opposite={opposite_checkbox}",
    )

    in_range = {"apply_time": "2030/01/15 20:21", "close_time": "2030-01-16"}
    outside = {"apply_time": "2030/01/14 23:59", "close_time": ""}
    failures += _common.check(
        "query_forms 可從 UOF 日期時間欄位抽出正確日期供 client-side 範圍過濾",
        str(_uof_row_date(in_range, "apply")) == "2030-01-15"
        and str(_uof_row_date(outside, "apply")) == "2030-01-14"
        and _uof_row_date(in_range, "sign") is None,
    )

    page1 = _form_rows_html([
        ("00000000-0000-0000-0000-000000000001", "2030/01/15 20:21"),
        ("00000000-0000-0000-0000-000000000002", "2030/01/15 09:00"),
        ("00000000-0000-0000-0000-000000000003", "2030/01/14 23:59"),
    ])
    page2 = _form_rows_html([
        ("00000000-0000-0000-0000-000000000004", "2030/01/14 12:00"),
    ])
    query_session = HttpSession.__new__(HttpSession)
    query_session.get = lambda _path: _Resp("https://uof.example/MyFormList.aspx", page1)
    query_posts = []

    def _query_post(_path, payload, retry_on_login=True):
        query_posts.append(dict(payload))
        html = page1 if any(k.endswith("wibQuery") for k in payload) else page2
        return _Resp("https://uof.example/MyFormList.aspx", html)

    query_session.post = _query_post
    date_result = query_session.search_forms(
        date_from="2030/01/15", date_to="2030/01/15", max_results=30
    )
    failures += _common.check(
        "query_forms 不以 max_results 補入日期範圍外資料且共 N 筆採符合筆數",
        date_result["ok"]
        and len(date_result["rows"]) == 2
        and date_result["total_matched"] == 2
        and all(r["apply_time"].startswith("2030/01/15") for r in date_result["rows"])
        and len(query_posts) == 2,
        str(date_result),
    )

    print("=" * 50)
    print("http_web 回歸測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
