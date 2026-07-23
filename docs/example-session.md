# 操作範例

以下以佔位符示範一般流程。實際欄位、表單版本與輸出內容依 UOF 站台而異。

## 1. 確認身份與表單

先用 `uof_custom_check_auth` 建立並確認目前 server entry 綁定的 session：

```text
uof_custom_check_auth()
```

從表單清單取得 `formId` 與 `formVersionId`，再查欄位：

```text
uof_custom_get_form_structure_by_id(form_id="<form-id>")
```

若欄位標示為 dialog，依序使用：

```text
uof_custom_get_dialog_structure(form_version_id="<form-version-id>", field_code="<field-code>")
uof_custom_search_dialog_options(form_version_id="<form-version-id>", field_code="<field-code>", keyword="<keyword>")
```

## 2. 起單與回讀

`preview_workflow` 目前只回傳能力說明，不會驗證流程或參數。`apply_form` 會直接異動資料，送出前應先確認欄位內容。

```text
uof_custom_apply_form(
  form_version_id="<form-version-id>",
  applicant_account="<configured-account>",
  first_signer_account="",
  fields={"<field-id>": "<value>"}
)
```

實際申請身份固定為該 server process 的 `UOF_ACCOUNT`。目前 `applicant_account` 與 `first_signer_account` 是相容性參數，不會改變身份或首站路由；需要指定首站簽核者時請使用 UOF Web UI。

成功後保存回傳的 `TaskId`，並立即回讀內容：

```text
uof_custom_get_task_data(task_id="<task-id>")
uof_custom_get_task_result(task_id="<task-id>", include_form_data=true)
```

## 3. 查詢與簽核

切換到簽核者的 server entry 後，用待簽清單取得目前輪到該帳號處理的工作：

```text
uof_custom_get_pending_sign_list()
```

同意目前站點：

```text
uof_custom_sign_next(task_id="<task-id>")
```

若需同意後送下一位簽核者，先用 `uof_custom_search_users` 取得 `UserGuid`，再傳入 `signer_guid`。`sign_next` 不接受簽核意見；退簽、並簽、會簽與固定流程逐站推進仍需使用 UOF Web UI。

否決目前待簽工作可使用：

```text
uof_custom_terminate_task(task_id="<task-id>", result="Reject", reason="<comment>")
```

`reason` 會作為簽核意見送出。實際可操作範圍由該帳號在 UOF 中的權限決定。

## 4. 撤回與狀態防護

申請者可對自己有權限撤回的簽核中表單執行：

```text
uof_custom_terminate_task(task_id="<task-id>", result="Cancel", reason="<reason>")
```

工具會先查詢目前狀態，避免重複操作已結案的表單。完成後以 `get_task_data` 或 `get_task_result` 再次確認結果。

完整工具契約見 [tools.md](tools.md)；身份綁定見 [integration.md](integration.md)。
