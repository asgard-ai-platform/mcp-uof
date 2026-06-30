"""
Domain: WKF — 電子簽核業務邏輯

本模組封裝 Wkf.asmx 的 SOAP 呼叫與 XML 回傳解析。
負責：
- 組裝 SOAP 請求參數
- 呼叫 soap_client 發送請求
- 解析 XML 回傳值並轉化為結構化結果

規格書參考: UOF 官方 WebService API 規格書「UOF 電子簽核 WebServiceAPI」章節。
"""

import json
from lxml import etree

from ...soap_client import uof_client
from .endpoints import WKF_ENDPOINT


# ── 查詢類 ──────────────────────────────────────────────────────


def get_form_list(token: str) -> str:
    """
    取得所有表單類別及表單名稱

    SOAP Method: GetFormList(token)
    回傳: XML FormList > Category > Form
    """
    try:
        result = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="GetFormList",
            params={"token": token},
        )

        if not result:
            return "📋 找不到任何表單"

        return _parse_form_list_xml(result)

    except Exception as e:
        return f"❌ 取得表單清單時發生錯誤: {str(e)}"


def get_external_form_list(token: str) -> str:
    """
    取得被標記為「非線上使用」的表單清單

    注意：此清單不等於「可外部起單的表單」。要判斷能否起單，請看表單是否有
    已發佈的 formVersionId（由 get_form_list 取得）。

    SOAP Method: GetExternalFormList(token)
    回傳: XML FormList > Category > Form
    """
    try:
        result = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="GetExternalFormList",
            params={"token": token},
        )

        if not result:
            return "📋 找不到任何外部表單"

        return _parse_form_list_xml(result)

    except Exception as e:
        return f"❌ 取得外部表單清單時發生錯誤: {str(e)}"


def get_form_structure(token: str, form_version_id: str) -> str:
    """
    根據表單版本代號取得表單欄位結構

    SOAP Method: GetFormStructure(token, formVersionId)
    回傳: XML FieldItem 欄位清單

    Args:
        token: 登入憑證
        form_version_id: 表單版本編號（可由 GetFormList 取得）
    """
    try:
        result = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="GetFormStructure",
            params={
                "token": token,
                "formVersionId": form_version_id,
            },
        )

        if not result:
            return f"📝 找不到版本 {form_version_id} 的表單結構"

        return _parse_form_structure_xml(result, form_version_id)

    except Exception as e:
        return f"❌ 取得表單結構時發生錯誤: {str(e)}"


def get_form_structure_by_form_id(token: str, form_id: str) -> str:
    """
    根據表單代號取得欄位結構

    SOAP Method: GetFormStructureByFormId(token, formId)
    回傳: XML FieldItem 欄位清單

    Args:
        token: 登入憑證
        form_id: 表單代號
    """
    try:
        result = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="GetFormStructureByFormId",
            params={
                "token": token,
                "formId": form_id,
            },
        )

        if not result:
            return f"📝 找不到表單 {form_id} 的結構"

        return _parse_form_structure_xml(result, form_id)

    except Exception as e:
        return f"❌ 取得表單結構時發生錯誤: {str(e)}"


def get_task_data(token: str, task_id: str) -> str:
    """
    查詢申請內容

    SOAP Method: GetTaskData(token, taskId)
    回傳: XML Applicant 節點

    Args:
        token: 登入憑證
        task_id: 表單申請編號（由 SendForm 回傳）
    """
    try:
        result = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="GetTaskData",
            params={
                "token": token,
                "taskId": task_id,
            },
        )

        if not result:
            return f"❌ 找不到表單 {task_id} 的資料"

        return _parse_task_data_xml(result, task_id)

    except Exception as e:
        if "no row at position" in str(e):
            return f"❌ 找不到表單 {task_id}，請確認 TaskId 是否正確"
        return f"❌ 查詢申請內容時發生錯誤: {str(e)}"


