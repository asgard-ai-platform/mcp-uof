# 實作設計（每個工具背後怎麼做）

> 這是一份**會持續更新**的實作參考：每個 MCP 工具底層用哪種機制、實際呼叫什麼、需要哪些資源，以及
> 怎麼新增工具。對外原則見 [architecture.md](architecture.md)、工具規格見 [tools.md](tools.md)。
> **唯一決策來源**是 `src/mcp_uof/ops/router.py` 的 `BINDING`；本文若與它不一致，以 `BINDING` 為準。

## 一句話原則

對外只有「工具」。每個工具底層用 **Web Service（SOAP/PublicAPI）** 還是 **網頁（Playwright）**，是
開發期決定、寫在 `BINDING` 的綁定，對使用者透明。能用 SOAP 做的用 SOAP；SOAP 沒有該 API 的才用網頁。

## 設計原則：機制由開發者在設計期決定

每個工具用 SOAP 還是網頁，是開發者在實作時就決定好的，不是執行期才臨時判斷。規則很單純：一個功能
SOAP/PublicAPI 做得到，就綁 SOAP；SOAP 沒有對應的 API，才改用網頁。`BINDING` 這層唯一走網頁的是
`query_forms`，因為 UOF 一代的 PublicAPI 沒有「列清單／搜尋」的 API。這個決定寫在 `BINDING` 裡，
使用者看不到、也不需要管。

> 起單還有**第二軸**的設計期分派：同一個 `apply_form` 下，本體是客製 plugin、中介欄位填不到內容的
> 表單（採購單）改走網頁完整填單，其餘走 SOAP 中介。這由 `ops/web_apply/registry.py`（設計期靜態登錄
> form→handler）決定，同樣對使用者透明；細節見 [web-apply-design.md](web-apply-design.md)、兩軸關係見
> [architecture.md](architecture.md)。

為什麼在設計期決定，而不是執行期「SOAP 失敗就自動退網頁」？有兩個理由。

第一，安全性靠的是開發者的明確判斷，而不是執行期去猜錯誤的性質。UOF 的 SOAP 對很多狀況都只回一個
HTTP 500——token 過期是 500、版本不符是 500、對自由流程呼叫 `sign_next` 也是 500。如果規則寫成
「看到 500 就退網頁」，業務錯誤會被誤判成「該退」，於是用另一條路給出不一致、甚至錯誤的資料。由懂語意
的人逐工具決定走哪條，就沒有這個風險。

第二，寫入操作永遠不退、也不重試。`apply_form`、`terminate_task`、`sign_next` 只綁 SOAP，因為它們會
改變伺服器狀態、不能重放——換一條路重做，可能變成重複送單或重複結案。這是刻意的決定。

這樣做也比較好預測、好測試：讀一眼 `BINDING` 就知道每個工具走哪條，行為不會隨執行期狀況改變。

要說清楚這套設計的邊界：它解決的是「**結構性、永久的** SOAP 做不到」（某功能 SOAP 根本沒有 API）。
它**不**處理「執行期、環境性的 SOAP 失效」——例如 PublicAPI 暫時當掉，或某個站台根本沒裝 PublicAPI。
那種情況設計期無從預知，要靠執行期 fallback 才接得住，而那是另一筆未來工作（見文末「機制覆蓋現況」）。

## 逐工具實作對照

| 工具 | 機制 | 背後實際呼叫 | 異動 |
|---|---|---|:-:|
| `check_auth` | Web Service（+網頁暖機） | `Authentication.asmx` 的 `GetToken`；**並順手登入網頁、快取 cookie** | 否 |
| `get_form_list` | Web Service | `Wkf.asmx` 的 `GetFormList` | 否 |
| `get_external_form_list` | Web Service | `Wkf.asmx` 的 `GetExternalFormList` | 否 |
| `get_form_structure` | Web Service | `Wkf.asmx` 的 `GetFormStructure` | 否 |
| `get_form_structure_by_id` | Web Service | `Wkf.asmx` 的 `GetFormStructureByFormId` | 否 |
| `preview_workflow` | Web Service | `Wkf.asmx` 的 `SimulationFlowByScript` | 否 |
| `apply_form` | Web Service／網頁＊ | `Wkf.asmx` 的 `SendForm`（含明細 DataGrid）；＊採購單依 registry 改走網頁填單 | 是 |
| `get_task_data` | Web Service | `Wkf.asmx` 的 `GetTaskData` | 否 |
| `get_task_result` | Web Service | `Wkf.asmx` 的 `GetTaskResult` | 否 |
| `terminate_task` | Web Service | `Wkf.asmx` 的 `TerminateTask`（送出前先 `GetTaskData` 擋已結案） | 是 |
| `sign_next` | Web Service | `Wkf.asmx` 的 `SignNext2`（自由流程不支援，回 HTTP 500） | 是 |
| `query_forms` | 網頁 | 開 `MyFormList.aspx?item=FormQuery`，填日期＋關鍵字、scrape RadGrid | 否 |

