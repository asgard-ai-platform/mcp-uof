"""
真實掛載 MCP 測試（Tier 3）— 真 stdio 子程序 + JSON-RPC（真實 UOFTEST，會異動資料）。

這是「Claude 實際怎麼用」的正本：server 以真正的 OS 子程序啟動（python -m mcp_uof.server），
身份只由注入的 env 決定，全程只走 stdio JSON-RPC（initialize → list_tools → call_tool）。
詳細定義見 tests/README.md。

涵蓋：
1) 註冊護欄 + query_forms 透明：list_tools 必須剛好回 12 個 uof_custom_* 工具；且「我有多少單子要處理」
   （query_forms）直接回清單——server 內部透明改用網頁取得，絕不回「請切換模式」（重現並擋住截圖那個 bug）。
   並驗起單分派對使用者透明：採購單→get_form_structure 回網頁起單 schema；原生表單→SOAP 中介欄位。
2) 多身份工作流程全程（原生測試表單，apply_form 走 SOAP）：申請人起單 → 主管查同一張 →
   主管核准 → 申請人撤自己的單 → admin 強制結案 → 已結案再結案被工具層攔截。
3) 負向認證：壞密碼子程序 → check_auth 回固定 🔒 訊息、require_auth 工具回 🔒 字串而非 crash/isError。

紀律：動態解析 formVersionId、三帳號、工作流程用原生表單(Adopt 不觸發外部服務)、
所有「非 Adopt」起出的單在 finally 以 admin 保證 Cancel。

執行：uv run python tests/mounted/test_mcp_stdio.py
"""
import asyncio
import sys
import tempfile
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
    created = []  # 所有起出的 TaskId；finally 以 admin 一律 Cancel（已結案者被擋下，無妨）

    def check(label, cond, detail=""):
        nonlocal failures
        if cond:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
            failures += 1

    # 前置：以直接 SOAP 動態解析版本（不走 MCP；formVersionId 會隨重新發佈而變）。
    # 工作流程用原生表單(SOAP 起單、快)；採購單僅供「分派驗證」(MCP 層已收斂為網頁起單)。
    tok = _common.get_token_for(applicant)
    wf_form_id, wf_version = _common.resolve_form(tok, _common.WORKFLOW_FORM_NAME)
    assert wf_version, f"找不到 {_common.WORKFLOW_FORM_NAME} 的已發佈版本"
    po_form_id, po_version = _common.resolve_form(tok, "採購單")
    print(f"ℹ️ 工作流程表單 {_common.WORKFLOW_FORM_NAME} formId={wf_form_id} version={wf_version}")
    print(f"ℹ️ 採購單(分派驗證) formId={po_form_id} version={po_version}\n")

    def apply_args(memo):
        return {
            "form_version_id": wf_version,
            "applicant_account": applicant,
            "first_signer_account": manager,
            "fields": {**_common.WORKFLOW_FIELDS, "003": memo},  # 003=客戶名稱帶 memo
            "comment": "mounted 測試起單",
        }

    try:
        # ── 1) 註冊護欄 + query_forms 透明經 web（重現截圖情境）──────
        print("═" * 60)
        print("  1) 工具註冊護欄 + query_forms（機制透明）")
        print("═" * 60)
        async with mounted_session(applicant, _dotenv) as s:
            names = await tool_names(s)
            check(f"expose 剛好 12 個工具（得 {len(names)}）", names == EXPECTED_TOOLS,
                  f"差異={names ^ EXPECTED_TOOLS}")
            # 「我有多少單子要處理」：UOF PublicAPI 無清單 API，server 內部透明改用 web 取清單，
            # 直接回清單；絕不該回「請切換 UOF_OPS_MODE / 模式」。
            r = await call(s, "uof_custom_query_forms", {"max_results": 5})
            check("query_forms 直接回清單（透明，無模式字樣）",
                  _common.ok(r) and "查詢表單" in r
                  and "UOF_OPS_MODE" not in r and "切換" not in r and "不支援" not in r,
                  r[:160])

        # ── 2) 多身份採購單全程 ────────────────────────────────────
        print("\n" + "═" * 60)
        print(f"  2A) 身份={applicant}（申請人）起單與查詢")
        print("═" * 60)
        task_id = None
        async with mounted_session(applicant, _dotenv) as s:
            r = await call(s, "uof_custom_check_auth")
            check("check_auth 成功", _common.ok(r), r[:80])
            r = await call(s, "uof_custom_get_form_list")
            check("get_form_list 含採購單", _common.ok(r) and "採購單" in r, r[:80])
            # 起單分派(對使用者透明)：採購單→網頁起單 schema(供應商/明細/主旨)；原生表單→SOAP 中介欄位。
            r = await call(s, "uof_custom_get_form_structure_by_id", {"form_id": po_form_id})
            check("採購單 → 網頁起單 schema(供應商/明細)", "供應商" in r and "明細" in r, r[:100])
            r = await call(s, "uof_custom_get_form_structure_by_id", {"form_id": wf_form_id})
            check("原生表單 → SOAP 中介欄位", _common.ok(r) and ("客戶名稱" in r or "明細子欄位" in r), r[:100])
            r = await call(s, "uof_custom_preview_workflow", apply_args("mounted 模擬"))
            check("preview_workflow 通過", _common.ok(r), r[:80])
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
                # 主管核准：驗「核准記為同意」。工作流程用原生表單(借機申請單)，Adopt 不觸發外部
                # PO Service，可安全核准；會觸發外部服務的採購單核准不放進此套件。
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
        print(f"  2D) 身份={admin}（管理員強制結案 + 已結案防護）")
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
                               {"task_id": tid_d, "result": "Cancel", "reason": "mounted：admin 強制結案"})
                check("admin 強制結案成功", _common.ok(r), r[:80])
                r = await call(s, "uof_custom_get_task_data", {"task_id": tid_d})
                check("結案後為『作廢』", "作廢" in r, r[:80])
                r = await call(s, "uof_custom_terminate_task",
                               {"task_id": tid_d, "result": "Cancel", "reason": "重複結案應被攔截"})
                check("已結案再結案被工具層攔截", "已結案" in r and "❌" in r, r[:80])

        # ── 3) 負向認證（壞密碼子程序）─────────────────────────────
        # 用全新的 HOME（空的 ~/.uof）避免命中先前真實執行留下的快取 token，
        # 否則 require_auth 即使密碼錯也會因快取命中而通過。
        print("\n" + "═" * 60)
        print("  3) 負向認證：壞密碼 → 🔒 而非 crash / isError")
        print("═" * 60)
        with tempfile.TemporaryDirectory() as tmp_home:
            async with mounted_session(applicant, _dotenv,
                                       password="__definitely_wrong__", home=tmp_home) as s:
                r = await call(s, "uof_custom_check_auth")
                check("check_auth 回固定 🔒 訊息", "🔒" in r, r[:80])
                r = await call(s, "uof_custom_get_form_list")
                check("require_auth 工具回 🔒 字串（非 isError）", "🔒" in r, r[:80])

    finally:
        # 保證清理：任何中途失敗都不留簽核中表單。已結案者再 Cancel 會被擋下，無妨。
        print("\n🧹 清理測試單（admin，stdio）")
        try:
            async with mounted_session(admin, _dotenv) as s:
                for tid in created:
                    r = await call(s, "uof_custom_terminate_task",
                                   {"task_id": tid, "result": "Cancel", "reason": "mounted 清理"})
                    print(f"  - {tid}: {(r.splitlines() or ['(無回應)'])[0]}")
        except Exception as e:
            print(f"  ⚠️ 清理時例外 {type(e).__name__}: {e}（請手動確認 UOFTEST 無殘留簽核中單）")

    print("\n" + "═" * 60)
    print("真實掛載 MCP 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    print("═" * 60)
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
