from __future__ import annotations
import html
import re
from typing import Optional
from urllib.parse import urlparse
from .constants import _ADD_FORM_SCRIPT_PATH
from .parsing import (
    _parse_hidden_fields,
    _parse_inline_controls,
    _parse_dialog_fields,
    _find_row_editor_openers,
    _lookup_dialog_target,
    _parse_datagrid_columns,
)
from .schema import FormSchema
from .payload import (
    _form_state_payload,
    _picker_search_payload,
    _decode_json_attr,
)


class DialogOperation:
    """Dialog, picker, row-editor, and datagrid operations."""

    def __init__(self, session) -> None:
        self._session = session

    def get(self, path):
        return self._session.get(path)

    def post(self, path, data, *, retry_on_login=True):
        return self._session.post(path, data, retry_on_login=retry_on_login)

    def _parse(self, response):
        return self._session._parse(response)

    def strip_vpath(self, url):
        return self._session.strip_vpath(url)

    def _resolve_form_ids(self, value):
        return self._session._resolve_form_ids(value)

    def list_dialog_options(self, dialog_url: str, keyword: str = "", limit: int = 20) -> list:
        """Candidate entities a picker dialog returns for `keyword`.

        Same round-trip as `search_dialog`, but returns every row instead of one match — the
        caller needs to see the candidates to choose (or to tell the user nothing matched)
        rather than silently taking the first hit.
        """
        path_only = self.strip_vpath(dialog_url)
        parsed = urlparse(path_only)
        if parsed.scheme:
            path_only = parsed.path + (("?" + parsed.query) if parsed.query else "")
        resp = self.get(path_only)
        if "Login.aspx" in str(resp.url):
            return []
        tree = self._parse(resp)
        payload = dict(_parse_hidden_fields(tree))
        payload.update(_picker_search_payload(tree, keyword))
        resp2 = self.post(path_only, payload, retry_on_login=True)
        if "Login.aspx" in str(resp2.url):
            return []
        out = []
        for row in self._parse(resp2).xpath("//*[@jsonData] | //*[@jsondata]"):
            jd = _decode_json_attr(row.get("jsonData") or row.get("jsondata") or "")
            if isinstance(jd, dict):
                out.append(jd)
            if len(out) >= limit:
                break
        return out

    def dialog_options(self, form_version_id: str, field_code: str,
                       keyword: str = "", limit: int = 20) -> dict:
        """Picker candidates for a dialog field — block-level, or a nested row-editor picker.

        {ok, reason, field, rows}. `field_code` may be a block dialog field code, or the id/name
        of a lookup control inside a row editor (料號/科目 …). The nested picker's url is followed
        from the DOM (surfaced by dialog_structure) — never hardcoded.
        """
        want = (field_code or "").upper()
        # 1) Resolve a block-level picker from the apply page only. Do not call dialog_structure:
        # that would open the block dialog and its row editors before list_dialog_options opens
        # the picker again.
        fid, vid = self._resolve_form_ids(form_version_id)
        if not fid:
            return {"ok": False, "reason": f"無法對應表單 {form_version_id}", "field": "", "rows": []}
        resp = self.get(f"{_ADD_FORM_SCRIPT_PATH}?formId={fid}&formVersionId={vid}&mode=apply")
        if "Login.aspx" in str(resp.url):
            return {"ok": False, "reason": "redirected to Login.aspx", "field": "", "rows": []}
        apply_page = self.get(self.strip_vpath(str(resp.url)))
        block = next((
            fb for fb in FormSchema.parse(
                self._parse(apply_page),
                include_dialog_companions=True,
            ).as_dicts()
            if (fb.get("code") or "").upper() == want
        ), None)
        if block is not None:
            full = (block.get("dialog_url") or "").replace("&amp;", "&")
            if not full:
                return {"ok": False, "reason": f"欄位 {field_code} 取不到查詢視窗位址",
                        "field": block.get("label") or "", "rows": []}
            return {"ok": True, "reason": "",
                    "field": f"{block.get('label') or ''}({block.get('code') or field_code})",
                    "rows": self._session.list_dialog_options(full, keyword, limit)}
        # 2) A nested control has no top-level field code. Inspect blocks lazily and return as
        # soon as the requested row-editor control is found.
        nested = self.dialog_structure(form_version_id, nested_control=field_code)
        if not nested.get("ok"):
            return {"ok": False, "reason": nested.get("reason", ""), "field": "", "rows": []}
        for f in nested["fields"]:
            for red in f.get("row_editors", []):
                for c in red.get("fields", []):
                    ids = {(c.get("id") or "").upper(), (c.get("name") or "").upper()}
                    if want in ids and want and c.get("picker_url"):
                        return {"ok": True, "reason": "",
                                "field": f"{c.get('label') or field_code}({field_code})",
                                "rows": self._session.list_dialog_options(c["picker_url"], keyword, limit)}
        return {"ok": False, "reason": f"找不到對話框欄位 {field_code}", "field": "", "rows": []}

    def _dialog_opener_name(self, tree, dialog_url: str) -> tuple:
        """Return the parent control that opens `dialog_url`.

        The dialog callback posts this control back to append the confirmed row to its grid.
        """
        base = dialog_url.split("/")[-1].split("?")[0]
        for el in tree.xpath("//input[@onclick] | //a[@onclick] | //button[@onclick]"):
            if base in html.unescape(el.get("onclick") or ""):
                return (
                    el.get("name") or "",
                    el.get("value") or "",
                    (el.get("type") or "").lower() == "submit",
                )
        return ("", "", False)

    def _resolve_dialog_url(self, form_version_id: str, field_code: str) -> str:
        """Full dialog URL for a form's dialog field, as the apply page hands it out.

        The URL already carries whatever key that dialog persists by (GridDataID for plugin row
        editors, formVersionId+fieldId+scriptId for the native one), so it is inherited, never
        reconstructed — that is what keeps this layer free of per-dialog knowledge.
        """
        fid, vid = self._resolve_form_ids(form_version_id)
        if not fid:
            return ""
        resp = self.get(f"{_ADD_FORM_SCRIPT_PATH}?formId={fid}&formVersionId={vid}&mode=apply")
        tree = self._parse(self.get(self.strip_vpath(str(resp.url))))
        for fb in FormSchema.parse(tree, include_dialog_companions=True).as_dicts():
            if (fb.get("code") or "").upper() == field_code.upper():
                return (fb.get("dialog_url") or "").replace("&amp;", "&")
        return ""

    def operate_dialog(self, form_version_id: str, field_code: str,
                       values: Optional[dict] = None, press: str = "") -> dict:
        """PROBE ONLY — set values in a dialog, press one button, report what the server changed.

        **Cannot be used to build up detail rows.** Each call re-opens the apply page and so gets a
        fresh GridDataID/scriptId; anything a confirm writes lands in a session this call then
        abandons, and the later `apply_form` runs in yet another one. Row writing therefore lives
        inside `apply_form`, which keeps one session throughout.

        Deliberately knows nothing about what any dialog means or which button confirms it —
        `press` comes from the caller. Use it to discover behaviour (does pressing this populate
        those fields?), then record the answer in the form's skill.

        Returns {ok, reason, url, before, after, changed}.
        """
        url = self._resolve_dialog_url(form_version_id, field_code)
        if not url:
            return {"ok": False, "reason": f"欄位 {field_code} 取不到對話框位址",
                    "url": "", "before": {}, "after": {}, "changed": {}}
        path = self.strip_vpath(url if url.startswith("/") else "/" + url)
        r = self.get(path)
        if "Login.aspx" in str(r.url):
            return {"ok": False, "reason": "redirected to Login.aspx",
                    "url": url, "before": {}, "after": {}, "changed": {}}
        tree = self._parse(r)

        def _state(t) -> dict:
            st = {}
            for el in t.xpath("//input[@type='text'] | //select | //textarea"):
                n = el.get("name") or ""
                if not n or n.startswith("__"):
                    continue
                if el.tag == "select":
                    sel = [o for o in el.xpath(".//option") if o.get("selected") is not None]
                    st[n] = (sel[0].get("value") or "") if sel else ""
                elif el.tag == "textarea":
                    st[n] = el.text or ""
                else:
                    st[n] = el.get("value") or ""
            return st

        before = _state(tree)
        payload = _form_state_payload(tree)
        unknown = []
        for k, v in (values or {}).items():
            hit = next((n for n in before if n == k or n.split("$")[-1] == k
                        or n.split("$")[-1].lower() == str(k).lower()), "")
            if not hit:
                unknown.append(k)
                continue
            payload[hit] = str(v)
        if unknown:
            return {"ok": False, "reason": f"這些控制項不在對話框中：{unknown}（請用 get_dialog_structure 核對）",
                    "url": url, "before": before, "after": {}, "changed": {}}
        if press:
            btn = next((el for el in tree.xpath("//input[@type='submit'] | //input[@type='button']")
                        if (el.get("name") or "").split("$")[-1] == press
                        or (el.get("id") or "").split("_")[-1] == press), None)
            if btn is not None:
                payload[btn.get("name")] = btn.get("value") or ""
            elif press in r.text:
                # Telerik RadButtons post through __EVENTTARGET rather than a name=value pair
                payload["__EVENTTARGET"] = f"ctl00${press}" if not press.startswith("ctl00") else press
                payload["__EVENTARGUMENT"] = ""
                payload["FASTReturnValue"] = "[DefaultNullValue]"
            else:
                return {"ok": False, "reason": f"對話框中找不到按鈕 {press}",
                        "url": url, "before": before, "after": {}, "changed": {}}
        r2 = self.post(path, payload, retry_on_login=False)
        after = _state(self._parse(r2))
        changed = {k: {"from": before.get(k, ""), "to": v}
                   for k, v in after.items() if before.get(k, "") != v}
        return {"ok": True, "reason": "", "url": url,
                "before": before, "after": after, "changed": changed}

    def search_dialog(
        self,
        dialog_url: str,
        search_key: str = "",
        match_code: str = "",
        code_field: str = "EntityId",
    ) -> Optional[dict]:
        """GET dialog, POST search, find row where jsondata[code_field] == match_code.

        Returns the parsed jsondata dict or None if not found.
        """
        # Strip vpath from the dialog_url
        path_only = self.strip_vpath(dialog_url)
        # If strip_vpath returned a full URL, extract just the path+query
        parsed = urlparse(path_only)
        if parsed.scheme:
            path_only = parsed.path + (("?" + parsed.query) if parsed.query else "")

        resp = self.get(path_only)
        if "Login.aspx" in str(resp.url):
            return None
        tree = self._parse(resp)
        payload = dict(_parse_hidden_fields(tree))
        payload.update(_picker_search_payload(tree, search_key))
        resp2 = self.post(path_only, payload, retry_on_login=True)
        if "Login.aspx" in str(resp2.url):
            return None

        tree2 = self._parse(resp2)
        # Result rows carry the full entity JSON (incl. its Id) in a jsonData attribute.
        # Note: the CDS pickers spell it `jsonData` (capital D) and double-HTML-encode it,
        # while some native dialogs use lowercase `jsondata` — match both.
        rows = tree2.xpath("//*[@jsonData] | //*[@jsondata]")
        for row in rows:
            jd = _decode_json_attr(row.get("jsonData") or row.get("jsondata") or "")
            if jd is None:
                continue
            if str(jd.get(code_field) or "").lower() == match_code.lower():
                return jd
        # If exact match not found, try substring on CompanyName / EntityId
        if search_key:
            for row in rows:
                jd = _decode_json_attr(row.get("jsonData") or row.get("jsondata") or "")
                if jd is None:
                    continue
                company = str(jd.get("CompanyName") or jd.get("EntityId") or "")
                if search_key.lower() in company.lower():
                    return jd
        return None

    # ── general dataGrid rows ─────────────────────────────────────────

    def datagrid_columns(self, dialog_full_path: str) -> list:
        """GET a dataGrid row-editor dialog and return its columns (see _parse_datagrid_columns)."""
        r = self.get(self.strip_vpath(dialog_full_path))
        if "Login.aspx" in str(r.url):
            return []
        return _parse_datagrid_columns(r.text)

    # ── apply_form_web ────────────────────────────────────────────────


    def search_users(self, keyword: str) -> list:
        """Search UOF users by name or account keyword via ChoiceHandler.ashx.

        Returns list of {UserGuid, Name, display_name, account}.
        Name field format from server: "顯示名稱(帳號)".
        """
        import json as _json
        resp = self.post("/Common/ChoiceCenter/ChoiceHandler.ashx", {
            "action": "SearchUser",
            "userType": "Employee",
            "keyword": keyword,
            "userGuid": "",
            "onlyAvailable": "1",
            "displayAllDept": "1",
        }, retry_on_login=True)
        try:
            raw = _json.loads(resp.text)
        except Exception:
            return []
        results = []
        for item in raw:
            name_full = item.get("Name") or ""
            m = re.match(r"^(.*?)\(([^)]+)\)$", name_full)
            display_name = m.group(1).strip() if m else name_full
            account = m.group(2) if m else ""
            results.append({
                "UserGuid": item.get("UserGuid") or "",
                "Name": name_full,
                "display_name": display_name,
                "account": account,
            })
        return results

    def dialog_structure(
        self,
        form_version_id: str,
        field_code: str = "",
        *,
        nested_control: str = "",
    ) -> dict:
        """Inner field structure of a form's dialog-backed fields.

        Opens the apply page, finds each dialog field's own page and parses it as a mini-form.
        `field_code` empty ⇒ every dialog field on the form. `nested_control` asks for the first
        block containing that row-editor control and stops further dialog fetches.

        Returns {ok, reason, fields: [{code, label, dialog, inner: [...]}]}.
        """
        fid, vid = self._resolve_form_ids(form_version_id)
        if not fid:
            return {"ok": False, "reason": f"無法對應表單 {form_version_id}", "fields": []}
        resp = self.get(f"{_ADD_FORM_SCRIPT_PATH}?formId={fid}&formVersionId={vid}&mode=apply")
        if "Login.aspx" in str(resp.url):
            return {"ok": False, "reason": "redirected to Login.aspx", "fields": []}
        first = self.get(self.strip_vpath(str(resp.url)))
        tree = self._parse(first)
        want = (field_code or "").upper()
        nested_want = (nested_control or "").upper()
        out = []
        for fb in FormSchema.parse(tree, include_dialog_companions=True).as_dicts():
            if fb.get("input_type") != "dialog":
                continue
            code = fb.get("code") or ""
            if want and code.upper() != want:
                continue
            url = (fb.get("dialog_url") or "").replace("&amp;", "&")
            entry = {"code": code, "label": fb.get("label") or "", "dialog": url.split("/")[-1][:60],
                     "dialog_url": url, "inner": [], "inline": [], "row_editor": "",
                     "press": "", "note": ""}
            # The button that opens this block's dialog. Callers need its name to press it
            # (`_lookups`), and it is knowable from the DOM — leaving it out forced every form's
            # skill to hardcode one.
            entry["press"] = (self._dialog_opener_name(tree, url)[0] or "").split("$")[-1] if url else ""
            # Plugin forms render their real controls inline inside the field's own
            # versionFieldUC block; _parse_field_blocks only reports the block itself, so those
            # controls (付款人 / 立帳日期 / 金額 …) are invisible without this.
            m_uc = re.search(r"(versionFieldUC\d+)", fb.get("input_name") or "")
            if m_uc:
                entry["inline"] = _parse_inline_controls(tree, m_uc.group(1))
            # A dialog carrying GridDataID *is* this block's row editor, rather than a picker
            # sitting beside one. Both shapes exist; report either as a row editor so callers get
            # the opener button and the row's own nested pickers the same way.
            block_is_row_editor = bool(url) and "GridDataID" in url
            if not url:
                entry["note"] = "取不到對話框位址"
            else:
                try:
                    d = self.get(self.strip_vpath(url if url.startswith("/") else "/" + url))
                    inner = _parse_datagrid_columns(d.text) or _parse_dialog_fields(d.text)
                    if not inner:
                        entry["note"] = "對話框內容無法解析（版型未知）"
                    if block_is_row_editor:
                        red = {"open_button": entry["press"],
                               "dialog": (entry["dialog"] or "").split("?")[0],
                               "fields": [], "note": entry["note"]}
                        for c in inner:
                            pd = ""
                            for bt in (c.get("lookup_buttons") or []):
                                pd = _lookup_dialog_target(d.text, bt)
                                if pd:
                                    break
                            c["picker_dialog"] = pd.split("/")[-1].split("?")[0] if pd else ""
                            c["picker_url"] = pd
                            red["fields"].append(c)
                        entry.setdefault("row_editors", []).append(red)
                    else:
                        entry["inner"] = inner
                except Exception as ex:
                    entry["note"] = f"讀取對話框失敗：{type(ex).__name__}: {ex}"
            # Detail-row editors this block owns (add-row buttons on the apply page). Parse each
            # editor's controls; if a control itself opens a picker, capture that nested picker's
            # dialog so it can be queried. All read from the DOM — no form/dialog name hardcoded.
            main_base = (entry["dialog"] or "").split("?")[0]
            for ed in _find_row_editor_openers(
                first.text,
                {main_base},
                m_uc.group(1) if m_uc else "",
            ):
                red = {"open_button": ed["open_button"], "dialog": ed["basename"], "fields": [], "note": ""}
                try:
                    edoc = self.get(self.strip_vpath(ed["url"] if ed["url"].startswith("/") else "/" + ed["url"]))
                    for c in _parse_dialog_fields(edoc.text):
                        pd = ""
                        for b in (c.get("lookup_buttons") or []):
                            pd = _lookup_dialog_target(edoc.text, b)
                            if pd:
                                break
                        c["picker_dialog"] = pd.split("/")[-1].split("?")[0] if pd else ""
                        c["picker_url"] = pd
                        red["fields"].append(c)
                except Exception as ex:
                    red["note"] = f"讀取列編輯器失敗：{type(ex).__name__}: {ex}"
                # An "editor" with no fillable controls (attachment/file-center dialogs) is not a
                # detail-row grid — skip it so only real detail editors surface.
                if red["fields"]:
                    entry.setdefault("row_editors", []).append(red)
            if nested_want:
                found = any(
                    nested_want in {
                        (c.get("id") or "").upper(),
                        (c.get("name") or "").upper(),
                    }
                    for red in entry.get("row_editors", [])
                    for c in red.get("fields", [])
                )
                if found:
                    return {"ok": True, "reason": "", "fields": [entry]}
                continue
            out.append(entry)
        return {"ok": True, "reason": "", "fields": out}

    # ── 待簽清單 (Homepage 待簽表單 widget) ────────────────────────────
