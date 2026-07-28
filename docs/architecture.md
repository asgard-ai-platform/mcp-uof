# Architecture

`mcp-uof` 是以 Python 實作的 MCP Server，把 UOF 一代平台封裝為語意化的 AI 工具。

## 核心概念：工具是唯一對外面向，機制對使用者透明

對外只有一組固定的 MCP 工具（`uof_custom_*`）。**每個工具底層怎麼跟 UOF 溝通，是開發期就決定、對使用者完全透明的實作細節**——使用者（與 agent）只面對「有哪些工具」，不選、也看不到機制。

UOF 一代 MCP **全面走單一機制**：

| 機制 | 怎麼操作 | 認證 |
| --- | --- | --- |
| http_web | httpx + lxml 打 UOF 的 aspx/ashx 網頁端點（同步整頁 postback） | session：Login.aspx 登入 + cookie |

> 沒有使用者可選的執行模式。`ops/router.py` 的 `BINDING` 用來登記每個工具目前採用的機制，現階段全部為 `http_web`。

## Runtime Flow

```text
MCP client (Claude Desktop / VS Code)
  -> mcp_uof.server                 FastMCP 工具（uof_custom_*），定義固定
  -> mcp_uof.ops.get_backend()      永遠回傳同一個 OpsRouter
  -> OpsRouter                          目前委派給 HttpWebBackend
       └─ HttpWebBackend -> HttpSession (httpx + lxml) -> UOF 網頁（aspx / ashx）
```

工具層只呼叫 `get_backend().<method>(...)`，從不直接碰機制實作。

## 起單與表單組合邊界

所有工具都走 http_web。MCP server 只負責基本操作：列表單、解析欄位、填寫可解析欄位、送出、查詢、簽核與結案。

特定表單的業務 SOP、語意欄位、送出前檢查與送出後驗證不內建在本 package；應由外部 agent/skill 依 `get_form_structure_by_id` 的結果與業務規則組合。若某張表單的欄位藏在特殊 JS/plugin 對話框中，通用解析可能無法完整涵蓋，應在外部 skill 或 UOF Web UI 流程中處理。

## 認證（機制的前提，非使用者選項）

只有一種認證機制：`Login.aspx` 的 cookie session，由 `SessionAuthProvider`（`mcp_uof/auth/`）管理。
取得那個 cookie 有三段來源，依序嘗試：

| 順序 | 來源 | 條件 |
| --: | --- | --- |
| 1 | session 存檔（`auth/store.py`，位置由 `UOF_SESSION_DIR` 決定，預設 `~/.uof`） | 存檔存在且探測 `Homepage.aspx` 沒被踢回 `Login.aspx` |
| 2 | 帳密自動登入 | `UOF_ACCOUNT` + `UOF_PASSWORD` 都有設（CI／`tests/mounted/` 走這條） |
| 3 | 瀏覽器登入（`auth/browser_login.py`） | 前兩者都不成立 → 要求呼叫 `uof_custom_login` |

`HttpTransport`（`httpx.Client`）持有 cookie jar；安全讀取若被導至 Login.aspx，會重新取得 session
後重試一次。起單、明細、簽核與作廢等不可重放寫入不會自動重送，而是回報未完成或未確認。
同一程序內的複合操作由 session lifecycle operation lease 序列化。

### 瀏覽器登入：為什麼是反向代理

瀏覽器裡的 cookie MCP 程序拿不到，而 UOF 的 session cookie 是 HttpOnly，頁面 JS 也讀不到。
所以 `uof_custom_login` 在 `127.0.0.1` 起臨時反向代理：

```text
使用者的瀏覽器 ──> 127.0.0.1:<隨機port>（代理）──> HttpSession._client ──> UOF Login.aspx
                                                        └── Set-Cookie 落在這裡
```

畫面是真的登入頁（AD 認證、驗證碼、隱藏欄位都自然相容），但請求由本程序發出，cookie 天生就屬於
我們，也不會踩到 session 綁 UA／IP 的問題。安全邊界見 `auth/browser_login.py` 的模組 docstring。

