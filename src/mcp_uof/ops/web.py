"""
WebBackend — Playwright-driven UOF web UI ops.

This backend exists for UOF deployments that don't ship the SOAP/ASMX PublicAPI module
(login redirects everything to Login.aspx; .asmx routes return 404). Operations are
driven by simulating the same actions a human would take in the browser.

Architecture notes:

- Playwright sync_api has *thread affinity*. We therefore run all browser operations in
  a single dedicated worker thread (ThreadPoolExecutor(max_workers=1)) and dispatch via
  `executor.submit(...).result()`. This keeps MCP tool functions sync-friendly and
  isolates Playwright from any asyncio loop FastMCP might be running on.
- Persistent state lives in `~/.uof/storage_state-<account>-<hash>.json` so cookies
  survive process restarts.
- ASP.NET session idle timeout is ~20 min; we re-login transparently when we detect a
  redirect to Login.aspx during an operation.

Tool coverage in this first cut: `check_auth` is fully implemented. All WKF operations
return a structured "not implemented in web mode" message via `web_not_implemented()`
until per-page Playwright scripts are written.
"""
from __future__ import annotations

import atexit
import os
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

from ..auth.base import get_session_provider
from .base import OpsBackend, web_not_implemented


from .._log import eprint as _eprint  # 診斷一律走 stderr（共用，勿在各檔複製）


# UOF web pages used by this backend (constants live here, not in domains/, because
# domain endpoints.py files describe the SOAP/PublicAPI surface).
FORM_PRINT_PATH = "/WKF/FormUse/FormPrint.aspx"
FORM_QUERY_PATH = "/WKF/FormUse/PersonalBox/MyFormList.aspx?item=FormQuery"
# ViewFormTemp.aspx wraps ViewForm.aspx which carries: form title/number, applicant,
# overall sign result, and the full per-site signing history grid.
VIEW_FORM_TEMP_PATH = "/WKF/FormUse/ViewFormTemp.aspx"
# ApplyFormList carries the formId↔formVersionId mapping inside the RadTreeView
# nodeData JSON ("value":"formId@formVersionId").
APPLY_FORM_LIST_PATH = "/WKF/FormUse/PersonalBox/ApplyFormList.aspx"
# AddFormScript needs BOTH formId AND formVersionId.
ADD_FORM_SCRIPT_PATH = "/WKF/FormUse/AddFormScript.aspx"

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