def get_task_result(
    token: str, task_id: str, is_contain_form_data: bool = True
) -> str:
    """
    取得表單每個站點簽核結果

    SOAP Method: GetTaskResult(token, taskId, isContainFormData)
    回傳: XML Applicant + Comment > Site > Signer

    Args:
        token: 登入憑證
        task_id: 表單代號
        is_contain_form_data: 是否包含表單內容
    """
    try:
        result = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="GetTaskResult",
            params={
                "token": token,
                "taskId": task_id,
                "isContainFormData": "true" if is_contain_form_data else "false",
            },
        )

        if not result:
            return f"❌ 找不到表單 {task_id} 的簽核結果"

        return _parse_task_result_xml(result, task_id)

    except Exception as e:
        if "no row at position" in str(e):
            return f"❌ 找不到表單 {task_id}，請確認 TaskId 是否正確"
        return f"❌ 取得簽核結果時發生錯誤: {str(e)}"


# ── 寫入類 ──────────────────────────────────────────────────────


def build_form_xml(
    form_version_id: str,
    applicant_account: str,
    first_signer_account: str,
    fields: dict,
    comment: str = "",
    urgent_level: str = "2",
) -> str:
    """
    由結構化參數組出 SendForm / SimulationFlowByScript 所需的表單 XML。

    支援範圍：單站自由流程 + 基本欄位型別（文字、自動編號、可空欄位、不帶檔案的附檔欄位）
    + 明細（DataGrid）。實際附檔（Attach）、多站與並簽/會簽尚未支援。

    欄位值以 lxml 寫入，特殊字元（& < > 等）會自動跳脫，可直接帶入使用者文字。

    Args:
        form_version_id: 表單版本代號（由 get_form_list 取得）
        applicant_account: 申請者帳號
        first_signer_account: 第一站簽核者帳號（自由流程必填）
        fields: 欄位值 {fieldId: value}；自動編號欄位帶空字串即可，系統會自動編號。
                **明細（dataGrid）欄位**：value 帶「列的清單」，每列是 {子欄位fieldId: 值}，例如
                {"004": [{"004_1":"品名A","004_3":"5"}, {"004_1":"品名B","004_3":"3"}]}。
        comment: 申請者意見
        urgent_level: 緊急程度（0 緊急 / 1 急 / 2 普通）
    """
    form = etree.Element("Form", formVersionId=form_version_id, urgentLevel=str(urgent_level))
    applicant = etree.SubElement(
        form, "Applicant", account=applicant_account, groupId="", jobTitleId=""
    )
    etree.SubElement(applicant, "Comment").text = comment or ""

    if first_signer_account:
        site = etree.SubElement(form, "FirstSiteInfo", signType="0", timeout="0")
        signer = etree.SubElement(site, "Signer")
        etree.SubElement(signer, "Account").text = first_signer_account

    field_value = etree.SubElement(form, "FormFieldValue")
    for field_id, value in (fields or {}).items():
        # 明細（DataGrid）：value 是「每列為 dict」的清單，每列 {子欄位fieldId: 值}。只有「全是 dict
        # 的清單」(含空清單) 才當明細——避免把多值/誤傳的非 dict 清單當明細，或在非 dict 列上 .items() 崩潰。
        if isinstance(value, (list, tuple)) and all(isinstance(r, dict) for r in value):
            # 對應 SendForm 的 <FieldItem><DataGrid><Row order><Cell fieldId fieldValue/></Row></DataGrid>。
            item = etree.SubElement(field_value, "FieldItem", fieldId=str(field_id))
            grid = etree.SubElement(item, "DataGrid")
            for order, row in enumerate(value):
                row_el = etree.SubElement(grid, "Row", order=str(order))
                for cell_id, cell_val in row.items():
                    etree.SubElement(
                        row_el, "Cell", fieldId=str(cell_id),
                        fieldValue="" if cell_val is None else str(cell_val),
                    )
        else:
            etree.SubElement(
                field_value, "FieldItem", fieldId=str(field_id),
                fieldValue="" if value is None else str(value),
            )

    return etree.tostring(form, encoding="unicode")


