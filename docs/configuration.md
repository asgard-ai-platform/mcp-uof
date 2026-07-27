# Configuration

設定可放在兩處，擇一即可：

- **MCP Host 設定的 `env` 區塊**（正式使用建議）——見 [integration.md](integration.md)
- **`.env` 檔**（本機開發、跑測試）——參考 `.env.example`

兩者讀的是同一組環境變數；MCP Host 傳入的 `env` 優先，`.env` 不覆寫既有環境變數。

## 登入方式

所有工具使用 httpx 網頁機制，認證是 `Login.aspx` 的 cookie session。取得這個 session 有兩條路：

### 1. 瀏覽器登入（預設，建議）

**不需要設定帳號密碼**，呼叫 `uof_custom_login` 即可：MCP 在 `127.0.0.1` 開一個只有本機能連的
臨時反向代理並開啟預設瀏覽器，使用者在**真實的 UOF 登入頁**輸入帳密（或公司 AD），登入成功後
代理自動關閉、session 存進 `UOF_SESSION_DIR`（預設 `~/.uof`，`0600`），**MCP 重啟不必重登**。

**帳密的實際流向**（請勿誤解為「完全不經過」）：登入表單是由本機代理轉送給 UOF 的，所以密碼會
以原樣通過 MCP 程序的記憶體。本 Server **不解析、不記錄、不落地、也不回傳給 AI**；唯一會讀取的
是帳號欄位（`txtAccount`），用來標示 session 屬於誰。相對於把密碼寫進設定檔，這個作法讓密碼不再
長期存在於磁碟上。原理見 [architecture.md](architecture.md)。

### 2. 帳密自動登入（備援）

同時設定 `UOF_ACCOUNT` 與 `UOF_PASSWORD` 就會啟用：需要登入時直接登入，不開瀏覽器。適用於 CI、
無人值守部署與 `tests/mounted/`。只設定其中一個不會生效（stderr 會警告並退回瀏覽器登入）。

### 實際的判斷順序

| 順序 | 來源 | 條件 |
| --: | --- | --- |
| 1 | session 存檔（`UOF_SESSION_DIR`） | 存檔存在且探測 `Homepage.aspx` 沒被踢回 `Login.aspx` |
| 2 | 帳密自動登入 | `UOF_ACCOUNT` + `UOF_PASSWORD` 都有設 |
| 3 | 瀏覽器登入 | 前兩者都不成立 → 工具回「🔑 尚未登入」，要求呼叫 `uof_custom_login` |

session 中途失效（被重導回 `Login.aspx`）走同一套：能自動重登就自動重登，不能就要求重開瀏覽器。

## 環境變數

| Variable | Required | Description |
| --- | --: | --- |
| `UOF_BASE_URL` | Yes | UOF 站台 URL，含虛擬路徑、不含尾斜線（例：`https://host/UOF`） |
| `UOF_ACCOUNT` | No | 帳密備援用的操作帳號；未設定時走瀏覽器登入 |
| `UOF_PASSWORD` | No | 該帳號密碼；未設定時走瀏覽器登入 |
| `UOF_SSL_VERIFY` | No | `true`（預設）嚴格驗證；自簽憑證測試環境才用 `false` |
| `UOF_LOGIN_WAIT_SECONDS` | No | `uof_custom_login` 同步等待使用者登入的秒數（預設 `45`） |
| `UOF_LOGIN_TIMEOUT_SECONDS` | No | 本機登入頁在背景保留的秒數（預設 `600`） |
| `UOF_SESSION_DIR` | No | session 存放目錄，支援 `~` 展開（預設 `~/.uof`） |
| `UOF_SESSION_NAMESPACE` | No | 同機多身份時用來區分是哪一位（見下）；未設時退回 `UOF_ACCOUNT` |
| `UOF_SESSION_PERSIST` | No | `false` 則 session 只留記憶體、不寫入磁碟（預設 `true`） |
| `UOF_LOGIN_DEBUG` | No | 設任意值則把登入代理的請求記錄印到 stderr（除錯用） |

> **操作** UOF 不需要瀏覽器 runtime（httpx + lxml 即可）；只有**登入那一次**會用到使用者自己的瀏覽器。
> 在無圖形介面的機器（如容器、Alpine）請改用帳密備援。

## Session 存檔

- 路徑：`<UOF_SESSION_DIR>/session-<帳號>-<身份雜湊>.json`，預設目錄 `~/.uof`；目錄 `0700`、檔案 `0600`。
- 想放在別處（例如加密磁碟區）就設 `UOF_SESSION_DIR`；設了 `UOF_SESSION_PERSIST=false` 則完全不落地。
- 檔名以「站台 + **實際登入帳號**」命名，不同人各自一份，不會互相覆蓋。
- 目錄與檔案的權限每次都會檢查並收緊（`0700` / `0600`）；目錄若不屬於目前使用者則直接拒絕存放。

### 同一台機器有多個身份時

啟動當下程序還不知道自己是誰（帳號要等登入才知道），所以定位規則是：

| 情況 | 行為 |
| --- | --- |
| 有設 `UOF_SESSION_NAMESPACE` 或 `UOF_ACCOUNT` | 直接定位到該身份的存檔 |
| 都沒設，且該站台只有一份存檔 | 沿用它（單人單 entry 的常態） |
| 都沒設，且該站台有多份存檔 | **不猜**，要求重新登入 |

> [!IMPORTANT]
> 同一台機器、同一 UOF 站台要跑多個代表**不同人**的 server entry 時，請為每個 entry 設定不同的
> `UOF_SESSION_NAMESPACE`。否則它們會落到同一個判定，導致頻繁要求重新登入。

若設了 `UOF_ACCOUNT`，載入時會比對存檔記錄的帳號；屬於別人的存檔一律不沿用，避免安靜地換身份。

> [!IMPORTANT]
> 存檔內容是**可重放的 session cookie，等同登入態**。請比照密碼保護：不要複製到其他機器、
> 不要放進版控或備份。要撤銷就呼叫 `uof_custom_logout`（或直接刪掉該檔案）。

## 測試專用變數（tests/）

| Variable | Description |
| --- | --- |
| `UOF_ACCOUNT_USER1` | 具測試清理權限的帳號 |
| `UOF_ACCOUNT_USER2` | 測試簽核帳號 |
| `UOF_ACCOUNT_USER3` | 測試申請帳號 |
| `UOF_TEST_WORKFLOW_FORM_NAME` | mounted 工作流程用的原生表單名（留空則該情境自動 skip） |
| `UOF_TEST_WORKFLOW_FIELDS` | 該隔離測試表單的 `fields` JSON；只放在未追蹤的 `.env` |
| `UOF_TEST_WORKFLOW_MEMO_FIELD` | 寫入每次測試識別文字的欄位 ID |

`tests/mounted/` 需要無人值守登入，因此**必須**設定 `UOF_ACCOUNT` / `UOF_PASSWORD`（測試帳號共用 `UOF_PASSWORD`）。

> 為保護明文密碼，建議將 `.env` 權限設為僅擁有者可讀寫（`chmod 600 .env`）。

## 啟動

```bash
uv sync
cp .env.example .env   # 至少填入 UOF_BASE_URL
uv run mcp-uof         # 以 stdio 啟動
```

啟動後在對話中呼叫 `uof_custom_login` 完成登入即可開始操作。MCP client 設定範例見 [examples/](../examples/)。
