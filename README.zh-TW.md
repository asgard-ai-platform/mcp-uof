# mcp-uof

**UOF (U-Office Force) 一代平台的 MCP Server 實作**

透過 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 將 UOF 的
SOAP/ASMX WebService 封裝為語意化的 AI 工具，讓 Claude、VS Code 等 MCP 客戶端
能直接操作電子簽核等企業協作功能。

## 本版本範圍

本版本提供**電子簽核（WKF）**模組，透過 stdio 連線供 Claude Desktop、VS Code 等 MCP 客戶端使用，
涵蓋表單查詢、流程預覽、外部起單、進度追蹤、簽核與強制結案。

### 機制對使用者透明（工具是唯一面向）

對外只有一組固定工具。每個工具底層用 SOAP/PublicAPI 還是 httpx 爬網頁取得資料，是
**開發期決定、對使用者透明**的實作細節——使用者（與 agent）不選、也看不到機制。原則：SOAP 能做的就
用 SOAP；SOAP 沒有該能力的才用 httpx web 補——目前是 `query_forms`（列清單/搜尋，PublicAPI 無此 API）、
`get_form_structure` 系列（表單欄位結構）、以及**起單時的特定表單**：採購單本體是客製 plugin、中介欄位
填不到內容，故 `apply_form` 對它內部改走網頁填單；其餘表單走 SOAP。哪張表單走網頁是設計期靜態登錄
（`ops/web_apply/registry.py`），使用者只挑表單、呼叫同一個 `apply_form`，
**不會遇到「用 web 還是 SOAP 起單」的選擇**。沒有使用者可選的「模式」。詳見 [docs/architecture.md](docs/architecture.md)。

其他特點：

- **Alpine Linux 相容**：網頁功能改以 httpx + lxml 實作，不需 Playwright 或 Chromium，可在 musl/Alpine 環境直接部署。
- **單一身份模型**：一個 Server 程序代表一位 UOF 使用者，身份在設定時綁定（見[身份模型](#身份模型)）。
- **認證跟著工具的機制走**：SOAP 工具用 RSA Token（含失效自動刷新）、httpx web 工具用 cookie session，彼此獨立、由用到的機制惰性取得；httpx web 工具因此**不需 SOAP token**（無 PublicAPI 站台也能用）。登入失敗回固定的設定檢查提示。

### 起單能力範圍

起單分兩條內部路線，依表單自動分派（對使用者透明，都經同一個 `apply_form`）：

- **SOAP 中介起單**（原生設計的表單）：單站自由流程（指定一位第一站簽核者）＋基本欄位型別（文字、
  自動編號、可空欄位、日期、單選、不帶檔案的附檔欄位）＋**明細（DataGrid，以列清單帶入）**。
  尚未支援：實際附檔上傳、多站流程與並簽/會簽、固定流程逐站推進。
- **網頁起單**（本體是客製 plugin、中介欄位填不到內容的表單，如採購單）：以 httpx + lxml 爬網頁
  完整填單送出（主旨/供應商/明細等）；較依賴頁面結構，簽核者由表單自身流程決定。

> 重要：可填的欄位僅限該表單**對外開放的中介欄位**（即 `get_form_structure_by_id` 回傳的欄位），
> 這可能少於 UOF 網頁上看到的完整表單。若網頁欄位未在後台對應為中介欄位，API 便無法填，
> 且起單時不會驗證網頁必填欄位——可能起單成功但內容不完整。需要完整填寫請於 UOF 網頁操作。

UOF 的起單 API 本身是通用的（單一 `SendForm` 端點），因此本服務以單一通用起單工具設計，
擴展方向是支援更多欄位型別與流程型態，而不是為每種表單新增工具。

## 快速開始

```bash
uv sync
cp .env.example .env   # 填入 UOF_BASE_URL、UOF_APP_NAME、UOF_RSA_PUBLIC_KEY、UOF_ACCOUNT、UOF_PASSWORD

uv run mcp-uof         # 以 stdio 啟動 MCP Server
```

設定細節（含 RSA 金鑰產生流程）見 [docs/configuration.md](docs/configuration.md)。

### MCP Client 設定（Claude Desktop / VS Code）

完整綁定與身份切換教學見 **[docs/integration.md](docs/integration.md)**。最小範例：

```json
{
  "mcpServers": {
    "uof": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": {
        "UOF_BASE_URL": "https://your-uof-domain.com/VirtualPath",
        "UOF_APP_NAME": "your_app_name",
        "UOF_RSA_PUBLIC_KEY": "your_rsa_public_key_base64",
        "UOF_ACCOUNT": "your_account",
        "UOF_PASSWORD": "your_password"
      }
    }
  }
}
```

範例檔見 [examples/](examples/)。

### 身份模型

UOF 一代採單一系統身份（RSA 帳密），沒有代表個別使用者的 OAuth。因此 `env` 區塊中的
`UOF_ACCOUNT` 就是這份設定的操作身份——這個 Server 的所有工具呼叫都以該帳號送出。
要以另一個人的身份操作，請新增一份帶不同帳號的 server entry；切換身份就是切換設定。
完整說明見 [docs/integration.md](docs/integration.md)。

## MCP Tools 總覽（12 個）

所有 Tool 名稱使用 `uof_custom_` 前綴。

| Domain | Tools |
|---|---|
| System | `check_auth` |
| WKF 電子簽核 | `get_form_list`, `get_external_form_list`, `query_forms`, `get_form_structure`, `get_form_structure_by_id`, `preview_workflow`, `apply_form`, `get_task_data`, `get_task_result`, `terminate_task`, `sign_next` |

**完整工具規格、人員角色模型、使用情境與能力邊界，請見
[docs/tools.md](docs/tools.md)（導入前必讀）。**

幾個關鍵邊界先說在前面：

- UOF 一代**沒有待簽核清單 API**——TaskId 必須由使用者提供（UI 或通知信）
- API **沒有逐站簽核動作**；但**單站流程**中待簽主管可用 `terminate_task`（Adopt/Reject）
  達成簽核語意，歷程記錄與 UI 簽核等價
- `terminate_task` 在 API 端**無權限管控**且會覆寫已結案結果——工具層已加攔截，使用仍須節制

## 測試

三層測試法（smoke / e2e / mounted），統一入口：

```bash
uv run python tests/run.py smoke     # 離線：import 探索、工具→機制綁定（CI 可跑）
uv run python tests/run.py e2e       # 真實測試環境：服務層採購單劇本（需 .env）
uv run python tests/run.py mounted   # 真實掛載 MCP：真 stdio 子程序、多身份（需 .env）
uv run python tests/run.py all       # 三層依序（缺 .env 時真實層自動 skip）
```

各層定義與測試紀律見 [docs/testing.md](docs/testing.md)。

## 文件

- [安裝與綁定教學](docs/integration.md) — Claude Desktop / VS Code Chat 手動綁定、身份切換
- [實際操作對話](docs/example-session.md) — 一段模擬工具呼叫與回應範例
- [表單需具備的系統設定](docs/form-requirements.md) — 表單要能被 MCP 完整操作的後台前提
- [設定](docs/configuration.md) — 環境變數、RSA 金鑰流程
- [架構](docs/architecture.md) — Domain 分層、SOAP client、身份模型
- [實作設計](docs/design.md) — 每個工具背後怎麼做、兩條認證、新增工具步驟（持續更新）
- [工具參考](docs/tools.md) — 12 個工具規格、使用情境、能力邊界
- [測試](docs/testing.md) — 三層測試法與測試紀律

## 授權

MIT License
