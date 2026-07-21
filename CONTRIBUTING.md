# Contributing

感謝對 `mcp-uof` 的貢獻。本專案是以 Python 撰寫的 UOF（一代）MCP Server。

> [!TIP] AI 開發代理人的具體開發規範與約束請參考本 repo 根目錄的 [AGENTS.md](AGENTS.md)。

## 開發環境

需求：Python 3.10+、[uv](https://docs.astral.sh/uv/)

```bash
uv sync
cp .env.example .env
```

請在 `.env` 填入實際連線設定與測試帳號。`.env`、憑證、Token、真實主機名稱與本機快取不得提交。

## 執行

```bash
uv run mcp-uof
```

## 測試

```bash
uv run python tests/run.py smoke
uv run python -m compileall src tests
uv run python tests/run.py mounted
```

測試原則：

- `smoke` 為離線測試，應可在 CI 執行。
- `mounted` 需要真實測試環境與 `.env`。
- 真實層測試只使用 `.env` 中設定的測試帳號。
- Mounted 測試只操作 `UOF_TEST_WORKFLOW_FORM_NAME` 指定的隔離測試表單。

## 送出前品質與機敏掃描 (Pre-commit Quality & Security Scan)

**本 repo 是公開的 GitHub repo**——任何提交都是公開提交，不是團隊內部可控的範圍。除了 `.env`、憑證、Token、真實主機名稱與本機快取（已由 `.gitignore` 排除）之外，commit/push 前另外檢查以下「看起來像範例、其實是真資料」的殘留：

- **真實測試帳號、供應商代碼、單號**：文件與範例一律用明顯的佔位符（如 `C0000001`、 `PO000000001`），不要貼實測時抓到的真實值。
- **客戶/廠商可識別名稱**：UOF 站台上客製 plugin、表單的命名可能內嵌真實客戶或廠商簡稱，文件與程式註解描述時用中性化說法（例如「客製 plugin」而非帶客戶簡稱的完整類別名），必要時再附上實際值僅寫在本機 `.env`/私有筆記。
- **表單 GUID / formVersionId**：執行時從表單清單動態解析，不要寫死在程式或文件裡。
- **內部表單名稱殘留**：舊版測試/文件曾用真實環境裡的表單名稱舉例，之後如在程式碼或文件裡看到具體、非佔位性質的表單名稱，一併清成通用描述。

不確定某個值是不是範例還是真資料時，先當作真資料處理，並在提交前使用 secret scanner 與 `git diff --cached` 複查。

## 架構原則

- 對外只暴露 MCP tools，使用者不選擇機制、沒有「模式」。
- 工具與 backend 的登記集中在 `src/mcp_uof/ops/router.py`；目前所有工具都使用 `http_web`。
- 所有操作以 httpx + lxml 打 UOF 的 aspx/ashx 網頁端點實作（不使用 SOAP/PublicAPI、不使用 Playwright）。
- 所有異動必須經過 UOF 既有 API 或 UI 流程，不得提供直接修改資料庫的能力。

## 程式慣例

- 使用 Python 3.10+ type hints。
- 依賴管理使用 `uv`，不要直接以 `pip` 修改專案依賴。
- MCP tool 名稱使用全小寫 snake_case，並加上 `uof_custom_` 前綴。
- 不做 API 的 1:1 包裝；tool 應提供清楚的業務語意。
- 網頁自動化使用 `httpx + lxml` 輕量實作（同步整頁 postback；不使用瀏覽器）。

## 文件

主要文件位於 `docs/`：

- `docs/architecture.md`：架構與身份模型
- `docs/configuration.md`：環境變數
- `docs/design.md`：工具設計與新增工具流程
- `docs/tools.md`：工具規格與能力邊界
- `docs/testing.md`：兩層測試法
- `docs/form-requirements.md`：表單可被 MCP 操作的後台前提
