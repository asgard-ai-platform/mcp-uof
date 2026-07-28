"""Characterization tests for the complete apply_form_web transaction.

apply_form_web fills and submits a UOF form across a multi-step httpx round-trip.
It has no mounted coverage (mounted is read-only), so before decomposing the
575-line method we pin its exact behavior here: fed a sanitized FirstSite capture
form (測試02_教育訓練, read-only GET fixture) plus a fixed `fields` dict, it must
produce the exact same POST payload sequence and result dict as the recorded
golden. Any change to the fill/submit logic that alters observable behavior
fails this test.

Regenerate golden only on an intentional behavior change, and sanitize the capture before commit.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_common.ensure_src_on_path()

from _scripted_uof import (  # noqa: E402
    ScriptedHttpSession,
    ScriptedResponse,
    get,
    post,
)

FX = Path(__file__).resolve().parent / "fixtures" / "apply_test02"


def _opening_steps(meta: dict, firstsite_html: str) -> list:
    return [
        get(meta["apply_path"], ScriptedResponse(meta["addform_final_url"])),
        get(meta["first_site_path"], ScriptedResponse(meta["addform_final_url"], firstsite_html)),
    ]


def _golden_session(meta: dict, firstsite_html: str) -> ScriptedHttpSession:
    neutral = ScriptedResponse(meta["addform_final_url"], "<html><body>ok</body></html>")
    return ScriptedHttpSession(
        _opening_steps(meta, firstsite_html) + [
            post(meta["first_site_path"], neutral, retry_on_login=False),
            post(meta["first_site_path"], neutral, retry_on_login=False),
        ],
        form_ids=(meta["fid"], meta["vid"]),
        virtual_path="/UOF",
    )


def _complete_session(
    meta: dict,
    firstsite_html: str,
    created_form=("11111111-2222-3333-4444-555555555555", "教育訓練申請單"),
) -> tuple[ScriptedHttpSession, str]:
    first_site_send = "/WKF/FormUse/DefinedTask/FirstSiteSend.aspx?scriptId=script-1"
    save_html = (
        '<input type="hidden" name="__VIEWSTATE" value="viewstate-after-save">'
        '<input type="hidden" name="__EVENTVALIDATION" value="validation-after-save">'
    )
    send_html = f'<script>location.href="~{first_site_send}";</script>'
    confirm_html = (
        '<input type="hidden" name="__VIEWSTATE" value="confirm-viewstate">'
        '<input type="hidden" name="ConfirmToken" value="confirm-token">'
    )
    session = ScriptedHttpSession(
        _opening_steps(meta, firstsite_html) + [
            post(
                meta["first_site_path"],
                ScriptedResponse(meta["addform_final_url"], save_html),
                retry_on_login=False,
            ),
            post(
                meta["first_site_path"],
                ScriptedResponse(meta["addform_final_url"], send_html),
                retry_on_login=False,
            ),
            get(
                first_site_send,
                ScriptedResponse("https://uof.example/UOF" + first_site_send, confirm_html),
            ),
            post(
                first_site_send,
                ScriptedResponse(
                    "https://uof.example/UOF" + first_site_send,
                    "<script>$uof.dialog.close()</script>表單 ET123456789 已建立",
                ),
                retry_on_login=False,
            ),
        ],
        form_ids=(meta["fid"], meta["vid"]),
        virtual_path="/UOF",
        created_form=created_form,
    )
    return session, first_site_send


def main() -> int:
    failures = 0
    meta = json.loads((FX / "meta.json").read_text(encoding="utf-8"))
    firstsite_html = (FX / "firstsite.html").read_text(encoding="utf-8")
    golden = json.loads((FX / "golden.json").read_text(encoding="utf-8"))

    unexpected = ScriptedHttpSession(
        [get("/expected", ScriptedResponse("https://uof.example/expected"))],
        form_ids=("form", "version"),
    )
    try:
        unexpected.get("/wrong")
        unexpected_failed_loud = False
    except AssertionError:
        unexpected_failed_loud = True
    missing = ScriptedHttpSession(
        [get("/missing", ScriptedResponse("https://uof.example/missing"))],
        form_ids=("form", "version"),
    )
    try:
        missing.assert_finished()
        missing_failed_loud = False
    except AssertionError:
        missing_failed_loud = True
    failures += _common.check(
        "scripted seam 對 unexpected 與 missing request 都 fail loud",
        unexpected_failed_loud and missing_failed_loud,
    )

    session = _golden_session(meta, firstsite_html)
    result = session.apply_form_web(meta["form_version_id"], golden["fields"], submit=True)
    session.assert_finished()
    posts = [
        {"path": request.path, "payload": request.data}
        for request in session.requests
        if request.method == "POST"
    ]

    failures += _common.check(
        "apply_form_web 回傳結果與 golden 逐鍵相同（含 filled/errors/ok/reason）",
        result == golden["result"],
        f"\n got={json.dumps(result, ensure_ascii=False)}\n want={json.dumps(golden['result'], ensure_ascii=False)}",
    )
    failures += _common.check(
        "送出的 POST 次數與 __EVENTTARGET 序列與 golden 相同",
        [p["payload"].get("__EVENTTARGET") for p in posts]
        == [p["payload"].get("__EVENTTARGET") for p in golden["posts"]],
        f"got targets={[p['payload'].get('__EVENTTARGET') for p in posts]}",
    )
    failures += _common.check(
        "每個 POST 的完整 payload（含 viewstate 與所有填入控制項）與 golden 逐鍵相同",
        [p["payload"] for p in posts] == [p["payload"] for p in golden["posts"]],
        _payload_diff(posts, golden["posts"]),
    )

    complete, first_site_send = _complete_session(meta, firstsite_html)
    completed = complete.apply_form_web(meta["form_version_id"], golden["fields"], submit=True)
    complete.assert_finished()
    save, send, confirm = [r for r in complete.requests if r.method == "POST"]
    failures += _common.check(
        "完整 WebForms transaction 依序執行 open/save/send/route/confirm",
        [(r.method, r.path) for r in complete.requests] == [
            ("GET", meta["apply_path"]),
            ("GET", meta["first_site_path"]),
            ("POST", meta["first_site_path"]),
            ("POST", meta["first_site_path"]),
            ("GET", first_site_send),
            ("POST", first_site_send),
        ],
        str([(r.method, r.path) for r in complete.requests]),
    )
    failures += _common.check(
        "save 後刷新 WebForms state，confirm 僅送派單頁 hidden state 與確認 target",
        save.data.get("__EVENTTARGET") == "ctl00$MasterPageRadButton1"
        and send.data.get("__EVENTTARGET") == "ctl00$MasterPageRadButton3"
        and send.data.get("__VIEWSTATE") == "viewstate-after-save"
        and send.data.get("__EVENTVALIDATION") == "validation-after-save"
        and confirm.data == {
            "__VIEWSTATE": "confirm-viewstate",
            "ConfirmToken": "confirm-token",
            "__EVENTTARGET": "ctl00$MasterPageRadButton2",
            "__EVENTARGUMENT": "",
        },
        f"send_state={send.data.get('__VIEWSTATE')}, confirm={confirm.data}",
    )
    expected_completed = {
        "ok": True,
        "reason": "",
        "task_id": "11111111-2222-3333-4444-555555555555",
        "form_number": "ET123456789",
        "form_name": "教育訓練申請單",
        "filled": golden["result"]["filled"],
        "errors": golden["result"]["errors"],
    }
    failures += _common.check(
        "確認成單後回傳 TaskId、表單編號、表單名與完整 filled",
        completed == expected_completed,
        f"\n got={json.dumps(completed, ensure_ascii=False)}",
    )

    unconfirmed_session, _ = _complete_session(meta, firstsite_html, created_form=("", ""))
    unconfirmed = unconfirmed_session.apply_form_web(
        meta["form_version_id"], golden["fields"], submit=True
    )
    unconfirmed_session.assert_finished()
    failures += _common.check(
        "已成單但查不到 TaskId 時回未確認，避免空 TaskId 被呈現成完整成功",
        unconfirmed.get("ok") is True
        and unconfirmed.get("submitted_unconfirmed") is True
        and unconfirmed.get("task_id") == ""
        and "未取得 TaskId" in unconfirmed.get("reason", ""),
        f"\n got={json.dumps(unconfirmed, ensure_ascii=False)}",
    )
    print("=" * 50)
    print("apply_form_web golden 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


def _payload_diff(got: list, want: list) -> str:
    if len(got) != len(want):
        return f"post count got={len(got)} want={len(want)}"
    lines = []
    for i, (g, w) in enumerate(zip(got, want)):
        gp, wp = g["payload"], w["payload"]
        for k in sorted(set(gp) | set(wp)):
            if gp.get(k) != wp.get(k):
                lines.append(f"[post {i}] {k!r}: got {gp.get(k)!r} want {wp.get(k)!r}")
    return "\n".join(lines[:40])


if __name__ == "__main__":
    sys.exit(main())
