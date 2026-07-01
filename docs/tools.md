# MCP Tools 參考（開發者導入指南）

本文件是導入 `mcp-uof` 的開發者參考：每個 MCP Tool 的規格、使用情境、行為與能力邊界。
所有 tool 名稱使用 `uof_custom_` 前綴。

工具清單與簽名固定；每個工具底層由哪種**機制**（SOAP/PublicAPI 或網頁）完成，是 server 內部、
開發期決定且對使用者透明的實作細節（下表「機制」欄僅供開發者參考，見 [architecture.md](architecture.md)）。

起單分兩條內部路線（對使用者透明，依表單自動分派）：
- **SOAP 中介起單**（原生設計的表單）：支援單站自由流程 + 基本欄位型別（文字、自動編號、可空欄位、
  日期、單選、不帶檔案的附檔欄位）+ **明細(dataGrid，以列清單帶入)**。實際附檔、多站與並簽/會簽、
  固定流程逐站推進尚未支援。
- **網頁起單**（本體是客製 plugin、中介欄位填不到內容的表單，如採購單）：以 httpx + lxml 爬網頁
  完整填單送出（主旨/供應商/明細等）。較依賴頁面結構；簽核者由表單自身流程決定。

> 綁定與身份切換見 [integration.md](integration.md)；環境變數見 [configuration.md](configuration.md)。

---

## 人員角色模型

UOF 的 API 權限完全跟隨登入 Token 的帳號。以採購單流程為例的三個角色：

| 角色 | 範例帳號 | 可用工具 |
|---|---|---|
| 申請人 | `applicant_account` | 查詢類全部、`apply_form`、`terminate_task`（撤自己的單） |
| 簽核主管 | `manager_account` | 查詢類全部、`terminate_task`（單站流程中 Adopt/Reject 等同簽核） |
| 系統管理員 | `admin` | 全部，含 `terminate_task` 結案他人表單 |

> [!IMPORTANT]
> **API 沒有逐站「簽核」動作**（無同意/否決＋意見的方法）。但：
> 在**單站**自由流程中，待簽主管以自己的 token 呼叫 `terminate_task`（Adopt/Reject），
> 簽核歷程會記錄為主管本人的 Approve/Disapprove——語意上等同簽核。
> 多站流程仍只能在 Web UI 逐站簽核。另外**無待簽清單 API**：主管必須自帶 TaskId
> （UI 或通知信），Agent 無法替主管「找出待辦」。

---

## Tool 總覽

所有工具一律對外可用。「機制」欄＝該工具內部用哪種方式完成（對使用者透明，僅供開發者參考）。

| Tool | 異動資料 | 機制 | 備註 |
|---|---|:-:|---|
| `uof_custom_check_auth` | 否 | SOAP | |
| `uof_custom_get_form_list` | 否 | SOAP | |
| `uof_custom_get_external_form_list` | 否 | SOAP | |
| `uof_custom_get_form_structure` | 否 | SOAP／web＊ | ＊網頁起單的表單回該單可填欄位說明 |
| `uof_custom_get_form_structure_by_id` | 否 | SOAP／web＊ | ＊同上 |
| `uof_custom_preview_workflow` | 否 | SOAP／web＊ | ＊網頁起單的表單改為「試填到送出前」驗證 |
| `uof_custom_apply_form` | 是 | SOAP／web＊ | ＊網頁起單的表單(如採購單)內部走網頁填單；其餘走 SOAP 中介。對使用者透明 |
| `uof_custom_get_task_data` | 否 | SOAP | |
| `uof_custom_get_task_result` | 否 | SOAP | |
| `uof_custom_terminate_task` | 是 | SOAP | |
| `uof_custom_query_forms` | 否 | web | 列清單/搜尋；UOF PublicAPI 無此 API，內部以網頁取得 |
| `uof_custom_sign_next` | 是 | SOAP | 自由流程不支援（HTTP 500），僅固定流程 |

> ＊「SOAP／web」表示同一個工具依**表單**內部分派：本體是客製 plugin、中介欄位填不到內容的表單
> （目前為採購單系列）走網頁；其餘走 SOAP。哪張表單走網頁登錄在 `ops/web_apply/registry.py`（設計期、
> 靜態），對使用者透明——使用者只挑表單、呼叫同一個工具。詳見 [web-apply-design.md](web-apply-design.md)。

---

## System

### `uof_custom_check_auth()`

檢查 Token 狀態；過期或不存在時自動重新取得。Token 由 `auth.py` 做記憶體＋磁碟雙層快取。

**使用情境**：對話開始時的健康檢查；除錯 `.env` 設定。

---

## WKF 電子簽核

### `uof_custom_get_form_list()` / `uof_custom_get_external_form_list()`

回傳表單類別、表單名稱、`formId`、`recentVersionId`。
external 版本只列被標記為「非線上使用」的表單。

