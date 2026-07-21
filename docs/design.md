# 實作設計（每個工具背後怎麼做）

> 本文記錄每個 MCP 工具使用的 UOF 網頁端點與新增工具步驟。對外契約見 [tools.md](tools.md)；實際行為以程式碼為準。

## 一句話原則

對外只有工具；目前全部使用 httpx + lxml 操作 UOF 網頁端點。`BINDING` 登記工具名稱與目前機制，特定表單的業務 SOP 由部署端私有 skill 組合。

## 為什麼機制在設計期決定

每個工具目前都由 `OpsRouter` 委派給 `HttpWebBackend`；`BINDING` 同時作為工具登記與 smoke test 護欄。`HttpSession.post()` 遇到登入頁會重新登入並重送一次，因此新增不可重放的寫入操作時必須評估重送風險。

## 逐工具實作對照

全部走 http_web（`ops/http_web.py` 的 `HttpWebBackend` + `HttpSession`）。

| 工具 | 背後實際呼叫 | 異動 |
| --- | --- | :-: |
| `check_auth` | GET `Homepage.aspx`，判斷是否被導回 `Login.aspx` | 否 |
| `get_form_list` | GET 查詢頁下拉（`MyFormList.aspx?item=FormQuery`），解析表單樹 | 否 |
| `get_external_form_list` | 無對應網頁端點（後台 admin 旗標），回說明並建議改用 `get_form_list` | 否 |
| `get_form_structure` | `AddFormScript.aspx?formVersionId=…` → 解析欄位區塊 | 否 |
| `get_form_structure_by_id` | `AddFormScript.aspx` ＋ `ApplyFormList.aspx`（formId↔version 對照） | 否 |
| `get_dialog_structure` | `AddFormScript.aspx?mode=apply` → 開各 dialog 欄位自己的頁面，解析為 mini-form | 否 |
| `search_dialog_options` | dialog 挑選器（`ChoiceHandler.ashx` 等）查候選 | 否 |
| `operate_dialog` | 起單 session 內對 dialog 填值/按鈕，回報連帶改動（探測用，session 用完即棄） | 否 |
| `preview_workflow` | 目前不提供，回「流程預覽需在網頁操作」 | 否 |
| `apply_form` | `AddFormScript.aspx?mode=apply` → `FirstSite.aspx` 填欄位（含 dialog `_lookups`/`_rows`）→ 儲存/送出/`FirstSiteSend` | 是 |
| `get_task_data` | `ViewFormTemp.aspx?TASK_ID=` 解析申請摘要＋欄位 | 否 |
| `get_task_result` | `ViewFormTemp.aspx?TASK_ID=` 解析 `SignCommentGrid` 簽核歷程（可含欄位） | 否 |
| `terminate_task` | Cancel＝`FormGetBack.aspx`（作廢）；Adopt/Reject＝委派 `sign_task`（見下）。送出前先查狀態擋已結案 | 是 |
| `sign_next` | `FreeTask/SignNodeForm.aspx` → 確認頁 `SendOtherSite.aspx`（原生）/`OtherSiteSend.aspx`（plugin） | 是 |
| `get_pending_sign_list` | GET `Homepage.aspx` 首頁「待簽表單」widget（DGFormList），翻頁解析 TASK_ID/SITE_ID/NODE_SEQ | 否 |
| `query_forms` | POST `MyFormList.aspx?item=FormQuery`（帶日期＋關鍵字＋`query_mode` apply/sign），翻頁解析 RadGrid 列 | 否 |
| `search_users` | `ChoiceCenter/ChoiceHandler.ashx` 人員查詢 | 否 |

## 認證（web session，單一種）

只有一種認證：`SessionAuthProvider`（`auth/session.py`）。

|          | 網頁（session）                                                   |
| -------- | ----------------------------------------------------------------- |
| 怎麼來   | 表單 POST `Login.aspx`（取 `__VIEWSTATE` 後帶帳密）               |
| 存哪     | `httpx.Client` 的程序記憶體 cookie jar；不落盤                         |
| 失效處理 | GET/POST 被導回 `Login.aspx` → 自動重登重試一次                   |