# ── Singleton Playwright runtime ──────────────────────────────────────
class WebRuntime:
    """Owns Playwright + browser + context. All operations dispatched to single worker thread."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uof-web")
        self._lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._context: Optional["BrowserContext"] = None
        self._page: Optional["Page"] = None
        self._initialized = False
        self._closed = False
        # Cache the formId → formVersionId mapping (built from ApplyFormList tree).
        # Cleared when shutdown / reset; lifetime is fine because form versions
        # rarely change inside a single MCP session.
        self._form_id_version_map: Optional[dict] = None

    def _submit(self, fn: Callable[..., Any], *args, **kwargs) -> Any:
        if self._closed:
            raise RuntimeError("WebRuntime already shut down")
        fut: Future = self._executor.submit(fn, *args, **kwargs)
        return fut.result()

    # ── Worker-thread methods (run inside executor) ────────────────
    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright is required for the web mechanism (e.g. query_forms). "
                "Install: `uv add playwright` then `uv run playwright install chromium`. "
                f"Original error: {e}"
            )
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
        except Exception as e:
            raise RuntimeError(
                "Failed to launch Chromium. You probably need to install the browser binary: "
                "`uv run playwright install chromium`.\n"
                f"Original error: {type(e).__name__}: {e}"
            )
        state_path = Path(get_session_provider().credentials_file())
        ctx_kwargs: dict = {"ignore_https_errors": True}
        if state_path.exists():
            ctx_kwargs["storage_state"] = str(state_path)
            _eprint(f"[ops.web] 🔄 載入既有 storage state: {state_path}")
        self._context = self._browser.new_context(**ctx_kwargs)
        self._page = self._context.new_page()
        self._initialized = True

    def _save_state(self) -> None:
        if self._context is None:
            return
        state_path = Path(get_session_provider().credentials_file())
        state_path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(state_path))
        os.chmod(state_path, 0o600)
        _eprint(f"[ops.web] 💾 storage state 已寫入 {state_path}")

    def _do_login(self) -> str:
        """Perform Login.aspx form post and return logged-in display name."""
        assert self._page is not None
        base = os.environ["UOF_BASE_URL"].rstrip("/")
        account = os.environ["UOF_ACCOUNT"]
        password = os.environ["UOF_PASSWORD"]
        page = self._page

        _eprint(f"[ops.web] 🔐 登入中: {base}/Login.aspx (account={account})")
        page.goto(f"{base}/Login.aspx", wait_until="domcontentloaded")
        page.fill("#txtAccount", account)
        page.fill("#txtPwd", password)
        # Submit + wait for navigation to Homepage
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.click("#btnSubmit")

        # After login, the redirect chain ends at Homepage.aspx. If we're still at
        # Login.aspx, something went wrong.
        current = page.url
        if "Login.aspx" in current and "Homepage" not in current:
            # Grab any error message rendered on the login page.
            try:
                err = page.text_content(".error, .ui.error.message, #lblError") or ""
            except Exception:
                err = ""
            raise RuntimeError(
                f"Login.aspx 登入失敗，仍停在 {current}。"
                + (f" 頁面訊息: {err.strip()[:120]}" if err.strip() else "")
            )

        display = self._scrape_display_name()
        self._save_state()
        return display

    def _scrape_display_name(self) -> str:
        """Best-effort: pull the logged-in user's display name from Homepage."""
        assert self._page is not None
        page = self._page
        base = os.environ["UOF_BASE_URL"].rstrip("/")
        try:
            if "Homepage.aspx" not in page.url:
                page.goto(f"{base}/Homepage.aspx", wait_until="domcontentloaded")
            # `#ctl00_lblAccount` is the UOF top-bar label, format "<dept> <displayName>".
            # The other selectors are kept as fallbacks for differently themed UOF builds.
            for selector in (
                "#ctl00_lblAccount", "#lblAccount",
                "#userName", ".user-name", "#hUserName", "[id*='UserName']",
                ".ui.menu .user", "#lblWelcome",
            ):
                try:
                    txt = page.text_content(selector, timeout=200)
                    if txt and txt.strip():
                        return txt.strip()
                except Exception:
                    continue
            title = page.title()
            if title:
                return f"(無法抓取顯示名稱; 頁面標題: {title.strip()[:60]})"
        except Exception as e:
            return f"(無法抓取顯示名稱: {type(e).__name__})"
        return "(未知)"

    def _is_authenticated(self) -> bool:
        """Quick check: hit Homepage.aspx without follow_redirects and see if it stays."""
        assert self._page is not None
        base = os.environ["UOF_BASE_URL"].rstrip("/")
        try:
            self._page.goto(f"{base}/Homepage.aspx", wait_until="domcontentloaded", timeout=8000)
            return "Login.aspx" not in self._page.url
        except Exception:
            return False

    # ── Public API (called from any thread) ─────────────────────────
    def ensure_logged_in(self) -> str:
        """Idempotent: returns logged-in display name. Re-logs in if session expired."""
        with self._lock:
            def _job():
                self._ensure_initialized()
                if self._is_authenticated():
                    return self._scrape_display_name()
                return self._do_login()
            return self._submit(_job)

    def force_relogin(self) -> str:
        """強制重新登入（無視目前 session 狀態，直接重跑 Login.aspx）。

        供網頁工具的 retry 使用：當快取 cookie 通過了 _is_authenticated 但操作中途仍被導回
        Login（session 伺服器端已失效）時，呼叫本方法重登後重試一次。"""
        with self._lock:
            def _job():
                self._ensure_initialized()
                return self._do_login()
            return self._submit(_job)

    def page_screenshot(self, path: str) -> None:
        """Capture current page (debug helper)."""
        def _job():
            if self._page:
                self._page.screenshot(path=path, full_page=True)
        self._submit(_job)

    # ── Task-level page scrapers ────────────────────────────────────
    def scrape_task_summary(self, task_id: str) -> dict:
        """
        Fetch /WKF/FormUse/FormPrint.aspx?TASK_ID=<task_id> and extract the fields
        get_task_data needs. Returns dict with keys:
            ok, reason, form_name, form_number, applicant_display, account,
            apply_time, url, raw_title
        On failure (no permission, task_id not found, login redirect): ok=False with reason.
        """
        with self._lock:
            def _job():
                self._ensure_initialized()
                # Make sure we're logged in before scraping
                if not self._is_authenticated():
                    self._do_login()
                page = self._page
                base = os.environ["UOF_BASE_URL"].rstrip("/")
                url = f"{base}{FORM_PRINT_PATH}?TASK_ID={task_id}"
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)

                # Detect dead ends
                final_url = page.url
                html = page.content()
                body = page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                ) or ""

                if "Login.aspx" in final_url:
                    return {
                        "ok": False,
                        "reason": "redirected to Login.aspx (session expired and re-login failed)",
                    }
                if "Error404.png" in html:
                    return {
                        "ok": False,
                        "reason": "task_id 不存在 / FormPrint.aspx 404",
                    }
                if "無此表單列印權限" in body:
                    return {
                        "ok": False,
                        "reason": "目前帳號無此表單的列印權限（task_id 可能屬於他人或表單已被刪除）",
                    }

                title = page.title() or ""
                # Page title pattern observed: "<form_name>-<form_number> <vendor_name>".
                # form_number consists of digits and dots; form_name precedes the dash.
                form_name = ""
                form_number = ""
                m = re.match(r"^([^-]+?)-([\d.]+)", title)
                if m:
                    form_name = m.group(1).strip()
                    form_number = m.group(2).strip()

                # Fallback: scrape "文號" label from body when title parse failed.
                if not form_number:
                    m = re.search(r"文號\s+([0-9.\-A-Za-z]+)", body)
                    if m:
                        form_number = m.group(1).strip()

                # Top-of-page header section format:
                #   "申請人：\t<displayname> ( <role> )"
                applicant_display = ""
                m = re.search(r"申請人[：:]\s*([^\(\n\r\t]+?)(?:\s*\([^)\n\r\t]+\))?\s*[\n\r\t]", body)
                if m:
                    applicant_display = m.group(1).strip()

                # Inside the form fields, the applicant value is rendered as
                # "User Name(account)" — account ID in parens. Different from top-header
                # which usually shows ( <role> ) instead.
                account = ""
                m = re.search(r"申請人\s+[^\(\n\r]+?\((\w+)\)", body)
                if m:
                    account = m.group(1).strip()

                apply_time = ""
                m = re.search(r"申請時間[：:]\s*([\d/]+\s+[\d:]+)", body)
                if m:
                    apply_time = m.group(1).strip()

                return {
                    "ok": True,
                    "reason": "",
                    "form_name": form_name,
                    "form_number": form_number,
                    "applicant_display": applicant_display,
                    "account": account,
                    "apply_time": apply_time,
                    "url": final_url,
                    "raw_title": title,
                }
            return self._submit(_job)

    def search_forms(
        self,
        keyword: str,
        date_from: str,
        date_to: str,
        max_results: int,
    ) -> dict:
        """
        Drive /WKF/FormUse/PersonalBox/MyFormList.aspx?item=FormQuery to perform a
        keyword + date-range search, then scrape resulting rows.

        Returns dict:
            ok: bool
            reason: str (if not ok)
            rows: list[dict(task_id, form_number, form_name, subject, applicant,
                            status, apply_time, close_time)]
            total_rows_on_page: int (could be > max_results; we just truncate)
            query: dict echoing the actual params used
        """
        with self._lock:
            def _job():
                self._ensure_initialized()
                if not self._is_authenticated():
                    self._do_login()
                page = self._page
                base = os.environ["UOF_BASE_URL"].rstrip("/")

                page.goto(f"{base}{FORM_QUERY_PATH}", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)

                if "Login.aspx" in page.url:
                    return {"ok": False, "reason": "redirected to Login.aspx", "rows": []}

                # Telerik RadDatePicker stores value in TWO inputs: hidden (yyyy-mm-dd)
                # and visible $dateInput (yyyy/mm/dd). Both must be set + change event
                # dispatched, otherwise server-side validation reverts to defaults.
                def _resolve_dates():
                    # Default range = last 7 days, matching UOF's own default.
                    from datetime import date, timedelta
                    today = date.today()
                    df = date_from or (today - timedelta(days=7)).strftime("%Y/%m/%d")
                    dt = date_to or today.strftime("%Y/%m/%d")
                    return df, dt
                df, dt = _resolve_dates()

                def _hidden_form(d: str) -> str:
                    # Convert "yyyy/mm/dd" → "yyyy-mm-dd" for the hidden input.
                    return d.replace("/", "-")

                page.evaluate(
                    """
                    ({df, dt, dfHidden, dtHidden}) => {
                        function setPair(suffix, hiddenVal, visibleVal) {
                            const h = document.querySelector('input[name$="' + suffix + '"]');
                            const v = document.querySelector('input[name$="' + suffix + '$dateInput"]');
                            if (h) { h.value = hiddenVal; h.dispatchEvent(new Event('change', {bubbles: true})); }
                            if (v) { v.value = visibleVal; v.dispatchEvent(new Event('change', {bubbles: true})); v.dispatchEvent(new Event('blur', {bubbles: true})); }
                        }
                        setPair('wdcQueryDateStart', dfHidden, df);
                        setPair('wdcQueryDateEnd', dtHidden, dt);
                    }
                    """,
                    {"df": df, "dt": dt, "dfHidden": _hidden_form(df), "dtHidden": _hidden_form(dt)},
                )

                if keyword:
                    page.fill(
                        "input[name$='txtKeywordByFormQuery']",
                        keyword,
                    )

                # Click the 查詢 button (NOT 進階查詢). Identified by `wibQuery` name suffix.
                search_btn = page.query_selector("input[name$='wibQuery']")
                if not search_btn:
                    return {"ok": False, "reason": "查詢 button not found on page", "rows": []}
                try:
                    with page.expect_response(
                        lambda r: "MyFormList" in r.url and r.status == 200,
                        timeout=20000,
                    ):
                        search_btn.click()
                except Exception as e:
                    return {"ok": False, "reason": f"查詢 click did not produce response: {e}", "rows": []}
                # Let Telerik finish DOM swap. networkidle isn't reliable on RadAjax.
                page.wait_for_timeout(2500)

                # Telerik RadGrid alternates row class between GridItem (odd) and
                # GridItemAlternating (even). Both must be queried, in DOM order.
                rows_el = page.query_selector_all("tr.GridItem, tr.GridItemAlternating")
                total = len(rows_el)
                rows: list[dict] = []
                for r in rows_el[:max_results]:
                    try:
                        # task_id lives in the first <a>'s onclick: $uof.dialog.open2('/UOF/WKF/FormUse/ViewFormTemp.aspx?TASK_ID=<GUID>...')
                        first_link = r.query_selector("a[onclick*='dialog.open2']")
                        task_id = ""
                        if first_link:
                            onclick = first_link.get_attribute("onclick") or ""
                            m = re.search(r"TASK_ID=([0-9a-f-]{36})", onclick, re.I)
                            if m:
                                task_id = m.group(1)
                        tds = r.query_selector_all("td")
                        cols = [(t.text_content() or "").strip() for t in tds]
                        # Observed column order on this UOF: 表單編號 / 表單名稱 / 主旨 /
                        # 申請者 / 狀態 / 申請時間 / 結案時間 / (空) / 操作
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
                    except Exception as e:
                        # one bad row shouldn't kill the whole scrape
                        _eprint(f"[ops.web] ⚠️ row scrape error: {type(e).__name__}: {e}")
                        continue

                return {
                    "ok": True,
                    "reason": "",
                    "rows": rows,
                    "total_rows_on_page": total,
                    "query": {"keyword": keyword, "date_from": df, "date_to": dt, "max_results": max_results},
                }
            return self._submit(_job)

    def _fetch_form_id_version_mapping(self) -> dict:
        """
        Open ApplyFormList and parse the RadTreeView nodeData to build the
        formId → formVersionId mapping.

        Each form node's `value` in the tree is encoded as "<formId>@<formVersionId>",
        which is exactly what `AddFormScript.aspx` needs (it requires both ids together).
        Result is cached on the runtime so subsequent calls are free.
        """
        if self._form_id_version_map is not None:
            return self._form_id_version_map

        page = self._page
        base = os.environ["UOF_BASE_URL"].rstrip("/")
        page.goto(f"{base}{APPLY_FORM_LIST_PATH}", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)

        html = page.content()
        # The pairs we want are literally substrings of the inline RadTreeView init JSON,
        # which spans many KB. Regex over the whole HTML is cheaper than parsing JSON.
        pairs = re.findall(
            r'"value":"([0-9a-f]{8}-[0-9a-f-]{27})@([0-9a-f]{8}-[0-9a-f-]{27})"',
            html,
            re.I,
        )
        mapping: dict = {}
        for form_id, version_id in pairs:
            mapping[form_id.lower()] = version_id.lower()
        self._form_id_version_map = mapping
        return mapping

    def scrape_form_structure(
        self,
        form_id: Optional[str] = None,
        form_version_id: Optional[str] = None,
    ) -> dict:
        """
        Open AddFormScript.aspx for the given form and scrape its field definitions.

        Either `form_id` or `form_version_id` is sufficient — the other is resolved via
        the ApplyFormList tree mapping.

        Returns dict:
            ok, reason
            form_id, form_version_id
            fields: list[{code, label, required, input_type, input_name, hint}]
            url
        """
        with self._lock:
            def _job():
                self._ensure_initialized()
                if not self._is_authenticated():
                    self._do_login()

                # Resolve missing id from the ApplyFormList mapping
                fid = form_id.lower() if form_id else None
                vid = form_version_id.lower() if form_version_id else None
                if not fid and not vid:
                    return {"ok": False, "reason": "需提供 form_id 或 form_version_id"}
                mapping = self._fetch_form_id_version_mapping()
                if fid and not vid:
                    vid = mapping.get(fid)
                    if not vid:
                        return {
                            "ok": False,
                            "reason": f"無法從 ApplyFormList 反查 formId={form_id} 的 formVersionId（可能此帳號無起單權限）",
                        }
                elif vid and not fid:
                    reverse = {v: k for k, v in mapping.items()}
                    fid = reverse.get(vid)
                    if not fid:
                        return {
                            "ok": False,
                            "reason": f"無法從 ApplyFormList 反查 formVersionId={form_version_id} 的 formId",
                        }

                page = self._page
                base = os.environ["UOF_BASE_URL"].rstrip("/")
                url = f"{base}{ADD_FORM_SCRIPT_PATH}?formId={fid}&formVersionId={vid}"
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2500)

                if "Login.aspx" in page.url:
                    return {"ok": False, "reason": "redirected to Login.aspx"}
                if "ErrorReport" in page.url:
                    return {
                        "ok": False,
                        "reason": "AddFormScript 回 ErrorReport（formId/formVersionId 配對錯誤或起單權限不足）",
                    }

                fields: list[dict] = []
                blocks = page.query_selector_all("table.fieldWidth")
                for block in blocks:
                    try:
                        title_el = block.query_selector(".TitleFont")
                        code_el = block.query_selector(".FieldHide")
                        if not title_el and not code_el:
                            continue  # not a field block; some .fieldWidth tables are layout-only
                        label = (title_el.text_content() or "").strip() if title_el else ""
                        code_raw = (code_el.text_content() or "").strip() if code_el else ""
                        code = code_raw.strip("()").strip() if code_raw else ""

                        # Required marker: red asterisk somewhere in the block's header
                        required_el = block.query_selector("span[style*='color:Red'], span[style*='color: Red']")
                        required = required_el is not None and "＊" in ((required_el.text_content() or ""))

                        # Pick the most relevant input for type inference. Prefer the first
                        # NON-hidden input/select/textarea inside .fieldPadding.
                        input_el = block.query_selector(
                            ".fieldPadding input:not([type='hidden']), "
                            ".fieldPadding select, "
                            ".fieldPadding textarea"
                        )
                        # Fallback: any visible input anywhere in the block
                        if not input_el:
                            input_el = block.query_selector(
                                "input:not([type='hidden']), select, textarea"
                            )

                        input_kind = ""
                        input_name = ""
                        input_class = ""
                        input_type_attr = ""
                        input_title = ""
                        if input_el:
                            input_kind = (input_el.evaluate("e => e.tagName") or "").lower()
                            input_name = input_el.get_attribute("name") or ""
                            input_class = input_el.get_attribute("class") or ""
                            input_type_attr = (input_el.get_attribute("type") or "").lower()
                            input_title = input_el.get_attribute("title") or ""

                        # Detect special structures (file uploader, DataGrid 明細) by their
                        # surrounding markup since the <input> alone doesn't tell us.
                        is_file = bool(block.query_selector(
                            "a[onclick*='RemoteFileDialog'], a[onclick*='FileCenter']"
                        ))
                        is_datagrid = bool(block.query_selector(
                            "[id*='DataGrid'], a[onclick*='SetupDataGridFieldValue']"
                        ))

                        # Type inference. Keep these labels aligned with SOAP's vocabulary
                        # (autoNumber, multiLineText, fileButton, dataGrid, ...) so callers
                        # familiar with SOAP results don't need a separate mapping.
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

                        # Drop pseudo-fields that aren't real form fields (empty label AND
                        # empty code → likely a layout artefact).
                        if not label and not code:
                            continue

                        fields.append({
                            "code": code,
                            "label": label,
                            "required": required,
                            "input_type": input_type,
                            "input_name": input_name,
                            "input_title": input_title,
                        })
                    except Exception as e:
                        _eprint(f"[ops.web] ⚠️ field block parse error: {type(e).__name__}: {e}")
                        continue

                return {
                    "ok": True,
                    "reason": "",
                    "form_id": fid,
                    "form_version_id": vid,
                    "fields": fields,
                    "url": page.url,
                }
            return self._submit(_job)

    def scrape_form_list(self) -> dict:
        """
        Scrape the form-name dropdown on `MyFormList.aspx?item=FormQuery`.
        That dropdown is a flat list of every form the current account can see,
        with values being formId GUIDs and labels formatted as "[<category>]<form_name>".

        Returns dict: ok, reason, forms: list[{form_id, form_name, category}]

        Note: this gives `formId` but NOT `formVersionId` (the dropdown's value is the
        type-id, not the version-id). For apply_form / preview_workflow we'll resolve
        versionId on-demand from the apply-form launcher page.
        """
        with self._lock:
            def _job():
                self._ensure_initialized()
                if not self._is_authenticated():
                    self._do_login()
                page = self._page
                base = os.environ["UOF_BASE_URL"].rstrip("/")
                page.goto(f"{base}{FORM_QUERY_PATH}", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)

                if "Login.aspx" in page.url:
                    return {"ok": False, "reason": "redirected to Login.aspx"}

                # The select id has the ContentPlaceHolder prefix; match by suffix.
                select_el = page.query_selector("select[name$='ddlFormQuery'], select[id$='ddlFormQuery'], select[name$='ddlForm']")
                if not select_el:
                    # Try a broader match — find any select with an "所有表單" option
                    all_selects = page.query_selector_all("select")
                    for s in all_selects:
                        opts = s.query_selector_all("option")
                        labels = [(o.text_content() or "").strip() for o in opts[:3]]
                        if any("所有表單" in l for l in labels):
                            select_el = s
                            break
                if not select_el:
                    return {"ok": False, "reason": "找不到表單名稱下拉選單"}

                options = select_el.query_selector_all("option")
                forms = []
                for opt in options:
                    val = (opt.get_attribute("value") or "").strip()
                    txt = (opt.text_content() or "").strip()
                    if not val or val == "all" or txt == "所有表單":
                        continue
                    # Label is "[<category>]<form_name>"
                    m = re.match(r"^\[(.+?)\](.+)$", txt)
                    if m:
                        category = m.group(1).strip()
                        form_name = m.group(2).strip()
                    else:
                        category = "(未分類)"
                        form_name = txt
                    forms.append({
                        "form_id": val,
                        "form_name": form_name,
                        "category": category,
                    })
                return {"ok": True, "reason": "", "forms": forms}
            return self._submit(_job)

    def scrape_sign_history(self, task_id: str) -> dict:
        """
        Open /WKF/FormUse/ViewFormTemp.aspx?TASK_ID=<task_id> and scrape:
        - form name + form number (from page title)
        - overall result (e.g. 同意 / 否決 / 簽核中)
        - per-site sign history rows: site / signer (display + account) / comment / time / status

        Returns dict with `ok` flag + the scraped fields, or `ok=False` with reason.
        """
        with self._lock:
            def _job():
                self._ensure_initialized()
                if not self._is_authenticated():
                    self._do_login()
                page = self._page
                base = os.environ["UOF_BASE_URL"].rstrip("/")
                url = f"{base}{VIEW_FORM_TEMP_PATH}?TASK_ID={task_id}"
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2500)

                html = page.content()
                if "Login.aspx" in page.url:
                    return {"ok": False, "reason": "redirected to Login.aspx"}
                if "Error404.png" in html:
                    return {"ok": False, "reason": "task_id 不存在或頁面不可訪問"}
                if "ErrorReport" in page.url or "ErrorReport" in html:
                    # Invalid task_id triggers UOF's centralized ErrorReport page
                    # (e.g. /UOF/Common/ErrorReport/Default.aspx?EventID=...)
                    return {"ok": False, "reason": "task_id 不存在（UOF 回 ErrorReport 頁）"}
                if "無此表單" in html and "權限" in html:
                    return {"ok": False, "reason": "目前帳號無此表單檢視權限"}

                # Form name / number have stable Telerik-style ids on ViewForm.aspx.
                # `page.title()` works on FormPrint but not here, so go through the DOM.
                form_name = ""
                form_number = ""
                try:
                    el = page.query_selector("[id$='_lblFormName']")
                    if el:
                        form_name = (el.text_content() or "").strip()
                except Exception:
                    pass
                try:
                    # lblAutoNumber lives on a form-version-specific control id, so we
                    # match anywhere it appears (suffix match).
                    el = page.query_selector("[id$='_lblAutoNumber']")
                    if el:
                        form_number = (el.text_content() or "").strip()
                except Exception:
                    pass

                # Overall result label uses a stable Telerik-style id.
                overall = ""
                try:
                    el = page.query_selector("#ctl00_ContentPlaceHolder1_UC_SignComment1_lblFormResult")
                    if el:
                        overall = (el.text_content() or "").strip()
                except Exception:
                    pass

                # The sign-history grid sits inside pcSignCommentGrid. Iterate its <tr>s
                # and pick rows whose first <td> contains a site number ("0", "1", "1.2"...)
                sites: list[dict] = []
                try:
                    rows = page.query_selector_all(
                        "#ctl00_ContentPlaceHolder1_UC_SignComment1_pcSignCommentGrid tr"
                    )
                    for row in rows:
                        tds = row.query_selector_all("td")
                        if len(tds) < 6:
                            continue
                        site_id = (tds[0].text_content() or "").strip()
                        if not site_id or not site_id[0].isdigit():
                            continue
                        signer_raw = (tds[2].text_content() or "").strip()
                        comment = (tds[3].text_content() or "").strip()
                        time_val = (tds[4].text_content() or "").strip()
                        status = (tds[5].text_content() or "").strip()

                        # Signer comes in shapes like:
                        #   "部門 User Name(account)"
                        #   "研發處-處級主管 Manager Name(account) (自動簽核)"
                        #   "副流程：直屬>部主管>處長>總經理>董事長"   ← sub-flow header row, no account
                        signer_name = signer_raw
                        signer_account = ""
                        auto_sign = False
                        if "(自動簽核)" in signer_raw:
                            auto_sign = True
                            signer_raw_for_parse = signer_raw.replace("(自動簽核)", "").strip()
                        else:
                            signer_raw_for_parse = signer_raw
                        ma = re.search(r"^(.+?)\s*\(([^)]+)\)\s*$", signer_raw_for_parse)
                        if ma:
                            signer_name = ma.group(1).strip()
                            signer_account = ma.group(2).strip()

                        sites.append({
                            "site": site_id,
                            "signer_name": signer_name,
                            "signer_account": signer_account,
                            "auto_sign": auto_sign,
                            "comment": comment,
                            "time": time_val,
                            "status": status,
                        })
                except Exception as e:
                    return {"ok": False, "reason": f"sign history scrape error: {e}"}

                # Applicant is conventionally the row with site=0 (status=申請)
                applicant_name = ""
                applicant_account = ""
                for s in sites:
                    if s["site"] == "0":
                        applicant_name = s["signer_name"]
                        applicant_account = s["signer_account"]
                        break

                return {
                    "ok": True,
                    "reason": "",
                    "form_name": form_name,
                    "form_number": form_number,
                    "applicant_name": applicant_name,
                    "applicant_account": applicant_account,
                    "overall_result": overall,
                    "sites": sites,
                    "url": page.url,
                }
            return self._submit(_job)

    def web_apply(self, handler: Any, form_name: str, payload: dict, dry_run: bool) -> dict:
        """在 worker thread 內執行某單種 handler 的填寫＋送出（重用既有 session）。

        所有 Playwright 步驟都在此單一執行緒內，符合 sync_api 的 thread affinity；handler 只拿到 page。"""
        with self._lock:
            def _job():
                self._ensure_initialized()
                if not self._is_authenticated():
                    self._do_login()
                try:
                    result = handler.fill_and_submit(self._page, form_name, payload, dry_run)
                except Exception:
                    self._do_login()
                    result = handler.fill_and_submit(self._page, form_name, payload, dry_run)
                if isinstance(result, dict) and not result.get("ok") and "Login.aspx" in str(result.get("reason", "")):
                    self._do_login()
                    result = handler.fill_and_submit(self._page, form_name, payload, dry_run)
                return result
            return self._submit(_job)

    def form_id_for_version(self, form_version_id: str) -> str:
        """從 ApplyFormList mapping 反查 formVersionId 對應的 formId。"""
        with self._lock:
            def _job():
                self._ensure_initialized()
                if not self._is_authenticated():
                    self._do_login()
                try:
                    mapping = self._fetch_form_id_version_mapping()
                except Exception:
                    self._do_login()
                    self._form_id_version_map = None
                    mapping = self._fetch_form_id_version_mapping()
                if not mapping or "Login.aspx" in (self._page.url if self._page else ""):
                    self._do_login()
                    self._form_id_version_map = None
                    mapping = self._fetch_form_id_version_mapping()
                reverse = {v: k for k, v in mapping.items()}
                return reverse.get(form_version_id.lower(), "")
            return self._submit(_job)

    def shutdown(self) -> None:
        if self._closed:
            return
        if not self._initialized:
            self._closed = True
            return

        def _job():
            try:
                self._save_state()
            except Exception as e:
                _eprint(f"[ops.web] ⚠️ save_state 失敗: {e}")
            try:
                if self._context:
                    self._context.close()
                if self._browser:
                    self._browser.close()
                if self._pw:
                    self._pw.stop()
            except Exception as e:
                _eprint(f"[ops.web] ⚠️ shutdown 異常: {e}")

        # Python's atexit shuts down all ThreadPoolExecutors before user-registered atexit
        # callbacks fire, so by the time we get here the executor may already be unusable.
        # Be defensive: swallow the "cannot schedule new futures after shutdown" RuntimeError.
        try:
            self._submit(_job)
        except RuntimeError as e:
            if "shutdown" not in str(e).lower():
                raise
            # Executor already gone; nothing we can do — Playwright will leak resources but
            # the process is exiting anyway, so OS cleanup handles it.
        finally:
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass
            self._closed = True


