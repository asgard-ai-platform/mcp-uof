# MCP Tools 參考（開發者導入指南）

本文件是導入 `mcp-uof` 的開發者參考：每個 MCP Tool 的規格、使用情境、行為與能力邊界。所有 tool 名稱使用 `uof_custom_` 前綴。

工具清單與簽名固定；底層使用 httpx + lxml 操作 UOF 網頁端點。架構見 [architecture.md](architecture.md)。

**起單**由 `apply_form` 解析並填寫起單頁欄位，支援基本欄位、dialog/plugin 與通用 dataGrid 明細。特定表單的業務 SOP 與欄位語意不內建於 MCP server，應由部署端私有 skill 依即時結構組合。附件、多站與並簽/會簽尚未支援。

> 綁定與身份切換見 [integration.md](integration.md)；環境變數見 [configuration.md](configuration.md)。

---

## 人員角色模型

UOF 的資料範圍完全跟隨登入身份的帳號。以一般簽核流程為例的三個角色：

| 角色 | 可用工具 |
| --- | --- |
| 申請人 | 查詢類、`apply_form`、`terminate_task`（撤回有權限的單） |
| 當站簽核者 | 查詢類、`sign_next`、`terminate_task`（Adopt/Reject） |
| 管理帳號 | 能力仍以該站台授予的 UOF 權限為準，工具不保證可操作他人表單 |

> [!IMPORTANT] **簽核以 `sign_next` 完成**：待簽者以自己的身份對輪到自己的自由流程單按「同意」，可選擇**結案** （此關為最後簽核 → 表單通過）或**指定下一關簽核者**（往下一站點）。逐站留詳細意見、並簽/會簽、固定流程逐站推進仍需 Web UI。待簽清單：用 `get_pending_sign_list` 列出「目前輪到本帳號簽核」的所有單（含 TaskId／SiteId／NodeSeq，資料來自首頁待簽 widget）；`query_forms` 則是查「自己送出或簽過」的單（依日期），兩者是不同集合。`terminate_task` 則用於撤單/作廢/結案（見下）。

---

## Tool 總覽

所有工具一律對外可用、一律走 httpx 網頁機制。

| Tool | 異動資料 | 備註 |
| --- | :-: | --- |
| `uof_custom_check_auth` | 否 | 回報 web session 登入狀態 |
| `uof_custom_get_form_list` | 否 | 表單清單（含 formId） |
| `uof_custom_get_external_form_list` | 否 | 「非線上使用」旗標需在後台查，工具回說明 |
| `uof_custom_get_form_structure` | 否 | 即時解析起單頁得到的欄位 |
| `uof_custom_get_form_structure_by_id` | 否 | 同上（建議優先用，資訊較完整） |
| `uof_custom_preview_workflow` | 否 | 流程模擬目前不提供，回「需在網頁操作」 |
| `uof_custom_apply_form` | 是 | 依欄位結構執行通用網頁起單 |
| `uof_custom_get_task_data` | 否 | 查單摘要＋表單已填欄位 |
| `uof_custom_get_task_result` | 否 | 逐站簽核歷程（可含表單欄位） |
| `uof_custom_get_dialog_structure` | 否 | 對話框型欄位的內部控制項結構 |
| `uof_custom_search_dialog_options` | 否 | 直接 picker dialog 的候選；不保證涵蓋 row-editor 內的巢狀 picker |
| `uof_custom_operate_dialog` | 否 | 【探測用】對 dialog 填值/按鈕，回報連帶改動 |
| `uof_custom_terminate_task` | 是 | Cancel＝作廢；Adopt/Reject＝走網頁簽核流程 |
| `uof_custom_get_pending_sign_list` | 否 | 目前輪到本帳號待簽的所有單（含 TaskId/SiteId/NodeSeq） |
| `uof_custom_query_forms` | 否 | 列清單/搜尋自己送出或簽過的單（含 TaskId） |
| `uof_custom_search_users` | 否 | 依姓名/帳號查人員與 UserGuid |
| `uof_custom_sign_next` | 是 | 自由流程逐站簽核（同意）：結案或指定下一關簽核者 |

## System

### `uof_custom_check_auth()`

回報目前帳號的記憶體 web session 是否已登入。此工具本身不觸發登入；任一套用認證閘的工具會在呼叫前建立 session。

**使用情境**：對話開始時的健康檢查；除錯 `.env` 設定。

---