def apply_form_structured(
    token: str,
    form_version_id: str,
    applicant_account: str,
    first_signer_account: str,
    fields: dict,
    comment: str = "",
    urgent_level: str = "2",
) -> str:
    """以結構化參數外部起單。內部組好 XML 後呼叫 SendForm。"""
    if not form_version_id:
        return "❌ 缺少 form_version_id（可由 get_form_list 取得）"
    if not applicant_account:
        return "❌ 缺少 applicant_account（申請者帳號）"
    if not first_signer_account:
        return (
            "❌ 缺少 first_signer_account。自由流程表單（如採購單）必須指定第一站簽核者，"
            "請向使用者確認要送給誰簽核。"
        )
    xml = build_form_xml(
        form_version_id, applicant_account, first_signer_account, fields, comment, urgent_level
    )
    return send_form(token, xml)


def preview_workflow_structured(
    token: str,
    form_version_id: str,
    applicant_account: str,
    first_signer_account: str,
    fields: dict = None,
    comment: str = "",
    urgent_level: str = "2",
) -> str:
    """以結構化參數模擬流程（不會真的起單）。參數與 apply_form_structured 相同。"""
    if not form_version_id:
        return "❌ 缺少 form_version_id（可由 get_form_list 取得）"
    if not first_signer_account:
        return (
            "❌ 缺少 first_signer_account。自由流程表單必須指定第一站簽核者才能模擬流程。"
        )
    xml = build_form_xml(
        form_version_id,
        applicant_account or "",
        first_signer_account,
        fields or {},
        comment,
        urgent_level,
    )
    return simulation_flow(token, xml)


def simulation_flow(token: str, content_xml: str) -> str:
    """
    取得預覽流程內容（模擬簽核流程走向）

    SOAP Method: SimulationFlowByScript(token, content)
    回傳: JSON { Status, Sites }

    Args:
        token: 登入憑證
        content_xml: 與外部起單的 XML 格式相同
    """
    try:
        result = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="SimulationFlowByScript",
            params={
                "token": token,
                "content": content_xml,
            },
        )

        if not result:
            return "❌ SimulationFlowByScript 回傳空值"

        # 回傳為 JSON 字串
        try:
            data = json.loads(result)
            status = data.get("Status", "Unknown")
            sites = data.get("Sites", "")

            if status == "Failure":
                return f"❌ 流程模擬失敗: {sites}"

            if isinstance(sites, str):
                try:
                    sites = json.loads(sites)
                except json.JSONDecodeError:
                    pass

            if isinstance(sites, list):
                lines = ["🔄 流程預覽："]
                for site in sites:
                    seq = site.get("SITE_SEQ", "")
                    signer = site.get("SIGNER", "")
                    sign_type_code = site.get("SIGN_TYPE", "")
                    sign_types = {"0": "一般", "1": "並簽", "2": "會簽"}
                    sign_type = sign_types.get(str(sign_type_code), str(sign_type_code))
                    remark = site.get("REMARK", "")

                    line = f"  {seq}. {signer} ({sign_type})"
                    if remark:
                        line += f" ⚠️ {remark}"
                    lines.append(line)

                return "\n".join(lines)

            return f"🔄 流程預覽: {result}"

        except json.JSONDecodeError:
            return f"🔄 流程預覽: {result}"

    except Exception as e:
        return f"❌ 流程模擬時發生錯誤: {str(e)}"


def send_form(token: str, xml_form_info: str) -> str:
    """
    申請表單（外部起單）

    SOAP Method: SendForm(token, xmlFormInfo)
    回傳: XML { Status, FormNumber, TaskId } 或 { Status, Type, Message }

    Args:
        token: 登入憑證
        xml_form_info: 表單內容 XML（中介欄位格式）
    """
    try:
        result = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="SendForm",
            params={
                "token": token,
                "xmlFormInfo": xml_form_info,
            },
        )

        if not result:
            return "❌ SendForm 回傳空值"

        return _parse_send_form_result(result)

    except Exception as e:
        return f"❌ 申請表單時發生錯誤: {str(e)}"


