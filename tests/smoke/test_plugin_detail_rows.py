"""Offline regression tests for PR #4 plugin detail-row behavior."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_common.ensure_src_on_path()

from mcp_uof.ops.http_web import (  # noqa: E402
    HttpSession,
    HttpWebBackend,
    _dialog_reject_reason,
    _find_row_editor_openers,
    _lookup_dialog_target,
    _missing_required_controls,
    _parse_dialog_fields,
)


class _Resp:
    def __init__(self, text: str, url: str = "https://uof.example/UOF/RowDialog.aspx"):
        self.text = text
        self.url = url


def _dialog_html(*, name: str = "", calculated: str = "", picked: str = "") -> str:
    return f"""
    <html><body><form>
      <input type="hidden" name="__VIEWSTATE" value="vs">
      <table><tr>
        <td>＊品名</td><td><input type="text" name="ctl00$ContentPlaceHolder1$txtName"
          id="ctl00_ContentPlaceHolder1_txtName" value="{name}"></td>
        <td>計算值</td><td><input type="text" name="ctl00$ContentPlaceHolder1$txtCalculated"
          id="ctl00_ContentPlaceHolder1_txtCalculated" value="{calculated}" readonly></td>
        <td>挑選結果</td><td><input type="text" name="ctl00$ContentPlaceHolder1$txtPicked"
          id="ctl00_ContentPlaceHolder1_txtPicked" value="{picked}" readonly></td>
        <td>
          <input type="submit" name="ctl00$ContentPlaceHolder1$btnCalc"
            id="ctl00_ContentPlaceHolder1_btnCalc" value="計算">
          <input type="submit" name="ctl00$ContentPlaceHolder1$btnLookup"
            id="ctl00_ContentPlaceHolder1_btnLookup" value="挑選">
          <input type="submit" name="ctl00$ContentPlaceHolder1$btnAfterLookup"
            id="ctl00_ContentPlaceHolder1_btnAfterLookup" value="查詢帶入值">
        </td>
      </tr></table>
    </form></body></html>
    """


def _sequence_session():
    session = HttpSession.__new__(HttpSession)
    session._vpath = "/UOF"
    order = []
    payloads = []
    current = {"html": _dialog_html()}

    def get(_path):
        return _Resp(current["html"])

    def post(_path, payload, retry_on_login=False):
        payloads.append(dict(payload))
        if any(k.endswith("$btnAfterLookup") for k in payload):
            order.append("after_lookup")
            current["html"] = _dialog_html(
                name=payload["ctl00$ContentPlaceHolder1$txtName"],
                calculated="after:" + payload["ctl00$ContentPlaceHolder1$txtPicked"],
                picked=payload["ctl00$ContentPlaceHolder1$txtPicked"],
            )
            return _Resp(current["html"])
        if any(k.endswith("$btnCalc") for k in payload):
            order.append("calculate")
            current["html"] = _dialog_html(name=payload["ctl00$ContentPlaceHolder1$txtName"],
                                           calculated="42")
            return _Resp(current["html"])
        if "DialogReturnValue" in payload:
            order.append("lookup")
            selected = json.loads(payload["DialogReturnValue"])
            current["html"] = _dialog_html(
                name=payload["ctl00$ContentPlaceHolder1$txtName"],
                calculated=payload["ctl00$ContentPlaceHolder1$txtCalculated"],
                picked=selected["InternalId"],
            )
            return _Resp(current["html"])
        order.append("confirm")
        return _Resp('<meta id="TempReturnValue" content="{&quot;ok&quot;:true}">')

    session.get = get
    session.post = post
    return session, order, payloads


def _structure_session():
    session = HttpSession.__new__(HttpSession)
    session._vpath = "/UOF"
    session._resolve_form_ids = lambda _vid: ("form-1", "version-1")
    session.fetches = []
    apply_html = """
    <html><body>
      <table class="fieldWidth">
        <tr><td><span class="TitleFont">區塊 A</span><span class="FieldHide">(BLOCK_A)</span></td></tr>
        <tr><td class="fieldPadding">
          <input type="button" name="ctl00$versionFieldUC1$btnMainA"
            id="ctl00_versionFieldUC1_btnMainA"
            onclick="$uof.dialog.open2('/UOF/MainA.aspx?id=1')">
          <input type="text" name="ctl00$versionFieldUC1$txtPayee"
            id="ctl00_versionFieldUC1_txtPayee">
          <input type="button" name="ctl00$versionFieldUC1$btnRowsA"
            id="ctl00_versionFieldUC1_btnRowsA"
            onclick="$uof.dialog.open2('/UOF/RowsA.aspx?GridDataID=A')">
        </td></tr>
      </table>
      <table class="fieldWidth">
        <tr><td><span class="TitleFont">區塊 B</span><span class="FieldHide">(BLOCK_B)</span></td></tr>
        <tr><td class="fieldPadding">
          <input type="button" name="ctl00$versionFieldUC2$btnMainB"
            id="ctl00_versionFieldUC2_btnMainB"
            onclick="$uof.dialog.open2('/UOF/MainB.aspx?id=2')">
          <input type="button" name="ctl00$versionFieldUC2$btnRowsB"
            id="ctl00_versionFieldUC2_btnRowsB"
            onclick="$uof.dialog.open2('/UOF/RowsB.aspx?GridDataID=B')">
        </td></tr>
      </table>
    </body></html>
    """
    main_dialog = """
    <table><tr><td>名稱</td><td><input type="text" name="ctl00$txtName"></td></tr></table>
    """
    row_a = """
    <table><tr><td>＊料號</td><td>
      <input type="text" name="ctl00$txtItemA" id="ctl00_txtItemA">
      <input type="button" name="ctl00$btnPickA"
        onclick="$uof.dialog.open2('/UOF/PickerA.aspx?kind=item')">
    </td></tr></table>
    """
    row_b = """
    <table><tr><td>說明</td><td>
      <input type="text" name="ctl00$txtMemoB" id="ctl00_txtMemoB">
    </td></tr></table>
    """

    def get(path):
        session.fetches.append(path)
        if "AddFormScript.aspx" in path:
            return _Resp("", "https://uof.example/UOF/FirstSite.aspx")
        if "FirstSite.aspx" in path:
            return _Resp(apply_html, "https://uof.example/UOF/FirstSite.aspx")
        if "RowsA.aspx" in path:
            return _Resp(row_a)
        if "RowsB.aspx" in path:
            return _Resp(row_b)
        if "MainA.aspx" in path or "MainB.aspx" in path:
            return _Resp(main_dialog)
        raise AssertionError(f"unexpected GET {path}")

    session.get = get
    return session


def _rejecting_session():
    session = HttpSession.__new__(HttpSession)
    session._vpath = "/UOF"
    session.get = lambda _path: _Resp(_dialog_html())
    session.post = lambda *_args, **_kwargs: _Resp(
        '<span id="ctl00_RF_txtItem"><font color="Red">品名必填</font></span>'
        '<script>var r={"errorMessage":["品項已停用"]};</script>'
    )
    return session


def main() -> int:
    failures = 0

    apply_html = """
    <div>
      <input name="ctl00$versionFieldUC1$btnRowsA" id="ctl00_versionFieldUC1_btnRowsA"
        onclick="$uof.dialog.open2('/UOF/A.aspx?GridDataID=1&amp;x=2')">
      <input name="ctl00$versionFieldUC2$btnRowsB" id="ctl00_versionFieldUC2_btnRowsB"
        onclick="$uof.dialog.open('/UOF/B.aspx?GridDataID=2')">
      <input name="ctl00$versionFieldUC1$btnPicker" id="ctl00_versionFieldUC1_btnPicker"
        onclick="$uof.dialog.open2('/UOF/MainPicker.aspx?id=3')">
      <input name="ctl00$versionFieldUC10$btnRowsTen" id="ctl00_versionFieldUC10_btnRowsTen"
        onclick="$uof.dialog.open2('/UOF/Ten.aspx?GridDataID=10')">
    </div>
    """
    openers = _find_row_editor_openers(
        apply_html, {"MainPicker.aspx"}, owner_prefix="versionFieldUC1"
    )
    failures += _common.check(
        "row editor 只歸屬同一個 plugin block，且排除主 picker",
        len(openers) == 1
        and openers[0]["open_button"] == "btnRowsA"
        and openers[0]["url"].endswith("GridDataID=1&x=2"),
        str(openers),
    )

    nested_html = """
    <table><tr><td>料號</td><td>
      <input type="text" name="ctl00$txtItem" id="ctl00_txtItem">
      <input type="button" name="ctl00$btnItem"
        onclick="$uof.dialog.open2('/UOF/ItemPicker.aspx?q=1&amp;mode=all')">
    </td></tr></table>
    """
    fields = _parse_dialog_fields(nested_html)
    target = _lookup_dialog_target(nested_html, "btnItem")
    failures += _common.check(
        "列內控制項能解析巢狀 picker 與開窗按鈕",
        fields[0]["lookup_buttons"] == ["btnItem"]
        and target == "/UOF/ItemPicker.aspx?q=1&mode=all",
        f"fields={fields}, target={target}",
    )

    rejection = """
    <span id="ctl00_RF_txtPayee"><font color="Red">付款人必填</font></span>
    <span id="ctl00_RF_hidden" style="display: none"><font color="Red">隱藏必填</font></span>
    <script>var result={"errorMessage":["憑證格式不符","發票字軌不符"]};</script>
    """
    reason = _dialog_reject_reason(rejection)
    failures += _common.check(
        "列確認失敗會回 required 與 server validation 實際原因",
        "txtPayee" in reason
        and "憑證格式不符" in reason
        and "發票字軌不符" in reason
        and "hidden" not in reason,
        reason,
    )
    failures += _common.check(
        "錯誤頁含損壞 validation JSON 時仍能回傳可解析部分",
        _dialog_reject_reason(
            '<span id="ctl00_RF_txtCode"><font color="Red">代碼必填</font></span>'
            '<script>{"errorMessage":["bad\\q"]}</script>'
        ) == "必填未填/未帶：txtCode；伺服器驗證訊息格式無法解析",
    )

    required_html = """
    <table><tr><td>＊付款人</td><td>
      <input type="text" name="ctl00$versionFieldUC1$txtPayee"
        id="ctl00_versionFieldUC1_txtPayee" value="">
    </td></tr>
    <tr><td>＊覆核人</td><td>
      <input type="text" name="ctl00$versionFieldUC1$txtReviewer" value="">
    </td></tr>
    <tr><td>＊其他區塊</td><td>
      <input type="text" name="ctl00$versionFieldUC10$txtOther"
        id="ctl00_versionFieldUC10_txtOther" value="">
    </td></tr></table>
    """
    required_tree = HttpSession.__new__(HttpSession)._parse(_Resp(required_html))
    missing = _missing_required_controls(
        required_tree,
        "versionFieldUC1",
        {
            "ctl00$versionFieldUC1$txtPayee": "",
            "ctl00$versionFieldUC1$txtReviewer": "",
        },
    )
    present = _missing_required_controls(
        required_tree,
        "versionFieldUC1",
        {
            "ctl00$versionFieldUC1$txtPayee": "alice",
            "ctl00$versionFieldUC1$txtReviewer": "bob",
        },
    )
    failures += _common.check(
        "必填控制項即使 caller 有傳 key，空字串仍視為缺漏且 name-only control 有短 id",
        {c["id"] for c in missing} == {"txtPayee", "txtReviewer"} and present == [],
        f"missing={missing}, present={present}",
    )

    row_required_html = """
    <table><tr><td>＊必填數值</td><td>
      <input type="text" name="ctl00$ContentPlaceHolder1$txtRequiredValue"
        id="ctl00_ContentPlaceHolder1_txtRequiredValue" value="">
      <input type="text" name="ctl00$ContentPlaceHolder1$txtInternalHelper"
        id="ctl00_ContentPlaceHolder1_txtInternalHelper" class="HideMe" value="">
    </td></tr>
    <tr><td>備註</td><td>
      <input type="text" name="ctl00$ContentPlaceHolder1$txtMemo"
        id="ctl00_ContentPlaceHolder1_txtMemo" value="">
    </td></tr></table>
    """
    required_row_session = HttpSession.__new__(HttpSession)
    required_row_session._vpath = "/UOF"
    required_row_session.get = lambda _path: _Resp(row_required_html)
    required_row_posts = []
    required_row_session.post = lambda path, payload, retry_on_login=False: (
        required_row_posts.append(dict(payload)) or _Resp(
            '<meta id="TempReturnValue" content="{&quot;ok&quot;:true}">'
        )
    )
    missing_rows = required_row_session.add_plugin_dialog_rows(
        "/UOF/RowDialog.aspx?GridDataID=required",
        [{"txtMemo": "缺 key"}, {"txtMemo": "空字串", "txtRequiredValue": ""}],
    )
    failures += _common.check(
        "row editor 列內必填缺 key 或空字串都擋下且不送確認",
        not missing_rows["ok"]
        and missing_rows["added"] == 0
        and len(missing_rows["errors"]) == 2
        and all("txtRequiredValue" in e for e in missing_rows["errors"])
        and all("txtInternalHelper" not in e for e in missing_rows["errors"])
        and required_row_posts == [],
        f"result={missing_rows}, posts={required_row_posts}",
    )

    session, order, payloads = _sequence_session()
    result = session.add_plugin_dialog_rows(
        "/UOF/RowDialog.aspx?GridDataID=1",
        [{
            "txtName": "測試品項",
            "_press_after": ["btnCalc"],
            "_lookups": [{"press": "btnLookup", "row": {"InternalId": "ITEM-7"}}],
            "_press_last": ["btnAfterLookup"],
        }],
    )
    confirm_payload = payloads[-1]
    failures += _common.check(
        "明細列 postback 順序為一般欄位→計算→lookup→後置按鈕→確認",
        result["ok"] and order == ["calculate", "lookup", "after_lookup", "confirm"],
        f"result={result}, order={order}",
    )
    failures += _common.check(
        "lookup 帶入值保留到最後確認 payload",
        confirm_payload.get("ctl00$ContentPlaceHolder1$txtCalculated") == "after:ITEM-7"
        and confirm_payload.get("ctl00$ContentPlaceHolder1$txtPicked") == "ITEM-7",
        str(confirm_payload),
    )

    invalid_session, invalid_order, _ = _sequence_session()
    invalid = invalid_session.add_plugin_dialog_rows(
        "/UOF/RowDialog.aspx?GridDataID=1",
        [{"txtName": "測試", "_lookups": [{"press": "", "row": None}]}],
    )
    failures += _common.check(
        "不完整 lookup 規格會擋下該列且不送確認",
        not invalid["ok"] and invalid_order == [] and "_lookups 需要" in invalid["errors"][0],
        str(invalid),
    )

    rejected = _rejecting_session().add_plugin_dialog_rows(
        "/UOF/RowDialog.aspx?GridDataID=1", [{"txtName": "測試"}]
    )
    failures += _common.check(
        "row editor 拒絕確認時整合實際 required/server 原因",
        not rejected["ok"]
        and "txtItem" in rejected["errors"][0]
        and "品項已停用" in rejected["errors"][0],
        str(rejected),
    )

    structure_session = _structure_session()
    structure = structure_session.dialog_structure("version-1")
    rows_a = structure["fields"][0].get("row_editors", [])
    rows_b = structure["fields"][1].get("row_editors", [])
    failures += _common.check(
        "dialog_structure 解析多個 block 時不交叉掛載 row editor",
        structure["ok"]
        and [r["dialog"] for r in rows_a] == ["RowsA.aspx"]
        and [r["dialog"] for r in rows_b] == ["RowsB.aspx"],
        str(structure),
    )
    failures += _common.check(
        "dialog_structure 回傳列內 required 與巢狀 picker metadata",
        rows_a[0]["fields"][0]["required"]
        and rows_a[0]["fields"][0]["picker_dialog"] == "PickerA.aspx"
        and rows_a[0]["fields"][0]["picker_url"].endswith("kind=item"),
        str(rows_a),
    )

    structure_session.list_dialog_options = lambda url, keyword, limit: [{
        "Code": keyword, "Source": url, "Limit": limit, "Extra": "must-keep",
    }]
    structure_session.fetches.clear()
    block_options = structure_session.dialog_options("version-1", "BLOCK_A", "A-001", 5)
    block_fetches = list(structure_session.fetches)
    structure_session.fetches.clear()
    lazy_nested = structure_session.dialog_options("version-1", "txtItemA", "A-001", 5)
    nested_fetches = list(structure_session.fetches)
    missing_options = structure_session.dialog_options("version-1", "NOT_FOUND", "", 5)
    failures += _common.check(
        "search_dialog_options 同時支援 block-level picker",
        block_options["ok"]
        and block_options["rows"][0]["Code"] == "A-001"
        and "MainA.aspx" in block_options["rows"][0]["Source"],
        str(block_options),
    )
    failures += _common.check(
        "block-level picker 查詢不預先抓取所有 dialog/row editor",
        not any("MainA.aspx" in p or "RowsA.aspx" in p
                or "MainB.aspx" in p or "RowsB.aspx" in p for p in block_fetches),
        str(block_fetches),
    )
    failures += _common.check(
        "nested picker 找到目標 block 後停止，不再抓後續 block",
        lazy_nested["ok"]
        and any("RowsA.aspx" in p for p in nested_fetches)
        and not any("MainB.aspx" in p or "RowsB.aspx" in p for p in nested_fetches),
        str(nested_fetches),
    )
    failures += _common.check(
        "未知 dialog field 明確回傳找不到",
        not missing_options["ok"] and "找不到" in missing_options["reason"],
        str(missing_options),
    )

    no_id = HttpSession.__new__(HttpSession)
    no_id._resolve_form_ids = lambda _vid: ("", "")
    failures += _common.check(
        "dialog_structure 無法解析 form id 時 fail closed",
        not no_id.dialog_structure("missing")["ok"],
    )

    failures += _common.check(
        "search_dialog_options 能沿列內控制項查巢狀 picker",
        lazy_nested["ok"] and lazy_nested["rows"][0]["Extra"] == "must-keep",
        str(lazy_nested),
    )

    options = type("_Options", (), {
        "dialog_options": lambda self, *_args: {
            "ok": True,
            "field": "料號",
            "rows": [{
                "Code": "ABC",
                "Name": "測試",
                "InternalId": "secret-id-needed-by-server",
                "_transport": "omit",
            }],
        }
    })()

    class _Backend(HttpWebBackend):
        @property
        def _session(self):
            return options

    backend = _Backend()
    rendered = backend.search_dialog_options("v1", "txtItem", "ABC")
    failures += _common.check(
        "候選結果輸出完整可重放 row JSON，並排除內部 transport key",
        '"InternalId": "secret-id-needed-by-server"' in rendered
        and '"_transport"' not in rendered
        and "row=" in rendered,
        rendered,
    )

    structure_options = type("_Structure", (), {
        "dialog_structure": lambda self, *_args: structure,
    })()

    class _StructureBackend(HttpWebBackend):
        @property
        def _session(self):
            return structure_options

    structure_rendered = _StructureBackend().get_dialog_structure("version-1")
    failures += _common.check(
        "公開結構輸出包含各 row editor、required 與 nested picker 操作提示",
        "RowsA.aspx" in structure_rendered
        and "RowsB.aspx" in structure_rendered
        and "＊料號" in structure_rendered
        and "picker: PickerA.aspx" in structure_rendered,
        structure_rendered,
    )

    print("=" * 50)
    print("plugin detail-row 回歸測試完成"
          + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
