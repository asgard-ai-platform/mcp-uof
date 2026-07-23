"""UOF MCP stdio server and public tool definitions."""
from __future__ import annotations

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


# ── 認證工具 ─────────────────────────────────────────────────────
@mcp.tool()
def uof_custom_check_auth() -> str:
    """確認目前以哪個 UOF 帳號身份操作，並檢查記憶體 web session 狀態。

    何時使用：對話開始時或工具回報認證錯誤時，用來確認身份與連線。首次建立 session 時會嘗試登入。
    每個 Server 程序只代表一個固定帳號（由設定綁定）。"""
    return get_backend().check_auth()


# ── 電子簽核 (WKF) 工具 ──────────────────────────────────────────
@mcp.tool()
@require_auth
def uof_custom_get_form_list() -> str:
    """列出目前帳號「可申請起單」的表單（來源：電子簽核 » 表單申請 樹），依類別分組，
    每張含 formId 與 formVersionId。

    何時使用：使用者問「系統有哪些表單」「我可以開哪些單」，或要起單但還不知道目標表單的
    formId / formVersionId 時。這是起單流程的第一步。清單中的每張表單都可起單，起單時把
    該表單的 formVersionId 交給 apply_form 即可。"""
    return get_backend().get_form_list()


@mcp.tool()
@require_auth
def uof_custom_get_external_form_list() -> str:
    """列出被標記為「非線上使用」的表單。

    何時使用：少數需要查詢非線上表單的情況。注意：此清單**不等於**可外部起單的表單。
    要判斷能否起單，請改看 get_form_list（其列出的即為可起單表單）。"""
    return get_backend().get_external_form_list()


@mcp.tool()
@require_auth
def uof_custom_get_form_structure(
    form_version_id: Annotated[str, Field(description="表單版本代號 formVersionId，由 get_form_list 取得")],
) -> str:
    """以表單版本代號取得欄位結構、型別、必填狀態與可選值。

    何時使用：手上只有 formVersionId 時；有 formId 時可使用 get_form_structure_by_id。"""
    return get_backend().get_form_structure(form_version_id)


@mcp.tool()
@require_auth
def uof_custom_get_form_structure_by_id(
    form_id: Annotated[str, Field(description="表單代號 formId，由 get_form_list 取得")],
) -> str:
    """以表單代號取得欄位結構，含每個欄位的型別、是否必填（＊）、可選值與填寫方式，並附上起單提示。

    何時使用：要起單前，先用本工具查清楚這張表單要填哪些欄位、哪些必填、單選/下拉的合法值有哪些、怎麼填。
    起單前務必照回傳的 ＊ 與『可選值』核對使用者給的內容（值不在清單會被伺服器默默丟棄）。
    建議優先用本工具（比 get_form_structure 多回 fieldType 與填寫指引）。
    注意：這是即時解析 UOF 起單網頁（httpx+lxml）的結果，理論上應涵蓋網頁上看得到的所有欄位；
    若懷疑某欄位漏掉，屬解析器待修的 bug（回報開發面，勿假設是系統本身的限制）。"""
    return get_backend().get_form_structure_by_id(form_id)


@mcp.tool()
@require_auth
def uof_custom_preview_workflow(
    form_version_id: Annotated[str, Field(description="表單版本代號，由 get_form_list 取得")],
    applicant_account: Annotated[str, Field(description="相容性參數；目前不改變實際申請身份")],
    first_signer_account: Annotated[str, Field(description="相容性參數；目前不套用到首站派送")],
    fields: Annotated[Optional[dict], Field(description="欄位值對應 {fieldId: 值}，可留空")] = None,
    comment: Annotated[str, Field(description="申請者意見（選填）")] = "",
    urgent_level: Annotated[str, Field(description="緊急程度：0 緊急 / 1 急 / 2 普通")] = "2",
) -> str:
    """回報流程預覽目前不受支援，不會真的起單。

    本工具不驗證參數或簽核路徑；需要預覽時請使用 UOF Web UI。"""
    return get_backend().preview_workflow(
        form_version_id, applicant_account, first_signer_account, fields, comment, urgent_level
    )