## WKF 電子簽核

### `uof_custom_get_form_list()` / `uof_custom_get_external_form_list()`

`get_form_list` 回傳表單類別、表單名稱、`formId`。`get_external_form_list` 因「非線上使用」是後台 admin 旗標、前端看不到，工具會回說明並建議改用 `get_form_list`。

> **注意**：能否用 `apply_form` 起單，看表單是否可從起單頁進入，而不是看它在不在 external 清單。

### `uof_custom_get_form_structure(form_version_id)` / `uof_custom_get_form_structure_by_id(form_id)`

兩者回傳相同的欄位資訊，差別僅在查詢鍵是 `formVersionId` 或 `formId`。手上已有 `formId` 時可使用 by_id 版本。

**重要邊界**：回傳的是**即時解析起單頁 DOM**（`table.fieldWidth` 區塊）的結果，理論上應涵蓋
網頁上看得到的原生欄位；**若某欄位漏掉，是解析器的 bug，不是伺服器刻意少開放**——回報開發面追查，
不要當成「這張表單本來就只能填這些」。網頁上的欄位若藏在特殊 JS 對話框或 plugin 本體中，可能需要外部
skill/agent 以其他 MCP primitive 或 UOF Web UI 組合處理。

### `uof_custom_preview_workflow(...)`

**目前不提供流程模擬**：此工具只回傳能力說明，不驗證參數或簽核路徑。`apply_form` 會實際異動資料；送出後可用 `get_task_result` 查看實際歷程。

### `uof_custom_apply_form(form_version_id, applicant_account, first_signer_account, fields, comment, urgent_level)`

起單，成功回傳 `TaskId` 與表單編號。呼叫端傳結構化 `fields`；server 內部以 httpx 解析並填寫起單頁上的欄位。
特定表單的語意 payload（例如把「報銷明細」轉成多個欄位或多列明細）應由外部 agent/skill 在呼叫前轉成 MCP 可接受的欄位資料。

| 參數 | 說明 |
| --- | --- |
| `form_version_id` | 表單版本代號，由 `get_form_list` 取得 |
| `applicant_account` | 相容性參數；目前不改變申請身份，實際身份固定為 `UOF_ACCOUNT` |
| `first_signer_account` | 相容性參數；目前尚未套用到派單頁，不能用來保證首站路由 |
| `fields` | `{fieldId: 值}`；dataGrid 明細為 `{fieldId: [row, ...]}`；**對話框欄位改帶 dict**（見下）。請先呼叫 `get_form_structure_by_id` |
| `comment` | 申請者意見（選填） |
| `urgent_level` | 緊急程度：`0` 緊急 / `1` 急 / `2` 普通（預設 `2`） |

dataGrid 範例（fields）：`{"<field-id>":[{"<column-id>":"<value>"}]}`。實際欄位與欄名以結構工具回傳為準。

**對話框（dialog）欄位**：含查詢視窗／明細表格的外掛欄位區塊改帶 dict，控制項名稱用 `get_dialog_structure` 查。四個保留鍵：

| 保留鍵 | 用途 |
| --- | --- |
| `_lookups` | `[{press:按鈕名, row:挑選器整筆JSON}]` — 唯讀欄位只能這樣填（`row` 來自 `search_dialog_options`，原樣帶入） |
| `_fill_before` | `{控制項:值}` — 會連動其他欄位的下拉，必須與 `_lookups` 同批送出 |
| `_press_after` | `[按鈕名]` — 填完值後按（如 `btnCalc` 計算金額） |
| `_rows` | 明細列清單；區塊有兩個明細表格時改用 `{開窗按鈕名: [列...]}` |

例：`{"主要欄位": {"_lookups": [{"press": "btnVendor", "row": {...}}], "txtSubject": "標題", "_rows": [{"txtQty": 2}]}}`。

> 送出前會檢查解析器辨識到的必填、選項與明細，但**回報成功不等於業務內容正確**。起單後應用 `get_task_data` 逐欄回讀比對；各表單的 dialog 操作順序由部署端私有 skill 定義。

**邊界**：

- **回傳的 TaskId 是後續所有查詢的鑰匙，務必保存**——雖然 `get_pending_sign_list` / `query_forms` 也能找回自己相關的單，但保存 apply_form 回傳最直接。
- 只能填結構工具與 dialog 工具辨識到的欄位。若回傳明顯少於網頁實際欄位，應視為解析器或 postback 配方缺口。
- 某些表單結案時會觸發下游模組；下游若因資料不足失敗，不一定會回報給本工具。後台前提見 [form-requirements.md](form-requirements.md)。

