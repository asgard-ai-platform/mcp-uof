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
| **Smoke** | `tests/smoke/` | 否（CI 可跑） | 模組可匯入（自動探索）、工具→機制綁定（一律 `http_web`）、認證閘（session） |
| **Mounted** | `tests/mounted/` | 是 | 真實掛載 MCP：工具註冊護欄 + 測試表單多身份全程 + 負向認證（深度保真） |

### Mounted（真實掛載 MCP）定義

與 Claude Desktop / VS Code 在 `mcp.json` 綁定的路徑一致： `StdioServerParameters(command=sys.executable, args=["-m","mcp_uof.server"], env=身份, cwd=repo根)` → 官方 SDK `stdio_client` + `ClientSession` → `initialize` → `list_tools` → `call_tool`。身份只由注入的 `env` 決定（一份設定 = 一個身份）。前提：stdio 下 server 不得寫任何東西到 stdout（診斷一律走 stderr），否則污染 JSON-RPC。

## 測試紀律（真實層）

- 只使用 `UOF_ACCOUNT_USER1~3` 指定的隔離測試帳號，共用 `UOF_PASSWORD`。
- Mounted 以隔離測試表單跑多身份起單/核准/作廢；後台流程須將測試申請帳號送給測試簽核帳號。
- 客製測試 schema 由 `.env` 的 `UOF_TEST_WORKFLOW_FIELDS` 與 `UOF_TEST_WORKFLOW_MEMO_FIELD` 注入，不得提交到 repo。
- `formVersionId` 動態解析（不可寫死；`_common.resolve_form_httpx`）。
- 所有起出的單在 `finally` 一律 `terminate_task(Cancel)`，不留簽核中表單。
- 真實主機名只在未入庫的 `.env`；斷言用語意字串（簽核中 / 作廢 / 已結案），不硬編環境值。

修改 WKF 程式後的完整回歸：`smoke` → `mounted`。

## 能力與邊界

- 「我有哪些待簽的單」：用 `get_pending_sign_list`（首頁待簽 widget，回含 TaskId/SiteId/NodeSeq）；`query_forms` 則列自己送出/簽過的單。兩者 server 內部皆以網頁取得。
- 多站流程的逐站簽核與留意見、並簽/會簽：目前僅 `sign_next`（自由流程單站同意）與 `terminate_task`（撤單/同意/否決）。
- `preview_workflow`（流程模擬）目前不提供 httpx 版，會提示改於網頁操作。