@mcp.tool()
@require_auth
def uof_custom_apply_form(
    form_version_id: Annotated[str, Field(description="表單版本代號，由 get_form_list 取得")],
    applicant_account: Annotated[str, Field(description="相容性參數；實際申請身份固定為 UOF_ACCOUNT")],
    first_signer_account: Annotated[
        str,
        Field(description="相容性參數；目前尚未套用到派單頁"),
    ],
    fields: Annotated[
        dict,
        Field(description=(
            "起單內容。請先用 get_form_structure_by_id 查表單欄位，使用回傳的確切欄位 ID 帶 "
            "{fieldId: 值}；dataGrid 明細帶列清單，例如 {\"004\":[{\"004_1\":\"品名\",\"004_3\":5}]}。\n"
            "外掛欄位區塊（含查詢視窗／明細表格者）改帶 dict，控制項名稱用 get_dialog_structure 查；"
            "四個保留鍵：\n"
            "  _lookups     [{press:按鈕名, row:挑選器整筆JSON}] — 唯讀欄位只能這樣填\n"
            "  _fill_before {控制項:值} — 會連動其他欄位的下拉，必須與 _lookups 同批送出\n"
            "  _press_after [按鈕名] — 填完值後按（如 btnCalc 計算金額）\n"
            "  _rows        明細列清單；區塊有兩個明細表格時改用 {開窗按鈕名: [列...]}\n"
            "例：{\"主要欄位\": {\"_lookups\": [{\"press\": \"btnVendor\", \"row\": {...}}], "
            "\"txtSubject\": \"標題\", \"_rows\": [{\"txtQty\": 2}]}}"
        )),
    ],
    comment: Annotated[str, Field(description="申請者意見（選填）")] = "",
    urgent_level: Annotated[str, Field(description="緊急程度：0 緊急 / 1 急 / 2 普通")] = "2",
) -> str:
    """正式起單，成功回傳 TaskId（後續查詢與結案都要用到，務必保存）。

    何時使用：已用 get_form_structure_by_id 確認這張表單要帶哪些欄位後，要正式送出時。
    呼叫端只要挑表單、填內容即可——底層固定走 httpx 網頁機制。

    送出前會擋掉「必填（＊）未填齊」「單選/下拉值不在可選清單」「明細列未被對話框接受」的呼叫並
    回報缺漏（避免建立不完整的單），被擋就照回報補齊再送。

    回報成功不等於內容正確：此驗證以能解析出欄位的表單為準，且伺服器對部分缺漏不報錯（例如唯讀
    欄位沒經挑選器帶入時留空、連動下拉送出順序錯誤時清掉相依欄位）。**起單後務必用 get_task_data
    逐欄回讀比對**，包含明細列數。附檔、多站/並簽會簽尚未支援。"""
    return get_backend().apply_form(
        form_version_id, applicant_account, first_signer_account, fields, comment, urgent_level
    )


@mcp.tool()
@require_auth
def uof_custom_get_task_data(
    task_id: Annotated[str, Field(description="表單工作代號 TaskId，由 apply_form 回傳或使用者自 UOF 網頁/通知信取得")],
) -> str:
    """查詢一張單的摘要（申請者／目前結果／結案日期）＋表單已填欄位內容。

    何時使用：想知道某張單「是什麼內容、現在什麼狀態」時。需要逐站簽核歷程請用 get_task_result。
    欄位以表單自身的欄位代碼呈現（與 apply_form 寫入時同一套代碼），不做任何表單別的解讀；
    欄位語意由部署端私有 skill 定義。
    TaskId 可由 get_pending_sign_list / query_forms 取得。"""
    return get_backend().get_task_data(task_id)


@mcp.tool()
@require_auth
def uof_custom_get_task_result(
    task_id: Annotated[str, Field(description="表單工作代號 TaskId")],
    include_form_data: Annotated[
        bool,
        Field(description="是否一併回傳表單已填欄位（欄位代碼／值／選項／明細表格）"),
    ] = True,
) -> str:
    """查詢一張單的逐站簽核歷程（每一站的簽核者、結果、意見、時間），可含表單已填欄位。

    何時使用：想看簽核走到哪一站、誰簽了什麼意見時。站點顯示「未簽核」代表表單停在該站。
    include_form_data=True 時一併回傳欄位內容：以表單自身欄位代碼為鍵，含單選/複選的所有選項
    與勾選狀態、明細表格逐列資料；不做表單別解讀，欄位語意由部署端私有 skill 定義。
    只要摘要狀態用 get_task_data 即可。"""
    return get_backend().get_task_result(task_id, include_form_data)


