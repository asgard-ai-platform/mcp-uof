# Configuration

設定可放在兩處，擇一即可：

- **MCP Host 設定的 `env` 區塊**（正式使用建議）——身份綁在這裡，見 [integration.md](integration.md)
- **`.env` 檔**（本機開發、跑測試）——參考 `.env.example`

兩者讀的是同一組環境變數；MCP Host 傳入的 `env` 優先，`.env` 不覆寫既有環境變數。

## 環境變數

所有工具使用 httpx 網頁機制；認證以帳密登入 `Login.aspx` 取得 cookie session。因此只需連線與帳密三個必填變數。

| Variable | Required | Description |
| --- | --: | --- |
| `UOF_BASE_URL` | Yes | UOF 站台 URL，含虛擬路徑、不含尾斜線（例：`https://host/UOF`） |
| `UOF_ACCOUNT` | Yes | MCP Server 執行時的操作帳號 |
| `UOF_PASSWORD` | Yes | 操作帳號密碼 |
| `UOF_SSL_VERIFY` | No | `true`（預設）嚴格驗證；自簽憑證測試環境才用 `false` |

> 不需要瀏覽器 runtime。在 Alpine Linux 或 musl 環境，仍需確認 Python 相依套件可安裝。

## 測試專用變數（tests/）

| Variable | Description |
| --- | --- |
| `UOF_ACCOUNT_USER1` | 具測試清理權限的帳號 |
| `UOF_ACCOUNT_USER2` | 測試簽核帳號 |
| `UOF_ACCOUNT_USER3` | 測試申請帳號 |
| `UOF_TEST_WORKFLOW_FORM_NAME` | mounted 工作流程用的原生表單名（留空則該情境自動 skip） |
| `UOF_TEST_WORKFLOW_FIELDS` | 該隔離測試表單的 `fields` JSON；只放在未追蹤的 `.env` |
| `UOF_TEST_WORKFLOW_MEMO_FIELD` | 寫入每次測試識別文字的欄位 ID |

測試帳號共用 `UOF_PASSWORD`。

> 為保護明文密碼，建議將 `.env` 權限設為僅擁有者可讀寫（`chmod 600 .env`）。

## 啟動

```bash
uv sync
cp .env.example .env   # 填入實際值
uv run mcp-uof         # 以 stdio 啟動
```

MCP client 設定範例見 [examples/](../examples/)。
