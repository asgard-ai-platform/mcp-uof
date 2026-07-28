"""Synthetic transaction tests for dialog/detail operation ownership."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_common.ensure_src_on_path()

from mcp_uof.ops.http_web.details import DetailOperation  # noqa: E402
from mcp_uof.ops.http_web.runtime import WebFormsRuntime  # noqa: E402


class _Response:
    def __init__(self, url: str, text: str):
        self.url = url
        self.text = text
        self.status_code = 200


class _Adapter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, path):
        self.requests.append(("GET", path, None))
        return self.responses.pop(0)

    def post(self, path, data):
        self.requests.append(("POST", path, dict(data)))
        return self.responses.pop(0)


def _dialog(picked=""):
    return f"""
    <form><input type="hidden" name="__VIEWSTATE" value="dialog-state">
      <table><tr><td>＊品名</td><td>
        <input name="ctl00$txtName" id="ctl00_txtName" type="text" value="品項">
      </td></tr><tr><td>挑選結果</td><td>
        <input name="ctl00$txtPicked" id="ctl00_txtPicked" type="text" value="{picked}">
        <input name="ctl00$btnLookup" id="ctl00_btnLookup" type="submit" value="挑選">
      </td></tr></table>
    </form>
    """


def _dialog_with_select():
    return """
    <form><input type="hidden" name="__VIEWSTATE" value="dialog-state">
      <table><tr><td>類型</td><td>
        <select name="ctl00$ddlType" id="ctl00_ddlType">
          <option value="A">甲</option>
        </select>
        <input name="ctl00$btnLookup" id="ctl00_btnLookup" type="submit" value="挑選">
      </td></tr></table>
    </form>
    """


def _parent(with_target_row=False):
    target = "<tr><td>新列</td></tr>" if with_target_row else "<tr><td>沒有資料</td></tr>"
    return f"""
    <form><input type="hidden" name="__VIEWSTATE" value="parent-state">
      <input name="ctl00$versionFieldUC1$btnRows" id="ctl00_versionFieldUC1_btnRows"
        type="button" onclick="$uof.dialog.open2('/UOF/RowDialog.aspx?GridDataID=one')">
      <table id="ctl00_versionFieldUC9_OtherGrid"><tr><td>既有其他明細</td></tr></table>
      <table id="ctl00_versionFieldUC1_TargetGrid">{target}</table>
    </form>
    """


def main():
    failures = 0
    base = "https://uof.example/UOF"
    adapter = _Adapter([
        _Response(base + "/RowDialog.aspx?GridDataID=one", _dialog()),
        _Response(base + "/RowDialog.aspx?GridDataID=one", _dialog("ITEM-7")),
        _Response(
            base + "/RowDialog.aspx?GridDataID=one",
            '<meta id="TempReturnValue" content="{&quot;name&quot;:&quot;品項&quot;}">',
        ),
        _Response(base + "/FirstSite.aspx", _parent(with_target_row=True)),
    ])
    runtime = WebFormsRuntime(adapter)
    operation = DetailOperation(runtime, lambda value: value.removeprefix("/UOF"))
    parent = runtime.hydrate(_Response(base + "/FirstSite.aspx", _parent()))
    result = operation.persist_plugin_batches(
        "/FirstSite.aspx",
        parent,
        parent.state,
        "versionFieldUC1",
        [("btnRows", [{
            "txtName": "品項",
            "_lookups": [{"press": "btnLookup", "row": {"InternalId": "ITEM-7"}}],
        }])],
    )
    failures += _common.check(
        "detail operation 擁有 discovery→lookup→confirm→parent replay 完整 transaction",
        result.ok
        and result.added == 1
        and [(method, path) for method, path, _ in adapter.requests] == [
            ("GET", "/RowDialog.aspx?GridDataID=one"),
            ("POST", "/RowDialog.aspx?GridDataID=one"),
            ("POST", "/RowDialog.aspx?GridDataID=one"),
            ("POST", "/FirstSite.aspx"),
        ],
        f"result={result}, requests={adapter.requests}",
    )
    lookup, confirm, replay = [data for method, _path, data in adapter.requests if method == "POST"]
    failures += _common.check(
        "lookup 與 parent replay 的回填協定封裝在 operation 且 confirm 不會安全重送",
        "DialogReturnValue" in lookup
        and confirm.get("__EVENTTARGET") == "ctl00$MasterPageRadButton1"
        and "DialogReturnValue" in replay,
        f"lookup={lookup}, confirm={confirm}, replay={replay}",
    )

    false_positive_adapter = _Adapter([
        _Response(base + "/RowDialog.aspx?GridDataID=one", _dialog()),
        _Response(
            base + "/RowDialog.aspx?GridDataID=one",
            '<meta id="TempReturnValue" content="{&quot;name&quot;:&quot;品項&quot;}">',
        ),
        _Response(base + "/FirstSite.aspx", _parent(with_target_row=False)),
    ])
    false_runtime = WebFormsRuntime(false_positive_adapter)
    false_operation = DetailOperation(false_runtime, lambda value: value.removeprefix("/UOF"))
    false_parent = false_runtime.hydrate(_Response(base + "/FirstSite.aspx", _parent()))
    rejected = false_operation.persist_plugin_batches(
        "/FirstSite.aspx",
        false_parent,
        false_parent.state,
        "versionFieldUC1",
        [("btnRows", [{"txtName": "品項"}])],
    )
    failures += _common.check(
        "persistence proof 僅接受指定 plugin block 的 Grid 新增列",
        not rejected.ok and "指定明細表格" in rejected.errors[-1],
        str(rejected),
    )

    ownership_page = false_runtime.hydrate(_Response(base + "/FirstSite.aspx", """
      <form>
        <input name="ctl00$versionFieldUC10$btnRows" type="button"
          onclick="$uof.dialog.open2('/UOF/WrongDialog.aspx?GridDataID=ten')">
        <input name="ctl00$versionFieldUC1$btnRows" type="button"
          onclick="$uof.dialog.open2('/UOF/RightDialog.aspx?GridDataID=one')">
      </form>
    """))
    failures += _common.check(
        "editor ownership 不會把 versionFieldUC1 與 versionFieldUC10 混為同一區塊",
        false_operation.discover_plugin_editors(ownership_page, "versionFieldUC1")
        == [("ctl00$versionFieldUC1$btnRows", "/UOF/RightDialog.aspx?GridDataID=one")],
    )

    invalid_adapter = _Adapter([
        _Response(base + "/RowDialog.aspx?GridDataID=one", _dialog_with_select()),
    ])
    invalid_operation = DetailOperation(
        WebFormsRuntime(invalid_adapter), lambda value: value.removeprefix("/UOF")
    )
    invalid_prefill = invalid_operation.persist_plugin_rows(
        "/RowDialog.aspx?GridDataID=one",
        [{
            "_fill_before": {"ddlType": "不存在"},
            "_lookups": [{"press": "btnLookup", "row": {"InternalId": "ITEM-7"}}],
        }],
    )
    failures += _common.check(
        "_fill_before 非法選項會阻擋該列且不送出 lookup/confirm",
        invalid_prefill.added == 0
        and any("_fill_before" in error for error in invalid_prefill.errors)
        and [request[0] for request in invalid_adapter.requests] == ["GET"],
        f"result={invalid_prefill}, requests={invalid_adapter.requests}",
    )

    apply_source = (
        Path(__file__).resolve().parents[2]
        / "src/mcp_uof/ops/http_web/submission.py"
    ).read_text(encoding="utf-8")
    failures += _common.check(
        "apply 不再知道 plugin choreography 或 dialog/Grid 回填協定",
        all(token not in apply_source for token in (
            "DialogReturnValue", "GridDataID", '"_rows"', '"_lookups"',
            '"_fill_before"', '"_press_after"', '"_press_last"',
            "_missing_required_controls", "_matches_uc_prefix",
            "_trigger_control", "_fill_control_value",
        )),
    )

    dialogs_source = (
        Path(__file__).resolve().parents[2]
        / "src/mcp_uof/ops/http_web/dialogs.py"
    ).read_text(encoding="utf-8")
    session_source = (
        Path(__file__).resolve().parents[2]
        / "src/mcp_uof/ops/http_web/session.py"
    ).read_text(encoding="utf-8")
    transport_source = (
        Path(__file__).resolve().parents[2]
        / "src/mcp_uof/ops/http_web/transport.py"
    ).read_text(encoding="utf-8")
    failures += _common.check(
        "legacy detail write methods 不會回到 DialogOperation/HttpSession",
        "def add_plugin_dialog_rows" not in dialogs_source
        and "def add_datagrid_rows" not in dialogs_source
        and "def add_plugin_dialog_rows" not in session_source
        and "def add_datagrid_rows" not in session_source,
    )
    failures += _common.check(
        "transport lifecycle 只接受注入 callback，不反向匯入 session",
        "from .session" not in transport_source
        and "restore_session" in transport_source
        and "password_authenticated" in transport_source,
    )

    print("=" * 50)
    print("detail operation transaction 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
