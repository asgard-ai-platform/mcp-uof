# Architecture

`mcp-uof` 是以 Python 實作的 MCP Server，把 UOF 一代平台封裝為語意化的 AI 工具。

## 核心概念：工具是唯一對外面向，機制對使用者透明

對外只有一組固定的 MCP 工具（`uof_custom_*`）。**每個工具底層用哪種「機制」取得資料，是開發期就
決定、對使用者完全透明的實作細節**——使用者（與 agent）只面對「有哪些工具」，不選、也看不到機制。

三種機制：

| 機制 | 怎麼操作 | 認證 |
|---|---|---|
| soap | 呼叫 SOAP / PublicAPI（`*.asmx`） | token：RSA 帳密 → GetToken |
| http_web | httpx + lxml 爬 UOF 網頁 | session：Login.aspx 登入 + cookie |
| web | Playwright 驅動 UOF 網頁（legacy，目前無工具綁定） | session：Login.aspx 登入 + cookie |

**綁定原則（開發者在實作工具時決定）**：能用 SOAP/PublicAPI 做的就用 SOAP；SOAP 沒有該能力的，才用
http_web 補。走 http_web 的有：`query_forms`（列清單/搜尋，PublicAPI 無此 API）、`get_form_structure`
系列；以及**起單時的特定表單**——採購單本體是客製 plugin、中介欄位填不到內容，故 `apply_form` 對它
改走網頁填單（見下「起單的兩軸分派」）。其餘走 SOAP。

> 沒有使用者可選的「模式」。也沒有「用 web 還是 SOAP 起單」的選擇——使用者只挑表單、呼叫工具，
> 走哪種機制是內部、對使用者透明的決定。舊版的 `UOF_OPS_MODE` / `UOF_AUTH_MODE` 已移除，設定也會被忽略。

## Runtime Flow

```text
MCP client (Claude Desktop / VS Code)
  -> mcp_uof.server                 FastMCP 工具（uof_custom_*），定義固定
  -> mcp_uof.ops.get_backend()      永遠回傳同一個 OpsRouter
  -> OpsRouter（依 ops/router.py 的 BINDING 靜態委派）
       ├─ SoapBackend      -> domains/wkf/service.py -> soap_client (lxml+httpx) -> UOF PublicAPI(*.asmx)
       ├─ HttpWebBackend  -> HttpSession (httpx+lxml) -> UOF 網頁（query_forms / get_form_structure / apply_form）
       └─ WebBackend      -> Playwright (legacy，目前無工具綁定)
```

工具層只呼叫 `get_backend().<method>(...)`，從不直接碰機制實作。

## 機制綁定（第一層決策點）

`mcp_uof/ops/router.py` 的 `BINDING` 是「哪個工具預設用哪種機制」的第一個決策點，開發期決定、寫死在程式裡：

```text
BINDING = { "query_forms": "web", 其餘 11 個工具: "soap" }
OpsRouter.<tool>()  -> 依 BINDING 委派到 SoapBackend 或 WebBackend
```

> 這裡的「其餘 11 個工具: soap」是工具層預設路由；`apply_form` 等起單類工具進入 server 後，
> 仍會再依 `ops/web_apply/registry.py` 做表單層分派。

- `SoapBackend` / `WebBackend` 是**機制實作**，惰性建立、各自取得所需認證（SOAP→token、web→session）。
- `BINDING` 管「工具層預設路由」；起單類工具另有「表單層分派」（見下節），用來處理同一個 `apply_form`
  下不同表單的填寫方式。

## 起單的兩軸分派（也是設計期決定，對使用者透明）

「機制」其實有兩個正交的設計期決策點，都不在 runtime 用 SOAP 去猜：

1. **每工具 → 預設機制**：`ops/router.py` 的 `BINDING`（上節）。query_forms→web、其餘→soap。
2. **每表單 → 起單方式**：`ops/web_apply/registry.py` 的登錄表。起單相關工具（`apply_form` /
   `preview_workflow` / `get_form_structure(_by_id)`）拿到 form id 後先查這張表：**登錄為「網頁起單」
   的表單（如採購單，本體是 `SWUnitechE_POMain` plugin）走 web handler 完整填單；其餘走 SOAP 中介起單。**
   `get_form_structure` 也據此回對應的「可填欄位說明」。使用者一律只呼叫同一個 `apply_form`，看不到差異。