def terminate_task(
    token: str,
    task_id: str,
    account: str,
    result: str,
    reason: str,
) -> str:
    """
    表單強制結案

    SOAP Method: TerminateTask(token, taskId, account, result, reason)
    回傳: XML { Status, Exception }

    Args:
        token: 登入憑證
        task_id: 表單代號
        account: 操作者帳號
        result: 結案動作（Adopt 同意 / Reject 否決 / Cancel 作廢）
        reason: 結案原因
    """
    if result not in ("Adopt", "Reject", "Cancel"):
        return f"❌ 無效的結案動作: {result}。請使用 Adopt（同意）、Reject（否決）或 Cancel（作廢）"

    # 防護：UOF API 對「已結案」的表單再呼叫 TerminateTask 仍回成功，
    # 且會直接覆寫最終結果（如將已同意的單改為作廢）。API 端無任何防護，
    # 必須在工具層先查狀態攔截。
    try:
        current = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="GetTaskData",
            params={"token": token, "taskId": task_id},
        )
        if current:
            cur_root = etree.fromstring(
                current.encode("utf-8") if isinstance(current, str) else current
            )
            cur_result = cur_root.findtext(".//Applicant/Result", "")
            if cur_result not in ("", "UnKnow", "Unknown"):
                closed_map = {"Adopt": "同意", "Reject": "否決", "Cancel": "作廢"}
                return (
                    f"❌ 表單已結案（結果: {closed_map.get(cur_result, cur_result)}），不可再結案。\n"
                    f"⚠️ 注意: UOF API 本身不會阻擋此操作且會覆寫最終結果，已由工具層攔截。"
                )
    except Exception as e:
        if "no row at position" in str(e):
            return f"❌ 找不到表單 {task_id}，請確認 TaskId 是否正確"
        return f"❌ 結案前狀態檢查失敗: {str(e)}"

    try:
        resp = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="TerminateTask",
            params={
                "token": token,
                "taskId": task_id,
                "account": account,
                "result": result,
                "reason": reason,
            },
        )

        if not resp:
            return "❌ TerminateTask 回傳空值"

        # 解析回傳（格式：<ReturnValue><Status>1</Status><Exception><Message>表單作廢成功</Message></Exception></ReturnValue>）
        try:
            root = etree.fromstring(resp.encode("utf-8") if isinstance(resp, str) else resp)
            # 相容直接回傳 <ReturnValue> 或被外層包一層的情況
            rv = root if root.tag == "ReturnValue" else (root.find(".//ReturnValue") if root.find(".//ReturnValue") is not None else root)
            status = rv.findtext("Status", "")
            message = rv.findtext("Exception/Message", "") or rv.findtext("Exception", "")

            if status == "1":
                action_map = {"Adopt": "同意", "Reject": "否決", "Cancel": "作廢"}
                return f"✅ 表單{action_map.get(result, result)}成功（{message}）"
            else:
                return f"❌ 表單結案失敗: {message}"

        except etree.XMLSyntaxError:
            return f"📄 結案回傳: {resp}"

    except Exception as e:
        return f"❌ 表單強制結案時發生錯誤: {str(e)}"


