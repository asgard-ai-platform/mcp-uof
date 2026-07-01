# 安裝與綁定教學

本文教你把 `mcp-uof` 手動綁進 **Claude Desktop** 與 **VS Code Chat**，
並說明如何以不同 UOF 帳號身份操作（context 切換）。

---

## 0. 前置準備

```bash
# 1. 取得程式碼後安裝依賴
cd /path/to/mcp-uof
uv sync

# 2. 確認可啟動（Ctrl-C 結束；無錯誤即正常）
uv run mcp-uof
```

你需要備齊這 5 個值（向 UOF 管理員索取，或見 [configuration.md](configuration.md)）：

| 值 | 說明 |
|---|---|
| `UOF_BASE_URL` | UOF 站台 URL，含虛擬路徑、不含尾斜線。例：`https://your-uof-host.example.com/UOF` |
| `UOF_APP_NAME` | 外部系統代號（UOF 後台「一般組態設定 → 整合服務 → API」） |
| `UOF_RSA_PUBLIC_KEY` | RSA 公鑰 Base64（見 configuration.md 的金鑰流程） |
| `UOF_ACCOUNT` | **這個 MCP 要代表哪個人**的 UOF 帳號 |
| `UOF_PASSWORD` | 該帳號密碼 |

> 取得 `/absolute/path/to/mcp-uof` 絕對路徑：在 repo 目錄執行 `pwd`。

---

## 0.5 機制對使用者透明（不需選擇）

對外只有一組固定工具。每個工具底層用 SOAP/PublicAPI 還是 httpx 爬網頁取得資料，是
server 內部、開發期決定且對使用者透明的實作細節——**你不需要、也無法選擇**。原則：SOAP 能做的用
SOAP；SOAP 沒有該能力的才用 httpx web 補（目前：`query_forms` 列清單/搜尋、`get_form_structure` 系列、
採購單系列的 `apply_form`）。沒有「模式」要設，也不需要安裝 Playwright 或 Chromium。

對綁定的實際影響：

- 一律填 `UOF_BASE_URL` / `UOF_ACCOUNT` / `UOF_PASSWORD`；**SOAP 工具**另需 `UOF_APP_NAME` / `UOF_RSA_PUBLIC_KEY`。
- **認證跟著工具的機制走**：SOAP 工具驗 token、httpx web 工具（`query_forms` / `get_form_structure` / `apply_form`）驗 cookie session（帳密登入），彼此獨立——
  所以 httpx web 工具**不需要 SOAP token**，沒有 PublicAPI 的站台仍能用；不需安裝 Playwright 或 Chromium。
- 舊版的 `UOF_OPS_MODE` / `UOF_AUTH_MODE` 已不再使用；即使保留也會被忽略，不影響運作。

---

## 1. 身份模型（先讀，否則會誤解）

UOF 一代沒有「代表個別使用者的 OAuth」；所有機制都是**單一系統身份**。
所以綁定時必須想清楚：

> **一份 server 設定 = 一個 UOF 帳號身份。** 你在 `env` 區塊填的 `UOF_ACCOUNT`
> 就是這個 MCP 之後所有操作的「人」。要以另一個人身份操作，就再加一份設定。

這也是為什麼**設定裡一定看得到要填的帳密**——身份就是在這裡綁的。
切換 context 不是在對話中切換，而是切換你呼叫的 server。

---

## 2. Claude Desktop

### 2.1 找到設定檔

| OS | 路徑 |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

也可從 Claude Desktop → 設定 → Developer → Edit Config 開啟。

### 2.2 填入設定

參考 [examples/claude_desktop_config.json](../examples/claude_desktop_config.json)，把
`mcpServers` 區段填入（路徑與帳密換成你的）：

```json
{
  "mcpServers": {
    "uof": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": {
        "UOF_BASE_URL": "https://your-uof-domain.com/VirtualPath",
        "UOF_APP_NAME": "your_app_name",
        "UOF_RSA_PUBLIC_KEY": "your_rsa_public_key_base64",
        "UOF_ACCOUNT": "applicant_account",
        "UOF_PASSWORD": "your_password"
      }
    }
  }
}
```

