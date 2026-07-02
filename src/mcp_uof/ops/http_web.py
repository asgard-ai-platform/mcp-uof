"""
HttpWebBackend — httpx + lxml UOF web ops (no Playwright).

Replaces the Playwright-driven WebBackend for environments where Chromium is
unavailable (e.g. Alpine Linux). All browser-simulated round-trips are done with
plain HTTPS requests; HTML is parsed with lxml.

Session management mirrors WebRuntime but uses httpx.Client (thread-safe) with
automatic re-login when a response redirects to Login.aspx.
"""
from __future__ import annotations

import html
import os
import re
import threading
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

from .._log import eprint as _eprint
from .base import OpsBackend, web_not_implemented


# ── Page path constants ───────────────────────────────────────────────
_LOGIN_PATH = "/Login.aspx"
_HOMEPAGE_PATH = "/Homepage.aspx"
_FORM_QUERY_PATH = "/WKF/FormUse/PersonalBox/MyFormList.aspx?item=FormQuery"
_APPLY_FORM_LIST_PATH = "/WKF/FormUse/PersonalBox/ApplyFormList.aspx"
_ADD_FORM_SCRIPT_PATH = "/WKF/FormUse/AddFormScript.aspx"

# ASP.NET hidden inputs to ALWAYS carry but never treat as field values.
_ASPNET_HIDDEN = frozenset([
    "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED",
    "__EVENTTARGET", "__EVENTARGUMENT", "__EVENTVALIDATION",
    "hdflag", "hfIsAdAuth",
])

_SKIP_HIDDEN_PREFIXES = ("__VIEWSTATE", "__EVENT", "ClientState", "TSM", "TSSM")


# ── lxml import (fail loudly so the error is obvious) ────────────────
try:
    from lxml import etree as _etree
    from lxml.html import fromstring as _html_fromstring
except ImportError as _e:
    raise ImportError(
        "lxml is required for http_web mode. Install: `uv add lxml`. "
        f"Original error: {_e}"
    ) from _e

try:
    import httpx as _httpx
except ImportError as _e:
    raise ImportError(
        "httpx is required for http_web mode. Install: `uv add httpx`. "
        f"Original error: {_e}"
    ) from _e


# ── Helpers ───────────────────────────────────────────────────────────

def _parse_hidden_fields(tree) -> dict:
    """Collect all <input type=hidden> from lxml tree into a flat dict."""
    result = {}
    for el in tree.xpath("//input[@type='hidden']"):
        name = el.get("name") or ""
        val = el.get("value") or ""
        if name:
            result[name] = val
    return result