@mcp.tool()
@require_auth
def uof_custom_get_dialog_structure(
    form_version_id: Annotated[str, Field(description="表單版本代號 formVersionId，由 get_form_list 取得")],
    field_code: Annotated[
        str,
        Field(description="只看某一個對話框欄位的代碼（如 PRItems）；留空則列出這張表單所有對話框欄位"),
    ] = "",
) -> str:
    """查對話框(dialog)型欄位的「內部欄位結構」：每個內部控制項的標籤、必填、型別、可選值、查找鈕。

    何時使用：get_form_structure_by_id 把某欄位標成〈dialog〉時，用本工具看它裡面到底有哪些欄位。
    UOF 的複合欄位（請購明細、主要欄位、費用明細…）實質內容都藏在對話框裡，主表結構看不到。

    只做結構擷取，不解讀語意：同一個標籤下可能有多個控制項（含隱藏輔助欄），
    要填哪一個、對應什麼業務概念，由部署端私有 skill 判斷。

    回傳的按鈕名可直接用在 apply_form 的 _lookups / _press_after / _rows。
    注意：必填不一定看得見——有的欄位沒有任何可操作的控制項，卻會在送出時被伺服器擋下。"""
    return get_backend().get_dialog_structure(form_version_id, field_code)


@mcp.tool()
@require_auth
def uof_custom_search_dialog_options(
    form_version_id: Annotated[str, Field(description="表單版本代號 formVersionId")],
    field_code: Annotated[str, Field(description="對話框欄位代碼，由 get_dialog_structure 取得（如 MAINFORM、主要欄位）")],
    keyword: Annotated[str, Field(description="查詢關鍵字（供應商名/料號/人名/採購單號等）")] = "",
    limit: Annotated[int, Field(description="最多回幾筆候選，預設 20")] = 20,
) -> str:
    """查直接 picker dialog 的候選項目；不自動深入 row-editor 內的巢狀 picker。

    何時使用：要填一個需要挑選的欄位，但手上只有名稱或部分關鍵字時。
    這是「不要捏造代碼」的正解——先查出真實候選，再挑其中一筆。

    回傳原始候選資料，不替你決定選哪筆：代碼要精確相符或名稱可信才算數，
    查無結果時應回問使用者，不可自行編一個值。整筆回傳的 JSON 就是 apply_form
    `_lookups` 的 `row` 所需的內容，請原樣帶入、不要只取代碼。

    候選清單可能含哨兵列或缺少必要關聯欄位，不可盲取第一筆；選取後仍需回讀確認。"""
    return get_backend().search_dialog_options(form_version_id, field_code, keyword, limit)


@mcp.tool()
@require_auth
def uof_custom_operate_dialog(
    form_version_id: Annotated[str, Field(description="表單版本代號 formVersionId")],
    field_code: Annotated[str, Field(description="對話框欄位代碼（如 PRItems、MAINFORM）")],
    values: Annotated[
        dict,
        Field(description="要填入的控制項，{控制項名稱: 值}；名稱由 get_dialog_structure 取得"),
    ] = {},
    press: Annotated[
        str,
        Field(description="要按下的按鈕名稱（如 btnQueryItem 查詢、btnCalc 計算、MasterPageRadButton1 確定）；留空只填值不按鈕"),
    ] = "",
) -> str:
    """【探測用】對 dialog 執行「填值 / 按鈕」一個步驟，回報伺服器連帶改動了哪些控制項。

    ⚠️ 不能用來累積明細列：每次呼叫都會重開一個起單 session（GridDataID 每次不同），
    寫進去的列會隨該 session 一起被丟棄。明細列請用 apply_form 一次帶齊。

    何時使用：想知道「按下某個鈕會發生什麼」「哪些欄位是系統連帶帶出的」時。

    本工具不知道任何一種 dialog 的意義，也不替你決定按哪個鈕——
    press 由呼叫端指定。各 dialog 的操作順序可能不同，應由部署端私有 skill 定義。

    回傳的「改動清單」是判斷欄位相依關係的依據：例如填入料號並按查詢後，
    若品名/單位一併被改動，代表它們是連帶帶出的，不應直接填。"""
    return get_backend().operate_dialog(form_version_id, field_code, values, press)


@mcp.tool()
@require_auth
def uof_custom_get_pending_sign_list() -> str:
    """列出「目前輪到本帳號簽核」的所有表單（含 TaskId／SiteId／NodeSeq）。

    何時使用：使用者問「有多少單要我簽」「待辦有什麼」，或要簽核但沒有 TaskId 時。
    資料來源是首頁「待簽表單」widget，會自動翻完所有頁；範圍嚴格是「輪到目前身份待簽」的單。
    注意與 query_forms 的差別：query_forms 查的是日期區間內「自己送出（或簽過）的單」，
    兩者是不同集合，問「要簽什麼」一律用本工具。"""
    return get_backend().get_pending_sign_list()


