# 實際操作對話

本文件是一段模擬操作範例，呈現透過 stdio MCP 協定（與 Claude Desktop、VS Code Chat
相同的連線方式）對 Server 發出的工具呼叫與回應。主機名稱、本機路徑與識別資訊皆使用佔位符。

> 本對話以採購單為例。工具底層用哪種機制取得資料對使用者透明（例如 `query_forms` 內部走網頁），
> 使用者只面對工具本身。

採購單為單一簽核站的自由流程：申請人 `applicant_account` 發起，第一站送主管 `manager_account`。
以下四段對話分別由綁定不同帳號的 Server 處理，示範「一份設定對應一個身份、切換身份就是切換設定」。

---

## 一、申請人發起採購單

由綁定 `applicant_account`（申請人）的 Server 處理。

**使用者：先確認我現在是用哪個帳號登入。**

呼叫 `uof_custom_check_auth`：

```
Token 有效，目前以 applicant_account 的身份操作。
   剩餘效期約 18843 分鐘。
Base URL: https://<你的-UOF-站台>
憑證路徑: ~/.uof/credentials-applicant_account-<hash>.json
要改以其他人的身份操作，請切換 MCP 設定（一份設定 = 一個身份）。
```

**使用者：系統裡有哪些表單？我要發採購單。**

呼叫 `uof_custom_get_form_list`（節錄）：

```
表單清單：
【人資類表單】 (ID: b3e353c0-...)
【不匯入正式區】 (ID: 0df5ff7a-...)
  - 員工費用報支 (代碼: a2158413-...)
  ...
（清單中含「採購單」）
```

**使用者：採購單需要填哪些欄位？**

呼叫 `uof_custom_get_form_structure_by_id`，帶入採購單的 formId：

```
表單 <form-id> 的欄位清單：
  - [FORM_NO] 表單編號〈autoNumber〉
  - [PO_Main] 主要欄位〈optionalField〉
  - [MEMO] 備註〈multiLineText〉
  - [ATTACH_FILES] 附件〈fileButton〉
```

**使用者：幫我填一張，備註寫「辦公室耗材採購」，送主管簽。先模擬流程確認沒問題再送。**

呼叫工具時以結構化參數傳入即可，不需自行組 XML（`form_version_id` 為此次紀錄的快照值；
表單版本會隨重新發佈而變，實際使用時請以 `get_form_list` 取得當前版本）：

```
form_version_id   = <由 get_form_list 取得的採購單當前版本>
applicant_account = applicant_account
first_signer_account = manager_account
fields = { "FORM_NO": "", "MEMO": "辦公室耗材採購" }   # 欄位以 get_form_structure_by_id 回傳者為準
```

先用相同參數呼叫 `uof_custom_preview_workflow`（不會真的起單）確認流程可走，
再呼叫 `uof_custom_apply_form` 正式送出：

```
表單申請成功！
  - 工作代號 (TaskId): 00000000-0000-0000-0000-000000000000
```

**使用者：這張單現在的狀態？**

呼叫 `uof_custom_get_task_data`：

```
表單申請內容：
  - 申請者: applicant_account
  - 簽核結果: 簽核中
```

TaskId 是後續查詢與簽核的唯一依據。系統沒有「待簽清單」查詢，申請人需把 TaskId
交給主管，或主管從 UOF 的通知信取得。

---

## 二、主管查詢並核准

切換到綁定 `manager_account`（主管）的 Server，帶入上一段取得的 TaskId。

**使用者：我現在是哪個身份？**

呼叫 `uof_custom_check_auth`：

```
Token 有效，目前以 manager_account 的身份操作。
Base URL: https://<你的-UOF-站台>
憑證路徑: ~/.uof/credentials-manager_account-<hash>.json
```

兩個 Server 的憑證以身份分別快取，互不影響。

**使用者：這張單（16d9bb5f-…）的內容和目前進度？**

呼叫 `uof_custom_get_task_result`：

```
表單 16d9bb5f-... 的簽核記錄：
  申請者: applicant_account | 最終結果: 簽核中

簽核歷程：
  站點 0 (sign): applicant_account → 同意 (2026/06/11 23:36)
    意見: 辦公室耗材採購
  站點 1 (sign): manager_account → 待簽
```

歷程顯示表單停在主管這一站（站點 1 為「待簽」）。

**使用者：我同意這張採購單。**

呼叫 `uof_custom_terminate_task`，動作為 `Adopt`：

```
表單同意成功
```

**使用者：確認核准後的結果。**

再次呼叫 `uof_custom_get_task_result`：

```
表單 16d9bb5f-... 的簽核記錄：
  申請者: applicant_account | 最終結果: 同意

簽核歷程：
  站點 0 (sign): applicant_account → 同意 (2026/06/11 23:36)
  站點 1 (sign): manager_account → 同意 (2026/06/11 23:37)
```

主管的核准記錄在主管本人名下，與在 UOF 網頁簽核同意的結果一致。

---

## 三、申請人撤回自己的單

由綁定 `applicant_account`（申請人）的 Server 處理。申請人發起一張新單後決定撤回。

**使用者：剛那張不買了，幫我作廢。**

先以 `uof_custom_apply_form` 起一張單，再呼叫 `uof_custom_terminate_task`，動作為 `Cancel`：

```
表單作廢成功
```

呼叫 `uof_custom_get_task_data` 確認：

```
表單申請內容：
  - 申請者: applicant_account
  - 簽核結果: 作廢
  - 結案日期: 2026/06/11 23:37:08
```

申請人可以作廢自己發起的單，不需要管理員介入。

---

## 四、管理員強制結案，並驗證重複結案會被擋下

由綁定 `admin`（管理員）的 Server 處理。情境是一張卡住的單需要直接結案。

呼叫 `uof_custom_check_auth` 確認身份為 `admin` 後，對一張簽核中的單呼叫
`uof_custom_terminate_task`，動作為 `Cancel`：

```
表單作廢成功
```

`uof_custom_get_task_data` 確認已作廢。**接著對同一張已結案的單再次呼叫
`uof_custom_terminate_task`**：

```
表單已結案（結果: 作廢），不可再結案。
注意: UOF API 本身不會阻擋此操作且會覆寫最終結果，已由工具層攔截。
```

UOF 後端對已結案的表單再次結案會回報成功並覆寫原結果，本工具在送出前先查詢狀態，
攔截了這個會破壞資料的操作。

---

## 對照表

| 對話 | 操作身份 | 用到的工具 |
|---|---|---|
| 一、發起採購單 | `applicant_account`（申請人） | check_auth、get_form_list、get_form_structure_by_id、preview_workflow、apply_form、get_task_data |
| 二、查詢與核准 | `manager_account`（主管） | check_auth、get_task_result、terminate_task |
| 三、撤回自己的單 | `applicant_account`（申請人） | apply_form、terminate_task、get_task_data |
| 四、強制結案與防護 | `admin`（管理員） | check_auth、terminate_task、get_task_data |

各工具的完整規格與能力邊界見 [tools.md](tools.md)；綁定與身份切換見 [integration.md](integration.md)。