def _parse_field_blocks(tree, include_dialog_companions: bool = False) -> list:
    """Parse table.fieldWidth blocks from an lxml HTML tree.

    Returns list of field dicts; skip blocks with neither label nor code.
    If include_dialog_companions=True, also populate display_name and hidden_name for dialog fields.
    """
    fields = []
    blocks = tree.xpath("//table[contains(@class,'fieldWidth')]")
    for block in blocks:
        try:
            # Label from .TitleFont
            title_els = block.xpath(".//*[contains(@class,'TitleFont')]")
            label = ""
            if title_els:
                label = (title_els[0].text_content() if hasattr(title_els[0], "text_content")
                         else "".join(title_els[0].itertext())).strip()

            # Code from .FieldHide
            code_els = block.xpath(".//*[contains(@class,'FieldHide')]")
            code = ""
            if code_els:
                code_raw = (code_els[0].text_content() if hasattr(code_els[0], "text_content")
                            else "".join(code_els[0].itertext())).strip()
                code = code_raw.strip("()").strip()

            if not label and not code:
                continue

            # Required: red asterisk
            required = False
            for span in block.xpath(".//span"):
                style = (span.get("style") or "").lower()
                if "color:red" in style.replace(" ", "") or "color: red" in style:
                    text = "".join(span.itertext())
                    if "＊" in text or "*" in text:
                        required = True
                        break

            # Special structures
            is_file = bool(block.xpath(
                ".//*[contains(@onclick,'RemoteFileDialog') or contains(@onclick,'FileCenter')]"
            ))
            is_datagrid = bool(block.xpath(
                ".//*[contains(@id,'DataGrid') or contains(@onclick,'SetupDataGridFieldValue')]"
            ))

            # Primary input: prefer first non-hidden in .fieldPadding, else anywhere
            def _first_input(xpath_expr):
                for el in block.xpath(xpath_expr):
                    return el
                return None

            input_el = _first_input(
                ".//*[contains(@class,'fieldPadding')]//input[@type!='hidden']"
                " | .//*[contains(@class,'fieldPadding')]//select"
                " | .//*[contains(@class,'fieldPadding')]//textarea"
            )
            if input_el is None:
                input_el = _first_input(
                    ".//input[@type!='hidden'] | .//select | .//textarea"
                )

            input_kind = ""
            input_name = ""
            input_class = ""
            input_type_attr = ""
            input_title = ""
            if input_el is not None:
                input_kind = (input_el.tag or "").lower()
                input_name = input_el.get("name") or ""
                input_class = input_el.get("class") or ""
                input_type_attr = (input_el.get("type") or "").lower()
                input_title = input_el.get("title") or ""

            # Dialog URL from onclick open2(...)
            dialog_url = ""
            for el in block.xpath(".//*[@onclick]"):
                onclick = el.get("onclick") or ""
                m = re.search(r"open2\(\s*['\"]([^'\"]+)['\"]", onclick)
                if m:
                    dialog_url = m.group(1)
                    break

            # Type inference
            cls_lower = input_class.lower()
            name_lower = input_name.lower()
            if is_datagrid:
                input_type = "dataGrid"
            elif is_file:
                input_type = "fileButton"
            elif "autonumber" in cls_lower or "tbxautonumber" in name_lower:
                input_type = "autoNumber"
            elif "raddatepicker" in cls_lower or "datepicker" in name_lower:
                input_type = "datePicker"
            elif "radnumeric" in cls_lower or "numerictextbox" in name_lower:
                input_type = "numeric"
            elif dialog_url:
                input_type = "dialog"
            elif input_kind == "textarea":
                input_type = "multiLineText"
            elif input_kind == "select":
                input_type = "dropDown"
            elif input_type_attr == "radio":
                input_type = "radio"
            elif input_type_attr == "checkbox":
                input_type = "checkbox"
            elif input_kind == "input":
                input_type = "text"
            else:
                input_type = "unknown"

            field: dict = {
                "code": code,
                "label": label,
                "required": required,
                "input_type": input_type,
                "input_name": input_name,
                "input_title": input_title,
                "dialog_url": dialog_url,
            }

            if include_dialog_companions and input_type == "dialog":
                # display_name: first text input in block that is NOT the dialog trigger button
                btn_name = input_name
                display_name = ""
                hidden_name = ""
                for el in block.xpath(".//input[@type='text']"):
                    n = el.get("name") or ""
                    if n and n != btn_name:
                        display_name = n
                        break
                for el in block.xpath(".//input[@type='hidden']"):
                    n = el.get("name") or ""
                    if n and not any(n.startswith(p) for p in _SKIP_HIDDEN_PREFIXES):
                        hidden_name = n
                        break
                field["display_name"] = display_name
                field["hidden_name"] = hidden_name

            fields.append(field)
        except Exception as ex:
            _eprint(f"[ops.http_web] ⚠️ field block parse error: {type(ex).__name__}: {ex}")
            continue
    return fields


# ── HttpSession ───────────────────────────────────────────────────────

