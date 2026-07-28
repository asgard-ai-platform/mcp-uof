"""Scripted transaction checks for guarded task lifecycle writes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_common.ensure_src_on_path()

from _scripted_uof import ScriptedHttpSession, ScriptedResponse, get, post  # noqa: E402


TASK = "11111111-1111-1111-1111-111111111111"
SITE = "22222222-2222-2222-2222-222222222222"
TASK_PATH = f"/WKF/FormUse/ViewFormTemp.aspx?TASK_ID={TASK}"
HOME = "/Homepage.aspx"
SIGN_PATH = (
    "/WKF/FormUse/FreeTask/SignNodeForm.aspx"
    f"?TASK_ID={TASK}&SITE_ID={SITE}&NODE_SEQ=3"
)
CONFIRM_PATH = "/WKF/FormUse/FreeTask/SendOtherSite.aspx?token=abc"
VOID_PATH = f"/WKF/FormUse/FormHandle/FormGetBack.aspx?TASK_ID={TASK}"


def response(path: str, text: str = "", status_code: int = 200) -> ScriptedResponse:
    return ScriptedResponse(f"https://uof.example{path}", text, status_code)


def task_page(
    status: str | None,
    *,
    applicant_account: str = "tester",
    applicant_name: str = "測試申請人",
    applicant_alias: str = "tester-alias",
    applicant_title: str = "測試職稱",
    signer_text: str = "",
) -> str:
    result = f"<div>表單審核結果： {status}</div>" if status else ""
    signer = signer_text or f"{applicant_name} {applicant_alias}({applicant_account})"
    return f"""
      <html><body>{result}<span>ABC123456</span>
      <span id="ctl00_ContentPlaceHolder1_lblApplicant">{applicant_alias} ( {applicant_title} )</span>
      <table id="SignCommentGrid"><tr>
        <td>1</td><td></td><td><span id="lblSigner">{signer}</span></td><td>送出</td>
        <td>2030/01/01 10:00</td><td>申請</td>
      </tr></table></body></html>
    """


def pending_page(include_task: bool = True) -> str:
    row = ""
    if include_task:
        row = (
            "<tr><td><a href=\"SignNodeForm.aspx?TASK_ID=" + TASK
            + "&amp;SITE_ID=" + SITE + "&amp;NODE_SEQ=3\">測試單</a></td></tr>"
        )
    return f'<html><body><span>共 {1 if include_task else 0} 筆</span><table id="DGFormList">{row}</table></body></html>'


def signing_steps(after_status: str):
    return [
        get(TASK_PATH, response(TASK_PATH, task_page("簽核中"))),
        get(HOME, response(HOME, pending_page())),
        get(SIGN_PATH, response(SIGN_PATH, '<input type="hidden" name="__VIEWSTATE" value="one">')),
        post(
            SIGN_PATH,
            response(SIGN_PATH, f'<a href="{CONFIRM_PATH.replace("&", "&amp;")}">confirm</a>'),
            retry_on_login=False,
        ),
        get(CONFIRM_PATH, response(CONFIRM_PATH, '<input type="hidden" name="__VIEWSTATE" value="two">')),
        post(CONFIRM_PATH, response(CONFIRM_PATH, "accepted"), retry_on_login=False),
        get(TASK_PATH, response(TASK_PATH, task_page(after_status))),
    ]


def session(steps, account: str = "tester") -> ScriptedHttpSession:
    scripted = ScriptedHttpSession(steps, form_ids=("form", "version"))
    scripted.session_account = account
    return scripted


def main() -> int:
    failures = 0

    unknown = session([get(TASK_PATH, response(TASK_PATH, task_page(None)))])
    snapshot = unknown.lifecycle_operation.inspect(TASK)
    failures += _common.check(
        "未知任務頁 fail closed，不預設為簽核中",
        not snapshot["ok"] and "可辨識" in snapshot["reason"],
        str(snapshot),
    )
    unknown.assert_finished()

    status_only = session([
        get(TASK_PATH, response(TASK_PATH, "<html><div>表單審核結果： 簽核中</div></html>"))
    ])
    snapshot = status_only.lifecycle_operation.inspect(TASK)
    failures += _common.check(
        "只有狀態字樣而無任務內容的錯誤頁仍 fail closed",
        not snapshot["ok"] and "evidence" in snapshot["reason"],
        str(snapshot),
    )
    status_only.assert_finished()

    formatted_identity = session([
        get(TASK_PATH, response(TASK_PATH, task_page(
            "簽核中",
            applicant_account="test_account",
            applicant_name="測試專用帳號",
            applicant_alias="employee02",
        )))
    ], account="test_account")
    formatted_snapshot = formatted_identity.lifecycle_operation.inspect(TASK)
    matching_guard = formatted_identity.lifecycle_operation._cancel_owner_guard(
        formatted_snapshot
    )
    alias_guard = session([], account="employee02").lifecycle_operation._cancel_owner_guard(
        formatted_snapshot
    )
    title_guard = session([], account="課長").lifecycle_operation._cancel_owner_guard(
        formatted_snapshot
    )
    failures += _common.check(
        "fixture 忠實區分 lblApplicant 別名職稱與 lblSigner 登入帳號",
        formatted_snapshot.get("applicant") == "測試專用帳號 employee02(test_account)"
        and "applicant_account" not in formatted_snapshot,
        str(formatted_snapshot),
    )
    failures += _common.check(
        "lblSigner 最後括號解析 test_account，帳號相符時放行",
        matching_guard is None,
        str(matching_guard),
    )
    failures += _common.check(
        "lblApplicant 的 employee02 別名與課長職稱都不得成為帳號來源",
        alias_guard is not None and title_guard is not None,
        f"snapshot={formatted_snapshot}, matching={matching_guard}, "
        f"alias={alias_guard}, title={title_guard}",
    )
    formatted_identity.assert_finished()

    malformed_pending = session([get(HOME, response(HOME, "<html>unexpected</html>"))])
    listing = malformed_pending.lifecycle_operation.pending()
    failures += _common.check(
        "未知首頁不會被誤判為空待簽清單",
        not listing["ok"] and not listing["rows"],
        str(listing),
    )
    malformed_pending.assert_finished()

    terminal = session([get(TASK_PATH, response(TASK_PATH, task_page("通過")))])
    guarded = terminal.lifecycle_operation.terminate(TASK, "Cancel", "不應送出")
    failures += _common.check(
        "終態正規化後 guard 攔截作廢且不送 POST",
        not guarded["ok"] and "同意" in guarded["reason"] and not any(
            request.method == "POST" for request in terminal.requests
        ),
        str(guarded),
    )
    terminal.assert_finished()

    not_owned = session(
        [get(TASK_PATH, response(TASK_PATH, task_page("簽核中")))],
        account="another-user",
    )
    rejected_cancel = not_owned.lifecycle_operation.terminate(
        TASK, "Cancel", "不可作廢他人申請"
    )
    failures += _common.check(
        "Cancel 在 POST 前驗證目前身份是表單申請人",
        not rejected_cancel["ok"]
        and "不是此表單申請人" in rejected_cancel["reason"]
        and not any(request.method == "POST" for request in not_owned.requests),
        str(rejected_cancel),
    )
    not_owned.assert_finished()

    unknown_owner = session([], account="test_account")
    unknown_guard = unknown_owner.lifecycle_operation._cancel_owner_guard({
        "applicant": "無法解析的顯示格式",
    })
    failures += _common.check(
        "無法解析申請人帳號時交由 UOF capability 判定而非拒絕",
        unknown_guard is None,
        str(unknown_guard),
    )

    direct_not_owned = session(
        [get(TASK_PATH, response(TASK_PATH, task_page("簽核中")))],
        account="another-user",
    )
    rejected_direct_void = direct_not_owned.lifecycle_operation.void(TASK, "不可繞過")
    failures += _common.check(
        "直接 void facade 也不能繞過 Cancel owner guard",
        not rejected_direct_void["ok"]
        and "不是此表單申請人" in rejected_direct_void["reason"],
        str(rejected_direct_void),
    )
    direct_not_owned.assert_finished()

    missing_capability = session([
        get(TASK_PATH, response(TASK_PATH, task_page("簽核中"))),
        get(VOID_PATH, response(
            VOID_PATH,
            '<input type="hidden" name="__VIEWSTATE" value="void">',
        )),
    ])
    rejected_capability = missing_capability.lifecycle_operation.terminate(
        TASK, "Cancel", "缺少作廢權限"
    )
    failures += _common.check(
        "作廢頁未提供作廢 capability 時不送 POST",
        not rejected_capability["ok"]
        and "未提供" in rejected_capability["reason"]
        and not any(request.method == "POST" for request in missing_capability.requests),
        str(rejected_capability),
    )
    missing_capability.assert_finished()

    unconfirmed = session(signing_steps("簽核中"))
    outcome = unconfirmed.lifecycle_operation.sign(TASK, approve=True)
    failures += _common.check(
        "HTTP 200 但任務狀態未變時回未確認而非成功",
        not outcome["ok"] and outcome.get("unconfirmed") and "請勿直接重送" in outcome["reason"],
        str(outcome),
    )
    failures += _common.check(
        "所有簽核寫入 POST 都明確禁止登入後 replay",
        len([request for request in unconfirmed.requests if request.method == "POST"]) == 2
        and all(request.retry_on_login is False for request in unconfirmed.requests if request.method == "POST"),
    )
    unconfirmed.assert_finished()

    confirmed = session(signing_steps("核准"))
    outcome = confirmed.lifecycle_operation.sign(TASK, approve=True)
    failures += _common.check(
        "簽核只在寫後任務狀態正規化為預期終態時成功",
        outcome["ok"] and outcome["result"] == "同意"
        and outcome["evidence"]["result"] == "同意",
        str(outcome),
    )
    confirmed.assert_finished()

    forwarded = session(signing_steps("處理中") + [get(HOME, response(HOME, pending_page(False)))])
    outcome = forwarded.lifecycle_operation.sign(
        TASK, approve=True, next_signer_guid="33333333-3333-3333-3333-333333333333"
    )
    failures += _common.check(
        "指定下一關時以任務仍有效且已離開目前身份待簽清單作 evidence",
        outcome["ok"] and outcome["evidence"]["result"] == "簽核中",
        str(outcome),
    )
    forwarded.assert_finished()

    voided = session([
        get(TASK_PATH, response(TASK_PATH, task_page("簽核中"))),
        get(VOID_PATH, response(VOID_PATH,
            '<input type="hidden" name="__VIEWSTATE" value="void">'
            '<input type="radio" name="rbGetBack" value="rbDeleteApplyForm">')),
        post(VOID_PATH, response(VOID_PATH, "accepted"), retry_on_login=False),
        get(TASK_PATH, response(TASK_PATH, task_page("作廢"))),
    ])
    outcome = voided.lifecycle_operation.terminate(TASK, "Cancel", "測試作廢")
    failures += _common.check(
        "作廢不可 replay 且只在寫後狀態為作廢時成功",
        outcome["ok"] and outcome["evidence"]["result"] == "作廢"
        and next(request for request in voided.requests if request.method == "POST").retry_on_login is False,
        str(outcome),
    )
    voided.assert_finished()

    print("=" * 50)
    print("Task lifecycle scripted 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
