# Contributing

感謝對 `mcp-uof` 的貢獻。本專案是 UOF（一代）平台的 MCP Server，以 Python 撰寫，採用 DDD（Domain-Driven Design）概念依業務邊界分層。

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
uv run mcp-uof-sse
```

## 測試

```bash
uv run python tests/run.py smoke
uv run python -m compileall src tests
uv run python tests/run.py e2e
uv run python tests/run.py mounted
```

測試原則：

- `smoke` 為離線測試，應可在 CI 執行。
- `e2e` 與 `mounted` 需要真實測試環境與 `.env`。
- 真實層測試只使用 `.env` 中設定的測試帳號。
- WKF 實測以採購單情境為主；其他表單需後台流程設定，API 無法保證完整覆蓋。

## 架構原則

- 對外只暴露 MCP tools，使用者不選擇 SOAP 或 web 模式。
- 工具底層實作由 `src/mcp_uof/ops/router.py` 的 binding 決定。
- SOAP 能完成的操作優先使用 SOAP/PublicAPI；PublicAPI 無法支援的能力才使用 web/Playwright 補足。
- 所有異動必須經過 UOF 既有 API 或 UI 流程，不得提供直接修改資料庫的能力。

## 程式慣例

- 使用 Python 3.10+ type hints。
- 依賴管理使用 `uv`，不要直接以 `pip` 修改專案依賴。
- MCP tool 名稱使用全小寫 snake_case，並加上 `uof_custom_` 前綴。
- 不做 API 的 1:1 包裝；tool 應提供清楚的業務語意。
- SOAP 呼叫使用 `lxml + httpx` 輕量封裝，不使用 `zeep`。

## 文件

主要文件位於 `docs/`：

- `docs/architecture.md`：架構與身份模型
- `docs/configuration.md`：環境變數與 RSA 設定
- `docs/design.md`：工具設計與新增工具流程
- `docs/tools.md`：工具規格與能力邊界
- `docs/testing.md`：三層測試法
- `docs/form-requirements.md`：表單可被 MCP 操作的後台前提
