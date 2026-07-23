# 安裝與綁定教學

本文教你把 `mcp-uof` 手動綁進 **Claude Desktop** 與 **VS Code Chat**，並說明如何以不同 UOF 帳號身份操作（context 切換）。

---

## 0. 前置準備

```bash
# 1. 取得程式碼後安裝依賴
cd /path/to/mcp-uof
uv sync

# 2. 確認可啟動（Ctrl-C 結束；無錯誤即正常）
uv run mcp-uof
```

你需要備齊這 3 個值（向 UOF 管理員索取，或見 [configuration.md](configuration.md)）：

| 值 | 說明 |
| --- | --- |
| `UOF_BASE_URL` | UOF 站台 URL，含虛擬路徑、不含尾斜線。例：`https://your-uof-host.example.com/UOF` |
| `UOF_ACCOUNT` | **這個 MCP 要代表哪個人**的 UOF 帳號 |
| `UOF_PASSWORD` | 該帳號密碼 |

> 取得 `/absolute/path/to/mcp-uof` 絕對路徑：在 repo 目錄執行 `pwd`。

---

## 0.5 執行方式

對外只有一組固定工具，全部使用 httpx 網頁機制，不需要安裝 Playwright 或 Chromium。

對綁定的實際影響：

- 只需填 `UOF_BASE_URL` / `UOF_ACCOUNT` / `UOF_PASSWORD` 三個值。
- 認證只有一種：以帳密登入 `Login.aspx` 取得 cookie session；沒有 PublicAPI 的站台也能用。

---

## 1. 身份模型（先讀，否則會誤解）

UOF 一代沒有「代表個別使用者的 OAuth」；所有機制都是**單一系統身份**。所以綁定時必須想清楚：

> **一份 server 設定 = 一個 UOF 帳號身份。** 你在 `env` 區塊填的 `UOF_ACCOUNT` 就是這個 MCP 之後所有操作的「人」。要以另一個人身份操作，就再加一份設定。

這也是為什麼**設定裡一定看得到要填的帳密**——身份就是在這裡綁的。切換 context 不是在對話中切換，而是切換你呼叫的 server。

---

## 2. Claude Desktop

### 2.1 找到設定檔

| OS      | 路徑                                                              |
| ------- | ----------------------------------------------------------------- |
| macOS   | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json`                     |

也可從 Claude Desktop → 設定 → Developer → Edit Config 開啟。

### 2.2 填入設定

參考 [examples/claude_desktop_config.json](../examples/claude_desktop_config.json)，把 `mcpServers` 區段填入（路徑與帳密換成你的）：

```json
{
  "mcpServers": {
    "uof": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": {
        "UOF_BASE_URL": "https://your-uof-domain.com/VirtualPath",
        "UOF_ACCOUNT": "<applicant_account>",
        "UOF_PASSWORD": "your_password"
      }
    }
  }
}
```

### 2.3 重啟與驗證

1. **完全結束** Claude Desktop（不是關視窗，是 Quit）再開啟
2. 在對話框看到工具圖示（🔨）出現 `uof` 的工具
3. 呼叫 `check_auth`；首次建立 session 時會嘗試登入並回報狀態

---

## 3. VS Code Chat

VS Code（含 GitHub Copilot Chat 的 Agent 模式）支援 MCP。

### 3.1 建立設定檔

在專案根目錄建立 `.vscode/mcp.json`（只給此專案用），或在使用者 `settings.json` 的 `mcp` 區段（全域）。參考 [examples/vscode_mcp.json](../examples/vscode_mcp.json)：

```json
{
  "inputs": [
    {
      "id": "uof_password",
      "type": "promptString",
      "description": "UOF 登入密碼",
      "password": true
    }
  ],
  "servers": {
    "uof": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": {
        "UOF_BASE_URL": "https://your-uof-domain.com/VirtualPath",
        "UOF_ACCOUNT": "<applicant_account>",
        "UOF_PASSWORD": "${input:uof_password}"
      }
    }
  }
}
```

> `${input:uof_password}` 會在啟動 server 時跳出密碼輸入框，避免密碼明文存在設定檔。正式環境請優先使用 MCP Host 提供的安全輸入或秘密管理方式。

### 3.2 啟動與驗證

1. 開啟 `.vscode/mcp.json`，在 `"uof"` 上方會出現 **Start** 行內按鈕，點它啟動（或開命令面板 → `MCP: List Servers` → 啟動）
2. 開 Chat → 切到 **Agent** 模式 → 工具面板應出現 `uof` 的工具
3. 呼叫 `check_auth`，確認首次登入與目前 session 狀態

---

## 4. Context 切換：以不同人員身份操作

實務情境（如測試）常需要「申請人起單、主管簽核」分別由不同帳號執行。做法是**為每個人各建一份 server 設定**：

```json
{
  "mcpServers": {
    "uof-applicant": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": {
        "...": "...",
        "UOF_ACCOUNT": "<applicant_account>",
        "UOF_PASSWORD": "..."
      }
    },
    "uof-manager": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": {
        "...": "...",
        "UOF_ACCOUNT": "<approver_account>",
        "UOF_PASSWORD": "..."
      }
    }
  }
}
```

- 兩份設定可**同時啟用**，工具名稱相同但分屬不同 server。對話時指明「用 uof-manager 的工具…」即以主管身份操作。
- 每個 server process 在記憶體中持有自己的 session cookie；程序重啟後會重新登入。
- 隨時用 `check_auth` 確認「目前這個工具是誰」。

> 每個 server entry 的可見資料與操作權限，仍由該 `UOF_ACCOUNT` 在 UOF 中的權限決定。

---

## 5. 疑難排解

| 症狀 | 可能原因與處置 |
| --- | --- |
| 工具沒出現 | 設定檔 JSON 格式錯誤；`uv`/絕對路徑不對；未完全重啟 |
| `check_auth` 回認證失敗 | 3 個 env 任一缺漏或錯誤；帳號密碼錯誤導致 Login.aspx 登入失敗 |
| 切了帳號卻還是舊身份 | 確認你呼叫的是正確的 server entry，並重新啟動該 server process |
| `command not found: uv` | 未安裝 uv，或 GUI 程式的 PATH 找不到；改用 `uv` 的絕對路徑 |