**使用情境**：使用者詢問系統有哪些表單時；同時為後續工具取得 formId 與 formVersionId。

> **注意**：`get_external_form_list` 的清單**不等於**「可外部起單的表單」——實測中採購單不在
> 該清單內卻能正常起單。要判斷一張表單能否用 `apply_form` 起單，看它有沒有
> 已發佈的 `recentVersionId`，而不是看它在不在 external 清單。

### `uof_custom_get_form_structure(form_version_id)` / `uof_custom_get_form_structure_by_id(form_id)`

回傳欄位清單。**建議優先用 by_id 版本**：它額外回傳 `fieldType`
（`autoNumber`/`optionalField`/`multiLineText`/`fileButton`），且會附上每個欄位的填寫方式，
以及如何用 `apply_form` 起單的提示——呼叫端不必自行得知 XML 結構。

**重要邊界**：回傳的是該表單**對外開放的「中介欄位」**，可能**遠少於** UOF 網頁上看到的完整表單。
網頁上的欄位（如採購單的主旨、供應商、幣別、採購明細）若未在後台對應為中介欄位，
就不會出現在這裡，API 也無法填。例如採購單只回 4 個中介欄位，但網頁版有十多個必填欄位。
另外兩者都不含必填標記；無法程式化判斷必填欄位，請以 `preview_workflow` 驗證。

### `uof_custom_preview_workflow(form_version_id, applicant_account, first_signer_account, fields, comment, urgent_level)`

以結構化參數模擬流程走向，**不會真的起單**。參數與 `apply_form` 相同，回傳各站點簽核者。

**使用情境**：`apply_form` 前的驗證步驟，確認欄位與簽核路徑正確。

### `uof_custom_apply_form(form_version_id, applicant_account, first_signer_account, fields, comment, urgent_level)`

外部起單，成功回傳 `TaskId` 與表單編號。呼叫端只需傳結構化 `fields`，不必自行組 XML。
一般表單由 server 內部組 XML 送 SOAP；registry 登錄的網頁起單表單（目前採購單系列）會改由
httpx + lxml 爬網頁完整填單，仍沿用同一個 `apply_form` 入口。

| 參數 | 說明 |
|---|---|
| `form_version_id` | 表單版本代號，由 `get_form_list` 取得 |
| `applicant_account` | 申請者帳號。web 起單表單必須等於目前 MCP 的 `UOF_ACCOUNT`，否則會被擋下 |
| `first_signer_account` | 第一站簽核者帳號。SOAP 自由流程必填；網頁起單表單由表單自身流程決定，會忽略此參數 |
| `fields` | 一般表單為欄位值對應 `{fieldId: 值}`；網頁起單表單為該 handler 說明的 payload，請先呼叫 `get_form_structure_by_id` |
| `comment` | 申請者意見（選填） |
| `urgent_level` | 緊急程度：`0` 緊急 / `1` 急 / `2` 普通（預設 `2`） |

採購單範例（fields）：`{"subject":"辦公室耗材採購","supplier":"C000007","supplier_query":"供應商名稱","details":[{"item_code":"A001","item_query":"A001","qty":2,"price":100,"unit":"個"}]}`。

**邊界**：
- SOAP 自由流程未提供 `first_signer_account` 會被擋下（底層為 `NoSignerException`）
- **回傳的 TaskId 是後續所有查詢的唯一鑰匙，務必保存**——系統無待簽清單 API，遺失即只能回 Web UI 找
- 一般 SOAP 表單只能填 `get_form_structure_by_id` 回傳的中介欄位。若中介對應不完整，SOAP 起單仍可能
  內容不完整；這類表單需補後台中介欄位，或明確登錄 web handler 才能完整填單。
- 採購單系列已登錄 web handler，可填主旨、供應商、幣別、付款條件、儲存地點、運送方式、到貨日與明細；
  實際欄位形狀以 `get_form_structure_by_id` 回傳的採購單說明為準。
- 某些表單結案時會觸發下游模組（如採購單的採購作業），下游若因資料不足失敗，不會回報給本工具。
  一張表單要能被 MCP 完整操作的後台前提，見 [form-requirements.md](form-requirements.md)。

**支援範圍與限制**：SOAP 起單支援基本欄位與 DataGrid 明細，但不支援實際附檔、多站與並簽/會簽。
web 起單目前只對 registry 登錄的採購單系列提供逐單 handler。要起其他表單，先用
`get_form_structure_by_id` 確認欄位形狀，並以 `preview_workflow` 驗證後再送出。

### `uof_custom_get_task_data(task_id)` / `uof_custom_get_task_result(task_id, include_form_data)`

`get_task_data` 回傳申請摘要（申請者、結果、結案日期）；
`get_task_result` 回傳逐站簽核歷程（簽核者、結果、意見、時間），站點結果顯示「待簽」代表表單停在該站。

