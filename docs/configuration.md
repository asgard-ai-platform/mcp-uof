# Configuration

設定可放在兩處，擇一即可：

- **MCP Host 設定的 `env` 區塊**（正式使用建議）——身份綁在這裡，見 [integration.md](integration.md)
- **`.env` 檔**（本機開發、跑測試）——參考 `.env.example`

兩者讀的是同一組環境變數；MCP Host 傳入的 `env` 優先，`.env` 不覆寫既有環境變數。

## 環境變數

沒有「模式」要設：每個工具底層用 SOAP 還是 httpx 網頁取得資料，是 server 內部決定、對使用者透明的。
**認證跟著工具的機制走**：SOAP 工具用 token、httpx web 工具（`query_forms` / `get_form_structure` /
`apply_form`）用 cookie session（帳密登入），兩者各自獨立。下表的「Required」因此分「全部工具」與「僅 SOAP 工具」兩種。

| Variable | Required | Description |
|---|---:|---|
| `UOF_BASE_URL` | Yes（全部） | UOF 站台 URL，含虛擬路徑、不含尾斜線（例：`https://host/UOF`） |
| `UOF_ACCOUNT` | Yes（全部） | MCP Server 執行時的操作帳號 |
| `UOF_PASSWORD` | Yes（全部） | 操作帳號密碼 |
| `UOF_APP_NAME` | 僅 SOAP 工具 | 外部系統代號（UOF「一般組態設定 → 整合服務 → API」），SOAP 取 Token 用；httpx web 工具不需 |
| `UOF_RSA_PUBLIC_KEY` | 僅 SOAP 工具 | RSA 公鑰（Base64），SOAP 加密帳密用；httpx web 工具不需 |
| `UOF_VERIFY_SSL` | No | `true`（預設）嚴格驗證；自簽憑證測試環境才用 `false` |
| `UOF_TOKEN_TTL` | No | SOAP 機制的 Token 快取秒數覆寫 |

> `query_forms` / `get_form_structure` / `apply_form` 走 httpx web，只需 `UOF_BASE_URL` / `UOF_ACCOUNT` /
> `UOF_PASSWORD`，**不需要 SOAP token**（沒有 PublicAPI 的站台仍能用這些工具），也不需要安裝 Playwright 或 Chromium。
> 舊版的 `UOF_OPS_MODE` / `UOF_AUTH_MODE` 已不再使用；即使保留也會被忽略。

## 測試專用變數（tests/）

| Variable | Description |
|---|---|
| `UOF_ACCOUNT_USER1` | 管理員測試帳號（`admin`）——強制結案 |
| `UOF_ACCOUNT_USER2` | 簽核主管測試帳號（`manager_account`）——第一站簽核者，可 MCP 簽核（單站流程） |
| `UOF_ACCOUNT_USER3` | 申請人測試帳號（`applicant_account`） |
| `UOF_TEST_FORM_VERSION_ID` | 採購單 formVersionId |
| `UOF_TEST_FORM_ID` | 採購單 formId |

測試帳號共用 `UOF_PASSWORD`。測試範圍限定這三個帳號與採購單情境。

## RSA 金鑰設定流程

1. 執行 `uv run python scripts/generate_no_plus_key.py` 產生不含 `+` 號的 key pair
   （含 `+` 的公鑰可能被 UOF Web UI 存成空白，導致 GetToken 解密失敗）
2. 將「私鑰 XML」貼到 UOF 後台「外部對應之私鑰」
3. 將「公鑰 Base64」填入 `.env` 的 `UOF_RSA_PUBLIC_KEY`

> 為保護明文密碼，建議將 `.env` 權限設為僅擁有者可讀寫（`chmod 600 .env`）。

## 啟動

```bash
uv sync
cp .env.example .env   # 填入實際值
uv run mcp-uof         # 以 stdio 啟動
```

MCP client 設定範例見 [examples/](../examples/)。
