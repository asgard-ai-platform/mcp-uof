# 測試

mcp-uof 採「三層測試法」，刻意維持可執行腳本風格（不使用 pytest）。各層定義與檔案結構的
權威說明在 [tests/README.md](../tests/README.md)；本文是公開文件版的概覽與紀律。

統一入口：

```bash
uv run python tests/run.py smoke     # Tier 1：離線，可進 CI
uv run python tests/run.py e2e       # Tier 2：真實測試環境，服務層
uv run python tests/run.py mounted   # Tier 3：真實掛載 MCP（真 stdio 子程序）
uv run python tests/run.py all       # 三層依序（缺 .env 時真實層自動 skip）
```

## 三層

| 層 | 路徑 | 是否需真實環境 | 涵蓋 |
|---|---|---|---|
| **Smoke** | `tests/smoke/` | 否（CI 可跑） | 模組可匯入（自動探索）、工具→機制綁定（`query_forms`→web、其餘→soap） |
| **E2E** | `tests/e2e/` | 是 | 服務層採購單：唯讀契約 + 起單→查→作廢 + 主管結案 + 已結案防護（廣度） |
| **Mounted** | `tests/mounted/` | 是 | 真實掛載 MCP：工具註冊護欄 + 採購單 schema 分派 + 原生表單多身份全程 + 負向認證（深度保真） |

**Tier 2 vs Tier 3 分工**：E2E 走 `domains/wkf/service.py` 函式，便宜地覆蓋行為分支；Mounted 把 server
當真 OS 子程序、只走 stdio JSON-RPC，證明「Claude 實際綁定路徑」可運作（工具註冊 / schema / env 注入身份 /
工具→機制派發 / stdout 乾淨）。

### Mounted（真實掛載 MCP）定義

與 Claude Desktop / VS Code 在 `mcp.json` 綁定的路徑一致：
`StdioServerParameters(command=sys.executable, args=["-m","mcp_uof.server"], env=身份, cwd=repo根)` →
官方 SDK `stdio_client` + `ClientSession` → `initialize` → `list_tools` → `call_tool`。身份只由注入的 `env`
決定（一份設定 = 一個身份）。前提：stdio 下 server 不得寫任何東西到 stdout（診斷一律走 stderr），否則污染 JSON-RPC。

## 測試紀律（真實層）

- 只用三個測試帳號（`UOF_ACCOUNT_USER1~3`：admin / manager_account / applicant_account，共用 `UOF_PASSWORD`）。
- E2E 服務層以採購單為主；Mounted 以原生測試表單跑多身份起單/核准/作廢，另驗採購單 schema 與 web_apply 分派。
- `formVersionId` 動態解析（不可寫死）。
- 所有起出的單在 `finally` 一律 `terminate_task(Cancel)`，不留簽核中表單。
- 採購單核准可能觸發外部 PO Service，因此 Mounted 的 Adopt 劇本使用原生測試表單；其餘一律 Cancel。
- 真實主機名只在未入庫的 `.env`；斷言用語意字串（採購單 / 簽核中 / 作廢 / 已結案），不硬編環境值。

修改 WKF 程式後的完整回歸：`smoke` → `e2e` → `mounted`。

## API 無法涵蓋、需在 UOF 網頁操作的情境

- 「我有哪些待簽的單」：UOF 一代 PublicAPI 沒有待簽清單 API，需從通知信或網頁取得 TaskId
  （`query_forms` 可列清單，server 內部以網頁取得）。
- 多站流程的逐站簽核與留意見：API 無此動作。
- `sign_next`（SignNext2）：自由流程不支援，僅適用後台設定的固定流程。
