from __future__ import annotations
import html
import json
import re
from datetime import date
from typing import Optional
from .constants import _DATAGRID_DIALOG_RE
from .parsing import _parse_inline_controls


def _mark_filled(filled: dict, caller_key: str, fb: dict, value: str) -> None:
    """Record a successful fill under every key validation may use."""
    filled[caller_key] = value
    code = fb.get("code") or ""
    label = fb.get("label") or ""
    if code:
        filled[code] = value
    if label:
        filled[label] = value




def _missing_required_controls(tree, uc_prefix: str, payload: dict) -> list:
    """Return required inline controls whose rendered/postback value is still empty."""
    return [
        c for c in _parse_inline_controls(tree, uc_prefix)
        if c.get("required") and not str(payload.get(c["name"], "")).strip()
    ]




def _missing_required_dialog_fields(fields: list, payload: dict) -> list:
    """Return required row-editor fields still empty immediately before confirm."""
    return [
        c for c in fields
        if c.get("required") and not c.get("hidden")
        and not str(payload.get(c["name"], "")).strip()
    ]




def _resolve_checkbox_value(options: list, value) -> tuple:
    """Resolve one ASP.NET checkbox to (checked, posted_value, error).

    A checkbox option labelled/value ``否`` is still a real selectable option: matching the
    server-advertised option takes precedence over interpreting free-form false-like strings.
    """
    sv = str(value).strip()
    hit = next((o for o in options if sv in (str(o["value"]), str(o["label"]))), None)
    if hit is not None:
        return True, str(hit["value"]), None
    if isinstance(value, bool):
        return value, (str(options[0]["value"]) if value and options else "on"), None
    low = sv.lower()
    if low in ("true", "1", "yes", "y", "checked"):
        return True, (str(options[0]["value"]) if options else "on"), None
    if low in ("", "false", "0", "no", "n", "unchecked"):
        return False, "", None
    allowed = "／".join(str(o["label"]) for o in options)
    hint = f"，只能填：{allowed}、true 或 false" if allowed else "，只能填 true 或 false"
    return False, "", f"值『{value}』不是有效 checkbox 選項{hint}"




def _uof_row_date(row: dict, query_mode: str) -> Optional[date]:
    """Extract the date used by query_forms from one rendered result row."""
    # The result grid exposes apply/close time, but not the time at which this user signed.
    # Therefore only apply-mode has a trustworthy client-side date source.
    if query_mode != "apply":
        return None
    raw = row.get("apply_time", "") or ""
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", raw)
    if not m:
        return None
    try:
        return date(*(int(v) for v in m.groups()))
    except ValueError:
        return None




def _map_row_to_columns(row, columns: list) -> tuple:
    """Map one user-supplied row to ({column_index: value}, [unmatched_keys]).

    Accepts a positional list/tuple (col order), or a dict keyed by column label (exact or
    contains) or by 1-based position / `<code>_N` suffix. Unmatched keys are returned instead
    of silently dropped — posting a row whose keys all missed produces an EMPTY row on the
    server (it accepts blank rows), which is exactly the incomplete-form trap to fail loudly on.
    """
    out: dict = {}
    unmatched: list = []
    if isinstance(row, (list, tuple)):
        if len(row) > len(columns):
            unmatched.append(f"多出 {len(row) - len(columns)} 個值（此明細只有 {len(columns)} 欄）")
        for col, v in zip(columns, row):
            out[col["index"]] = v
        return out, unmatched
    if isinstance(row, dict):
        by_label = {c["label"]: c for c in columns if c["label"]}
        for k, v in row.items():
            ks = str(k)
            col = by_label.get(ks)
            if col is None:
                for c in columns:
                    if c["label"] and (ks in c["label"] or c["label"] in ks):
                        col = c
                        break
            if col is None:
                mnum = re.search(r"(\d+)$", ks)
                if mnum:
                    pos = int(mnum.group(1))
                    if 1 <= pos <= len(columns):
                        col = columns[pos - 1]
            if col is None:
                unmatched.append(ks)
            else:
                out[col["index"]] = v
        return out, unmatched
    return out, [f"列格式需為 dict 或 list（收到 {type(row).__name__}）"]


# ── HttpSession ───────────────────────────────────────────────────────



def _temp_return_value(html_text: str):
    """Extract the FAST dialog's return value from the `TempReturnValue` meta content.

    A FAST edit-dialog (expense line, etc.) does not echo its value in FASTReturnValue's
    value attr; on a successful 確定 it emits `<meta id="TempReturnValue" ... content="<row JSON>">`.
    That decoded JSON string is exactly the DialogReturnValue the parent's row-add button expects.
    Returns the (html-unescaped) row JSON string, or None if absent / "NeedPostBack".
    """
    m = re.search(r'id="TempReturnValue"[^>]*content="([^"]*)"', html_text)
    if not m:
        return None
    val = html.unescape(m.group(1))
    return None if val in ("", "NeedPostBack", "[DefaultNullValue]") else val




def _dialog_reject_reason(html_text: str) -> str:
    """Why a row-editor dialog refused a row, read straight off the confirm response.

    Two independent signals, whichever the page carries: ASP.NET required-field validators that
    fired (their span renders without `display:none`), and any server-side validation-API failure
    echoed into the page. Stays domain-free — it reports whatever field markers / messages the
    page itself names, so the caller stops guessing 「沒帶內部 Id」 when the real reason is 必填未填
    or a business rule (e.g. 憑證格式與發票字軌不匹配)."""
    parts: list = []
    fired: list = []
    for m in re.finditer(r'id="[^"]*?_RF_(\w+)"([^>]*)>\s*<font color="Red">[^<]*必填', html_text):
        style = re.search(r'style="([^"]*)"', m.group(2) or "")
        if not (style and "display:none" in style.group(1).replace(" ", "").lower()):
            fired.append(m.group(1))
    if fired:
        parts.append("必填未填/未帶：" + "、".join(dict.fromkeys(fired)))
    decoder = json.JSONDecoder()
    decoded_html = html.unescape(html_text)
    for m in re.finditer(r'"errorMessage"\s*:\s*', decoded_html):
        try:
            messages, _ = decoder.raw_decode(decoded_html[m.end():])
            if not isinstance(messages, list):
                raise ValueError("errorMessage 不是陣列")
            for message in messages:
                if message not in (None, ""):
                    parts.append("伺服器驗證：" + str(message))
        except (TypeError, ValueError):
            parts.append("伺服器驗證訊息格式無法解析")
    return "；".join(parts)




def _datagrid_dialog_path(page_html: str, field_code: str) -> str:
    """Extract a dataGrid field's row-editor dialog URL from the FirstSite page HTML.

    The row-add button's `$uof.dialog.open2('…SetupDataGridFieldValue.aspx?…fieldId=<code>…')`
    carries the runtime scriptId/applicantGuid. Returns the (html-unescaped) path, or "".
    """
    m = re.search(_DATAGRID_DIALOG_RE.format(code=re.escape(field_code)), page_html)
    return html.unescape(m.group(1)) if m else ""

