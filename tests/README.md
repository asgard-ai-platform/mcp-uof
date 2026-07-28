# 測試（兩層測試法）

mcp-uof 的測試刻意維持「可執行腳本」風格（**不使用 pytest**）：每支測試是一個獨立檔案，直接 `uv run python …` 執行，以 `sys.exit(失敗數)` 回報結果。兩層各司其職、用目錄分隔。

```
tests/
  run.py        # 統一入口：uv run python tests/run.py [smoke|mounted|all]
  _common.py    # 兩層共用：路徑、.env 載入、skip 判斷與斷言工具
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
- `smoke/test_http_web_regressions.py`：通用 http_web 寫入重送、快取與原生明細欄位映射。
- `smoke/test_plugin_detail_rows.py`：plugin 明細 editor／巢狀 picker 解析、完整候選 row、計算與 lookup
  postback 順序、必填控制項及拒絕原因。
- `smoke/test_session_store.py`：session 存檔的 round-trip、檔案權限、身份隔離、過期處理、`UOF_SESSION_DIR` 與安全固定檔名覆寫。
- `smoke/test_browser_login.py`：對假 UOF upstream 驗證登入代理的安全邊界（token/Host 檢查、上游 `Set-Cookie` 不外流）、網址改寫與登入成功偵測。
- `smoke/test_auth_flow.py`：三段認證優先序、`check_auth`/`logout` 文案分流，以及 `uof_custom_login` 全程整合（假瀏覽器，不會開視窗）。

> 離線層固定跨部署都應成立的協定與安全邊界；部署端表單版型與寫入流程由部署端測試工具驗證。

## Tier 2 — 真實掛載 MCP（mounted）

**定義（本專案最在意、最逼真的一層）**：把 server 當**真正的 OS 子程序**啟動，與 Claude Desktop / VS Code 在 `mcp.json` 綁定的執行路徑一致，全程只走 **stdio JSON-RPC**。

- **掛載方式**：`StdioServerParameters(command=sys.executable, args=["-m","mcp_uof.server"], env=…, cwd=<repo根>)` → 官方 SDK `mcp.client.stdio.stdio_client` → `mcp.client.session.ClientSession`。被測操作**只**走 JSON-RPC，不在程序內 import server 內部。樣板見 `mounted/_client.py`。
- **身份綁定**：一個子程序 = 一個身份；身份**只**由注入的 `env`（`UOF_ACCOUNT` + 站台/密碼 + `PYTHONPATH`）決定，對應 `mcp.json` 的 `env` 區塊。SDK 對子程序只繼承白名單環境變數，故 `UOF_*` 必須明確帶入。
- **協定序列**：`initialize()` →（SDK 自動送 `notifications/initialized`，不可重送）→ `list_tools()` → `call_tool()`。
- **斷言**（`mounted/test_mcp_stdio.py`）：
  1. 註冊護欄：`list_tools` 剛好回傳 `EXPECTED_TOOLS` 那組 `uof_custom_*`，且 `query_forms` 可直接查詢。
  2. 單一身份唯讀流程：`check_auth`、`get_form_list` 與 `query_forms`。
  3. 負向認證：壞密碼 → `check_auth` / require_auth 工具回固定 🔒 字串，而非 crash / isError。
  4. 登入態管理：已登入時 `login` 直接回報不開瀏覽器；`logout` 後帳密備援會自動重登。
- **HOME 隔離（必要，勿移除）**：mounted 把 `HOME` 指到暫存目錄，避免 session 存檔污染開發者本機；
  **負向認證那段另用全新的 HOME**，否則會沿用前面同帳號存下的*有效* session，壞密碼走不到登入流程，
  那段測試就失效了。
- **瀏覽器登入不在 mounted 覆蓋範圍**：需要真人操作。代理行為由 `smoke/test_browser_login.py` 對假
  upstream 驗證，真實登入頁的渲染只能人工確認。
- **前提**：stdio 下 server **不得寫任何東西到 stdout**（會污染 JSON-RPC）。src 的診斷訊息一律走 stderr（`_eprint`）。

---

## 測試紀律（真實層務必遵守）

- **只用單一身份**：由 `UOF_ACCOUNT` / `UOF_PASSWORD` 指定。
- **只做唯讀驗證**：不建立、簽核、撤回或結案表單。
- **不依賴部署端 schema**：不要求特定表單名稱、欄位或流程角色。
- **真實主機名與帳密只在未入庫的 `.env`**；斷言不硬編環境值。
