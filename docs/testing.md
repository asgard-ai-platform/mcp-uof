# 測試

mcp-uof 採「兩層測試法」，刻意維持可執行腳本風格（不使用 pytest）。各層定義與檔案結構的權威說明在 [tests/README.md](../tests/README.md)；本文是公開文件版的概覽與紀律。

統一入口：

```bash
uv run python tests/run.py smoke     # Tier 1：離線，可進 CI
uv run python tests/run.py mounted   # Tier 2：真實掛載 MCP（真 stdio 子程序）
uv run python tests/run.py all       # 兩層依序（缺 .env 時真實層自動 skip）
```

## 兩層

| 層 | 路徑 | 是否需真實環境 | 涵蓋 |
| --- | --- | --- | --- |
| **Smoke** | `tests/smoke/` | 否（CI 可跑） | 模組可匯入、工具綁定、認證閘、session／瀏覽器登入，以及 plugin 明細解析、postback 順序、必填與錯誤訊息 |
| **Mounted** | `tests/mounted/` | 是 | 真實掛載 MCP：工具註冊護欄 + 單一身份認證 + 唯讀查詢 + 登入態管理 |

> 瀏覽器登入需要真人操作，不在自動化覆蓋範圍：代理行為由 smoke 層對假 upstream 驗證，真實 UOF 登入頁的渲染需人工確認一次（`UOF_LOGIN_DEBUG=1` 可看逐筆代理請求）。
> mounted 會把 `HOME` 指到暫存目錄以隔離 session 存檔（預設在 `~/.uof`）——負向認證段落尤其依賴這點。

### Mounted（真實掛載 MCP）定義

與 Claude Desktop / VS Code 在 `mcp.json` 綁定的路徑一致： `StdioServerParameters(command=sys.executable, args=["-m","mcp_uof.server"], env=身份, cwd=repo根)` → 官方 SDK `stdio_client` + `ClientSession` → `initialize` → `list_tools` → `call_tool`。身份只由注入的 `env` 決定（一份設定 = 一個身份）。前提：stdio 下 server 不得寫任何東西到 stdout（診斷一律走 stderr），否則污染 JSON-RPC。

## 測試紀律（真實層）

- 只使用 `.env` 的 `UOF_ACCOUNT` / `UOF_PASSWORD` 單一身份。
- 只執行認證、工具註冊與唯讀查詢，不建立、簽核、撤回或結案表單。
- 不依賴部署端表單名稱、欄位 schema 或流程角色。
- 真實主機名與帳密只放在未入庫的 `.env`，斷言不硬編環境值。

修改後的公開 repo 回歸：`smoke` → `mounted`。部署端客製表單的寫入驗證由部署端測試工具負責。

## 能力與邊界

- 「我有哪些待簽的單」：用 `get_pending_sign_list`（首頁待簽 widget，回含 TaskId/SiteId/NodeSeq）；`query_forms` 則列自己送出/簽過的單。兩者 server 內部皆以網頁取得。
- 多站流程的逐站簽核與留意見、並簽/會簽：目前僅 `sign_next`（自由流程單站同意）與 `terminate_task`（撤單/同意/否決）。
- `preview_workflow`（流程模擬）目前不提供 httpx 版，會提示改於網頁操作。