@mcp.tool()
@require_auth
def uof_custom_terminate_task(
    task_id: Annotated[str, Field(description="表單工作代號 TaskId")],
    result: Annotated[str, Field(description="結案動作：Adopt 同意 / Reject 否決 / Cancel 作廢")],
    reason: Annotated[str, Field(description="Adopt/Reject 的簽核意見，或 Cancel 的作廢原因")],
) -> str:
    """結案一張單。申請人撤單(Cancel)、或待簽者同意/否決(Adopt/Reject)。

    何時使用：
    - 申請人要撤回自己、簽核中的單 → result=Cancel（走網頁「表單取回 → 作廢表單」）
    - 待簽者要同意/否決停在自己這站的單 → result=Adopt/Reject（走網頁簽核流程）
    操作者固定為本 Server 綁定的身份（UOF_ACCOUNT），不由呼叫端指定。
    邊界：Cancel 僅限自己申請、簽核中的單；Adopt/Reject 僅限輪到目前身份待簽的單。
    對已結案（同意/否決/作廢）的單會被工具層擋下（避免覆寫最終結果）。"""
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
        Field(description="最多回幾筆（會自動翻頁湊滿；預設 50，UOF 一頁通常 10–20 筆）"),
    ] = 50,
    query_mode: Annotated[
        str,
        Field(description="日期依據：apply=申請日期（自己送出的單）、sign=簽核日期（自己簽過的單）"),
    ] = "apply",
) -> str:
    """搜尋 UOF 表單（依日期範圍 + 關鍵字），回傳含 TaskId 的清單。

    何時使用：使用者沒有 TaskId、想列出某段期間自己送出或簽過的單時。
    取得 TaskId 後可丟給 get_task_data / get_task_result 看單張詳情。

    ⚠️ 這不是待簽清單：query_mode=apply 查的是「自己申請的單」、sign 是「自己簽過的單」，
    都跟「現在輪到我簽」是不同集合。問「有多少單要我簽」請用 get_pending_sign_list。

    限制：範圍等同使用者在 UOF 網頁「查詢表單」頁所看到的；會翻頁到湊滿 max_results 為止，
    因此 max_results 給太小會看起來像「只有這些」。"""
    return get_backend().query_forms(keyword, date_from, date_to, max_results, query_mode)


@mcp.tool()
@require_auth
def uof_custom_search_users(
    keyword: Annotated[
        str,
        Field(description="查詢關鍵字：輸入姓名或帳號的一部分即可，例如「asgard」「王小明」"),
    ],
) -> str:
    """依姓名或帳號關鍵字查詢 UOF 人員，回傳姓名、帳號與 UserGuid。

    何時使用：`sign_next` 需要指定下一關簽核者時，先用本工具取得正確的 UserGuid。

    限制：回傳範圍為目前帳號可看到的 UOF 人員（同 ChoiceCenter 選人清單）。"""
    return get_backend().search_users(keyword)


@mcp.tool()
@require_auth
def uof_custom_sign_next(
    task_id: Annotated[str, Field(description="要簽核的 TaskId（用 get_pending_sign_list 取得）")],
    signer_guid: Annotated[str, Field(description="留空＝此關結案（此關為最後簽核 → 表單通過）；填入＝同意後送往下一站點的簽核者 UserGuid（用 search_users 取得）")] = "",
    site_id: Annotated[str, Field(description="（不需提供）站點代號，由待簽清單自動定位；留空即可")] = "",
    node_seq: Annotated[int, Field(description="（不需提供）節點順序，自動定位；留 0 即可")] = 0,
) -> str:
    """簽核（同意）目前待簽的一關，支援自由流程（內部走網頁簽核流程）。

    何時使用：主管對輪到自己待簽的單按同意——`signer_guid` 留空＝此關結案（表單通過）；
    填入下一關簽核者的 UserGuid＝同意後送往下一站點。只能簽輪到目前身份（本 Server 綁定帳號）待簽的單。
    `site_id`/`node_seq` 不需提供（由待簽清單自動定位）。目前僅實作「同意」；否決/退簽、逐站留詳細意見、
    並簽/會簽請走 Web UI 或 terminate_task。"""
    return get_backend().sign_next(task_id, site_id, node_seq, signer_guid)


def main():
    mcp.run(transport='stdio')


if __name__ == "__main__":
    main()
