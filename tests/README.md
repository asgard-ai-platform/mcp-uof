# 測試（兩層測試法）

mcp-uof 的測試刻意維持「可執行腳本」風格（**不使用 pytest**）：每支測試是一個獨立檔案，直接 `uv run python …` 執行，以 `sys.exit(失敗數)` 回報結果。兩層各司其職、用目錄分隔。

```
tests/
  run.py        # 統一入口：uv run python tests/run.py [smoke|mounted|all]
  _common.py    # 兩層共用：路徑、.env 載入與 skip 判斷、三帳號、表單版本 httpx 動態解析、TaskId 解析
  smoke/        # Tier 1 — 離線
  mounted/      # Tier 2 — 真實掛載 MCP（真 stdio 子程序）
```

執行：

```bash
uv run python tests/run.py smoke     # 離線，CI 可跑、秒級
uv run python tests/run.py mounted   # 需 .env：真子程序 stdio JSON-RPC
uv run python tests/run.py all       # 兩層依序（缺 .env 時真實層自動 skip）
```

## Tier 1 — Smoke（離線）

**定義**：不碰網路、不碰 UOF、不起子程序。每次改動的最低門檻，可進 CI。

- `smoke/test_imports.py`：自動探索並 import `src/mcp_uof` 下所有模組（語法 / 相依 / 循環匯入）。自動探索可避免手動清單漂移。
- `smoke/test_binding.py`：檢查工具登記、router 委派與認證閘行為。

> 依專案取捨，離線層刻意精簡：不為每個解析分支寫細緻 mock；重點放在 Tier 2。

## Tier 2 — 真實掛載 MCP（mounted）

**定義（本專案最在意、最逼真的一層）**：把 server 當**真正的 OS 子程序**啟動，與 Claude Desktop / VS Code 在 `mcp.json` 綁定的執行路徑一致，全程只走 **stdio JSON-RPC**。

- **掛載方式**：`StdioServerParameters(command=sys.executable, args=["-m","mcp_uof.server"], env=…, cwd=<repo根>)` → 官方 SDK `mcp.client.stdio.stdio_client` → `mcp.client.session.ClientSession`。被測操作**只**走 JSON-RPC，不在程序內 import server 內部。樣板見 `mounted/_client.py`。
- **身份綁定**：一個子程序 = 一個身份；身份**只**由注入的 `env`（`UOF_ACCOUNT` + 站台/密碼 + `PYTHONPATH`）決定，對應 `mcp.json` 的 `env` 區塊。SDK 對子程序只繼承白名單環境變數，故 `UOF_*` 必須明確帶入。
- **協定序列**：`initialize()` →（SDK 自動送 `notifications/initialized`，不可重送）→ `list_tools()` → `call_tool()`。
- **斷言**（`mounted/test_mcp_stdio.py`）：
  1. 註冊護欄：`list_tools` 剛好回傳 17 個 `uof_custom_*`，且 `query_forms` 可直接查詢。
  2. 多身份工作流程全程：申請、簽核、撤回、清理與已結案防護；實際能力依測試帳號權限而定。
  3. 負向認證：壞密碼 → `check_auth` / require_auth 工具回固定 🔒 字串，而非 crash / isError。
- **前提**：stdio 下 server **不得寫任何東西到 stdout**（會污染 JSON-RPC）。src 的診斷訊息一律走 stderr（`_eprint`）。

---

## 測試紀律（真實層務必遵守）

- **只用三個隔離測試帳號**：由 `UOF_ACCOUNT_USER1~3` 指定並共用 `UOF_PASSWORD`。
- **只操作已知的測試表單（表單名走 `UOF_TEST_WORKFLOW_FORM_NAME`）**。
- **客製 schema 不入庫**：fields JSON 與 memo 欄位走 `UOF_TEST_WORKFLOW_FIELDS` / `UOF_TEST_WORKFLOW_MEMO_FIELD`。
- **動態解析 formVersionId**（會隨重新發佈而變，不可寫死；見 `_common.resolve_form_httpx`）。
- **保證清理**：每張起出的單在 `finally` 一律 `terminate_task(Cancel)`，不留簽核中表單。
- **真實主機名只在未入庫的 `.env`**；斷言一律用「簽核中 / 作廢 / 已結案」等語意字串，不硬編環境值。
