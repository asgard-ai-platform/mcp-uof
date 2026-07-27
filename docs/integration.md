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

你只需要備齊 1 個值（向 UOF 管理員索取，或見 [configuration.md](configuration.md)）：

| 值 | 必填 | 說明 |
| --- | :-: | --- |
| `UOF_BASE_URL` | ✅ | UOF 站台 URL，含虛擬路徑、不含尾斜線。例：`https://your-uof-host.example.com/UOF` |
| `UOF_ACCOUNT` / `UOF_PASSWORD` | — | 只有無人值守（CI、自動化）才需要；一般使用改用瀏覽器登入 |

**身份不是在設定檔綁的**：綁好之後在對話中呼叫 `uof_custom_login`，會在你的瀏覽器開啟真實的 UOF
登入頁，登入完成後 session 自動交回 MCP 並存起來（位置可用 `UOF_SESSION_DIR` 指定，預設 `~/.uof`），重啟免重登。密碼由本機代理原樣轉送、不解析不落地，也不會回傳給 AI。

> 取得 `/absolute/path/to/mcp-uof` 絕對路徑：在 repo 目錄執行 `pwd`。

---

## 0.5 執行方式

對外只有一組固定工具，全部使用 httpx 網頁機制，不需要安裝 Playwright 或 Chromium。

對綁定的實際影響：

- 設定檔只需填 `UOF_BASE_URL`。
- 認證機制只有一種：`Login.aspx` 的 cookie session；沒有 PublicAPI 的站台也能用。
  取得那個 session 的方式有兩種——**瀏覽器登入**（預設）或**帳密自動登入**（備援，需填帳密）。
- 登入那一次會用到你自己的瀏覽器；操作 UOF 本身不需要瀏覽器 runtime。無圖形介面的機器請用帳密備援。

---

## 1. 身份模型（先讀，否則會誤解）

UOF 一代沒有「代表個別使用者的 OAuth」，一個 server process 只能是一個身份。要切換身份，看你用哪種登入方式：

> **一個 server process = 一個身份。**

| 登入方式 | 身份是誰 | 怎麼換人 |
| --- | --- | --- |
| 瀏覽器登入（預設） | **實際在瀏覽器登入的那個人** | `uof_custom_logout` 後重新 `uof_custom_login`，或 `uof_custom_login(force=True)` |
| 帳密自動登入（備援） | `env` 區塊的 `UOF_ACCOUNT` | 再加一份帶不同帳號的 server 設定，切換你呼叫的 server |

瀏覽器登入下切換身份可以在對話中完成；帳密備援下則是切換你呼叫的 server entry。
兩種方式都可以用 `check_auth` 隨時確認「目前這個工具是誰」。

---

## 2. Claude Desktop

### 2.1 找到設定檔

| OS      | 路徑                                                              |
| ------- | ----------------------------------------------------------------- |
| macOS   | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json`                     |

也可從 Claude Desktop → 設定 → Developer → Edit Config 開啟。

### 2.2 填入設定

參考 [examples/claude_desktop_config.json](../examples/claude_desktop_config.json)，把 `mcpServers` 區段填入（路徑換成你的）：

```json
{
  "mcpServers": {
    "uof": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": {
        "UOF_BASE_URL": "https://your-uof-domain.com/VirtualPath"
      }
    }
  }
}
```

> 不需要在這裡填帳密——身份由稍後的瀏覽器登入決定。

### 2.3 重啟與驗證

1. **完全結束** Claude Desktop（不是關視窗，是 Quit）再開啟
2. 在對話框看到工具圖示（🔨）出現 `uof` 的工具
3. 呼叫 `uof_custom_login` 在瀏覽器完成登入，再用 `check_auth` 確認狀態

---

## 3. VS Code Chat

VS Code（含 GitHub Copilot Chat 的 Agent 模式）支援 MCP。

### 3.1 建立設定檔

在專案根目錄建立 `.vscode/mcp.json`（只給此專案用），或在使用者 `settings.json` 的 `mcp` 區段（全域）。參考 [examples/vscode_mcp.json](../examples/vscode_mcp.json)：

```json
{
  "servers": {
    "uof": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/mcp-uof", "run", "mcp-uof"],
      "env": {
        "UOF_BASE_URL": "https://your-uof-domain.com/VirtualPath"
      }
    }
  }
}
```

> 設定檔裡沒有密碼——身份由稍後的瀏覽器登入決定。若因無人值守而必須使用帳密備援，請改用 VS Code 的
> `inputs` + `${input:uof_password}` 讓密碼在啟動時輸入，不要明文寫進設定檔。

### 3.2 啟動與驗證

1. 開啟 `.vscode/mcp.json`，在 `"uof"` 上方會出現 **Start** 行內按鈕，點它啟動（或開命令面板 → `MCP: List Servers` → 啟動）
2. 開 Chat → 切到 **Agent** 模式 → 工具面板應出現 `uof` 的工具
3. 呼叫 `uof_custom_login` 在瀏覽器完成登入，再用 `check_auth` 確認狀態

---

## 4. Context 切換：以不同人員身份操作

實務情境（如測試）常需要「申請人起單、主管簽核」分別由不同帳號執行。有兩種做法：

**做法 A：同一個 server 換人登入**（互動情境建議）——呼叫 `uof_custom_logout` 後重新
`uof_custom_login`，在瀏覽器換一個人登入即可。缺點是同一時間只能是一個身份。

**做法 B：為每個人各建一份帶帳密的 server 設定**（無人值守／需要同時具備兩種身份時）：

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
- 每個 server process 各自持有 session cookie，並依身份分別存到 session 目錄；程序重啟後直接沿用，失效才重登。
- 隨時用 `check_auth` 確認「目前這個工具是誰」。

> 每個 server entry 的可見資料與操作權限，仍由該 `UOF_ACCOUNT` 在 UOF 中的權限決定。

---

## 5. 疑難排解

| 症狀 | 可能原因與處置 |
| --- | --- |
| 工具沒出現 | 設定檔 JSON 格式錯誤；`uv`/絕對路徑不對；未完全重啟 |
| `check_auth` 回 🔑 尚未登入 | 正常狀態，呼叫 `uof_custom_login` 在瀏覽器完成登入即可 |
| `check_auth` 回 🔒 認證失敗 | `UOF_BASE_URL` 缺漏或錯誤；或有設帳密備援但帳密錯誤導致 Login.aspx 登入失敗 |
| 瀏覽器沒有自動打開 | 工具回傳訊息裡會附上本機登入網址，手動複製到瀏覽器開啟即可 |
| 切了帳號卻還是舊身份 | 瀏覽器登入：先 `uof_custom_logout` 再重登（舊 session 存檔會被清掉）。帳密備援：確認呼叫的是正確的 server entry 並重啟該 process |
| `command not found: uv` | 未安裝 uv，或 GUI 程式的 PATH 找不到；改用 `uv` 的絕對路徑 |
