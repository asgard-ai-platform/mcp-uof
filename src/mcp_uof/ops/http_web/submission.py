from __future__ import annotations
import re
from datetime import date
from ..._log import eprint as _eprint
from .constants import _ADD_FORM_SCRIPT_PATH, _html_fromstring
from .parsing import _parse_hidden_fields
from .schema import FormSchema
from .field_codec import FieldCodec
from .runtime import WebFormsRuntime
from .validation import _mark_filled


class SubmissionOperation:
    """Owns open, fill, validate, save, send, route confirmation, and outcome."""

    def __init__(self, session) -> None:
        self._session = session

    @property
    def detail_operation(self):
        return self._session.detail_operation

    def get(self, path):
        return self._session.get(path)

    def post(self, path, data, *, retry_on_login=False):
        return self._session.post(path, data, retry_on_login=retry_on_login)

    def _parse(self, response):
        return self._session._parse(response)

    def strip_vpath(self, url):
        return self._session.strip_vpath(url)

    def _resolve_form_ids(self, value):
        return self._session._resolve_form_ids(value)

    def _lookup_created_form(self, form_number):
        return self._session._lookup_created_form(form_number)

    def search_dialog(self, *args, **kwargs):
        return self._session.dialog_operation.search_dialog(*args, **kwargs)

    def submit(
        self,
        form_version_id: str,
        fields: dict,
        comment: str = "",
        urgent_level: str = "2",
        submit: bool = True,
    ) -> dict:
        """Fill and submit a form via httpx. Returns {ok, task_id, form_number, filled, errors, reason}.

        `submit=False` fills and only 儲存 (saves a draft — routes to nobody), returning
        `draft=True` without 送出; useful for verifying fill without creating a routed task.
        """
        opened = self._apply_open_form_page(form_version_id, urgent_level)
        if "first_site_path" not in opened:
            return opened  # an error dict (lacks first_site_path)
        first_site_path = opened["first_site_path"]
        resp2 = opened["resp2"]
        tree2 = opened["tree2"]
        detail_page = WebFormsRuntime.hydrate(resp2)
        payload = opened["payload"]
        schema = opened["schema"]
        codec = FieldCodec(tree2)

        errors: list = []      # soft: extra/unknown fields, skipped — form still submits
        blocking: list = []    # hard: bad option value / missing required — refuse to submit
        filled: dict = {}
        bad_option_codes: set = set()  # fields flagged as invalid-option — don't also报「未提供」
        datagrid_pending: list = []  # (code, fb, rows) — filled via dialog after the main loop

        # 4. Fill each field
        for code, value in (fields or {}).items():
            field = schema.find(code)
            fb = field.as_dict() if field else None
            # Any value the caller supplied that we cannot write is silent data loss: the form
            # would submit looking successful while missing exactly what was asked for. Block.
            if fb is None:
                blocking.append(f"欄位 {code} 在表單中找不到——值『{value}』無法寫入")
                continue

            itype = fb.get("input_type", "text")
            iname = fb.get("input_name") or ""

            if field.disabled:
                errors.append(codec.encode(field, value, payload).warning)
                continue

            if itype == "dataGrid":
                # detail rows are filled via the row-editor dialog after this loop
                if isinstance(value, (list, tuple)) and value:
                    datagrid_pending.append((fb.get("code") or code, fb, list(value)))
                else:
                    blocking.append(f"明細「{fb.get('label') or code}」的值需為非空列清單（list），收到 {type(value).__name__}")
                continue

            if itype in ("autoNumber", "fileButton"):
                _eprint(f"[ops.http_web] skip {code} ({itype})")
                continue

            if itype == "dialog" or isinstance(value, (dict, list, tuple)):
                # A structured value addresses a plugin block even when the block was inferred as
                # plain text because it exposes no dialog button.
                dialog_url = fb.get("dialog_url") or ""
                if isinstance(value, (list, tuple, dict)):
                    m_uc = re.search(r"(versionFieldUC\d+)", iname or "")
                    detail_result = self.detail_operation.persist_plugin_block(
                        first_site_path,
                        detail_page,
                        payload,
                        m_uc.group(1) if m_uc else "",
                        fb.get("label") or code,
                        value,
                    )
                    detail_page = detail_result.page
                    tree2 = detail_page.tree
                    payload.update(detail_page.state)
                    detail_label = fb.get("label") or code
                    errors.extend(
                        f"明細「{detail_label}」：{note}"
                        for note in detail_result.notes
                    )
                    if not detail_result.ok:
                        blocking.extend(
                            f"明細「{detail_label}」：{error}"
                            for error in detail_result.errors
                        )
                        if not detail_result.errors:
                            blocking.append(f"明細「{detail_label}」未完整")
                        continue
                    _mark_filled(filled, code, fb, detail_result.summary)
                    continue
                if not dialog_url:
                    blocking.append(f"欄位「{fb.get('label') or code}」是查詢視窗型但取不到視窗位址，"
                                    f"值『{value}』無法寫入")
                    continue
                jd = self.search_dialog(dialog_url, search_key=str(value), match_code=str(value))
                if jd is None:
                    blocking.append(f"欄位「{fb.get('label') or code}」在查詢視窗中找不到『{value}』，無法寫入")
                    continue
                # Fill display + hidden companion
                display_name = fb.get("display_name") or ""
                hidden_name = fb.get("hidden_name") or ""
                display_val = str(jd.get("CompanyName") or jd.get("EntityId") or "")
                hidden_val = str(jd.get("Id") or "")
                if display_name:
                    payload[display_name] = display_val
                if hidden_name:
                    payload[hidden_name] = hidden_val
                # Also fill the trigger button field value if possible
                if iname:
                    payload[iname] = display_val
                _mark_filled(filled, code, fb, display_val)

            else:
                encoded = codec.encode(field, value, payload)
                if encoded.warning:
                    errors.append(encoded.warning)
                elif encoded.blocking:
                    blocking.append(encoded.blocking)
                    if itype in ("dropDown", "radio", "checkbox"):
                        bad_option_codes.add(fb.get("code") or "")
                else:
                    _mark_filled(filled, code, fb, encoded.filled_value)

        # 4b. Fill general-dataGrid detail rows via their row-editor dialog. Each row's dialog
        #     確定 persists to the server store keyed by this session's scriptId; the 儲存/送出
        #     below (same session) then reads them. Done before validation so required grids count.
        for code, fb, rows in datagrid_pending:
            # Classic templates need grid_field_id because their public code is only the label.
            dlg_path = self.detail_operation.discover_datagrid_editor(
                detail_page, fb.get("grid_field_id") or fb.get("code") or code
            )
            if not dlg_path:
                blocking.append(f"明細「{fb.get('label') or code}」找不到列編輯對話框位址，無法填列")
                continue
            added, detail_errors = self.detail_operation.persist_datagrid_rows(dlg_path, rows)
            if added:
                _mark_filled(filled, code, fb, f"{added} 列")
            for e in detail_errors:
                errors.append(f"明細 {code}: {e}")
            if added != len(rows) or detail_errors:
                blocking.append(
                    f"明細「{fb.get('label') or code}」{len(rows)} 列僅成功加入 {added} 列，未完整"
                )

        # 5. Fill comment
        if comment:
            for inp in tree2.xpath("//textarea"):
                name = inp.get("name") or ""
                if "tbxComment" in name or "comment" in name.lower():
                    payload[name] = comment
                    break

        return self._apply_submit_form(
            submit=submit,
            first_site_path=first_site_path,
            tree2=tree2,
            payload=payload,
            filled=filled,
            errors=errors,
            blocking=blocking,
            schema=schema,
            bad_option_codes=bad_option_codes,
        )

    def _apply_submit_form(
        self,
        *,
        submit: bool,
        first_site_path: str,
        tree2,
        payload: dict,
        filled: dict,
        errors: list,
        blocking: list,
        schema: FormSchema,
        bad_option_codes: set,
    ) -> dict:
        """Validate required fields, then run 儲存→送出→派單確認 and build the result."""
        def _extract_result(resp_obj):
            """Return (task_id, form_number, submitted) from a POST response."""
            url_str = str(resp_obj.url)
            html_str = resp_obj.text
            tid = ""
            m_task = re.search(r"TASK_ID=([0-9a-f-]{36})", url_str + html_str, re.I)
            if m_task:
                tid = m_task.group(1)
            fno = ""
            m_no = re.search(r"\b([A-Z]{2,4}\d{9,})\b", html_str)
            if m_no:
                fno = m_no.group(1)
            # dialog.close() in response means the popup closed = form submitted successfully
            done = "dialog.close()" in html_str or "$uof.dialog.close()" in html_str
            return tid, fno, done

        def _refresh_viewstate(src_html: str, target: dict) -> None:
            nh = _parse_hidden_fields(_html_fromstring(src_html))
            for k in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED", "__EVENTVALIDATION"):
                if k in nh:
                    target[k] = nh[k]

        def _result(task_id, form_number, ok=True, reason="", unconfirmed=False, form_name=""):
            r = {"ok": ok, "reason": reason, "task_id": task_id, "form_number": form_number,
                 "form_name": form_name, "filled": filled, "errors": errors}
            if unconfirmed:
                r["submitted_unconfirmed"] = True
            return r

        # 5b. UOF may silently discard missing or invalid values, so validate every parsed field
        # before submitting.
        for field in schema.missing_required(filled, bad_option_codes):
            label = field.label or field.code or "(未命名欄位)"
            itype = field.input_type
            if itype == "dataGrid":
                blocking.append(f"必填明細「{label}」未提供任何列——請在該欄位帶列清單（每列一個 dict/list）")
            elif itype == "fileButton":
                blocking.append(f"必填欄位「{label}」是附件型，apply_form 目前無法上傳，需在 UOF 網頁完成")
            elif itype == "dialog":
                blocking.append(f"必填欄位「{label}」需以查詢視窗選取，未提供或找不到對應項目")
            else:
                blocking.append(f"必填欄位「{label}」未提供")

        if blocking:
            errors.extend(blocking)
            return _result(
                "", "", ok=False,
                reason="填寫未通過驗證，未送出（避免建立不完整的單）——請補齊必填、改用合法選項值後重試",
            )

        # 完整送出序列：儲存 → 送出 → 派單頁確認。
        # 6. 儲存（RadButton1）：存草稿、伺服器配 scriptId
        payload["__EVENTTARGET"] = "ctl00$MasterPageRadButton1"
        payload["__EVENTARGUMENT"] = ""
        if not submit:
            # 儲存-as-draft requires a draft name. It is a plain text input, so it is absent from
            # the hidden-only payload — look it up on the page, or the save always fails and
            # submit=False is unusable.
            for el in tree2.xpath("//input[@type='text'][@name]"):
                if el.get("name", "").split("$")[-1] == "tbxScriptName":
                    payload[el.get("name")] = f"MCP draft {date.today():%Y%m%d}"
                    break
        resp_save = self.post(first_site_path, payload, retry_on_login=False)
        if "Login.aspx" in str(resp_save.url):
            return _result("", "", ok=False, reason="redirected to Login.aspx on save")

        if not submit:
            # Only count visible validator output. A bare word scan matched「請選擇」inside every
            # dropdown's placeholder <option>, so any form with a select looked like it failed.
            vt = self._parse(resp_save)
            reds = sorted({
                t for t in (
                    re.sub(r"\s+", " ", "".join(el.itertext())).strip()
                    for el in vt.xpath(
                        "//*[contains(@class,'Error') or contains(@class,'error')]"
                        " | //span[@style and contains(@style,'color:Red')]"
                        " | //*[contains(@id,'Validator') or contains(@id,'valSummary')]")
                )
                if t and len(t) <= 60 and t.strip("＊* ")
            })
            r = _result("", "", ok=not reds,
                        reason="草稿已儲存（未送出）" if not reds else f"儲存後仍有必填未過：{reds}")
            r["draft"] = True
            r["errors"] = errors + reds
            return r

        # 7. 送出（RadButton3）：回應帶出 FirstSiteSend URL
        _refresh_viewstate(resp_save.text, payload)
        payload["__EVENTTARGET"] = "ctl00$MasterPageRadButton3"
        resp_send = self.post(first_site_path, payload, retry_on_login=False)
        send_html = resp_send.text
        m_fss = re.search(r"[~/][^\"'\s]*FirstSiteSend\.aspx\?[^\"'\s]*", send_html)
        if not m_fss:
            # 沒出現派單頁：可能舊式直接成單，退回原偵測；否則回未確認
            tid, fno, done = _extract_result(resp_send)
            if done or tid:
                return _result(tid, fno)
            return _result("", "", reason="送出後未見 FirstSiteSend 派單頁，請至待辦確認", unconfirmed=True)

        # 8. GET FirstSiteSend（派單/確認頁）
        fss_path = m_fss.group(0).replace("&amp;", "&").lstrip("~")
        resp_fss = self.get(fss_path)
        if "Login.aspx" in str(resp_fss.url):
            return _result("", "", ok=False, reason="redirected to Login.aspx on FirstSiteSend")
        confirm_payload = _parse_hidden_fields(self._parse(resp_fss))
        # TODO: map first_signer_account to the signer picker before confirming free-flow routing.
        confirm_payload["__EVENTTARGET"] = "ctl00$MasterPageRadButton2"  # 確定
        confirm_payload["__EVENTARGUMENT"] = ""

        # 9. 確定（RadButton2）→ 真正送進工作流
        resp_confirm = self.post(fss_path, confirm_payload, retry_on_login=False)
        chtml = resp_confirm.text
        m_created = re.search(r"表單\s*([A-Za-z]{2,4}\d{6,})\s*已建立", chtml)
        form_number = m_created.group(1) if m_created else ""
        created = bool(m_created) or "dialog.close()" in chtml or "$uof.dialog.close()" in chtml
        if not created:
            return _result("", form_number, reason="確定後未見成單訊號，請至待辦確認", unconfirmed=True)

        # 10. 依表單編號列近期單，取回 TaskId 與解析後的表單名。
        task_id, real_name = self._lookup_created_form(form_number)
        return _result(task_id, form_number, form_name=real_name,
                       reason="" if task_id else "已成單但未取得 TaskId（可用 query_forms 查）",
                       unconfirmed=not task_id)

    def _apply_open_form_page(self, form_version_id: str, urgent_level: str) -> dict:
        """Resolve ids, open FirstSite, parse hidden fields + field map.

        Returns the parsing context on success, or an error dict (which lacks
        the "first_site_path" key) when navigation/parsing fails."""
        fid, vid = self._resolve_form_ids(form_version_id)
        if not fid:
            return {
                "ok": False,
                "reason": f"無法從 ApplyFormList 對應 {form_version_id}（既非 formId 也非 formVersionId）",
                "task_id": "", "form_number": "", "filled": {}, "errors": [],
            }

        # 1. GET AddFormScript.aspx with mode=apply → follow redirect to FirstSite.aspx
        apply_path = f"{_ADD_FORM_SCRIPT_PATH}?formId={fid}&formVersionId={vid}&mode=apply"
        resp = self.get(apply_path)
        if "Login.aspx" in str(resp.url):
            return {"ok": False, "reason": "redirected to Login.aspx", "task_id": "", "form_number": "", "filled": {}, "errors": []}

        # The final URL after redirect is the FirstSite.aspx path; strip vpath for GET/POST
        first_site_path = self.strip_vpath(str(resp.url))

        _eprint(f"[ops.http_web] apply → first_site_path={first_site_path}")

        # 2. GET FirstSite.aspx, parse hidden + field map
        resp2 = self.get(first_site_path)
        if "Login.aspx" in str(resp2.url):
            return {"ok": False, "reason": "redirected to Login.aspx on FirstSite", "task_id": "", "form_number": "", "filled": {}, "errors": []}
        tree2 = self._parse(resp2)
        payload = _parse_hidden_fields(tree2)

        # 3. Parse field blocks with companion info for dialog
        schema = FormSchema.parse(tree2, include_dialog_companions=True)
        if not schema.fields:
            # Validation depends on the schema. Refuse an unvalidated header-only submission
            # when neither parser can identify fields.
            return {"ok": False, "task_id": "", "form_number": "", "filled": {},
                    "errors": ["此表單目前無法解析出任何欄位（httpx 解析器 bug，非表單本身限制），"
                               "無法驗證內容完整性，已擋下——請改到 UOF 網頁操作，並回報開發面追查。"],
                    "reason": "表單欄位解析失敗，為避免建立無法驗證的空殼單，未送出"}
        # Handle urgent_level if there's an urgentLevel field
        for field in schema.fields:
            name = field.input_name
            if "urgentLevel" in name or "urgent" in name.lower():
                payload[name] = urgent_level
                break

        return {
            "first_site_path": first_site_path,
            "resp2": resp2,
            "tree2": tree2,
            "payload": payload,
            "schema": schema,
        }