SOAP 工具的業務邏輯在 `domains/wkf/service.py`；`SoapBackend`（`ops/soap.py`）只是把它接到機制上。

## 兩條認證（同一組帳密、各自獨立、用到才登入）

| | Web Service（token） | 網頁（session） |
|---|---|---|
| 怎麼來 | RSA 加密帳密 → `GetToken` | 表單 POST `Login.aspx` |
| 存哪 | `~/.uof/credentials-<帳號>-<hash>.json` | `~/.uof/storage_state-<帳號>-<hash>.json`（cookie） |
| 失效處理 | 呼叫回 HTTP 500 → 強制重新 `GetToken` 重試一次（`SoapBackend._call`） | 中途被導回 Login → 強制重登重試一次（`WebBackend._call_web`） |
| 程式 | `auth/token.py`（`get_token_provider()`） | `auth/session.py`（`get_session_provider()`） |

兩者都是**單一系統身份**（一個程序 = 一個 `UOF_ACCOUNT`），惰性建立、各自快取，互不污染。

### 入口認證閘：跟著工具的機制走（`require_auth`）

工具入口的 `@require_auth`（`auth/base.py`）**依「該工具設計綁定的機制」驗對應認證**，而非一律驗 SOAP token：

- 取得該工具可走的機制清單（`ops.router.mechanisms_for`，SOAP 優先），逐一驗其 provider，
  **任一通過即放行（OR）**；全部不過才回固定失敗訊息。對使用者只有「通過與否」。
- 因此 `query_forms`（綁 web）驗的是 **session**，**不需 SOAP token**；`apply_form` 等（綁 soap）驗 token。
  兩條認證彼此獨立——SOAP 拿不到 token 時，走 web 的工具不受影響。
- 驗證只集中在這一道入口閘，工具內部不重複驗證；機制本身的失效重試（token 自動刷新 / web 重登）
  仍各自在 backend 處理。
- 目前每個工具單一機制（`mechanisms_for` 回單一）；未來某工具若可 fallback（SOAP→web），在
  `mechanisms_for` 回多個、SOAP 在前，入口閘的 OR 即自動變成「token 不行就看 session」。

> 歷史 bug（已修）：先前 `require_auth` 固定驗 SOAP token，導致 web 綁定的 `query_forms` 在沒有
> PublicAPI / 拿不到 token 時，進 router 前就被 🔒 擋掉。離線護欄見 `tests/smoke/test_auth_binding.py`。

### 一個工具「可用」的定義（給 reviewer 的提醒）

一個工具是否可用，看的是它綁定的機制裡有沒有任一條認證通過（OR）：只要通過一條就放行，全部都不過才回
不可用。對使用者來說只有「能不能用」，不分 token 還是 session。

由此衍生一個常被誤判成 bug 的情況，請不要再回報一次：**某個綁 SOAP 的工具，在沒有 PublicAPI 的站台上
不可用，是正確的、不是缺陷。** 那個工具本來就是 SOAP 工具，而那種站台沒有 SOAP，所以它回不可用剛好對。
同樣地，`WebBackend` 裡那些沒被 `BINDING` 指到的 `scrape_*` 方法（`get_form_list`、`get_task_data` 等的
網頁版）是刻意保留、尚未綁定的，留給未來逐工具補 fallback 用，並不是「本該可達卻被擋住」的能力。

要讓這些工具在無 PublicAPI 時也能用，是一筆明確的未來工作（做法見文末「機制覆蓋現況」），不是修 bug。

### `check_auth` 的行為（就緒檢查）

`check_auth` **分別、獨立**回報兩條認證的狀態，**不短路**：