### 2.3 重啟與驗證

1. **完全結束** Claude Desktop（不是關視窗，是 Quit）再開啟
2. 在對話框看到工具圖示（🔨）出現 `uof` 的工具
3. 輸入：「用 check_auth 確認登入狀態」
   → 應回「✅ Token 有效，目前以 **applicant_account** 的身份操作」

---

## 3. VS Code Chat

VS Code（含 GitHub Copilot Chat 的 Agent 模式）支援 MCP。

### 3.1 建立設定檔

在專案根目錄建立 `.vscode/mcp.json`（只給此專案用），或在使用者
`settings.json` 的 `mcp` 區段（全域）。參考
[examples/vscode_mcp.json](../examples/vscode_mcp.json)：

```json
{
  "inputs": [
    { "id": "uof_password", "type": "promptString", "description": "UOF 登入密碼", "password": true }
  ],
  "servers": {
    "uof": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": {
        "UOF_BASE_URL": "https://your-uof-domain.com/VirtualPath",
        "UOF_APP_NAME": "your_app_name",
        "UOF_RSA_PUBLIC_KEY": "your_rsa_public_key_base64",
        "UOF_ACCOUNT": "applicant_account",
        "UOF_PASSWORD": "${input:uof_password}"
      }
    }
  }
}
```

> `${input:uof_password}` 會在啟動 server 時跳出密碼輸入框，避免密碼明文存在設定檔。
> 不介意明文可直接把密碼寫進 `env`。

### 3.2 啟動與驗證

1. 開啟 `.vscode/mcp.json`，在 `"uof"` 上方會出現 **Start** 行內按鈕，點它啟動
   （或開命令面板 → `MCP: List Servers` → 啟動）
2. 開 Chat → 切到 **Agent** 模式 → 工具面板應出現 `uof` 的工具
3. 輸入：「用 check_auth 確認登入狀態」驗證

---

## 4. Context 切換：以不同人員身份操作

實務情境（如測試）常需要「申請人起單、主管簽核」分別由不同帳號執行。
做法是**為每個人各建一份 server 設定**：

```json
{
  "mcpServers": {
    "uof-applicant": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": { "...": "...", "UOF_ACCOUNT": "applicant_account", "UOF_PASSWORD": "..." }
    },
    "uof-manager": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": { "...": "...", "UOF_ACCOUNT": "manager_account",   "UOF_PASSWORD": "..." }
    }
  }
}
```

- 兩份設定可**同時啟用**，工具名稱相同但分屬不同 server。
  對話時指明「用 uof-manager 的工具…」即以主管身份操作。
- Token 快取以身份區分（`~/.uof/credentials-<account>-<hash>.json`），
  兩個身份互不污染。
- 隨時用 `check_auth` 確認「目前這個工具是誰」。

> 這模擬了真實場景：申請人 `applicant_account` 在自己的 MCP 起單 → 主管 `manager_account`
> 看到通知後，在他的 MCP（uof-manager）查詢並簽核。受限於 UOF 一代無使用者級認證，
> 這個「人的邊界」靠設定切換來達成，而非系統強制。

---

## 5. 疑難排解

| 症狀 | 可能原因與處置 |
|---|---|
| 工具沒出現 | 設定檔 JSON 格式錯誤；`uv`/絕對路徑不對；未完全重啟 |
| `check_auth` 回認證失敗 | 5 個 env 任一缺漏或錯誤；密碼錯誤時 GetToken 回空 |
| `The input is not a valid Base-64 string` | RSA 公鑰含 `+` 被 UOF Web UI 存壞，見 [configuration.md](configuration.md) 金鑰流程 |
| 切了帳號卻還是舊身份 | 確認你呼叫的是對的 server entry；必要時刪 `~/.uof/credentials-*.json` 重新登入 |
| `command not found: uv` | 未安裝 uv，或 GUI 程式的 PATH 找不到；改用 `uv` 的絕對路徑 |