一個程序固定使用一個 `UOF_ACCOUNT`；不同 server process 各自持有 session。

### 入口認證閘（`require_auth`）

工具入口的 `@require_auth`（`auth/base.py`）在每次呼叫前 `get_session_provider().ensure_valid()`；失敗回固定的登入失敗訊息（🔒），成功才放行。工具本體的例外原樣拋出、不被包成登入失敗。裝飾期會 fail-loud 驗證該工具已在 `BINDING` 登錄（漏綁/改名會在 import server 時立刻爆）。

### `check_auth` 的行為

`check_auth` 不需認證即可呼叫。首次取得 `HttpSession` 時會嘗試登入，接著 GET `Homepage.aspx`；被導回 `Login.aspx`＝未登入，否則＝已登入。

## httpx 網頁抓取流程（共用）

實作在 `ops/http_web.py`：`HttpSession`（`httpx.Client`）+ `HttpWebBackend`。複合 WebForms 操作不應在同一 session 中並行交錯。

1. 首次呼叫 → GET `Login.aspx` 取 `__VIEWSTATE`，POST 帳密登入，cookie 由 `httpx.Client` 自動維持。
2. 每次 GET/POST 若被導回 `Login.aspx`（session 過期），自動重新登入後重試一次。
3. 本實作只支援同步整頁 postback（帶 `__EVENTTARGET` 與頁面狀態）；尚未支援 async partial postback。

所需設定：`UOF_BASE_URL` / `UOF_ACCOUNT` / `UOF_PASSWORD`。不需瀏覽器 runtime；在 Alpine Linux 或 musl 環境仍須確認相依套件可安裝。

## 怎麼新增一個工具（可直接 follow）

1. **實作機制**：在 `ops/http_web.py` 的 `HttpSession` 加 scrape/postback 方法（httpx GET/POST + lxml 解析； `get()` / `post()` 已含 session 失效重登重試），再在 `HttpWebBackend` 加對外方法。
2. **宣告介面**：在 `ops/base.py` 的 `OpsBackend` 加上這個 `@abstractmethod`。
3. **登記綁定**：在 `ops/router.py` 的 `OpsRouter` 加同名方法 `return self._route("<name>", ...)`，並在 `BINDING` 標記它走 `"http_web"`。
4. **對外暴露**：在 `server.py` 加一個 `@mcp.tool` 的 `uof_custom_<name>`，內部 `return get_backend().<name>(...)`，並寫清楚 docstring（何時用、限制；**不要**提機制/模式）。

> 護欄：`tests/smoke/test_binding.py` 會斷言「`BINDING` 鍵集 == `OpsBackend` 抽象方法集」，所以漏掉第 2 或第 3 步會在 smoke 直接變紅。完成後跑 `uv run python tests/run.py smoke`，真實行為再跑 `mounted`。

## 網頁端點常數

httpx 端點常數集中在 `ops/http_web.py` 頂部（`Login.aspx`、`ApplyFormList.aspx`、`AddFormScript.aspx`、 `ViewFormTemp.aspx`、`FormGetBack.aspx`、`SignNodeForm.aspx` 等）。已打通的網頁自動化能力（起單/送出、通用 dataGrid、簽核三步、作廢）封裝在 `HttpSession`。

## 能力現況

- **查詢**：`get_form_list` / `get_form_structure(_by_id)` / `get_task_data` / `get_task_result` / `query_forms` / `search_users` 皆以 httpx 完成。`get_external_form_list` 無對應網頁端點，回說明。
- **起單**：`apply_form_web` 支援 text / select / radio / datePicker / dialog picker 欄位與通用 dataGrid 明細。實際身份固定為 `UOF_ACCOUNT`，目前尚未套用 `first_signer_account`。附件、多站與並簽/會簽尚未支援。
- **簽核/結案**：`sign_next`（自由流程單站同意）、`terminate_task`（Cancel 作廢 / Adopt·Reject 走簽核流程）。
- **不提供**：`preview_workflow`（流程模擬）目前回「需在網頁操作」。