_runtime: Optional[WebRuntime] = None
_runtime_lock = threading.Lock()

_BROWSER_ENABLED = os.environ.get("UOF_BROWSER_ENABLED", "true").lower() not in ("false", "0", "no")


def get_web_runtime() -> WebRuntime:
    if not _BROWSER_ENABLED:
        raise RuntimeError(
            "瀏覽器功能已停用（UOF_BROWSER_ENABLED=false）。\n"
            "此工具需要 Playwright 支援。若平台環境已備妥 playwright wheel，"
            "請移除 UOF_BROWSER_ENABLED=false 或改設為 true 後重啟。"
        )
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = WebRuntime()
            atexit.register(_runtime.shutdown)
    return _runtime


def reset_web_runtime() -> None:
    """Shut down current runtime; next get_web_runtime() builds a fresh one."""
    global _runtime
    with _runtime_lock:
        if _runtime is not None:
            _runtime.shutdown()
            _runtime = None


# ── WebBackend ────────────────────────────────────────────────────────
class WebBackend(OpsBackend):
    # ── System ──────────────────────────────────────────────────────
    def check_auth(self) -> str:
        return get_session_provider().status_report()

    # ── WKF reads (not yet implemented in web mode) ────────────────
    def get_form_list(self) -> str:
        runtime = get_web_runtime()
        try:
            data = runtime.scrape_form_list()
        except Exception as e:
            return f"❌ 取得表單清單時發生錯誤 ({type(e).__name__}): {e}"
        if not data.get("ok"):
            return f"❌ 取得表單清單失敗：{data.get('reason', '(unknown)')}"

        forms = data["forms"]
        if not forms:
            return "📋 找不到任何表單（可能此帳號無任何表單檢視/起單權限）"

        # Group by category, preserve dropdown order within each category.
        from collections import OrderedDict
        by_cat: "OrderedDict[str, list]" = OrderedDict()
        for f in forms:
            by_cat.setdefault(f["category"], []).append(f)

        lines = [
            f"📋 表單清單（網頁模式，from 查詢表單下拉，共 {len(forms)} 個表單）：",
        ]
        for cat, items in by_cat.items():
            lines.append(f"\n📁 【{cat}】")
            for f in items:
                lines.append(f"  - {f['form_name']} (formId: {f['form_id']})")
        lines.append(
            "\n💡 此清單來源是查詢用下拉，含 formId 但**不**含 formVersionId。"
            "formVersionId 起單時才需要，apply_form 會自動從起單頁解析（後續實作）。"
        )
        return "\n".join(lines)

    def get_external_form_list(self) -> str:
        # 「非線上使用」是 admin 在 UOF 後台「表單管理」勾選的旗標——一般 user 的網頁
        # 介面（包含此帳號的「表單申請」樹、「查詢表單」下拉）都不會把這個旗標暴露出來。
        # SOAP `GetExternalFormList` 直接讀 DB 旗標，網頁模式沒有對應 scrape 目標。
        return (
            "⚠️ 網頁機制無法可靠回 `get_external_form_list`。\n\n"
            "「非線上使用」是 UOF 後台「表單管理」中的 admin 旗標，一般 user 在前端\n"
            "（表單申請樹、查詢表單下拉、列表頁）都看不到這個旗標——只有 SOAP\n"
            "GetExternalFormList 直接讀 DB 才能取得。\n\n"
            "可行替代：\n"
            "- 用 `get_form_list` 看「目前帳號可查詢/起單」的所有表單\n"
            "- 「非線上使用」與「可外部起單」並非相同概念（採購單就不在前者卻能起單），\n"
            "  若是想知道「哪些表單可以起單」，請看 get_form_list 結果是否有 formVersionId。"
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
        runtime = get_web_runtime()
        try:
            data = runtime.scrape_form_structure(form_id=form_id, form_version_id=form_version_id)
        except Exception as e:
            return f"❌ 取得表單結構時發生錯誤 ({type(e).__name__}): {e}"
        if not data.get("ok"):
            return f"❌ 取得表單結構失敗（by {by_label}）：{data.get('reason', '(unknown)')}"

        fields = data["fields"]
        # Group helpful hints by input_type — mirrors SOAP's fill_hint table.
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
            "unknown": "型別未知（可能是版面元件）",
        }
        unsupported_for_apply = [f for f in fields if f["input_type"] in ("dataGrid", "fileButton")]

        lines = [
            f"📝 表單 {form_id or form_version_id} 的欄位清單"
            f"（網頁模式，from AddFormScript.aspx）",
            f"  formId: {data['form_id']}",
            f"  formVersionId: {data['form_version_id']}",
            f"  共 {len(fields)} 個欄位：",
        ]
        for f in fields:
            mark = "＊" if f["required"] else " "
            code = f["code"] or "—"
            hint = fill_hint.get(f["input_type"], f["input_type"])
            lines.append(
                f"  {mark} [{code}] {f['label']} 〈{f['input_type']}〉 — {hint}"
            )

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
        runtime = get_web_runtime()
        try:
            data = runtime.scrape_task_summary(task_id)
        except Exception as e:
            return (
                f"❌ 查詢 task {task_id} 時發生錯誤 ({type(e).__name__}): {e}\n"
                f"💡 網頁模式仍在 alpha，遇到非預期頁面結構時可能誤判。"
            )
        if not data.get("ok"):
            return f"❌ 找不到 task {task_id}：{data.get('reason', '(unknown)')}"

        lines = [
            "📄 表單申請內容（from FormPrint.aspx）：",
            f"  - 表單名稱: {data['form_name'] or '(未取得)'}",
            f"  - 表單編號: {data['form_number'] or '(未取得)'}",
            f"  - 申請者: {data['applicant_display'] or '(未取得)'}"
            + (f" ({data['account']})" if data['account'] else ""),
            f"  - 申請時間: {data['apply_time'] or '(未取得)'}",
            f"  - 簽核結果: (網頁模式尚未支援；需另開「實際簽核流程」 iframe 或對應 list 視圖)",
            f"  - 結案日期: (網頁模式尚未支援)",
            "",
            f"🔗 來源頁: {data['url']}",
            "💡 SOAP 可直接取得 result/resultDate；"
            "本部署無 PublicAPI，因此網頁模式僅能拿到列印頁可見欄位。",
        ]
        return "\n".join(lines)

    def get_task_result(self, task_id: str, include_form_data: bool = True) -> str:
        runtime = get_web_runtime()
        try:
            data = runtime.scrape_sign_history(task_id)
        except Exception as e:
            return (
                f"❌ 查詢 task {task_id} 簽核歷程時發生錯誤 "
                f"({type(e).__name__}): {e}"
            )
        if not data.get("ok"):
            return f"❌ 找不到 task {task_id} 的簽核歷程：{data.get('reason', '(unknown)')}"

        lines = [
            f"📄 表單 {data['form_name']} {data['form_number']} 的簽核記錄"
            f"（網頁模式，task_id={task_id}）：",
            f"  申請者: {data['applicant_name']}"
            + (f" ({data['applicant_account']})" if data['applicant_account'] else "")
            + f" | 最終結果: {data['overall_result'] or '(進行中)'}",
            "",
            "📝 簽核歷程：",
        ]
        for s in data["sites"]:
            tag_parts = []
            if s["auto_sign"]:
                tag_parts.append("自動")
            tag = f" ({'/'.join(tag_parts)})" if tag_parts else ""
            line = (
                f"  站點 {s['site']}: {s['signer_name']}"
                + (f" ({s['signer_account']})" if s['signer_account'] else "")
                + tag
                + f" → {s['status'] or '(待簽)'}"
                + (f" [{s['time']}]" if s['time'] else "")
            )
            lines.append(line)
            if s["comment"]:
                lines.append(f"    意見: {s['comment']}")

        if include_form_data:
            lines.append(
                "\n💡 表單欄位內容未在此模式輸出——若需檢視欄位值請用 get_task_data；"
                "完整欄位內容可在 UOF 網頁「觀看」彈窗中查看。"
            )
        lines.append(f"\n🔗 來源頁: {data['url']}")
        return "\n".join(lines)

    # ── WKF writes (not yet implemented in web mode) ───────────────
    def preview_workflow(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: Optional[dict] = None,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str:
        return web_not_implemented(
            "preview_workflow",
            "需驅動表單起單畫面到「預覽流程」步驟並 scrape 結果。",
        )

    def apply_form(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: dict,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str:
        return web_not_implemented(
            "apply_form",
            "需驅動表單填寫頁面、處理 Telerik partial postback、送出取得 TaskId。"
            "這是 11 個 tool 中工作量最大的一個（含 __VIEWSTATE 連動與動態欄位）。",
        )

    def terminate_task(self, task_id: str, result: str, reason: str) -> str:
        return web_not_implemented(
            "terminate_task",
            f"需開表單 {task_id} 的詳細頁、點「強制結案/作廢」按鈕、填理由送出。",
        )

    def sign_next(
        self, task_id: str, site_id: str, node_seq: int, signer_guid: str
    ) -> str:
        return web_not_implemented(
            "sign_next",
            "固定流程 SignNext2 的對應 UI 操作；自由流程不適用。",
        )

    # ── Web-only: search forms ──────────────────────────────────────
    def _call_web(self, fn: Callable[..., Any]) -> Any:
        """跑網頁操作；session 失效（中途被導回 Login，或拋例外）時，強制重登後重試一次。

        對應 SoapBackend._call 的 token 失效自動刷新——讓所有網頁工具都具備 retry：快取 cookie
        可能通過了 _is_authenticated 卻在操作中途過期，這裡攔截並重登重試，使用者無感。"""
        runtime = get_web_runtime()
        try:
            result = fn(runtime)
        except Exception:
            runtime.force_relogin()
            return fn(runtime)
        if isinstance(result, dict) and not result.get("ok") \
                and "Login.aspx" in str(result.get("reason", "")):
            runtime.force_relogin()
            result = fn(runtime)
        return result

    def query_forms(
        self,
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        max_results: int = 50,
    ) -> str:
        try:
            result = self._call_web(
                lambda rt: rt.search_forms(keyword, date_from, date_to, max_results)
            )
        except Exception as e:
            return (
                f"❌ 查詢表單時發生錯誤 ({type(e).__name__}): {e}\n"
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
            + (f"，關鍵字「{q['keyword']}」" if q['keyword'] else "")
            + "\n"
        )
        if not rows:
            return header + "📋 查無資料"

        lines = [header + f"共 {total} 筆"
                 + (f"（僅顯示前 {len(rows)} 筆）" if total > len(rows) else "") + "："]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"\n[{i}] {r['form_name']} {r['form_number']}  〈{r['status']}〉"
                f"\n    TaskId: {r['task_id'] or '(無法擷取)'}"
                f"\n    申請者: {r['applicant']}"
                f"\n    申請時間: {r['apply_time']}"
                + (f"\n    結案時間: {r['close_time']}" if r['close_time'] else "")
                + (f"\n    摘要: {r['subject']}" if r['subject'] else "")
            )
        lines.append(
            "\n💡 把 TaskId 帶入 `get_task_data` / `get_task_result` 可查單張詳情。"
        )
        return "\n".join(lines)