> 為何兩軸：BINDING 解「同一個 op 用哪種傳輸」；registry 解「同一個 op(起單) 下、不同表單因本體型態
> 不同要用不同填法」。兩者都是設計期靜態登錄，不靠 runtime 偵測。

## 認證（機制的前提，非使用者選項）

每個機制各自宣告它需要哪種認證（`mcp_uof/auth/`），由用到該機制的工具惰性觸發；兩者共用同一身份。

- **SOAP → TokenAuthProvider**：RSA 加密 `UOF_ACCOUNT`/`UOF_PASSWORD` 呼叫 `Authentication.asmx` 的
  `GetToken`，記憶體＋磁碟雙層快取。**失效自動刷新**：Token 伺服器端有效期可能短於本地 TTL，失效時
  WKF 呼叫回 HTTP 500；`SoapBackend._call` 偵測到就 `fetch_token(force_refresh=True)` 重試一次。
- **http_web → SessionAuthProvider**：`HttpSession`（httpx.Client）POST `Login.aspx` 取得 cookie；
  每次 GET/POST 若被重導至 Login.aspx 就自動重新登入後重試。執行緒安全，可直接在任意執行緒呼叫。
- **web（legacy Playwright）→ SessionAuthProvider**：存為 Playwright storage_state；目前無工具綁定。

### 身份模型（單一身份，設定時綁定）

UOF 一代沒有代表個別使用者的 OAuth，所有機制都是**單一系統身份**：

- **一個 MCP Server 程序 = 一個固定身份**，由 `UOF_ACCOUNT` 決定（寫在 MCP Host 設定的 `env` 區塊）。
- **要切換操作者 = 切換 MCP 設定**（不同 server entry 帶不同 `UOF_ACCOUNT`/`UOF_PASSWORD`）。
- 憑證**以身份區分**快取：token 為 `~/.uof/credentials-<account>-<hash>.json`、
  session 為 `~/.uof/storage_state-<account>-<hash>.json`；切換帳號不互相污染。

> 後端 UOF 收到的是系統級身份，不會依操作者過濾資料。MCP 能碰到的資料，該帳號權限內都看得到。

登入失敗（帳密錯、站台無 PublicAPI 等）一律回固定的失敗說明（`auth.base.auth_failure_message`），
明確要使用者檢查設定，不讓 AI 自行臆測。

## 工具與機制對照

12 個工具一律對外可用；下表只說明各工具**內部由哪種機制完成**（對使用者透明）：

| 工具 | 機制 |
|---|---|
| check_auth / get_form_list / get_external_form_list | SOAP |
| get_form_structure(_by_id) | SOAP／web＊ |
| preview_workflow / apply_form | SOAP／web＊ |
| get_task_data / get_task_result | SOAP |
| terminate_task / sign_next（簽核·結案） | SOAP |
| query_forms（列清單/搜尋，補 SOAP 無待簽清單 API 的缺口） | web（內部 scrape） |

> ＊`get_form_structure(_by_id)` / `preview_workflow` / `apply_form` 會先查 `ops/web_apply/registry.py`。
> 登錄為網頁起單的表單（目前為採購單系列）走 web handler；其餘走 SOAP 中介。
> 讀取類 fallback（例如 `get_task_data` 從 SOAP 退到 web scrape）尚未啟用，屬未來逐工具補強。

## Domain Layout

SOAP 機制的業務邏輯仍依 Domain 分（對應 ASMX 端點）：

| Domain | ASMX 端點 | 工具 |
|---|---|---|
| `system` | Authentication.asmx | `check_auth` |
| `wkf` | Wkf.asmx | 電子簽核相關工具 |

## Package Layout

```text
mcp-uof/
├── src/mcp_uof/
│   ├── server.py        # MCP Server 入口，註冊 uof_custom_* 工具，派發到 get_backend()
│   ├── soap_client.py   # SOAP 請求組裝與回傳解析（SOAP 機制用）
│   ├── ops/             # 操作面：router(BINDING 綁定)、base(協定)、soap、http_web(httpx)、web(Playwright legacy)
│   ├── auth/            # 認證（機制前提）：base、token、session
│   └── domains/         # 業務邏輯：system、wkf（service.py）
├── tests/               # 三層測試：smoke（離線）/ e2e（服務層）/ mounted（真實掛載 MCP）
└── docs/
```