def sign_next(
    token: str,
    task_id: str,
    site_id: str,
    node_seq: int,
    signer_guid: str,
) -> str:
    """
    表單送下一站（指定下一站預計簽核者）

    SOAP Method: SignNext2(token, taskId, siteId, nodeSeq, signerGuid)
    回傳: XML <ReturnValue><Result/><Message/></ReturnValue>

    ⚠️ 能力邊界：
    - 自由流程表單（如採購單）呼叫本方法會得到 HTTP 500（Server fault），不支援
    - siteId / nodeSeq / signerGuid 無法由任何 WKF 查詢 API 取得，
      只能從固定流程的表單流程設計（UOF 後台）得知
    - 本方法不含「同意/否決/意見」參數——簽核動作本身只能在 UOF Web UI 完成

    Args:
        token: 登入憑證
        task_id: 表單代號
        site_id: 目前站點代號
        node_seq: 節點順序
        signer_guid: 預計簽核者 Guid
    """
    try:
        resp = uof_client.call(
            endpoint_path=WKF_ENDPOINT,
            method_name="SignNext2",
            params={
                "token": token,
                "taskId": task_id,
                "siteId": site_id,
                "nodeSeq": node_seq,
                "signerGuid": signer_guid,
            },
        )

        if not resp:
            return "❌ SignNext2 回傳空值"

        try:
            root = etree.fromstring(resp.encode("utf-8") if isinstance(resp, str) else resp)
            result_code = root.findtext("Result", "")
            message_code = root.findtext("Message", "")

            success_messages = {"1": "送往下一站", "2": "結案", "3": "會簽站點未有結果"}
            failure_messages = {
                "1": "表單被別人鎖定中",
                "2": "自由流程表單（不支援 SignNext）",
                "3": "非本站可處理站點",
                "4": "讀取組織流程時發生錯誤",
                "5": "自訂站點",
            }

            if result_code == "1":
                return f"✅ 送下一站成功: {success_messages.get(message_code, message_code)}"
            return f"❌ 送下一站失敗: {failure_messages.get(message_code, message_code)}"

        except etree.XMLSyntaxError:
            return f"📄 SignNext2 回傳: {resp}"

    except Exception as e:
        return (
            f"❌ 送下一站時發生錯誤: {str(e)}\n"
            f"💡 提示: 自由流程表單（如採購單）不支援 SignNext2，"
            f"簽核動作請改由 UOF Web UI 完成，或由有權限者使用 terminate_task 強制結案。"
        )


# ── XML 解析輔助函式 ─────────────────────────────────────────────


def _parse_form_list_xml(xml_str: str) -> str:
    """解析 GetFormList / GetExternalFormList 的 XML 回傳"""
    try:
        root = etree.fromstring(xml_str.encode("utf-8") if isinstance(xml_str, str) else xml_str)

        categories = root.findall(".//Category") if root.tag != "Category" else [root]
        if root.tag == "FormList":
            categories = root.findall("Category")

        if not categories:
            return "📋 找不到任何表單類別"

        lines = ["📋 表單清單："]
        for cat in categories:
            cat_name = cat.get("categoryName", "未分類")
            cat_id = cat.get("categoryId", "")
            lines.append(f"\n📁 【{cat_name}】 (ID: {cat_id})")

            forms = cat.findall("Form")
            for form in forms:
                form_id = form.get("formId", "")
                form_name = form.get("formName", "")
                version_id = form.get("recentVersionId", "")
                lines.append(f"  - {form_name} (代碼: {form_id}, 版本: {version_id})")

        return "\n".join(lines)

    except etree.XMLSyntaxError:
        return "📋 表單資料已取得但 XML 格式無法解析"