> 目前部署沒有外部 SSO，代理只轉發同 host 請求。日後若導入跨網域 SSO（如 ADFS）需改為網域白名單。

### 身份模型（單一身份，程序層綁定）

每個 server process 使用一個固定的 UOF 使用者身份：

- **一個 MCP Server 程序 = 一個身份**。走帳密備援時由 `UOF_ACCOUNT` 決定；走瀏覽器登入時由**實際登入的人**決定。
- **實際身份怎麼知道**：登入表單經過本機代理時取出帳號欄位（只取帳號，不碰密碼）。所有身份顯示與
  session 存檔歸屬都以它為準——**不會退回 `UOF_ACCOUNT`**，因為使用者可能登入了另一個人。
- **要換身份**：呼叫 `uof_custom_logout` 後重新 `uof_custom_login`（或 `uof_custom_login(force=True)`）；
  帳密備援模式則是改設定、換 server entry。
- session cookie 存在程序記憶體，並依「站台＋實際登入帳號」分別存到 `UOF_SESSION_DIR`
  （預設 `~/.uof`，`0600`），重啟免重登。多身份共機的定位規則見 [configuration.md](configuration.md)。

> 可見與可操作的資料由該身份在 UOF 中的權限決定。

**身份不會在程序中途改變**：一旦以瀏覽器登入，session 失效時**不會**改用 `UOF_ACCOUNT` 的帳密自動
重登（那會讓操作身份悄悄從實際登入者變回設定值），而是再次要求瀏覽器登入。實際登入者與
`UOF_ACCOUNT` 不同時，`login` / `check_auth` 會主動警告。

登入失敗分兩類，訊息刻意分開，不讓 AI 混為一談：

- **尚未登入** → `auth.base.browser_login_required_message`（🔑）：要 AI 去呼叫 `uof_custom_login`，
  並明確禁止向使用者索取帳密。
- **設定層級失敗**（連線錯、備援帳密錯）→ `auth.base.auth_failure_message`（🔒）：要使用者檢查設定，不讓 AI 自行臆測。

## 工具對照

19 個工具一律對外可用、一律走 http_web：

| 工具 | 說明 |
| --- | --- |
| login / logout | 開瀏覽器登入取得 session／清除記憶體與磁碟的 session |
| check_auth / get_form_list / get_external_form_list | 網頁查詢 |
| get_form_structure(_by_id) | 即時解析起單頁得到的欄位結構 |
| get_dialog_structure / search_dialog_options / operate_dialog | 對話框欄位的內部結構／挑選器候選／填值按鈕探測 |
| preview_workflow | 流程模擬目前不提供，回「需在網頁操作」；可改用 apply_form + get_task_result |
| apply_form | 依欄位結構執行通用網頁起單（含對話框欄位） |
| get_task_data / get_task_result | 查單摘要＋欄位 / 逐站簽核歷程（ViewFormTemp 解析） |
| terminate_task | Cancel＝作廢（FormGetBack）；Adopt/Reject＝走網頁簽核流程 |
| sign_next | 自由流程單站同意（SignNodeForm → SendOtherSite/OtherSiteSend） |
| get_pending_sign_list | 目前輪到本身份待簽的單（首頁待簽 widget） |
| query_forms / search_users | 查詢自己送出/簽過的單 / 查人員 |

## Package Layout

```text
mcp-uof/
├── src/mcp_uof/
│   ├── server.py        # MCP Server 入口，註冊 uof_custom_* 工具，派發到 get_backend()
│   ├── ops/             # 操作面：router(BINDING)、base(協定)、http_web(httpx+lxml)
│   └── auth/            # 認證（機制前提）：base、session、browser_login(反向代理)、store(session 落地)
├── tests/               # 兩層測試：smoke（離線）/ mounted（真實掛載 MCP）
└── docs/
```