class HttpSession:
    """Thread-safe httpx.Client with UOF session management."""

    def __init__(self) -> None:
        base_raw = os.environ.get("UOF_BASE_URL", "").rstrip("/")
        parsed = urlparse(base_raw)
        # _vpath is the virtual path prefix, e.g. /UOFTEST (may be empty)
        self._vpath = parsed.path.rstrip("/")
        # _base is scheme+host only
        self._base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        verify_env = os.environ.get("UOF_SSL_VERIFY", "true").lower()
        self._verify = verify_env not in ("false", "0", "no")
        self._client = _httpx.Client(
            verify=self._verify,
            follow_redirects=True,
            timeout=30.0,
        )
        self._lock = threading.Lock()
        self._form_id_version_map: Optional[dict] = None

    # ── Internal helpers ─────────────────────────────────────────────

    def _full_url(self, path: str) -> str:
        """Prepend base (scheme+host) to an absolute path."""
        return self._base + path

    def strip_vpath(self, url: str) -> str:
        """Remove virtual path prefix; always returns path+query (no scheme/host).

        Safe to pass result directly to get() / post(), which re-prepend base+vpath.
        """
        parsed = urlparse(url)
        path = parsed.path
        if self._vpath and path.startswith(self._vpath):
            path = path[len(self._vpath):]
            if not path.startswith("/"):
                path = "/" + path
        query_part = ("?" + parsed.query) if parsed.query else ""
        return path + query_part

    def _page_html(self, resp: "_httpx.Response") -> str:
        return resp.text

    def _parse(self, resp: "_httpx.Response"):
        """Parse response body with lxml and return tree."""
        return _html_fromstring(resp.text, base_url=str(resp.url))

    def _is_login_page(self, resp: "_httpx.Response") -> bool:
        return "Login.aspx" in str(resp.url)

    def _do_login(self) -> None:
        """GET Login.aspx, parse VIEWSTATE, POST credentials."""
        account = os.environ.get("UOF_ACCOUNT", "")
        password = os.environ.get("UOF_PASSWORD", "")
        login_url = self._full_url(self._vpath + _LOGIN_PATH)
        _eprint(f"[ops.http_web] 🔐 logging in: {login_url} (account={account})")
        resp = self._client.get(login_url)
        tree = self._parse(resp)
        hidden = _parse_hidden_fields(tree)
        payload = {
            **hidden,
            "txtAccount": account,
            "txtPwd": password,
            "btnSubmit": "登入",
            "hdflag": "false",
            "hfIsAdAuth": "false",
        }
        resp2 = self._client.post(login_url, data=payload)
        if "Login.aspx" in str(resp2.url):
            raise RuntimeError(
                f"UOF login failed, still on Login.aspx. "
                f"Check UOF_ACCOUNT / UOF_PASSWORD."
            )
        _eprint(f"[ops.http_web] ✅ login ok, landed at {resp2.url}")

    def get(self, path: str) -> "_httpx.Response":
        """GET path (relative to base+vpath), auto-relogin on Login.aspx redirect."""
        url = self._full_url(self._vpath + path)
        resp = self._client.get(url)
        if self._is_login_page(resp):
            _eprint(f"[ops.http_web] 🔄 session expired, re-logging in")
            self._do_login()
            resp = self._client.get(url)
        return resp

    def post(self, path: str, data: dict) -> "_httpx.Response":
        """POST to path (relative to base+vpath), auto-relogin on Login.aspx redirect."""
        url = self._full_url(self._vpath + path)
        resp = self._client.post(url, data=data)
        if self._is_login_page(resp):
            _eprint(f"[ops.http_web] 🔄 session expired on POST, re-logging in")
            self._do_login()
            resp = self._client.post(url, data=data)
        return resp

    def _ensure_logged_in(self) -> None:
        """Check homepage; login if redirected."""
        url = self._full_url(self._vpath + _HOMEPAGE_PATH)
        resp = self._client.get(url)
        if self._is_login_page(resp):
            self._do_login()

    # ── formId ↔ formVersionId mapping ──────────────────────────────

    def get_form_id_version_mapping(self) -> dict:
        """Parse ApplyFormList.aspx RadTreeView JSON; returns {formId: versionId} (lowercase)."""
        if self._form_id_version_map is not None:
            return self._form_id_version_map
        resp = self.get(_APPLY_FORM_LIST_PATH)
        html_text = resp.text
        pairs = re.findall(
            r'"value":"([0-9a-f]{8}-[0-9a-f-]{27})@([0-9a-f]{8}-[0-9a-f-]{27})"',
            html_text,
            re.I,
        )
        mapping: dict = {}
        for form_id, version_id in pairs:
            mapping[form_id.lower()] = version_id.lower()
        self._form_id_version_map = mapping
        _eprint(f"[ops.http_web] formId→versionId map: {len(mapping)} entries")
        return mapping

    # ── Form structure ───────────────────────────────────────────────

    def scrape_form_structure(
        self,
        form_id: Optional[str] = None,
        form_version_id: Optional[str] = None,
    ) -> dict:
        """GET AddFormScript.aspx and parse field blocks. Returns structured dict."""
        fid = form_id.lower() if form_id else None
        vid = form_version_id.lower() if form_version_id else None
        if not fid and not vid:
            return {"ok": False, "reason": "需提供 form_id 或 form_version_id"}
        mapping = self.get_form_id_version_mapping()
        if fid and not vid:
            vid = mapping.get(fid)
            if not vid:
                return {
                    "ok": False,
                    "reason": f"無法從 ApplyFormList 反查 formId={form_id} 的 formVersionId",
                }
        elif vid and not fid:
            reverse = {v: k for k, v in mapping.items()}
            fid = reverse.get(vid)
            if not fid:
                return {
                    "ok": False,
                    "reason": f"無法從 ApplyFormList 反查 formVersionId={form_version_id} 的 formId",
                }
        path = f"{_ADD_FORM_SCRIPT_PATH}?formId={fid}&formVersionId={vid}"
        resp = self.get(path)
        if "Login.aspx" in str(resp.url):
            return {"ok": False, "reason": "redirected to Login.aspx"}
        if "ErrorReport" in str(resp.url):
            return {
                "ok": False,
                "reason": "AddFormScript 回 ErrorReport（formId/formVersionId 配對錯誤或起單權限不足）",
            }
        tree = self._parse(resp)
        fields = _parse_field_blocks(tree)
        return {
            "ok": True,
            "reason": "",
            "form_id": fid,
            "form_version_id": vid,
            "fields": fields,
            "url": str(resp.url),
        }

    # ── Form list ────────────────────────────────────────────────────

    def scrape_form_list(self) -> dict:
        """Scrape the form-name dropdown on MyFormList.aspx?item=FormQuery."""
        resp = self.get(_FORM_QUERY_PATH)
        if "Login.aspx" in str(resp.url):
            return {"ok": False, "reason": "redirected to Login.aspx"}
        tree = self._parse(resp)
        # Find the select with ddlFormQuery or a "所有表單" option
        select_el = None
        for sel in tree.xpath("//select[contains(@name,'ddlFormQuery') or contains(@id,'ddlFormQuery') or contains(@name,'ddlForm')]"):
            select_el = sel
            break
        if select_el is None:
            for sel in tree.xpath("//select"):
                opts = sel.xpath(".//option")
                labels = ["".join(o.itertext()).strip() for o in opts[:3]]
                if any("所有表單" in l for l in labels):
                    select_el = sel
                    break
        if select_el is None:
            return {"ok": False, "reason": "找不到表單名稱下拉選單"}

        forms = []
        for opt in select_el.xpath(".//option"):
            val = (opt.get("value") or "").strip()
            txt = "".join(opt.itertext()).strip()
            if not val or val == "all" or txt == "所有表單":
                continue
            m = re.match(r"^\[(.+?)\](.+)$", txt)
            if m:
                category = m.group(1).strip()
                form_name = m.group(2).strip()
            else:
                category = "(未分類)"
                form_name = txt
            forms.append({"form_id": val, "form_name": form_name, "category": category})
        return {"ok": True, "reason": "", "forms": forms}

    # ── Search forms ─────────────────────────────────────────────────

    def search_forms(
        self,
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        max_results: int = 50,
    ) -> dict:
        """POST query to MyFormList.aspx and parse GridItem rows."""
        # GET first to collect VIEWSTATE
        resp = self.get(_FORM_QUERY_PATH)
        if "Login.aspx" in str(resp.url):
            return {"ok": False, "reason": "redirected to Login.aspx", "rows": []}
        tree = self._parse(resp)
        hidden = _parse_hidden_fields(tree)

        today = date.today()
        # Normalize to dash format for hidden fields, slash for display inputs
        df_raw = date_from or (today - timedelta(days=7)).strftime("%Y-%m-%d")
        dt_raw = date_to or today.strftime("%Y-%m-%d")
        df_dash = df_raw.replace("/", "-")
        dt_dash = dt_raw.replace("/", "-")
        df_slash = df_dash.replace("-", "/")
        dt_slash = dt_dash.replace("-", "/")

        date_prefix = "ctl00$ctl00$ContentPlaceHolder1$RightContentPlaceHolder$"
        payload = dict(hidden)
        payload[date_prefix + "wdcQueryDateStart"] = df_dash
        payload[date_prefix + "wdcQueryDateStart$dateInput"] = df_slash
        payload[date_prefix + "wdcQueryDateEnd"] = dt_dash
        payload[date_prefix + "wdcQueryDateEnd$dateInput"] = dt_slash
        # wibQuery is a submit button — include as name=value, not EVENTTARGET
        payload[date_prefix + "wibQuery"] = "查詢"
        if keyword:
            payload[date_prefix + "txtKeywordByFormQuery"] = keyword

        resp2 = self.post(_FORM_QUERY_PATH, payload)
        if "Login.aspx" in str(resp2.url):
            return {"ok": False, "reason": "redirected to Login.aspx after search", "rows": []}

        tree2 = self._parse(resp2)
        row_els = tree2.xpath("//tr[contains(@class,'GridItem') or contains(@class,'GridItemAlternating')]")
        total = len(row_els)
        rows: list = []
        for row in row_els[:max_results]:
            try:
                task_id = ""
                for a in row.xpath(".//a[@onclick]"):
                    onclick = a.get("onclick") or ""
                    m = re.search(r"TASK_ID=([0-9a-f-]{36})", onclick, re.I)
                    if m:
                        task_id = m.group(1)
                        break
                tds = row.xpath(".//td")
                cols = ["".join(td.itertext()).strip() for td in tds]

                def col(i: int) -> str:
                    return cols[i] if i < len(cols) else ""

                rows.append({
                    "task_id": task_id,
                    "form_number": col(0),
                    "form_name": col(1),
                    "subject": col(2),
                    "applicant": col(3),
                    "status": col(4),
                    "apply_time": col(5),
                    "close_time": col(6),
                })
            except Exception as ex:
                _eprint(f"[ops.http_web] ⚠️ row scrape error: {type(ex).__name__}: {ex}")
                continue

        return {
            "ok": True,
            "reason": "",
            "rows": rows,
            "total_rows_on_page": total,
            "query": {"keyword": keyword, "date_from": df_dash, "date_to": dt_dash, "max_results": max_results},
        }

    # ── Dialog search ────────────────────────────────────────────────

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
        hidden = _parse_hidden_fields(tree)
        payload = dict(hidden)
        # Try to fill search keyword if a text input exists
        for inp in tree.xpath("//input[@type='text']"):
            name = inp.get("name") or ""
            if name and "search" in name.lower() or "keyword" in name.lower() or "key" in name.lower():
                payload[name] = search_key
                break

        resp2 = self.post(path_only, payload)
        if "Login.aspx" in str(resp2.url):
            return None

        tree2 = self._parse(resp2)
        # Rows carry jsondata attribute with HTML-entity-encoded JSON
        for row in tree2.xpath("//tr[@jsondata]"):
            jd_raw = row.get("jsondata") or ""
            jd_decoded = html.unescape(jd_raw)
            try:
                import json
                jd = json.loads(jd_decoded)
            except Exception:
                continue
            val = str(jd.get(code_field) or "")
            if val.lower() == match_code.lower():
                return jd

        # If exact match not found, try substring on CompanyName / EntityId
        if search_key:
            for row in tree2.xpath("//tr[@jsondata]"):
                jd_raw = row.get("jsondata") or ""
                jd_decoded = html.unescape(jd_raw)
                try:
                    import json
                    jd = json.loads(jd_decoded)
                except Exception:
                    continue
                company = str(jd.get("CompanyName") or jd.get("EntityId") or "")
                if search_key.lower() in company.lower():
                    return jd
        return None

    # ── apply_form_web ────────────────────────────────────────────────

    def apply_form_web(
        self,
        form_version_id: str,
        fields: dict,
        comment: str = "",
        urgent_level: str = "2",
    ) -> dict:
        """Fill and submit a form via httpx. Returns {ok, task_id, form_number, filled, errors, reason}."""
        vid = form_version_id.lower()
        mapping = self.get_form_id_version_mapping()
        reverse = {v: k for k, v in mapping.items()}
        fid = reverse.get(vid, "")
        if not fid:
            return {
                "ok": False,
                "reason": f"無法從 ApplyFormList 反查 formVersionId={form_version_id} 的 formId",
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
        field_blocks = _parse_field_blocks(tree2, include_dialog_companions=True)
        field_map: dict = {}
        for fb in field_blocks:
            c = (fb.get("code") or "").upper()
            if c:
                field_map[c] = fb

        # Handle urgent_level if there's an urgentLevel field
        for fb in field_blocks:
            name = fb.get("input_name") or ""
            if "urgentLevel" in name or "urgent" in name.lower():
                payload[name] = urgent_level
                break

        errors: list = []
        filled: dict = {}

        # 4. Fill each field
        for code, value in (fields or {}).items():
            code_upper = code.upper()
            fb = field_map.get(code_upper)
            if fb is None:
                # Try case-insensitive label search as fallback
                for k, v in field_map.items():
                    if k == code_upper or (v.get("label") or "").lower() == code.lower():
                        fb = v
                        break
            if fb is None:
                errors.append(f"欄位 {code} 在表單中找不到，已跳過")
                continue

            itype = fb.get("input_type", "text")
            iname = fb.get("input_name") or ""

            if itype in ("autoNumber", "fileButton", "dataGrid"):
                _eprint(f"[ops.http_web] skip {code} ({itype})")
                continue

            if not iname:
                errors.append(f"欄位 {code} 找不到 input_name，已跳過")
                continue

            if itype == "datePicker":
                # value can be yyyy-mm-dd or yyyy/mm/dd
                v_norm = str(value).replace("/", "-")
                v_slash = v_norm.replace("-", "/")
                payload[iname] = v_norm
                payload[iname + "$dateInput"] = v_slash
                filled[code] = v_norm

            elif itype == "dropDown":
                # Find matching option in tree
                sel_els = tree2.xpath(f"//select[@name='{iname}']")
                matched = False
                if sel_els:
                    sel_el = sel_els[0]
                    str_val = str(value)
                    for opt in sel_el.xpath(".//option"):
                        opt_val = opt.get("value") or ""
                        opt_txt = "".join(opt.itertext()).strip()
                        if opt_val == str_val or opt_txt == str_val:
                            payload[iname] = opt_val
                            filled[code] = opt_val
                            matched = True
                            break
                    if not matched:
                        # label contains match
                        for opt in sel_el.xpath(".//option"):
                            opt_val = opt.get("value") or ""
                            opt_txt = "".join(opt.itertext()).strip()
                            if str_val.lower() in opt_txt.lower():
                                payload[iname] = opt_val
                                filled[code] = opt_val
                                matched = True
                                break
                if not matched:
                    errors.append(f"dropDown {code}: 選項「{value}」找不到，已跳過")

            elif itype == "radio":
                payload[iname] = str(value)
                filled[code] = str(value)

            elif itype == "dialog":
                dialog_url = fb.get("dialog_url") or ""
                if not dialog_url:
                    errors.append(f"dialog {code}: 無 dialog_url，已跳過")
                    continue
                jd = self.search_dialog(dialog_url, search_key=str(value), match_code=str(value))
                if jd is None:
                    errors.append(f"dialog {code}: 找不到「{value}」，已跳過")
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
                filled[code] = display_val

            else:
                # text, textarea, numeric, checkbox, unknown
                payload[iname] = str(value)
                filled[code] = str(value)

        # 5. Fill comment
        if comment:
            for inp in tree2.xpath("//textarea"):
                name = inp.get("name") or ""
                if "tbxComment" in name or "comment" in name.lower():
                    payload[name] = comment
                    break

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

        # 6. POST to submit (MasterPageRadButton1 = 送出申請)
        payload["__EVENTTARGET"] = "ctl00$MasterPageRadButton1"
        payload["__EVENTARGUMENT"] = ""
        resp_submit = self.post(first_site_path, payload)
        if "Login.aspx" in str(resp_submit.url):
            return {"ok": False, "reason": "redirected to Login.aspx on submit", "task_id": "", "form_number": "", "filled": filled, "errors": errors}

        task_id, form_number, submitted = _extract_result(resp_submit)
        if submitted or task_id:
            return {
                "ok": True,
                "reason": "",
                "task_id": task_id,
                "form_number": form_number,
                "filled": filled,
                "errors": errors,
            }

        # 7. Not submitted yet — try MasterPageRadButton3 (some forms use this for final submit)
        tree_save = self._parse(resp_submit)
        new_hidden = _parse_hidden_fields(tree_save)
        for k in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED", "__EVENTVALIDATION"):
            if k in new_hidden:
                payload[k] = new_hidden[k]

        payload["__EVENTTARGET"] = "ctl00$MasterPageRadButton3"
        resp_submit2 = self.post(first_site_path, payload)
        task_id, form_number, submitted = _extract_result(resp_submit2)
        if submitted or task_id:
            return {
                "ok": True,
                "reason": "",
                "task_id": task_id,
                "form_number": form_number,
                "filled": filled,
                "errors": errors,
            }

        # Status 200 but no clear confirmation
        return {
            "ok": True,
            "reason": "送出後未收到確認訊號，請至待辦事項確認是否已建立",
            "submitted_unconfirmed": True,
            "task_id": "",
            "form_number": form_number,
            "filled": filled,
            "errors": errors,
        }

    # ── search_users ──────────────────────────────────────────────────

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
        })
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