def _parse_form_structure_xml(xml_str: str, identifier: str) -> str:
    """解析表單欄位結構 XML"""
    try:
        root = etree.fromstring(xml_str.encode("utf-8") if isinstance(xml_str, str) else xml_str)

        fields = root.findall(".//FieldItem")
        if not fields:
            return f"📝 表單 {identifier} 沒有欄位"

        # 欄位型別 → 填寫方式提示（給 apply_form 的 fields 參數用）
        fill_hint = {
            "autoNumber": "系統自動編號，帶空字串",
            "fileButton": "附檔欄位，目前帶空字串（尚未支援實際上傳）",
            "multiLineText": "填入文字",
            "optionalField": "可填文字或留空",
        }

        unsupported = []
        lines = [f"📝 表單 {identifier} 的欄位清單："]
        for field in fields:
            field_id = field.get("fieldId", "")
            field_name = field.get("fieldName", "")
            field_type = field.get("fieldType", "")
            hint = fill_hint.get(field_type, "填入文字")
            type_note = f"〈{field_type}〉" if field_type else ""
            lines.append(f"  - [{field_id}] {field_name}{type_note} — {hint}")

            # 檢查是否有明細欄位（lxml element 無子節點時為 falsy，故用顯式 None 判斷）
            data_grid = field.find(".//DataGrid")
            if data_grid is None:
                data_grid = field.find(".//dataGrid")
            if data_grid is not None:
                unsupported.append(field_id)
                grid_items = data_grid.findall("DataGridItem")
                for item in grid_items:
                    item_id = item.get("fieldId", item.get("field", ""))
                    item_name = item.get("fieldName", "")
                    lines.append(f"      ↳ [{item_id}] {item_name}（明細子欄位）")

        lines.append(
            "\n💡 起單方式：呼叫 apply_form，fields 參數帶 {fieldId: 值} 的對應；"
            "自由流程需另外提供 first_signer_account（第一站簽核者帳號）。"
            "建議先用 preview_workflow 以相同參數驗證流程再送出。"
        )
        lines.append(
            "⚠️ 以上為此表單對外開放的「中介欄位」，可能少於 UOF 網頁上看到的欄位"
            "（網頁的主旨、供應商、明細等若未對應為中介欄位，API 無法填）。"
            "起單時系統不會驗證網頁必填欄位，缺欄位仍可能起單成功但內容不完整。"
        )
        if unsupported:
            lines.append(
                f"📋 此表單含明細欄位（{', '.join(unsupported)}）。起單時該欄位帶「列的清單」，"
                "每列是 {子欄位fieldId: 值}，例如 "
                f"{{\"{unsupported[0]}\": [{{\"<子欄位>\": \"值\"}}, ...]}}。"
            )
        return "\n".join(lines)

    except etree.XMLSyntaxError:
        return f"📝 表單結構已取得但 XML 格式無法解析"


def _parse_send_form_result(xml_str: str) -> str:
    """解析 SendForm 回傳的 XML"""
    try:
        root = etree.fromstring(xml_str.encode("utf-8") if isinstance(xml_str, str) else xml_str)
        # 相容直接回傳 <ReturnValue> 或被外層包一層的情況
        rv = root if root.tag == "ReturnValue" else (root.find(".//ReturnValue") if root.find(".//ReturnValue") is not None else root)

        status = rv.findtext("Status", "")

        if status == "1":
            form_number = rv.findtext("FormNumber", "")
            task_id = rv.findtext("TaskId", "")
            return (
                f"✅ 表單申請成功！\n"
                f"  - 表單編號: {form_number}\n"
                f"  - 工作代號 (TaskId): {task_id}"
            )
        else:
            error_type = rv.findtext("Type", "未知錯誤")
            error_message = rv.findtext("Message", "")

            # 錯誤類別中文轉譯
            error_types = {
                "NoSignerException": "找不到簽核者",
                "FormVersionNotFound": "找不到表單版本",
                "ApplicantNotFound": "找不到申請者",
                "FieldValidationError": "欄位驗證錯誤",
            }
            error_desc = error_types.get(error_type, error_type)

            return (
                f"❌ 表單申請失敗\n"
                f"  - 錯誤類別: {error_desc}\n"
                f"  - 錯誤訊息: {error_message}"
            )

    except etree.XMLSyntaxError:
        return f"❌ 回傳資料無法解析: {xml_str[:200]}"


