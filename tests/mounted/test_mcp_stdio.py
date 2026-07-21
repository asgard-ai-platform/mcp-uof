"""Mounted stdio JSON-RPC tests against the configured isolated UOF environment."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

_dotenv = _common.load_env()
if not _common.has_live_env():
    print(f"⏭️  跳過 mounted：缺少真實環境設定 {_common.missing_env()}（請設定 mcp-uof/.env）")
    sys.exit(0)

import _client  # noqa: E402  (在 path 設定後 import)
from _client import EXPECTED_TOOLS, mounted_session, call, tool_names


async def run() -> int:
    applicant, manager, admin = _common.accounts()
    failures = 0
    created = []

    def check(label, cond, detail=""):
        nonlocal failures
        if cond:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
            failures += 1

    # 前置：以 httpx 網頁機制動態解析版本（不走 MCP；formVersionId 會隨重新發佈而變）。
    # 工作流程用測試表單；表單名稱由環境指定。
    wf_name = _common.workflow_form_name()
    if not wf_name:
        print("⏭️  跳過 mounted：未設定 UOF_TEST_WORKFLOW_FORM_NAME（工作流程情境需要一張原生表單名）")
        return 0
    wf_form_id, wf_version = _common.resolve_form_httpx(wf_name)
    assert wf_version, f"找不到 {wf_name} 的已發佈版本"
    print("ℹ️ 已解析隔離測試表單版本")

    def apply_args(memo):
        return {
            "form_version_id": wf_version,
            "applicant_account": applicant,
            "first_signer_account": "",
            "fields": _common.workflow_fields(memo),
            "comment": "mounted 測試起單",
        }

    try:
        # ── 1) 註冊護欄與 query_forms ───────────────────────────────
        print("═" * 60)
        print("  1) 工具註冊護欄 + query_forms（機制透明）")
        print("═" * 60)
        async with mounted_session(applicant, _dotenv) as s:
            names = await tool_names(s)
            check(f"expose 剛好 17 個工具（得 {len(names)}）", names == EXPECTED_TOOLS,
                  f"差異={names ^ EXPECTED_TOOLS}")
            r = await call(s, "uof_custom_query_forms", {"max_results": 5})
            check("query_forms 直接回清單（透明，無模式字樣）",
                  _common.ok(r) and "查詢表單" in r
                  and "UOF_OPS_MODE" not in r and "切換" not in r and "不支援" not in r,
                  r[:160])

        # ── 2) 多身份工作流程全程 ────────────────────────────────────
        print("\n" + "═" * 60)
        print(f"  2A) 身份={applicant}（申請人）起單與查詢")
        print("═" * 60)
        task_id = None
        async with mounted_session(applicant, _dotenv) as s:
            r = await call(s, "uof_custom_check_auth")
            check("check_auth 成功", _common.ok(r), r[:80])
            r = await call(s, "uof_custom_get_form_list")
            check("get_form_list 回可起單表單", _common.ok(r) and "formVersionId" in r, r[:80])
            r = await call(s, "uof_custom_get_form_structure_by_id", {"form_id": wf_form_id})
            check("get_form_structure 回傳欄位", _common.ok(r) and "http_web 模式" in r, r[:100])
            r = await call(s, "uof_custom_preview_workflow", apply_args("mounted 模擬"))
            check("preview_workflow 回傳不支援說明", "目前不提供" in r, r[:80])
            r = await call(s, "uof_custom_apply_form", apply_args("mounted 全程測試單"))
            task_id = _common.extract_task_id(r) if _common.ok(r) else None
            if task_id:
                created.append(task_id)
            check("apply_form 起單並取得 TaskId", bool(task_id), r[:120])
            if task_id:
                r = await call(s, "uof_custom_get_task_data", {"task_id": task_id})
                check("get_task_data 為『簽核中』", "簽核中" in r, r[:80])

        if not task_id:
            print("  ⚠️ 未取得 TaskId，略過後續核准劇本")
        else:
            print("\n" + "═" * 60)
            print(f"  2B) 身份={manager}（主管）查同一張單並核准（唯一 Adopt）")
            print("═" * 60)
            async with mounted_session(manager, _dotenv) as s:
                r = await call(s, "uof_custom_check_auth")
                check("主管身份正確", _common.ok(r) and manager in r, r[:80])
                r = await call(s, "uof_custom_get_task_result",
                               {"task_id": task_id, "include_form_data": False})
                check("核准前歷程含主管", _common.ok(r) and manager in r, r[:120])
                # 主管核准：驗「核准記為同意」。工作流程用測試表單，避免觸發正式下游整合。
                r = await call(s, "uof_custom_terminate_task",
                               {"task_id": task_id, "result": "Adopt", "reason": "mounted：主管核准"})
                check("Adopt 成功", _common.ok(r), r[:80])
                r = await call(s, "uof_custom_get_task_result",
                               {"task_id": task_id, "include_form_data": False})
                check("核准後最終結果為『同意』", _common.ok(r) and "同意" in r, r[:120])

        print("\n" + "═" * 60)
        print(f"  2C) 身份={applicant}（申請人撤自己的單）")
        print("═" * 60)
        async with mounted_session(applicant, _dotenv) as s:
            r = await call(s, "uof_custom_apply_form", apply_args("mounted 撤單測試單"))
            tid_c = _common.extract_task_id(r) if _common.ok(r) else None
            if tid_c:
                created.append(tid_c)
            check("撤單劇本起單成功", bool(tid_c), r[:120])
            if tid_c:
                r = await call(s, "uof_custom_terminate_task",
                               {"task_id": tid_c, "result": "Cancel", "reason": "mounted：申請人撤單"})
                check("Cancel 成功", _common.ok(r), r[:80])
                r = await call(s, "uof_custom_get_task_data", {"task_id": tid_c})
                check("撤單後為『作廢』", "作廢" in r, r[:80])

        print("\n" + "═" * 60)
        print("  2D) 清理權限帳號 + 已結案防護")
        print("═" * 60)
        tid_d = None
        async with mounted_session(applicant, _dotenv) as s:
            r = await call(s, "uof_custom_apply_form", apply_args("mounted admin 結案測試單"))
            tid_d = _common.extract_task_id(r) if _common.ok(r) else None
            if tid_d:
                created.append(tid_d)
            check("admin 劇本起單成功（申請人）", bool(tid_d), r[:120])
        if tid_d:
            async with mounted_session(admin, _dotenv) as s:
                r = await call(s, "uof_custom_check_auth")
                check("admin 身份正確", _common.ok(r) and admin in r, r[:80])
                r = await call(s, "uof_custom_terminate_task",
                               {"task_id": tid_d, "result": "Cancel", "reason": "mounted 清理測試"})
                check("具權限帳號結案成功", _common.ok(r), r[:80])
                r = await call(s, "uof_custom_get_task_data", {"task_id": tid_d})
                check("結案後為『作廢』", "作廢" in r, r[:80])
                r = await call(s, "uof_custom_terminate_task",
                               {"task_id": tid_d, "result": "Cancel", "reason": "重複結案應被攔截"})
                check("已結案再結案被工具層攔截", "已結案" in r and "❌" in r, r[:80])

        # ── 3) 負向認證 ─────────────────────────────────────────────
        print("\n" + "═" * 60)
        print("  3) 負向認證：壞密碼 → 🔒 而非 crash / isError")
        print("═" * 60)
        async with mounted_session(applicant, _dotenv, password="__definitely_wrong__") as s:
            r = await call(s, "uof_custom_check_auth")
            check("check_auth 回未登入狀態", "未登入" in r, r[:80])
            r = await call(s, "uof_custom_get_form_list")
            check("require_auth 工具回 🔒 字串（非 isError）", "🔒" in r, r[:80])

    finally:
        # 保證清理：任何中途失敗都不留簽核中表單。已結案者再 Cancel 會被擋下，無妨。
        print("\n🧹 清理測試單（admin，stdio）")
        try:
            async with mounted_session(admin, _dotenv) as s:
                for index, tid in enumerate(created, start=1):
                    r = await call(s, "uof_custom_terminate_task",
                                   {"task_id": tid, "result": "Cancel", "reason": "mounted 清理"})
                    print(f"  - 測試單 {index}: {(r.splitlines() or ['(無回應)'])[0]}")
        except Exception as e:
            print(f"  ⚠️ 清理時例外 {type(e).__name__}: {e}（請手動確認測試環境無殘留簽核中單）")

    print("\n" + "═" * 60)
    print("真實掛載 MCP 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    print("═" * 60)
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
