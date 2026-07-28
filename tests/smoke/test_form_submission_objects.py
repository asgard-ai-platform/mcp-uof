"""Synthetic checks for form schema, field codec, and submission composition."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_common.ensure_src_on_path()

from mcp_uof.ops.http_web import HttpSession  # noqa: E402
from mcp_uof.ops.http_web.constants import _html_fromstring  # noqa: E402
from mcp_uof.ops.http_web.field_codec import FieldCodec  # noqa: E402
from mcp_uof.ops.http_web.schema import FormSchema  # noqa: E402
from mcp_uof.ops.http_web.submission import SubmissionOperation  # noqa: E402


def main() -> int:
    failures = 0
    tree = _html_fromstring("""
      <table class="fieldWidth"><tr><td>
        <span class="TitleFont">日期</span><span class="FieldHide">(DATE)</span>
        <span id="lblStart1">＊</span>
        <div class="fieldPadding">
          <input class="RadDatePicker" name="ctl$DATE" type="text">
        </div>
      </td></tr></table>
      <table class="fieldWidth"><tr><td>
        <span class="TitleFont">地點</span><span class="FieldHide">(PLACE)</span>
        <div class="fieldPadding"><select name="ctl$PLACE">
          <option value="">─請選擇─</option><option value="N">台北</option>
        </select></div>
      </td></tr></table>
      <table class="fieldWidth"><tr><td>
        <span class="TitleFont">唯讀</span><span class="FieldHide">(LOCKED)</span>
        <div class="fieldPadding"><input name="ctl$LOCKED" disabled type="text"></div>
      </td></tr></table>
      <table class="fieldWidth"><tr><td>
        <span class="TitleFont">數量</span><span class="FieldHide">(QTY)</span>
        <div class="fieldPadding"><input class="RadNumericTextBox" name="ctl$QTY" type="text"></div>
      </td></tr></table>
      <table class="fieldWidth"><tr><td>
        <span class="TitleFont">類別</span><span class="FieldHide">(KIND)</span>
        <div class="fieldPadding">
          <input id="kind_a" name="ctl$KIND" type="radio" value="A"><label for="kind_a">甲</label>
          <input id="kind_b" name="ctl$KIND" type="radio" value="B"><label for="kind_b">乙</label>
        </div>
      </td></tr></table>
      <table class="fieldWidth"><tr><td>
        <span class="TitleFont">啟用</span><span class="FieldHide">(ENABLED)</span>
        <span id="lblStartEnabled">＊</span>
        <div class="fieldPadding">
          <input id="enabled" name="ctl$ENABLED" type="checkbox" value="Y"><label for="enabled">是</label>
        </div>
      </td></tr></table>
      <table class="fieldWidth"><tr><td>
        <span class="TitleFont">權限欄位</span><span class="FieldHide">(PERMISSION)</span>
        <div class="fieldPadding"><input class="fieldDisabled" name="ctl$PERMISSION" type="text"></div>
      </td></tr></table>
    """)
    schema = FormSchema.parse(tree)
    failures += _common.check(
        "schema 集中 field lookup、required、disabled 與合法選項",
        schema.find("date").required
        and schema.find("地點").options[0].value == "N"
        and schema.find("LOCKED").disabled
        and schema.find("PERMISSION").disabled,
    )

    classic = FormSchema.parse(_html_fromstring("""
      <table><tr><td class="ul" align="right"><font color="Red">＊</font><span>主旨：</span></td>
      <td class="ul"><input name="ctl$versionFieldUC1$TextBox1" type="text"></td></tr></table>
    """))
    failures += _common.check(
        "schema 以同一入口解析 classic fallback",
        classic.find("主旨").required and classic.find("主旨").input_type == "text",
    )

    payload = {
        "ctl_DATE_dateInput_ClientState": "{}",
        "ctl_QTY_ClientState": "{}",
    }
    codec = FieldCodec(tree)
    date_result = codec.encode(schema.find("DATE"), "2030-01-02", payload)
    select_result = codec.encode(schema.find("PLACE"), "台北", payload)
    numeric_result = codec.encode(schema.find("QTY"), 2.5, payload)
    radio_result = codec.encode(schema.find("KIND"), "乙", payload)
    checkbox_result = codec.encode(schema.find("ENABLED"), "是", payload)
    unchecked_result = codec.encode(schema.find("ENABLED"), False, payload)
    invalid_result = codec.encode(schema.find("PLACE"), "高雄", payload)
    disabled_result = codec.encode(schema.find("LOCKED"), "不可寫", payload)
    failures += _common.check(
        "codec 集中 date/numeric/radio/checkbox/select 與 silent-drop 防護",
        date_result.filled_value == "2030/01/02"
        and payload["ctl$DATE$dateInput"] == "2030/01/02"
        and "2030/01/02" in payload["ctl_DATE_dateInput_ClientState"]
        and select_result.filled_value == "N"
        and payload["ctl$PLACE"] == "N"
        and numeric_result.filled_value == "2.5"
        and "2.5" in payload["ctl_QTY_ClientState"]
        and radio_result.filled_value == "B"
        and payload["ctl$KIND"] == "B"
        and checkbox_result.filled_value == "Y"
        and unchecked_result.filled_value == ""
        and "ctl$ENABLED" not in payload
        and invalid_result.blocking
        and disabled_result.warning
        and "ctl$LOCKED" not in payload,
    )

    enabled = schema.find("ENABLED")
    required_schema = FormSchema((enabled,))
    failures += _common.check(
        "required 欄位空值與未勾選 checkbox 仍視為缺漏",
        required_schema.missing_required({"ENABLED": ""}, set()) == (enabled,),
    )

    lookup_session = HttpSession.__new__(HttpSession)
    lookup_session.search_forms = lambda **_kwargs: {
        "rows": [{
            "form_number": "FORM000000001",
            "task_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "form_name": "請購單",
        }]
    }
    failures += _common.check(
        "成單 lookup 由持有 search_forms 的 HttpSession 解析 TaskId",
        lookup_session._lookup_created_form("FORM000000001")
        == ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "請購單"),
    )

    session = HttpSession.__new__(HttpSession)
    session._detail_operation = object()
    operation = session.submission_operation
    failures += _common.check(
        "HttpSession 保留 facade 並以物件組合 SubmissionOperation/DetailOperation",
        isinstance(operation, SubmissionOperation)
        and operation.detail_operation is session.detail_operation
        and callable(session.apply_form_web),
    )

    print("=" * 50)
    print("form submission objects 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