**使用情境**：查詢一張單目前的進度與簽核歷程。

**邊界**：
- `task_id` 無法由 API 查得，描述中須引導使用者提供（apply_form 回傳／Web UI／通知信）
- 簽核中狀態實際回傳 `UnKnow`（非 `Unknown`），service 層已同時容納兩種拼法

### `uof_custom_terminate_task(task_id, result, reason)`

結案。`result`: `Adopt`（同意）/ `Reject`（否決）/ `Cancel`（作廢）。操作者固定為本 Server
綁定的身份（`UOF_ACCOUNT`），不由呼叫端指定，因此歷程記錄一定是這份設定所代表的人。

**權限行為（UOF API 端沒有任何權限管控）**：

| 操作者 | 對象 | 可執行的動作 |
|---|---|---|
| 申請人 | 自己的單 | Cancel（撤單）、Adopt（自我核准，API 不阻止） |
| 主管（當站待簽者） | 待簽的單 | Adopt/Reject，歷程記為主管本人，等同網頁簽核 |
| 主管 | 與自己無關的單 | Cancel（API 也不阻止） |
| admin | 任何簽核中的單 | Adopt / Reject / Cancel，歷程記錄為 admin 操作 |

**使用情境**：主管核准或否決、申請人撤回自己的單、管理員強制結案卡住的單。

**工具層防護**：UOF API 對「已結案」的單再次結案會回報成功並覆寫原結果（例如將已同意的單改為作廢）；
本工具在送出前先查詢狀態，發現已結案就攔截。

> [!WARNING]
> - 信任邊界完全依賴「誰拿得到 Token」：申請人可自我核准、無關者可結他人的單。
>   Agent 不應主動引導這些用法；部署時建議依情境限制 Adopt/Reject 的使用者。
> - `reason` 不會寫入簽核歷程的意見欄（Text 欄為空）；需留意見請改走 Web UI。
> - 這是「整張單終結」：單站流程中等同簽核，多站流程會跳過後續所有站。

### `uof_custom_sign_next(task_id, site_id, node_seq, signer_guid)`

表單送下一站（指定預計簽核者）。對應 `SignNext2`。

> [!CAUTION]
> **此工具對自由流程表單（含採購單）不支援**，呼叫會回 HTTP 500。
> 且 `site_id` / `node_seq` / `signer_guid` 無法由任何 WKF 查詢 API 取得，
> 只能從固定流程的後台流程設計得知。固定流程不在本版本支援範圍內。
> 注意參數中**沒有**同意/否決/意見——這不是「簽核」工具。

### `uof_custom_query_forms(keyword, date_from, date_to, max_results)`

依日期範圍＋關鍵字搜尋表單，回傳含 TaskId 的清單。UOF 一代 PublicAPI 沒有清單/搜尋 API，本工具內部
以 httpx + lxml 爬網頁取得清單（對使用者透明）。**認證走 web session（帳號/密碼登入），不需 SOAP token**，
因此沒有 PublicAPI 的站台也能用；不需安裝 Playwright 或 Chromium，Alpine Linux 可直接部署。

這是補上「UOF 一代沒有待簽清單 API」缺口的入口：使用者沒有 TaskId 時，先用本工具列出
自己最近的單或搜尋關鍵字，取得 TaskId 後再丟給 `get_task_data` / `get_task_result` 看詳情。
範圍等同使用者在 UOF 網頁「查詢表單」頁所見；只取第一頁，需更精確請縮日期或加關鍵字。

---

## 情境 → 工具速查

| 使用者說 | 工具組合 | 使用者需提供 |
|---|---|---|
| 「系統有哪些表單？」 | `get_form_list` | 無 |
| 「採購單要填什麼？」 | `get_form_list` → `get_form_structure_by_id` | 無 |
| 「幫我發一張採購單」 | `get_form_structure_by_id` → `preview_workflow` → `apply_form` | 欄位值、第一站簽核者帳號 |
| 「我那張單到哪了？」 | `get_task_data` + `get_task_result` | TaskId |
| 「我不買了，撤單」 | `get_task_data` → `terminate_task(Cancel)` | TaskId |
| 「（主管）這張單我同意/否決」 | `get_task_result` 確認停在自己 → `terminate_task(Adopt/Reject)` | TaskId（單站流程限定） |
| 「這張卡住的單直接核准/否決」 | `get_task_data` → `terminate_task(Adopt/Reject)` | TaskId |
| 「列出我最近的單／搜尋表單」 | `query_forms`（**僅 web 後端**） | 日期範圍、關鍵字（皆可選） |
| 「列出我的待簽清單」 | soap 後端無此 API；web 後端用 `query_forms` 列清單再篩 | — |
| 「多站流程逐站簽核並留意見」 | 不支援，請引導至 UOF Web UI 操作 | — |