def _parse_task_data_xml(xml_str: str, task_id: str) -> str:
    """解析 GetTaskData 回傳的 XML"""
    try:
        root = etree.fromstring(xml_str.encode("utf-8") if isinstance(xml_str, str) else xml_str)

        applicant = root.find(".//Applicant")
        if applicant is None:
            return f"❌ 找不到表單 {task_id} 的申請者資訊"

        form_number = applicant.findtext("FormNumber", "")
        account = applicant.findtext("Account", "")
        form_id = applicant.findtext("FormId", "")
        form_version_id = applicant.findtext("FormVersionId", "")
        result_text = applicant.findtext("Result", "")
        result_date = applicant.findtext("ResultDate", "")

        result_map = {
            "Adopt": "同意",
            "Reject": "否決",
            "Cancel": "作廢",
            "Unknown": "簽核中",
            "UnKnow": "簽核中",  # API 實際回傳值為 UnKnow（與文件拼法 Unknown 不同）
        }
        result_desc = result_map.get(result_text, result_text)

        lines = [
            "📄 表單申請內容：",
            f"  - 表單編號: {form_number}",
            f"  - 申請者: {account}",
            f"  - 表單代號: {form_id}",
            f"  - 表單版本: {form_version_id}",
            f"  - 簽核結果: {result_desc}",
            f"  - 結案日期: {result_date}",
        ]

        return "\n".join(lines)

    except etree.XMLSyntaxError:
        return "📄 表單資料已取得但 XML 格式無法解析"


def _parse_task_result_xml(xml_str: str, task_id: str) -> str:
    """解析 GetTaskResult 回傳的 XML"""
    try:
        root = etree.fromstring(xml_str.encode("utf-8") if isinstance(xml_str, str) else xml_str)

        # 檢查是否為錯誤回傳
        status = root.findtext("Status")
        if status == "0":
            exception = root.findtext("Exception", "未知錯誤")
            return f"❌ 查詢失敗: {exception}"

        # 解析申請資訊
        applicant = root.find(".//Applicant")
        lines = [f"📄 表單 {task_id} 的簽核記錄："]

        if applicant is not None:
            account = applicant.findtext("Account", "")
            result_text = applicant.findtext("Result", "")
            result_map = {"Adopt": "同意", "Reject": "否決", "Cancel": "作廢", "Unknown": "簽核中", "UnKnow": "簽核中"}
            lines.append(f"  申請者: {account} | 最終結果: {result_map.get(result_text, result_text)}")

        # 解析表單欄位內容（isContainFormData=true 時回傳；僅含對外中介欄位）
        field_value = root.find(".//FormFieldValue")
        if field_value is not None:
            items = field_value.findall("FieldItem")
            if items:
                lines.append("\n📋 表單欄位內容：")
                for item in items:
                    fid = item.get("fieldId", "")
                    fname = item.get("fieldName", "")
                    fval = item.get("fieldValue", "")
                    lines.append(f"  - [{fid}] {fname}: {fval if fval else '（空）'}")
                lines.append("  （以上僅為對外中介欄位，可能少於 UOF 網頁上的完整表單）")

        # 解析簽核意見
        comments = root.find(".//Comment")
        if comments is not None:
            sites = comments.findall("Site")
            lines.append("\n📝 簽核歷程：")
            for site in sites:
                order = site.get("order", "")
                site_type = site.get("type", "")

                signers = site.findall(".//Signer")
                for signer in signers:
                    sign_time = signer.get("signTime", "")
                    signer_account = signer.findtext("Account", "")
                    signer_result = signer.findtext("Result", "")
                    signer_text = signer.findtext("Text", "")

                    result_map_site = {"Approve": "同意", "Disapprove": "否決", "Return": "退簽"}
                    # 結果為空代表該站尚未處理，表單目前停在此站
                    result_desc = result_map_site.get(signer_result, signer_result) or "待簽"

                    line = f"  站點 {order} ({site_type}): {signer_account} → {result_desc}"
                    if sign_time:
                        line += f" ({sign_time})"
                    if signer_text:
                        line += f"\n    意見: {signer_text}"
                    lines.append(line)

        return "\n".join(lines)

    except etree.XMLSyntaxError:
        return "📄 簽核結果已取得但 XML 格式無法解析"