# ── Singleton ─────────────────────────────────────────────────────────

_session: Optional[HttpSession] = None
_session_lock = threading.Lock()


def get_http_session() -> HttpSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = HttpSession()
            try:
                _session._ensure_logged_in()
            except Exception as ex:
                _eprint(f"[ops.http_web] ⚠️ initial login failed: {ex}")
    return _session


def reset_http_session() -> None:
    """Discard the singleton so the next caller gets a fresh session."""
    global _session
    with _session_lock:
        _session = None


# ── HttpWebBackend ────────────────────────────────────────────────────

class HttpWebBackend(OpsBackend):
    """OpsBackend implemented with httpx + lxml (no Playwright)."""

    @property
    def _session(self) -> HttpSession:
        return get_http_session()

    # ── System ──────────────────────────────────────────────────────
    def check_auth(self) -> str:
        account = os.environ.get("UOF_ACCOUNT", "(未設定 UOF_ACCOUNT)")
        base = os.environ.get("UOF_BASE_URL", "(未設定 UOF_BASE_URL)")
        try:
            resp = self._session._client.get(
                self._session._full_url(self._session._vpath + _HOMEPAGE_PATH),
                follow_redirects=False,
            )
            if resp.is_redirect and "Login.aspx" in (resp.headers.get("location") or ""):
                return (
                    f"⚠️ http_web session：帳號 {account} 未登入（重新導向到 Login.aspx）\n"
                    f"   伺服器: {base}"
                )
            logged_in = "Login.aspx" not in str(resp.url)
        except Exception as ex:
            return f"❌ http_web session 檢查失敗 ({type(ex).__name__}): {ex}"
        if logged_in:
            return (
                f"✅ http_web session：帳號 {account} 已登入\n"
                f"   伺服器: {base}"
            )
        return (
            f"⚠️ http_web session：帳號 {account} 未登入\n"
            f"   伺服器: {base}"
        )

    # ── WKF reads ───────────────────────────────────────────────────
    def get_form_list(self) -> str:
        try:
            data = self._session.scrape_form_list()
        except Exception as ex:
            return f"❌ 取得表單清單時發生錯誤 ({type(ex).__name__}): {ex}"
        if not data.get("ok"):
            return f"❌ 取得表單清單失敗：{data.get('reason', '(unknown)')}"
        forms = data["forms"]
        if not forms:
            return "📋 找不到任何表單（可能此帳號無任何表單檢視/起單權限）"
        from collections import OrderedDict
        by_cat: OrderedDict = OrderedDict()
        for f in forms:
            by_cat.setdefault(f["category"], []).append(f)
        lines = [f"📋 表單清單（http_web 模式，from 查詢表單下拉，共 {len(forms)} 個表單）："]
        for cat, items in by_cat.items():
            lines.append(f"\n📁 【{cat}】")
            for f in items:
                lines.append(f"  - {f['form_name']} (formId: {f['form_id']})")
        lines.append(
            "\n💡 此清單來源是查詢用下拉，含 formId 但**不**含 formVersionId。"
            "formVersionId 起單時才需要，apply_form 會自動從起單頁解析。"
        )
        return "\n".join(lines)

    def get_external_form_list(self) -> str:
        return (
            "⚠️ 網頁機制無法可靠回 `get_external_form_list`。\n\n"
            "「非線上使用」是 UOF 後台「表單管理」中的 admin 旗標，一般 user 在前端\n"
            "（表單申請樹、查詢表單下拉、列表頁）都看不到這個旗標——只有 SOAP\n"
            "GetExternalFormList 直接讀 DB 才能取得。\n\n"
            "可行替代：\n"
            "- 用 `get_form_list` 看「目前帳號可查詢/起單」的所有表單\n"
            "- 「非線上使用」與「可外部起單」並非相同概念，\n"
            "  若是想知道「哪些表單可以起單」，請看 get_form_list 結果。"
        )

    def get_form_structure(self, form_version_id: str) -> str:
        return self._render_form_structure(form_version_id=form_version_id, by_label="formVersionId")

    def get_form_structure_by_id(self, form_id: str) -> str:
        return self._render_form_structure(form_id=form_id, by_label="formId")

    def _render_form_structure(
        self,
        form_id: Optional[str] = None,
        form_version_id: Optional[str] = None,
        by_label: str = "formId",
    ) -> str:
        try:
            data = self._session.scrape_form_structure(form_id=form_id, form_version_id=form_version_id)
        except Exception as ex:
            return f"❌ 取得表單結構時發生錯誤 ({type(ex).__name__}): {ex}"
        if not data.get("ok"):
            return f"❌ 取得表單結構失敗（by {by_label}）：{data.get('reason', '(unknown)')}"
        fields = data["fields"]
        fill_hint = {
            "autoNumber": "系統自動編號（讀取用）",
            "datePicker": "日期欄位 (yyyy/MM/dd)",
            "numeric": "數值欄位",
            "multiLineText": "多行文字",
            "dropDown": "下拉選單，需從選項挑值",
            "fileButton": "附檔欄位（網頁起單流程才能上傳）",
            "dataGrid": "明細欄位，目前 apply_form 尚未支援填寫",
            "radio": "單選",
            "checkbox": "多選/勾選",
            "text": "單行文字",
            "dialog": "彈窗選取欄位",
            "unknown": "型別未知（可能是版面元件）",
        }
        unsupported_for_apply = [f for f in fields if f["input_type"] in ("dataGrid", "fileButton")]
        lines = [
            f"📝 表單 {form_id or form_version_id} 的欄位清單"
            f"（http_web 模式，from AddFormScript.aspx）",
            f"  formId: {data['form_id']}",
            f"  formVersionId: {data['form_version_id']}",
            f"  共 {len(fields)} 個欄位：",
        ]
        for f in fields:
            mark = "＊" if f["required"] else " "
            code = f["code"] or "—"
            hint = fill_hint.get(f["input_type"], f["input_type"])
            lines.append(f"  {mark} [{code}] {f['label']} 〈{f['input_type']}〉 — {hint}")
        if unsupported_for_apply:
            codes = ", ".join(
                f"{f['code'] or f['label']}({f['input_type']})" for f in unsupported_for_apply
            )
            lines.append(
                f"\n⚠️ 含明細/附檔欄位（{codes}）；apply_form 目前不支援填寫這些型別，"
                "請於 UOF 網頁操作。"
            )
        lines.append(
            "\n💡 起單時把 fields 帶 `{欄位代碼: 值}` 對應；自動編號欄位帶空字串即可。"
            "\n🔗 來源頁: " + data["url"]
        )
        return "\n".join(lines)

    def get_task_data(self, task_id: str) -> str:
        return web_not_implemented("get_task_data", "http_web 模式尚未實作")

    def get_task_result(self, task_id: str, include_form_data: bool = True) -> str:
        return web_not_implemented("get_task_result", "http_web 模式尚未實作")

    def preview_workflow(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: Optional[dict] = None,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str:
        return web_not_implemented("preview_workflow", "http_web 模式尚未實作")

    def apply_form(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: dict,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str:
        try:
            r = self._session.apply_form_web(form_version_id, fields, comment, urgent_level)
        except Exception as ex:
            return f"❌ 起單時發生錯誤 ({type(ex).__name__}): {ex}"
        errors = r.get("errors") or []
        err_block = ("\n⚠️ 填寫警告：\n" + "\n".join(f"  - {e}" for e in errors)) if errors else ""
        if not r.get("ok"):
            return f"❌ 起單失敗：{r.get('reason', '(unknown)')}{err_block}"
        if r.get("submitted_unconfirmed"):
            return (
                f"⚠️ 起單可能已送出，但 TaskId 未確認\n"
                f"   表單編號：{r.get('form_number') or '(未取得)'}\n"
                f"   說明：{r.get('reason')}\n"
                "   請先用 query_forms 或 UOF 網頁確認，勿直接重送。"
                + err_block
            )
        return (
            f"✅ 起單成功\n"
            f"   表單編號：{r.get('form_number') or '(未取得)'}\n"
            f"   TaskId：{r.get('task_id')}"
            + err_block
        )

    def terminate_task(self, task_id: str, result: str, reason: str) -> str:
        return web_not_implemented("terminate_task", "http_web 模式尚未實作")

    def sign_next(self, task_id: str, site_id: str, node_seq: int, signer_guid: str) -> str:
        return web_not_implemented("sign_next", "http_web 模式尚未實作")

    def query_forms(
        self,
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        max_results: int = 50,
    ) -> str:
        try:
            result = self._session.search_forms(keyword, date_from, date_to, max_results)
        except Exception as ex:
            return (
                f"❌ 查詢表單時發生錯誤 ({type(ex).__name__}): {ex}\n"
                f"💡 此清單為自動擷取，遇非預期頁面結構時可能誤判。"
            )
        if not result.get("ok"):
            return f"❌ 查詢失敗：{result.get('reason', '(unknown)')}"
        rows = result["rows"]
        q = result["query"]
        total = result["total_rows_on_page"]
        header = (
            f"🔍 查詢表單 —"
            f" {q['date_from']} ~ {q['date_to']}"
            + (f"，關鍵字「{q['keyword']}」" if q["keyword"] else "")
            + "\n"
        )
        if not rows:
            return header + "📋 查無資料"
        lines = [
            header + f"共 {total} 筆"
            + (f"（僅顯示前 {len(rows)} 筆）" if total > len(rows) else "") + "："
        ]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"\n[{i}] {r['form_name']} {r['form_number']}  〈{r['status']}〉"
                f"\n    TaskId: {r['task_id'] or '(無法擷取)'}"
                f"\n    申請者: {r['applicant']}"
                f"\n    申請時間: {r['apply_time']}"
                + (f"\n    結案時間: {r['close_time']}" if r["close_time"] else "")
                + (f"\n    摘要: {r['subject']}" if r["subject"] else "")
            )
        lines.append(
            "\n💡 把 TaskId 帶入 `get_task_data` / `get_task_result` 可查單張詳情。"
        )
        return "\n".join(lines)

    def search_users(self, keyword: str) -> str:
        if not keyword or not keyword.strip():
            return "❌ 請提供查詢關鍵字（姓名或帳號）。"
        try:
            users = self._session.search_users(keyword.strip())
        except Exception as ex:
            return f"❌ 查詢人員時發生錯誤 ({type(ex).__name__}): {ex}"
        if not users:
            return f"📋 找不到符合「{keyword}」的人員。"
        lines = [f"👥 人員查詢結果（關鍵字：「{keyword}」，共 {len(users)} 筆）："]
        for u in users:
            lines.append(
                f"\n  姓名：{u['display_name']}　帳號：{u['account']}"
                f"\n  UserGuid：{u['UserGuid']}"
            )
        lines.append("\n💡 帳號可用於 apply_form 的 first_signer_account 參數。")
        return "\n".join(lines)
