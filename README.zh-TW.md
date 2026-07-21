# mcp-uof

**UOF (U-Office Force) 一代平台的 MCP Server 實作**

透過 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 將 UOF 的電子簽核等操作封裝為語意化的 AI 工具，**全程以 httpx 網頁自動化實作**，讓 Claude、VS Code 等 MCP 客戶端能直接以自然語言操作企業協作功能。

## 本版本範圍

本版本提供**電子簽核（WKF）**模組，透過 stdio 連線供 Claude Desktop、VS Code 等 MCP 客戶端使用，涵蓋表單查詢、流程預覽、起單、進度追蹤、簽核與結案。

### 機制對使用者透明（工具是唯一面向）

對外只有一組固定工具，底層以 httpx + lxml 操作 UOF 網頁端點，不需要瀏覽器 runtime。MCP Server 提供表單清單、欄位解析、通用起單、查詢、簽核與結案；特定表單的填寫 SOP 與業務驗證由部署端私有 skill 組合。詳見 [docs/architecture.md](docs/architecture.md)。

其他特點：

- **不需瀏覽器 runtime**：全部以 httpx + lxml 實作；在 musl/Alpine 環境部署時，仍需確認相依套件有 binary wheel 或備妥原生建置工具。
- **單一身份模型**：一個 Server 程序代表一位 UOF 使用者，身份在設定時綁定（見[身份模型](#身份模型)）。
- **認證**：以帳密登入 `Login.aspx` 取得 cookie session；登入失敗回固定的設定檢查提示。

### 起單能力範圍

起單由同一個 `apply_form` 完成，支援通用欄位型別（文字、自動編號、可空欄位、日期、單選/下拉、通用 dataGrid 明細）。實際申請身份固定為 `UOF_ACCOUNT`；目前工具簽名中的 `applicant_account` 與 `first_signer_account` 尚未改變身份或派單路徑，需要指定首站簽核者時請使用 UOF Web UI。本 repo 不內建特定表單的業務 SOP，欄位組合與檢查應由部署端的私有 skill 依表單結構與業務規則決定。

> 重要：一般網頁起單可填的欄位以 `get_form_structure_by_id` 回傳者為準；若某欄位漏掉，應先視為解析器待修問題。通用 dataGrid 明細已支援，附件上傳、多站/並簽會簽尚未支援。

## 快速開始

```bash
uv sync
cp .env.example .env   # 填入 UOF_BASE_URL、UOF_ACCOUNT、UOF_PASSWORD

uv run mcp-uof         # 以 stdio 啟動 MCP Server
```

設定細節見 [docs/configuration.md](docs/configuration.md)。

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
        "UOF_ACCOUNT": "your_account",
        "UOF_PASSWORD": "your_password"
      }
    }
  }
}
```

範例檔見 [examples/](examples/)。

### 身份模型

UOF 一代以帳密登入，沒有代表個別使用者的 OAuth。因此 `env` 區塊中的 `UOF_ACCOUNT` 就是這份設定的操作身份——這個 Server 的所有工具呼叫都以該帳號送出。要以另一個人的身份操作，請新增一份帶不同帳號的server entry；切換身份就是切換設定。完整說明見 [docs/integration.md](docs/integration.md)。

## MCP Tools 總覽（17 個）

所有 Tool 名稱使用 `uof_custom_` 前綴。

| Domain | Tools |
| --- | --- |
| System | `check_auth` |
| WKF 電子簽核 | `get_form_list`, `get_external_form_list`, `query_forms`, `get_pending_sign_list`, `search_users`, `get_form_structure`, `get_form_structure_by_id`, `get_dialog_structure`, `search_dialog_options`, `operate_dialog`, `preview_workflow`, `apply_form`, `get_task_data`, `get_task_result`, `sign_next`, `terminate_task` |

**完整工具規格、人員角色模型、使用情境與能力邊界，請見 [docs/tools.md](docs/tools.md)（導入前必讀）。**

幾個關鍵邊界先說在前面：

- 待簽清單用 `get_pending_sign_list`（回「目前輪到本帳號待簽」的單，含 TaskId/SiteId/NodeSeq）；`query_forms` 則查「自己送出/簽過」的單（依日期，`query_mode` 選 apply/sign），兩者是不同集合
- 複合欄位（明細、供應商挑選等）藏在**對話框**裡：用 `get_dialog_structure` 看內部控制項、`search_dialog_options` 查真實候選（勿捏造代碼），再經 `apply_form` 的 `_lookups`/`_rows` 等保留鍵帶入；`operate_dialog` 僅供探測、不能累積明細列
- `terminate_task`：`Cancel` 撤單作廢（走網頁表單取回頁）、`Adopt`/`Reject` 走網頁簽核流程同意/否決；對已結案的單會被工具層攔截、不重複結案
- `preview_workflow`（流程模擬）目前不提供 httpx 版，會提示改於網頁操作；可直接 `apply_form` 起單後用 `get_task_result` 看實際簽核路徑
- `get_external_form_list` 目前只回傳能力說明，不會列出後台的「非線上使用」表單

## 測試

兩層測試法（smoke / mounted），統一入口：

```bash
uv run python tests/run.py smoke     # 離線：import 探索、工具→機制綁定（CI 可跑）
uv run python tests/run.py mounted   # 真實掛載 MCP：真 stdio 子程序、多身份（需 .env）
uv run python tests/run.py all       # 兩層依序（缺 .env 時真實層自動 skip）
```

各層定義與測試紀律見 [docs/testing.md](docs/testing.md)。

## 文件

- [安裝與綁定教學](docs/integration.md) — Claude Desktop / VS Code Chat 手動綁定、身份切換
- [實際操作對話](docs/example-session.md) — 一段模擬工具呼叫與回應範例
- [表單需具備的系統設定](docs/form-requirements.md) — 表單要能被 MCP 完整操作的後台前提
- [設定](docs/configuration.md) — 環境變數
- [架構](docs/architecture.md) — Domain 分層、httpx 網頁機制、身份模型
- [實作設計](docs/design.md) — 每個工具背後怎麼做、認證、新增工具步驟（持續更新）
- [工具參考](docs/tools.md) — 17 個工具規格、使用情境、能力邊界
- [測試](docs/testing.md) — 兩層測試法與測試紀律

## 授權

MIT License
