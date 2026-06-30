# 測試（三層測試法）

mcp-uof 的測試刻意維持「可執行腳本」風格（**不使用 pytest**）：每支測試是一個獨立檔案，
直接 `uv run python …` 執行，以 `sys.exit(失敗數)` 回報結果。三層各司其職、用目錄分隔。

```
tests/
  run.py        # 統一入口：uv run python tests/run.py [smoke|e2e|mounted|all]
  _common.py    # 三層共用：路徑、.env 載入與 skip 判斷、三帳號、GetToken、採購單版本動態解析、TaskId 解析
  smoke/        # Tier 1 — 離線
  e2e/          # Tier 2 — 真實測試環境，服務層
  mounted/      # Tier 3 — 真實掛載 MCP（真 stdio 子程序）
```

執行：

```bash
uv run python tests/run.py smoke     # 離線，CI 可跑、秒級
uv run python tests/run.py e2e       # 需 .env：真實測試環境
uv run python tests/run.py mounted   # 需 .env：真子程序 stdio JSON-RPC
uv run python tests/run.py all       # 三層依序（缺 .env 時真實層自動 skip）
```

---

## Tier 1 — Smoke（離線）

**定義**：不碰網路、不碰 UOF、不起子程序。每次改動的最低門檻，可進 CI。

- `smoke/test_imports.py`：自動探索並 import `src/mcp_uof` 下所有模組（語法 / 相依 / 循環匯入）。
  自動探索可避免手動清單漂移。
- `smoke/test_routing.py`：工具→機制綁定（`query_forms`→web、其餘→soap；`ops.router.BINDING` 為唯一
  決策點），及 router 委派到正確機制。

> 依專案取捨，離線層刻意精簡：不為每個解析分支寫細緻 mock；重點放在 Tier 3。

## Tier 2 — E2E（真實測試環境，服務層）

**定義**：透過 `domains/wkf/service.py` 的 Python 函式直接打真實 UOF（**不經 MCP 傳輸層**）。
追求「廣度」——用最便宜的方式覆蓋 WKF 行為分支。

- `e2e/test_wkf_purchase_order.py`：唯讀契約（GetFormList/…/SimulationFlowByScript）+ 異動劇本
  （起單→查→作廢；主管結他人單；已結案防護）。所有起出的單在 `finally` 保證 Cancel。

## Tier 3 — 真實掛載 MCP（mounted）

**定義（本專案最在意、最逼真的一層）**：把 server 當**真正的 OS 子程序**啟動，與 Claude Desktop /
VS Code 在 `mcp.json` 綁定的執行路徑一致，全程只走 **stdio JSON-RPC**。

- **掛載方式**：`StdioServerParameters(command=sys.executable, args=["-m","mcp_uof.server"], env=…, cwd=<repo根>)`
  → 官方 SDK `mcp.client.stdio.stdio_client` → `mcp.client.session.ClientSession`。被測操作**只**走 JSON-RPC，
  不在程序內 import server 內部。樣板見 `mounted/_client.py`。
- **身份綁定**：一個子程序 = 一個身份；身份**只**由注入的 `env`（`UOF_ACCOUNT` + 站台/密碼/公鑰 + `PYTHONPATH`）
  決定，對應 `mcp.json` 的 `env` 區塊。SDK 對子程序只繼承白名單環境變數，故 `UOF_*` 必須明確帶入。
- **協定序列**：`initialize()` →（SDK 自動送 `notifications/initialized`，不可重送）→ `list_tools()` → `call_tool()`。
- **斷言**（`mounted/test_mcp_stdio.py`）：
  1. 註冊護欄：`list_tools` 剛好 12 個 `uof_custom_*`（工具集固定，與底層機制無關）。
  2. 多身份採購單全程：申請人起單 → 主管核准（**整輪唯一一次 Adopt**）→ 申請人撤單 → admin 強制結案 → 已結案防護。
  3. 負向認證：壞密碼 → `check_auth` / require_auth 工具回固定 🔒 字串，而非 crash / isError。
- **前提**：stdio 下 server **不得寫任何東西到 stdout**（會污染 JSON-RPC）。src 的診斷訊息一律走 stderr
  （`_eprint`）。

**Tier 2 vs Tier 3 分工**：Tier 2 = 服務層「廣度」行為覆蓋（便宜、好加分支）；Tier 3 = 真實綁定路徑的
「深度保真」證明（工具註冊 / schema / env 注入身份 / 工具→機制派發 / stdout 乾淨 / `UOF_ACCOUNT` 自動代入）。

---

## 測試紀律（真實層務必遵守）

- **只用三個測試帳號**：`UOF_ACCOUNT_USER1=admin`、`USER2=manager_account`、`USER3=applicant_account`（共用 `UOF_PASSWORD`）。
- **只操作「採購單」**（唯一已驗證可外部起單的自由流程表單）。
- **動態解析 formVersionId**（會隨重新發佈而變，不可寫死；見 `_common.resolve_form`）。
- **保證清理**：每張起出的單在 `finally` 一律 `terminate_task(Cancel)`，不留簽核中表單。
- **整輪僅一次刻意 Adopt**（核准會觸發外部 PO Service；只在 mounted 2B 出現一次），其餘一律 Cancel。
- **真實主機名只在未入庫的 `.env`**；斷言一律用「採購單 / 簽核中 / 作廢 / 已結案」等語意字串，不硬編環境值。