1. 真實打一次 `GetToken` 驗證 **Web Service（token）**（不是只看本地快取 TTL）。
2. 不論 token 成功與否，都**獨立**登入網頁、暖好 cookie，回報 **網頁（session）** 狀態（best-effort）。

> 因此 `check_auth` 訊息有兩段：token 就緒與否 + session 就緒與否。即使 token 失敗，只要 session 正常，
> 使用者就知道走 web 的工具（`query_forms`）仍可用。

## `query_forms` 抓取流程與所需資源

流程（`ops/web.py`：`WebBackend.query_forms` → `WebRuntime.search_forms`）：

1. 啟動無頭 Chromium，載入快取 cookie（storage_state）。
2. 若未登入 → POST `Login.aspx`（帳密來自 env），存回 cookie。
3. 開 `MyFormList.aspx?item=FormQuery`，設定 Telerik RadDatePicker 日期區間（隱藏 + 顯示雙 input）
   與關鍵字，送出查詢。
4. 解析 RadGrid 每一列 → TaskId / 單號 / 狀態 / 申請人 / 申請時間 / 結案時間。

所需資源/設定：

- `uv run playwright install chromium`（只需一次）。
- `UOF_BASE_URL` / `UOF_ACCOUNT` / `UOF_PASSWORD`；`~/.uof` 可寫（存 cookie）。
- 依賴 UOF 網頁的頁面與選擇器——**站台換版/換佈景可能要調整**（這是網頁機制的本質脆弱點）。
- Playwright 跑在單一專屬 worker thread，與 FastMCP 的 asyncio loop 隔離。

## 網頁工具的 retry 機制

網頁機制對應 SOAP 的「token 失效自動刷新」，提供對等的韌性：`WebBackend._call_web(fn)` 跑網頁操作，
若**中途被導回 Login（快取 cookie 通過初檢卻已在伺服器端過期）或拋例外**，就呼叫
`WebRuntime.force_relogin()` 強制重登後**重試一次**，使用者無感。新增網頁工具時一律經 `_call_web`
呼叫，即可免費獲得此 retry。

## 怎麼新增一個工具（可直接 follow）

1. **實作機制**
   - 走 **Web Service**：在 `domains/wkf/service.py` 加函式（`uof_client.call(endpoint_path=WKF_ENDPOINT,
     method_name="<ASMX method>", params={...})` 打 API、解析回傳字串），再在 `ops/soap.py` 的
     `SoapBackend` 加方法，經 `self._call(...)` 呼叫它（`_call` 已含 token 失效重試）。
   - 走 **網頁**：在 `ops/web.py` 的 `WebRuntime` 加 scrape 方法（Playwright 操作頁面），再在
     `WebBackend` 加方法，經 `self._call_web(...)` 呼叫它（`_call_web` 已含 session 失效重登重試）。
2. **宣告介面**：在 `ops/base.py` 的 `OpsBackend` 加上這個 `@abstractmethod`。
3. **登記綁定**：在 `ops/router.py` 的 `OpsRouter` 加同名方法 `return self._route("<name>", ...)`，
   並在 `BINDING` 標記它走 `"soap"` 還是 `"web"`。
4. **對外暴露**：在 `server.py` 加一個 `@mcp.tool` 的 `uof_custom_<name>`，內部 `return get_backend().<name>(...)`，
   並寫清楚 docstring（何時用、限制；**不要**提機制/模式）。

> 護欄：`tests/smoke/test_routing.py` 會斷言「`BINDING` 鍵集 == `OpsBackend` 抽象方法集」，所以漏掉
> 第 2 或第 3 步會在 smoke 直接變紅。完成後跑 `uv run python tests/run.py smoke`，真實行為再跑 `mounted`。

## 加 web fallback 時：各工具對應的 UOF 網頁端點（**紀錄處**）

要為某個目前綁 SOAP 的工具加上網頁 fallback（或在無 PublicAPI 站台改走網頁），第一步是**找出能重現
該功能的 UOF 網頁端點**。這張表就是**紀錄處**——有需要時在這裡補上端點與實作狀態。
（`✅ 已有 scrape`＝`WebBackend` 內已有對應方法、只是 `BINDING` 沒指向；`❌`＝尚未有/待調研。）

