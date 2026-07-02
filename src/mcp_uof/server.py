"""
UOF MCP Server — stdio 入口。

對外是穩定的 12 個工具；工具層只呼叫 get_backend().<method>()，由 ops.OpsRouter 依每工具的
靜態綁定（見 ops/router.py 的 BINDING）派發到對應機制——SOAP/PublicAPI 或 Playwright 網頁。
用哪種機制是開發期決定、對使用者透明的實作細節，使用者不需也無法選擇。
每個 Server 程序只代表一個固定帳號（由設定綁定）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field

# Load .env from cwd or repo root (don't clobber externally injected env vars).
load_dotenv(Path.cwd() / ".env", override=False)
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

from .auth import require_auth
from .ops import get_backend


mcp = FastMCP("UOF_WebService")


def _web_apply_identity_error(applicant_account: str) -> str:
    current = os.getenv("UOF_ACCOUNT", "")
    if applicant_account and current and applicant_account != current:
        return (
            "❌ 網頁起單只能以目前 MCP 登入身份起單。"
            f"目前身份：{current}；傳入 applicant_account：{applicant_account}。"
        )
    return ""


# ── 認證工具 ─────────────────────────────────────────────────────
@mcp.tool()
def uof_custom_check_auth() -> str:
    """確認目前以哪個 UOF 帳號身份操作，並檢查 Token / Session 是否有效。

    何時使用：對話開始時、或任何工具回報認證錯誤時，用來確認身份與連線。
    每個 Server 程序只代表一個固定帳號（由設定綁定）。"""
    return get_backend().check_auth()


# ── 電子簽核 (WKF) 工具 ──────────────────────────────────────────
@mcp.tool()
@require_auth
def uof_custom_get_form_list() -> str:
    """列出所有表單類別與表單，含 formId 與表單版本代號（recentVersionId）。

    何時使用：使用者問「系統有哪些表單」，或要起單但還不知道目標表單的 formId /
    formVersionId 時。這是起單流程的第一步。有 recentVersionId 的表單才可外部起單。"""
    return get_backend().get_form_list()


@mcp.tool()
@require_auth
def uof_custom_get_external_form_list() -> str:
    """列出被標記為「非線上使用」的表單。

    何時使用：少數需要查詢非線上表單的情況。注意：此清單**不等於**可外部起單的表單
    （採購單就不在其中卻能起單）。要判斷能否起單，請改看 get_form_list 的 recentVersionId。"""
    return get_backend().get_external_form_list()


@mcp.tool()
@require_auth
def uof_custom_get_form_structure(
    form_version_id: Annotated[str, Field(description="表單版本代號，由 get_form_list 的 recentVersionId 取得")],
) -> str:
    """以表單版本代號取得欄位結構（只有 fieldId 與名稱，無型別）。

    何時使用：手上只有 formVersionId 時。一般建議改用 get_form_structure_by_id（資訊較完整）。"""
    from .ops import web_apply
    try:
        desc = web_apply.describe_version(form_version_id)   # 登錄為網頁起單的表單 → 回它的可填欄位說明
    except Exception as e:
        return web_apply.resolve_error_message(form_version_id, e)
    return desc if desc else get_backend().get_form_structure(form_version_id)


@mcp.tool()
@require_auth
def uof_custom_get_form_structure_by_id(
    form_id: Annotated[str, Field(description="表單代號 formId，由 get_form_list 取得")],
) -> str:
    """以表單代號取得欄位結構，含每個欄位的型別與填寫方式，並附上起單提示。

    何時使用：要起單前，先用本工具查清楚這張表單要填哪些欄位、怎麼填。
    建議優先用本工具（比 get_form_structure 多回 fieldType 與填寫指引）。
    注意：回傳的是表單對外開放的「中介欄位」，可能少於 UOF 網頁上的完整表單。"""
    from .ops import web_apply
    desc = web_apply.describe(form_id)
    return desc if desc else get_backend().get_form_structure_by_id(form_id)


@mcp.tool()
@require_auth
def uof_custom_preview_workflow(
    form_version_id: Annotated[str, Field(description="表單版本代號，由 get_form_list 取得")],
    applicant_account: Annotated[str, Field(description="申請者帳號；web 起單表單必須等於目前 MCP 的 UOF_ACCOUNT")],
    first_signer_account: Annotated[str, Field(description="第一站簽核者帳號（自由流程必填）")],
    fields: Annotated[Optional[dict], Field(description="欄位值對應 {fieldId: 值}，可留空")] = None,
    comment: Annotated[str, Field(description="申請者意見（選填）")] = "",
    urgent_level: Annotated[str, Field(description="緊急程度：0 緊急 / 1 急 / 2 普通")] = "2",
) -> str:
    """模擬簽核流程走向，不會真的起單。參數與 apply_form 完全相同。

    何時使用：呼叫 apply_form 正式起單**之前**，用相同參數先驗證流程與簽核路徑是否正確。
    註：網頁起單的表單(如採購單)改以「試填到送出前」的填寫驗證代替（其簽核路徑於送出時的確認視窗呈現）。"""
    from .ops import web_apply
    try:
        entry = web_apply.resolve_version(form_version_id)   # 網頁起單的表單：以「試填到送出前」當預覽
    except Exception as e:
        return web_apply.resolve_error_message(form_version_id, e)
    if entry:
        err = _web_apply_identity_error(applicant_account)
        if err:
            return err
        return web_apply.apply_web(entry, fields or {}, dry_run=True, form_version_id=form_version_id)
    return get_backend().preview_workflow(
        form_version_id, applicant_account, first_signer_account, fields, comment, urgent_level
    )


@mcp.tool()
@require_auth
def uof_custom_apply_form(
    form_version_id: Annotated[str, Field(description="表單版本代號，由 get_form_list 取得")],
    applicant_account: Annotated[str, Field(description="申請者帳號；web 起單表單必須等於目前 MCP 的 UOF_ACCOUNT")],
    first_signer_account: Annotated[
        str,
        Field(description="第一站簽核者帳號（SOAP 自由流程必填，請先向使用者確認送給誰簽）；網頁起單的表單(如採購單)由表單自身流程決定簽核者，此參數會被略過"),
    ],
    fields: Annotated[
        dict,
        Field(description=(
            "起單內容，**形狀依該表單而定，請先用 get_form_structure_by_id 查**："\
            "一般表單帶 {fieldId: 值}（明細欄位帶列清單 {\"004\":[{\"004_1\":\"品名\",\"004_3\":5}]}）；"\
            "少數客製表單（如採購單）帶該表單說明的內容（主旨/供應商/明細…）。"
        )),
    ],
    comment: Annotated[str, Field(description="申請者意見（選填）")] = "",
    urgent_level: Annotated[str, Field(description="緊急程度：0 緊急 / 1 急 / 2 普通")] = "2",
) -> str:
    """正式起單，成功回傳 TaskId（後續查詢與結案都要用到，務必保存）。

    何時使用：已用 get_form_structure_by_id 確認這張表單要帶哪些欄位後，要正式送出時。
    呼叫端只要挑表單、填內容即可——**這張表單背後怎麼起（系統內部用哪種方式）不需要你管**。

    限制：起單不會驗證網頁必填欄位，缺欄位仍可能起單成功但內容不完整；附檔、多站/並簽會簽尚未支援。"""
    from .ops import web_apply
    try:
        entry = web_apply.resolve_version(form_version_id)   # 設計期登錄為網頁起單的表單 → 走 web；否則 SOAP 中介
    except Exception as e:
        return web_apply.resolve_error_message(form_version_id, e)
    if entry:
        err = _web_apply_identity_error(applicant_account)
        if err:
            return err
        return web_apply.apply_web(entry, fields, form_version_id=form_version_id)
    return get_backend().apply_form(
        form_version_id, applicant_account, first_signer_account, fields, comment, urgent_level
    )


@mcp.tool()
@require_auth
def uof_custom_get_task_data(
    task_id: Annotated[str, Field(description="表單工作代號 TaskId，由 apply_form 回傳或使用者自 UOF 網頁/通知信取得")],
) -> str:
    """查詢一張單的摘要：申請者、目前結果（簽核中/同意/否決/作廢）、結案日期。

    何時使用：想快速知道某張單目前的狀態時。需要逐站簽核歷程請改用 get_task_result。
    系統沒有待簽清單 API，TaskId 必須由使用者提供。"""
    return get_backend().get_task_data(task_id)


@mcp.tool()
@require_auth
def uof_custom_get_task_result(
    task_id: Annotated[str, Field(description="表單工作代號 TaskId")],
    include_form_data: Annotated[bool, Field(description="是否一併回傳表單欄位內容")] = True,
) -> str:
    """查詢一張單的逐站簽核歷程（每一站的簽核者、結果、意見、時間）。

    何時使用：想看簽核走到哪一站、誰簽了什麼意見時。站點顯示「待簽」代表表單停在該站。
    只要摘要狀態用 get_task_data 即可。"""
    return get_backend().get_task_result(task_id, include_form_data)


@mcp.tool()
@require_auth
def uof_custom_terminate_task(
    task_id: Annotated[str, Field(description="表單工作代號 TaskId")],
    result: Annotated[str, Field(description="結案動作：Adopt 同意 / Reject 否決 / Cancel 作廢")],
    reason: Annotated[str, Field(description="結案原因（不會寫入簽核歷程意見欄）")],
) -> str:
    """結案一張單。可作為申請人撤單(Cancel)、或主管/管理員核准否決(Adopt/Reject)。

    何時使用：
    - 申請人要撤回自己的單 → result=Cancel
    - 主管要同意/否決停在自己這站的單 → result=Adopt/Reject（單站自由流程中等同簽核）
    - 管理員要強制結案卡住的單 → 任一動作
    操作者固定為本 Server 綁定的身份（UOF_ACCOUNT），不由呼叫端指定。
    注意：這是「整張單終結」，多站流程會跳過後續站點；對已結案的單會被擋下。"""
    return get_backend().terminate_task(task_id, result, reason)


@mcp.tool()
@require_auth
def uof_custom_query_forms(
    keyword: Annotated[
        str,
        Field(description="關鍵字（可查表單編號、標題、申請者、內容）；留空則只用日期過濾"),
    ] = "",
    date_from: Annotated[
        str,
        Field(description="申請日期起 (yyyy/mm/dd)；留空則預設為「今天往前 7 天」"),
    ] = "",
    date_to: Annotated[
        str,
        Field(description="申請日期迄 (yyyy/mm/dd)；留空則預設為「今天」"),
    ] = "",
    max_results: Annotated[
        int,
        Field(description="最多回幾筆（只看第一頁；預設 50，UOF 一頁通常 10–20 筆）"),
    ] = 50,
) -> str:
    """搜尋 UOF 表單（依日期範圍 + 關鍵字），回傳含 TaskId 的清單。

    何時使用：使用者沒有 TaskId、但想列出自己最近的單或搜尋特定關鍵字時。
    本工具是補上「UOF 一代沒有待簽清單 API」這個缺口最直接的入口；
    取得 TaskId 後可丟給 get_task_data / get_task_result 看單張詳情。

    限制：
    - 範圍是「目前帳號可看到的單」，等同於使用者在 UOF 網頁「查詢表單」頁所看到的範圍。
    - 只取第一頁結果；極端情況需要更精確過濾請縮日期範圍或加關鍵字。"""
    return get_backend().query_forms(keyword, date_from, date_to, max_results)


@mcp.tool()
@require_auth
def uof_custom_search_users(
    keyword: Annotated[
        str,
        Field(description="查詢關鍵字：輸入姓名或帳號的一部分即可，例如「asgard」「王小明」"),
    ],
) -> str:
    """依姓名或帳號關鍵字查詢 UOF 人員，回傳姓名、帳號與 UserGuid。

    何時使用：需要指定 apply_form 的 first_signer_account（第一簽核者帳號）時，
    先用本工具確認對方在 UOF 的正確帳號，避免帳號輸錯導致起單失敗。

    限制：回傳範圍為目前帳號可看到的 UOF 人員（同 ChoiceCenter 選人清單）。"""
    return get_backend().search_users(keyword)


@mcp.tool()
@require_auth
def uof_custom_sign_next(
    task_id: Annotated[str, Field(description="表單工作代號 TaskId")],
    site_id: Annotated[str, Field(description="目前站點代號（僅固定流程的後台設計才有）")],
    node_seq: Annotated[int, Field(description="節點順序")],
    signer_guid: Annotated[str, Field(description="預計簽核者 Guid")],
) -> str:
    """將固定流程的表單推進到下一站並指定簽核者。

    何時使用：僅適用於後台設計好的固定流程表單。
    自由流程（如採購單）不支援本工具；要在自由流程上同意/否決請改用 terminate_task。
    site_id/node_seq/signer_guid 無法由查詢 API 取得，需由表單流程設計提供。"""
    return get_backend().sign_next(task_id, site_id, node_seq, signer_guid)


def main():
    mcp.run(transport='stdio')


if __name__ == "__main__":
    main()