### `uof_custom_get_task_data(task_id)` / `uof_custom_get_task_result(task_id, include_form_data)`

`get_task_data` 回申請摘要（申請者、結果、結案日期）＋表單已填欄位；`get_task_result` 回逐站簽核歷程（簽核者、結果、意見、時間），站點顯示「未簽核」代表表單停在該站，`include_form_data=True` 時一併回傳欄位內容。兩者以 `ViewFormTemp.aspx` 解析取得。欄位以表單自身欄位代碼呈現（與 `apply_form` 寫入同一套代碼），不做表單別解讀；欄位語意請搭配該表單的 skill。

**邊界**：`task_id` 由 `get_pending_sign_list` / `query_forms` 取得，或使用者提供（apply_form 回傳／Web UI／通知信）。未結案的最終結果顯示為「簽核中／處理中」，結案為「同意／否決／作廢」。

### `uof_custom_get_dialog_structure(form_version_id, field_code)` / `uof_custom_search_dialog_options(...)` / `uof_custom_operate_dialog(...)`

UOF 的複合欄位（請購明細、主要欄位、費用明細…）實質內容藏在**對話框（dialog）**裡，主表結構看不到。這三個工具是「看進 dialog 裡」的探測面：

- **`get_dialog_structure(form_version_id, field_code="")`**：查 dialog 型欄位的內部控制項結構（標籤、必填、型別、可選值、查找鈕）。`field_code` 留空列出整張表單所有 dialog 欄位。`get_form_structure_by_id` 把某欄位標成〈dialog〉時用它看內容。只做結構擷取，不解讀語意；回傳的按鈕名可直接用於 `apply_form` 的 `_lookups` / `_press_after` / `_rows`。
- **`search_dialog_options(form_version_id, field_code, keyword="", limit=20)`**：查直接 picker dialog 的候選，整筆回傳 JSON 可作為 `_lookups` 的 `row`。目前不會自動深入 row-editor 裡由按鈕開啟的巢狀 picker；查不到時不得猜值，應改用 UOF Web UI 或部署端已驗證的資料來源。
- **`operate_dialog(form_version_id, field_code, values={}, press="")`**：【探測用】對 dialog 執行「填值/按鈕」一步，回報伺服器連帶改動了哪些控制項，用來判斷欄位相依關係。⚠️ **不能用來累積明細列**：每次呼叫都重開一個起單 session（GridDataID 每次不同），寫進去的列會被丟棄——明細列請用 `apply_form` 一次帶齊。本工具不知道任何 dialog 的意義，`press` 由呼叫端指定；正確操作順序請查該表單 skill。

### `uof_custom_terminate_task(task_id, result, reason)`

結案。`result`: `Adopt`（同意）/ `Reject`（否決）/ `Cancel`（作廢）。操作者固定為本 Server 綁定的身份（`UOF_ACCOUNT`），不由呼叫端指定。

- **Cancel（作廢/撤單）**：對自己申請、簽核中的單走網頁「表單取回 → 作廢表單」（`FormGetBack.aspx`）。
- **Adopt / Reject（同意/否決）**：委派網頁簽核流程，只能對**輪到目前身份待簽**的單執行；歷程記為本人。

| 操作者 | 對象 | 動作 |
| --- | --- | --- |
| 申請人 | 自己簽核中的單 | Cancel（撤單作廢） |
| 主管（當站待簽者） | 輪到自己待簽的單 | Adopt/Reject，歷程記為主管本人，等同網頁簽核 |
| 具對應權限的帳號 | 自己申請或目前待簽的單 | 依站台授權與上述規則 |

**使用情境**：申請人撤回自己的單、主管核准或否決停在自己這站的單。

**工具層防護**：對「已結案」（同意/否決/作廢）的單再操作，UOF 會覆寫原結果；本工具送出前先查狀態，發現已結案就攔截。

> [!WARNING]
>
> - Adopt/Reject 的 `reason` 會作為簽核意見；Cancel 的 `reason` 是作廢原因。`sign_next` 本身不接受意見。
> - Cancel 僅限自己申請、簽核中的單；Adopt/Reject 僅限輪到自己待簽的單（沿用網頁簽核的權限邊界）。

