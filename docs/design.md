# 實作設計（每個工具背後怎麼做）

> 本文記錄每個 MCP 工具使用的 UOF 網頁端點與新增工具步驟。對外契約見 [tools.md](tools.md)；實際行為以程式碼為準。

## 一句話原則

對外只有工具；目前全部使用 httpx + lxml 操作 UOF 網頁端點。`BINDING` 登記工具名稱與目前機制，特定表單的業務 SOP 由部署端私有 skill 組合。

## 為什麼機制在設計期決定

每個工具目前都由 `OpsRouter` 委派給 `HttpWebBackend`；`BINDING` 同時作為工具登記與 smoke test 護欄。`WebFormsRuntime` 是 replay policy 的唯一 owner：安全查詢可在重新登入後重送一次，不可重放寫入一律禁止自動重送。

## 逐工具實作對照

全部走 http_web（`ops/http_web/` package 的 `HttpWebBackend` + `HttpSession` composition root）。

| 工具 | 背後實際呼叫 | 異動 |
| --- | --- | :-: |
| `check_auth` | GET `Homepage.aspx`，判斷是否被導回 `Login.aspx` | 否 |
| `login` | 127.0.0.1 反向代理真實 `Login.aspx`，取得 cookie 後存進 session 目錄 | 否 |
| `logout` | 清空 cookie jar、關閉登入代理、刪除 session 存檔 | 否 |
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

## 認證（web session，單一種機制）

只有一種認證機制：`Login.aspx` 的 cookie session，由 `SessionAuthProvider`（`auth/session.py`）管理。

|          | 網頁（session）                                                   |
| -------- | ----------------------------------------------------------------- |
| 怎麼來   | 三段來源依序：session 存檔 → 帳密 POST `Login.aspx` → 瀏覽器登入 |
| 存哪     | `httpx.Client` 的程序記憶體 cookie jar；另存 `UOF_SESSION_DIR`（預設 `~/.uof`，`0600`）供重啟沿用 |
| 失效處理 | 安全查詢被導回 `Login.aspx` → 重新取得 session 後重試一次；不可重放寫入不重送 |

一個程序固定一個身份；不同 server process 各自持有 session。

### 三段來源（`auth/session.py`、`auth/store.py`、`auth/browser_login.py`）

| 順序 | 來源 | 條件 |
| --: | --- | --- |
| 1 | session 存檔（`store.load_session`，在 `HttpSession.__init__` 灌回 cookie jar） | 探測 `Homepage.aspx` 沒被踢回 `Login.aspx` |
| 2 | 帳密自動登入（`HttpSession._do_login`） | `UOF_ACCOUNT` + `UOF_PASSWORD` 都有設 |
| 3 | 瀏覽器登入（`browser_login.start_login_flow`） | 前兩者都不成立 → 拋 `BrowserLoginRequired` |

瀏覽器登入在 `127.0.0.1` 起臨時反向代理，把真實的 `Login.aspx` 代理給使用者的瀏覽器；請求由
`HttpSession._client` 實際發出，因此 `Set-Cookie` 直接落在既有的 cookie jar。安全邊界（只綁
localhost、Host 檢查、一次性 token、不外流上游 `Set-Cookie`、同 host 限制、成功／逾時自關）寫在
`auth/browser_login.py` 的模組 docstring。登入成功後 `store.save_session` 落地。

### 入口認證閘（`require_auth`）

工具入口的 `@require_auth`（`auth/base.py`）在每次呼叫前 `get_session_provider().ensure_valid()`，成功才放行。失敗分兩種訊息：

- `BrowserLoginRequired` → `browser_login_required_message`（🔑）：要 AI 呼叫 `uof_custom_login`，禁止索取帳密。
- 其他 → `auth_failure_message`（🔒）：設定層級問題，要使用者檢查設定。

工具本體也一併攔 `BrowserLoginRequired`（session 可能在通過閘門之後才過期）；其他例外原樣拋出、不被包成登入失敗。裝飾期會 fail-loud 驗證該工具已在 `BINDING` 登錄（漏綁/改名會在 import server 時立刻爆）。

