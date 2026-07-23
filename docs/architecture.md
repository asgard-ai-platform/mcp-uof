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

只有一種認證：`SessionAuthProvider`（`mcp_uof/auth/`）。首次建立 `HttpSession` 時會嘗試登入，之後由工具入口與 HTTP redirect 處理 session 驗證或重登。

- **http_web → SessionAuthProvider**：`HttpSession`（`httpx.Client`）POST `Login.aspx` 取得 cookie；每次 GET/POST 若被重導至 Login.aspx 就自動重新登入後重試。同一程序內的複合操作不應並行交錯。

### 身份模型（單一身份，設定時綁定）

UOF 一代以帳密登入，每個 server process 使用一個固定的 UOF 使用者身份：

- **一個 MCP Server 程序 = 一個固定身份**，由 `UOF_ACCOUNT` 決定（寫在 MCP Host 設定的 `env` 區塊）。
- **要切換操作者 = 切換 MCP 設定**（不同 server entry 帶不同 `UOF_ACCOUNT`/`UOF_PASSWORD`）。
- session cookie 只保存在該程序的記憶體；程序重啟後會重新登入。不同 server entry 各自持有獨立 session。

> 可見與可操作的資料由該帳號在 UOF 中的權限決定。

登入失敗（帳密錯、連線設定錯等）一律回固定的失敗說明（`auth.base.auth_failure_message`），明確要使用者檢查設定，不讓 AI 自行臆測。

## 工具對照

17 個工具一律對外可用、一律走 http_web：

| 工具 | 說明 |
| --- | --- |
| check_auth / get_form_list / get_external_form_list | 網頁查詢 |
| get_form_structure(_by_id) | 即時解析起單頁得到的欄位結構 |
| get_dialog_structure / search_dialog_options / operate_dialog | 對話框欄位的內部結構／挑選器候選／填值按鈕探測 |
| preview_workflow | 流程模擬目前不提供，回「需在網頁操作」；可改用 apply_form + get_task_result |
| apply_form | 依欄位結構執行通用網頁起單（含對話框欄位） |
| get_task_data / get_task_result | 查單摘要＋欄位 / 逐站簽核歷程（ViewFormTemp 解析） |
| terminate_task | Cancel＝作廢（FormGetBack）；Adopt/Reject＝走網頁簽核流程 |
| sign_next | 自由流程單站同意（SignNodeForm → SendOtherSite/OtherSiteSend） |
| get_pending_sign_list | 目前輪到本帳號待簽的單（首頁待簽 widget） |
| query_forms / search_users | 查詢自己送出/簽過的單 / 查人員 |

## Package Layout

```text
mcp-uof/
├── src/mcp_uof/
│   ├── server.py        # MCP Server 入口，註冊 uof_custom_* 工具，派發到 get_backend()
│   ├── ops/             # 操作面：router(BINDING)、base(協定)、http_web(httpx+lxml)
│   └── auth/            # 認證（機制前提）：base、session
├── tests/               # 兩層測試：smoke（離線）/ mounted（真實掛載 MCP）
└── docs/
```