### `uof_custom_sign_next(task_id, site_id, node_seq, signer_guid)`

**簽核（同意）目前待簽的一關**，走網頁簽核流程（自由流程適用）。操作者為本 Server 綁定的身份，只能簽「輪到自己待簽」的單。

| 參數 | 說明 |
| --- | --- |
| `task_id` | 要簽核的 TaskId（用 `get_pending_sign_list` 取得） |
| `site_id` / `node_seq` | **不需呼叫端提供**：由待簽清單自動定位（留空即可） |
| `signer_guid` | **留空＝此關結案**（表單通過）；**填入＝指定下一關簽核者**（GUID 由 `search_users` 取得） |

**使用情境**：主管對輪到自己的自由流程單按同意並結案，或同意後指定下一關簽核者。

> [!NOTE] 目前僅實作「同意」。否決/退簽、逐站留詳細意見、並簽/會簽、固定流程逐站推進請走 Web UI 或 `terminate_task`。

### `uof_custom_search_users(keyword)`

依姓名或帳號關鍵字查人員，回傳 `UserGuid` / 姓名 / 帳號。

**使用情境**：`sign_next` 要指定下一關簽核者時，先用本工具取得對方的 `UserGuid`。

### `uof_custom_get_pending_sign_list()`

列出「目前輪到本帳號簽核」的所有表單，含 `TaskId` / `SiteId` / `NodeSeq`。資料來源是首頁「待簽表單」widget，自動翻完所有頁。

**使用情境**：使用者問「有多少單要我簽」「待辦有什麼」，或要簽核（`sign_next` / `terminate_task`）但沒有 TaskId 時。範圍嚴格是「輪到目前身份待簽」的單——與 `query_forms`（自己送出/簽過的單，依日期）是不同集合，問「要簽什麼」一律用本工具。

### `uof_custom_query_forms(keyword, date_from, date_to, max_results, query_mode)`

依日期範圍＋關鍵字搜尋表單，回傳含 TaskId 的清單。UOF 一代沒有清單/搜尋 API，本工具內部以 httpx + lxml 爬網頁取得（對使用者透明）。

`query_mode`：`apply`＝依申請日期查「自己送出的單」（預設）、`sign`＝依簽核日期查「自己簽過的單」。

⚠️ **這不是待簽清單**：兩種 mode 都跟「現在輪到我簽」是不同集合，問「有多少單要我簽」請用 `get_pending_sign_list`。使用者沒有 TaskId、想列出某段期間自己送出或簽過的單時用本工具，取得 TaskId 後再丟給 `get_task_data` / `get_task_result`。範圍等同使用者在 UOF 網頁「查詢表單」頁所見；會自動翻頁湊滿 `max_results`，因此 `max_results` 給太小會看起來像「只有這些」。

---

## 情境 → 工具速查

| 使用者說 | 工具組合 | 使用者需提供 |
| --- | --- | --- |
| 「系統有哪些表單？」 | `get_form_list` | 無 |
| 「這張表單要填什麼？」 | `get_form_list` → `get_form_structure_by_id`（欄位標〈dialog〉時再 `get_dialog_structure`） | 無 |
| 「幫我發一張表單」 | `get_form_structure_by_id`（→ `get_dialog_structure` / `search_dialog_options`）→ `apply_form` | 欄位值；需指定首站路由時改用 Web UI |
| 「我那張單到哪了／填了什麼？」 | `get_task_data` + `get_task_result` | TaskId |
| 「我不買了，撤單」 | `get_task_data` → `terminate_task(Cancel)` | TaskId |
| 「有多少單要我簽？」 | `get_pending_sign_list`（回含 TaskId/SiteId/NodeSeq） | 無 |
| 「（主管）這張單我同意／放行」 | `get_pending_sign_list` 找待簽 → `sign_next`（留空＝結案；填 `signer_guid`＝指定下一關） | TaskId（＋下一關簽核者，如需） |
| 「同意並指定下一關給某人」 | `search_users` 取 GUID → `sign_next(signer_guid=…)` | TaskId、下一關簽核者 |
| 「（主管）否決停在我這站的單」 | `terminate_task(Reject)` | TaskId |
| 「列出我某段期間送出／簽過的單」 | `query_forms`（`query_mode` 選 apply/sign，回含 TaskId） | 日期範圍、關鍵字（皆可選） |
| 「退簽、並簽/會簽、固定流程逐站推進」 | 引導至 UOF Web UI | — |