### `check_auth` / `login` / `logout` 的行為

三者都不套認證閘。`check_auth` GET `Homepage.aspx`，被導回 `Login.aspx`＝未登入，並依是否具備帳密備援給不同指示；`login` 起代理並同步等 `UOF_LOGIN_WAIT_SECONDS`；`logout` 清記憶體 session、關閉登入流程並刪除 session 存檔。

## httpx 網頁抓取流程（共用）

實作在 `ops/http_web/`：`HttpSession` 組合 `HttpTransport`、`WebFormsRuntime` 與各操作 module，再由 `HttpWebBackend` 呈現 MCP 結果。session lifecycle 的 operation lease 會序列化完整複合操作，避免同一 cookie session 的 WebForms state 交錯。

1. 建立 `HttpSession` 時先從 session 存檔灌回上次的 cookie；沒有或已失效才走登入。
2. 帳密備援登入 → GET `Login.aspx` 取 `__VIEWSTATE`，POST 帳密，cookie 由 `httpx.Client` 自動維持。
3. 安全讀取／查詢若被導回 `Login.aspx`，重新取得 session 後重試一次；起單、明細、簽核與作廢寫入明確使用 `ReplayPolicy.NEVER`。
4. 本實作只支援同步整頁 postback（帶 `__EVENTTARGET` 與頁面狀態）；尚未支援 async partial postback。

所需設定：`UOF_BASE_URL`（必填），`UOF_ACCOUNT` / `UOF_PASSWORD`（選填，帳密備援用）。
操作 UOF 不需瀏覽器 runtime；只有瀏覽器登入那一次會用到使用者自己的瀏覽器，無圖形介面的機器請用帳密備援。
在 Alpine Linux 或 musl 環境仍須確認相依套件可安裝。

## 怎麼新增一個工具（可直接 follow）

1. **實作操作**：把完整、安全的 UOF transaction 放進 `ops/http_web/` 對應 operation module；共用 WebForms state、postback 與 replay policy 由 `runtime.py` 負責，HTTP/session 機制由 `transport.py` 負責。`HttpSession` 只作 composition root 與必要的相容 facade，再由 `HttpWebBackend` 加入對外呈現。
2. **宣告介面**：在 `ops/base.py` 的 `OpsBackend` 加上這個 `@abstractmethod`。
3. **登記綁定**：在 `ops/router.py` 的 `OpsRouter` 加同名方法 `return self._route("<name>", ...)`，並在 `BINDING` 標記它走 `"http_web"`。
4. **對外暴露**：在 `server.py` 加一個 `@mcp.tool` 的 `uof_custom_<name>`，內部 `return get_backend().<name>(...)`，並寫清楚 docstring（何時用、限制；**不要**提機制/模式）。

> 護欄：`tests/smoke/test_binding.py` 會斷言「`BINDING` 鍵集 == `OpsBackend` 抽象方法集」，所以漏掉第 2 或第 3 步會在 smoke 直接變紅。完成後跑 `uv run python tests/run.py smoke`，真實行為再跑 `mounted`。

## 網頁端點常數

httpx 端點常數集中在 `ops/http_web/constants.py`。WebForms 協定封裝在 `runtime.py`；起單、明細與任務 lifecycle 分別由 `submission.py`、`details.py`、`lifecycle.py` 擁有完整 transaction，`HttpSession` 負責明確組合這些操作。

## 能力現況

- **查詢**：`get_form_list` / `get_form_structure(_by_id)` / `get_task_data` / `get_task_result` / `query_forms` / `search_users` 皆以 httpx 完成。`get_external_form_list` 無對應網頁端點，回說明。
- **起單**：`apply_form_web` 支援 text / select / radio / datePicker / dialog picker 欄位與通用 dataGrid 明細。實際身份固定為本程序目前登入的身份，目前尚未套用 `first_signer_account`。附件、多站與並簽/會簽尚未支援。
- **簽核/結案**：`sign_next`（自由流程單站同意）、`terminate_task`（Cancel 作廢 / Adopt·Reject 走簽核流程）。
- **不提供**：`preview_workflow`（流程模擬）目前回「需在網頁操作」。
