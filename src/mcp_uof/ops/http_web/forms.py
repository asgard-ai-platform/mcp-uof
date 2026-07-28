from __future__ import annotations
import re
from datetime import date, timedelta
from typing import Optional
from ..._log import eprint as _eprint
from .constants import _FORM_QUERY_PATH, _ADD_FORM_SCRIPT_PATH
from .parsing import _parse_hidden_fields
from .schema import FormSchema
from .validation import _uof_row_date, _datagrid_dialog_path


class FormsOperation:
    """Form structure, list, and query operations with explicit dependencies."""

    def __init__(self, session) -> None:
        self._session = session

    def get(self, path):
        return self._session.get(path)

    def post(self, path, data, *, retry_on_login=False):
        return self._session.post(path, data, retry_on_login=retry_on_login)

    def _parse(self, response):
        return self._session._parse(response)

    def strip_vpath(self, url):
        return self._session.strip_vpath(url)

    def get_form_id_version_mapping(self):
        return self._session.get_form_id_version_mapping()

    def datagrid_columns(self, path):
        return self._session.dialog_operation.datagrid_columns(path)

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
        fields = FormSchema.parse(tree).as_dicts()
        # For general dataGrid fields, attach the row's column list (品名/數量…) so callers know
        # the row shape. Best-effort: needs the row-editor dialog, which carries a scriptId only on
        # the apply-mode page — fetch that once if any dataGrid is present.
        if any(f["input_type"] == "dataGrid" for f in fields):
            try:
                apply_resp = self.get(f"{path}&mode=apply")
                apply_html = self.get(self.strip_vpath(str(apply_resp.url))).text
                for f in fields:
                    if f["input_type"] != "dataGrid":
                        continue
                    dlg = _datagrid_dialog_path(apply_html, f.get("grid_field_id") or f["code"])
                    if dlg:
                        f["columns"] = self.datagrid_columns(dlg)
            except Exception as ex:
                _eprint(f"[ops.http_web] ⚠️ dataGrid column probe failed: {type(ex).__name__}: {ex}")
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
        query_mode: str = "apply",
    ) -> dict:
        """POST query to MyFormList.aspx and parse GridItem rows.

        `query_mode` mirrors the page's radio: "apply" = 申請日期, "sign" = 簽核日期.
        Different sets of forms, not just a different ordering.
        """
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
        try:
            df_date = date.fromisoformat(df_dash)
            dt_date = date.fromisoformat(dt_dash)
        except ValueError:
            return {"ok": False, "rows": [],
                    "reason": "date_from / date_to 需為 yyyy/mm/dd 或 yyyy-mm-dd"}
        if df_date > dt_date:
            return {"ok": False, "rows": [], "reason": "date_from 不可晚於 date_to"}

        date_prefix = "ctl00$ctl00$ContentPlaceHolder1$RightContentPlaceHolder$"
        payload = dict(hidden)
        # 查詢模式 + 狀態/表單下拉：頁面上是 radio/<select>（預設 rbQuerySignDate / all / all），
        # 但 _parse_hidden_fields 只收 <input hidden>、不含這些，若不明確帶上，伺服器會套空過濾器 → 回 0 筆。
        _MODES = {"apply": "rbQueryApplyDate", "sign": "rbQuerySignDate"}  # 頁面預設 apply
        if query_mode not in _MODES:
            return {"ok": False, "rows": [],
                    "reason": f"query_mode 只接受 {sorted(_MODES)}，收到 {query_mode!r}"}
        payload[date_prefix + "QueryMode"] = _MODES[query_mode]
        payload[date_prefix + "ddlQueryTaskStatus"] = "all"        # 所有狀態
        payload[date_prefix + "ddlFormNameQuery"] = "all"          # 所有表單
        payload[date_prefix + "wdcQueryDateStart"] = df_dash
        payload[date_prefix + "wdcQueryDateStart$dateInput"] = df_slash
        payload[date_prefix + "wdcQueryDateEnd"] = dt_dash
        payload[date_prefix + "wdcQueryDateEnd$dateInput"] = dt_slash
        payload[date_prefix + "wibQuery"] = "查詢"   # 送出鈕（name=value，非 EVENTTARGET）
        # ⚠️ 不送 txtKeywordByFormQuery：UOF 伺服器端的關鍵字過濾會對候選列讀強型別 row.DOC_NBR，
        # 候選集只要有一列表單編號為 null（如未取號即作廢的測試單），就丟 StrongTypingException →
        # 帶關鍵字 + status=all/已結案/作廢 一律 500 被導向 ErrorReport（舊 code 誤當「查無資料」）。
        # 因此先取回候選列，再於 Python 端過濾，並同時支援比對表單名稱。

        def _parse_rows(tree) -> list:
            out = []
            for row in tree.xpath("//tr[contains(@class,'GridItem') or contains(@class,'GridItemAlternating')]"):
                try:
                    task_id = ""
                    for a in row.xpath(".//a[@onclick]"):
                        m = re.search(r"TASK_ID=([0-9a-f-]{36})", a.get("onclick") or "", re.I)
                        if m:
                            task_id = m.group(1)
                            break
                    cols = ["".join(td.itertext()).strip() for td in row.xpath(".//td")]
                    c = lambda i: cols[i] if i < len(cols) else ""  # noqa: E731
                    out.append({"task_id": task_id, "form_number": c(0), "form_name": c(1),
                                "subject": c(2), "applicant": c(3), "status": c(4),
                                "apply_time": c(5), "close_time": c(6)})
                except Exception as ex:
                    _eprint(f"[ops.http_web] ⚠️ row scrape error: {type(ex).__name__}: {ex}")
            return out

        resp2 = self.post(_FORM_QUERY_PATH, payload, retry_on_login=True)
        if "Login.aspx" in str(resp2.url):
            return {"ok": False, "reason": "redirected to Login.aspx after search", "rows": []}
        if "ErrorReport" in str(resp2.url):
            return {"ok": False, "reason": "查詢被導向 ErrorReport（伺服器端查詢例外）", "rows": []}

        all_rows = _parse_rows(self._parse(resp2))
        seen = {r["task_id"] for r in all_rows if r["task_id"]}
        kw = (keyword or "").strip().lower()

        # UOF deployments have been observed ignoring the posted date controls. Keep the server
        # filter for efficiency, but enforce the advertised date boundary locally as well.
        def _in_range(row: dict) -> bool:
            if query_mode != "apply":
                return True
            d = _uof_row_date(row, query_mode)
            return d is not None and df_date <= d <= dt_date

        cond = {k: v for k, v in payload.items()
                if k.startswith(date_prefix) and not k.endswith("wibQuery")}
        grid = date_prefix + "grdQuery"
        cur = resp2
        page_rows = all_rows
        for page in range(2, 40):   # backstop：至多 ~40 頁
            # Apply-query rows are rendered newest-first. Once a whole page is older than the
            # lower bound, later pages cannot contribute a match. Sign-query ordering is less
            # predictable, so it deliberately scans until the pager ends/backstop.
            page_dates = [_uof_row_date(r, query_mode) for r in page_rows]
            if (query_mode == "apply" and page_dates and all(
                    d is not None and d < df_date for d in page_dates)):
                break
            h = _parse_hidden_fields(self._parse(cur))
            h.update(cond)                         # pager postback 必須重帶條件欄位，否則伺服器用預設重繫結
            h.pop(date_prefix + "wibQuery", None)  # 翻頁不按查詢鈕
            h["__EVENTTARGET"] = grid
            h["__EVENTARGUMENT"] = f"Page${page}"
            cur = self.post(_FORM_QUERY_PATH, h, retry_on_login=True)
            if "Login.aspx" in str(cur.url) or "ErrorReport" in str(cur.url):
                break
            page_rows = [r for r in _parse_rows(self._parse(cur))
                         if r["task_id"] and r["task_id"] not in seen]
            if not page_rows:
                break
            seen.update(r["task_id"] for r in page_rows)
            all_rows.extend(page_rows)

        matched = [r for r in all_rows if _in_range(r)]
        if kw:
            matched = [r for r in matched if kw in (
                f"{r['form_number']} {r['form_name']} {r['subject']} {r['applicant']}").lower()]
        rows = matched[:max_results]
        return {
            "ok": True,
            "reason": "",
            "rows": rows,
            "total_matched": len(matched),
            "total_scanned": len(all_rows),
            "query": {"keyword": keyword, "date_from": df_dash, "date_to": dt_dash,
                      "max_results": max_results, "query_mode": query_mode},
        }

    # ── Dialog search ────────────────────────────────────────────────
