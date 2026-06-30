"""
E2E（Tier 2）— WKF 採購單，服務層端到端（真實 UOFTEST，會異動資料）。

入口為 domains/wkf/service.py 的 Python 函式（不經 MCP 傳輸層；協定層由 mounted/ 負責）。
這層追求「廣度」：用最便宜的方式覆蓋 WKF 行為分支。

涵蓋：
- 唯讀契約：GetFormList（含採購單）/ GetExternalFormList / GetFormStructure(_by_id) / SimulationFlowByScript
- 異動劇本：起單 → 查摘要(簽核中) → 查歷程(第一站待簽) → 作廢
- 邊界：主管（非申請人非 admin）結他人單（API 無權限管控）；已結案再結案被工具層攔截、原結果不被覆寫

紀律：只用採購單、只用三帳號、動態解析 formVersionId、**所有起出來的單在 finally 保證 Cancel**。
全程不使用 Adopt（採購單核准會觸發外部 PO Service；核准語意改由 mounted 層單次刻意驗證）。

執行：uv run python tests/e2e/test_wkf_purchase_order.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

_common.load_env()
if not _common.has_live_env():
    print(f"⏭️  跳過 e2e：缺少真實環境設定 {_common.missing_env()}（請設定 mcp-uof/.env）")
    sys.exit(0)

_common.ensure_src_on_path()
from mcp_uof.domains.wkf import service as wkf


def main() -> int:
    applicant, manager, admin = _common.accounts()
    failures = 0

    print("=" * 60)
    print("E2E — WKF 採購單（服務層，真實環境）")
    print("=" * 60)

    tok_applicant = _common.get_token_for(applicant)
    tok_admin = _common.get_token_for(admin)
    tok_manager = _common.get_token_for(manager)
    print(f"✅ Token：{applicant} / {manager} / {admin}")

    form_id, form_version_id = _common.resolve_form(tok_applicant, "採購單")
    assert form_version_id, "找不到採購單的已發佈版本"
    print(f"✅ 採購單 formId={form_id} version={form_version_id}")

    created = []  # 累積本測試起出的 TaskId；finally 一律以 admin Cancel 清理

    def apply_po(memo: str):
        r = wkf.apply_form_structured(
            tok_applicant,
            form_version_id=form_version_id,
            applicant_account=applicant,
            first_signer_account=manager,
            fields={**_common.PO_FIELDS, "MEMO": memo},
            comment=memo,
        )
        tid = _common.extract_task_id(r) if _common.ok(r) else None
        if tid:
            created.append(tid)
        return r, tid

    def check(label: str, cond: bool, detail: str = ""):
        nonlocal failures
        if cond:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
            failures += 1

    try:
        # ── 唯讀契約 ──────────────────────────────────────────────
        print("\n📌 唯讀契約")
        r = wkf.get_form_list(tok_applicant)
        check("GetFormList 含採購單", _common.ok(r) and "採購單" in r, r[:80])

        r = wkf.get_external_form_list(tok_applicant)
        check("GetExternalFormList 回應正常", _common.ok(r), r[:80])

        r = wkf.get_form_structure(tok_applicant, form_version_id)
        check("GetFormStructure 含 FORM_NO", _common.ok(r) and "FORM_NO" in r, r[:80])

        r = wkf.get_form_structure_by_form_id(tok_applicant, form_id)
        check("GetFormStructureByFormId 含 FORM_NO", _common.ok(r) and "FORM_NO" in r, r[:80])

        r = wkf.preview_workflow_structured(
            tok_applicant, form_version_id=form_version_id,
            applicant_account=applicant, first_signer_account=manager,
            fields={**_common.PO_FIELDS, "MEMO": "E2E 流程模擬（不起單）"}, comment="E2E 模擬",
        )
        check("SimulationFlowByScript 模擬通過", _common.ok(r), r[:80])

        # ── 劇本 A：起單 → 查 → 作廢 ─────────────────────────────
        print("\n📌 劇本 A：起單 → 查進度 → 作廢")
        r, tid = apply_po("E2E 劇本 A（將自動作廢）")
        check("起單成功並取得 TaskId", _common.ok(r) and bool(tid), r[:120])
        if tid:
            d = wkf.get_task_data(tok_applicant, tid)
            check("摘要為『簽核中』", "簽核中" in d, d[:80])
            h = wkf.get_task_result(tok_applicant, tid, is_contain_form_data=False)
            check("歷程第一站為主管待簽", manager in h, h[:120])
            c = wkf.terminate_task(tok_admin, tid, admin, "Cancel", "E2E 劇本 A 作廢")
            check("作廢成功", _common.ok(c), c[:80])
            d = wkf.get_task_data(tok_admin, tid)
            check("作廢後摘要為『作廢』", "作廢" in d, d[:80])

        # ── 劇本 B：主管結他人單（邊界 4）+ 已結案防護（邊界 5）──
        print("\n📌 劇本 B：主管結案（API 無權限管控）+ 已結案防護")
        r, tidb = apply_po("E2E 劇本 B（將自動作廢）")
        check("劇本 B 起單成功", _common.ok(r) and bool(tidb), r[:120])
        if tidb:
            c = wkf.terminate_task(tok_manager, tidb, manager, "Cancel", "主管結案（E2E 劇本 B）")
            check("主管（非申請人非 admin）能結案", _common.ok(c), c[:80])
            d = wkf.get_task_data(tok_admin, tidb)
            check("結案後為『作廢』", "作廢" in d, d[:80])
            blocked = wkf.terminate_task(tok_manager, tidb, manager, "Adopt", "重複結案應被攔截")
            check("已結案再結案被攔截", "已結案" in blocked and "❌" in blocked, blocked[:80])
            d = wkf.get_task_data(tok_admin, tidb)
            check("最終結果未被覆寫（仍作廢）", "作廢" in d, d[:80])

    finally:
        # 保證清理：任何中途失敗都不留簽核中表單。已作廢者再 Cancel 會被工具層擋下，無妨。
        print("\n🧹 清理測試單")
        for tid in created:
            try:
                res = wkf.terminate_task(tok_admin, tid, admin, "Cancel", "E2E 清理")
                first = res.splitlines()[0] if res else "(無回應)"
                print(f"  - {tid}: {first}")
            except Exception as e:
                print(f"  - {tid}: 清理時例外 {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    print("E2E 完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    print("=" * 60)
    return failures


if __name__ == "__main__":
    sys.exit(main())