| 工具 | SOAP method | 對應網頁端點 | 網頁實作狀態 |
|---|---|---|---|
| （登入/認證） | `GetToken` | `Login.aspx` | ✅ 已用（session 登入） |
| `query_forms` | （無 API） | `MyFormList.aspx?item=FormQuery` | ✅ 已綁 |
| `get_form_list` | `GetFormList` | `MyFormList.aspx?item=FormQuery` | ✅ 已有 scrape（未綁） |
| `get_form_structure` | `GetFormStructure` | `AddFormScript.aspx` | ✅ 已有 scrape；已對 registry 命中的網頁起單表單回 handler schema |
| `get_form_structure_by_id` | `GetFormStructureByFormId` | `AddFormScript.aspx` ＋ `ApplyFormList.aspx`（formId↔version 對照） | ✅ 已有 scrape；已對 registry 命中的網頁起單表單回 handler schema |
| `get_task_data` | `GetTaskData` | `FormPrint.aspx` | ✅ 已有 scrape（未綁） |
| `get_task_result` | `GetTaskResult` | `ViewFormTemp.aspx` | ✅ 已有 scrape（未綁） |
| `get_external_form_list` | `GetExternalFormList` | （無對應網頁：admin 後台 DB 旗標，前端看不到） | ❌ 無對應 |
| `preview_workflow` | `SimulationFlowByScript` | 起單頁試填到送出前 | ✅ 對 registry 命中的網頁起單表單以 dry-run 試填代替流程預覽 |
| `apply_form`（寫入） | `SendForm` | 起單頁（`ApplyFormList.aspx` → `AddFormScript.aspx` / `FirstSite.aspx` 流程） | ✅ 對 registry 命中的採購單系列已實作 web handler；其餘表單仍走 SOAP |
| `terminate_task`（寫入） | `TerminateTask` | 表單詳細頁「強制結案/作廢」按鈕 | ❌ 待實作；**寫入不開自動 fallback** |
| `sign_next`（寫入） | `SignNext2` | 固定流程簽核 UI | ❌ 待實作；**寫入不開自動 fallback** |

> 網頁端點常數集中在 `ops/web.py` 頂部（`FORM_PRINT_PATH` / `FORM_QUERY_PATH` / `VIEW_FORM_TEMP_PATH` /
> `APPLY_FORM_LIST_PATH` / `ADD_FORM_SCRIPT_PATH`）；`Login.aspx` 登入在 `auth/session.py` + `ops/web.py`。
> 讀取類的網頁實作其實已經存在（`WebBackend.scrape_*`）。要啟用 fallback，主要是把 `BINDING` 改成有序鏈，
> 再於 router 補上「只在結構性錯誤時退、業務錯誤不退」的判斷（理由見「設計原則」一節的邊界說明）。

## 機制覆蓋現況

- **Web Service**：涵蓋 SOAP 可完成的查詢、起單與簽核/結案工具；一般表單的 `apply_form` 仍走 SOAP 中介。
- **網頁**：`query_forms` 已綁定；`apply_form` / `preview_workflow` / `get_form_structure(_by_id)` 對
  `ops/web_apply/registry.py` 命中的表單（目前採購單系列）走 web handler；另有多個讀取類 scrape 方法已實作但未綁成 fallback（見上表）。
- **未來：支援無 PublicAPI 或 PublicAPI 不穩的站台。** 這是一筆明確的逐工具工作，分兩步，不是修 bug：

  1. **認證層**：在 `BINDING` 把可行的讀取類工具改綁成有序的 `("soap", "web")`（SOAP 在前）。
     一旦 `mechanisms_for` 回多個機制，入口認證閘的 OR 就會自動變成「token 不行就看 session」。
  2. **執行層**：同時把執行的 fallback 也接上。目前 `_route` 只走單一機制，SOAP 失敗時還不會改打網頁；
     認證已經 fallback-ready、執行還沒，兩步必須一起做，否則會出現「認證過了、操作卻失敗」。

  另外，`WebBackend` 那些讀取 scrape 目前是 alpha、能力不完整（例如 `get_task_data` 拿不到簽核結果與
  結案日），綁定前要先確認完整度。寫入類不做自動 fallback；`apply_form` 只對 registry 明確登錄的
  表單走對應 web handler，`terminate_task` / `sign_next` 仍只走 SOAP。
