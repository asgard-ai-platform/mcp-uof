"""
HttpWebBackend — httpx + lxml UOF web ops (no Playwright).

The only web mechanism: browser-simulated round-trips against the ASP.NET
server-side-render site are done with plain HTTPS requests (httpx.Client,
thread-safe, cookie jar); HTML is parsed with lxml. Works where Chromium is
unavailable (e.g. Alpine Linux). Re-logs in automatically when a response
redirects to Login.aspx.
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

from .._log import eprint as _eprint
from .base import OpsBackend


# ── Page path constants ───────────────────────────────────────────────
_LOGIN_PATH = "/Login.aspx"
_HOMEPAGE_PATH = "/Homepage.aspx"
_FORM_QUERY_PATH = "/WKF/FormUse/PersonalBox/MyFormList.aspx?item=FormQuery"
_APPLY_FORM_LIST_PATH = "/WKF/FormUse/PersonalBox/ApplyFormList.aspx"
_ADD_FORM_SCRIPT_PATH = "/WKF/FormUse/AddFormScript.aspx"
_FORM_CACHE_TTL_SECONDS = 300.0

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

def _parse_apply_form_tree(html_text: str) -> list:
    """Parse ApplyFormList.aspx (電子簽核 » 表單申請 tree) into applyable forms.

    Returns [{form_id, form_version_id, form_name, category}] — the forms this account can
    *initiate*. This is the correct source for "what can I start"; the FormQuery dropdown
    (``scrape_form_list``) is the *queryable* set (broader, and without version ids).

    The RadTreeView renders node names as ``<span class="rtIn">…</span>`` in depth-first
    render order; its client config embeds ``"nodeData":[{value:<catGuid>, items:[{value:
    "<formId>@<verId>", ...}]}]`` in that same order. Zip the two: a node whose value has no
    ``@`` is a category folder, a leaf whose value is ``formId@formVersionId`` is a form.
    """
    names = [
        html.unescape(re.sub(r"<[^>]+>", "", m)).strip()
        for m in re.findall(r'class="rtIn"[^>]*>(.*?)</span>', html_text, re.S)
    ]
    # balanced-bracket scan of the nodeData array
    p = html_text.find('"nodeData":')
    if p == -1:
        return []
    start = html_text.find("[", p)
    depth = 0
    end = -1
    for i in range(start, len(html_text)):
        c = html_text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return []
    try:
        node_data = json.loads(html_text[start:end])
    except (ValueError, TypeError):
        return []

    flat: list = []  # (kind, value) in render order

    def _walk(nodes):
        for n in nodes:
            v = n.get("value", "")
            flat.append(("form" if "@" in v else "cat", v))
            if n.get("items"):
                _walk(n["items"])

    _walk(node_data)
    # names come from the DOM in the same render order; if the counts drift, blank the names
    # rather than risk pairing a form with the wrong label.
    if len(flat) != len(names):
        _eprint(
            f"[ops.http_web] ApplyFormList tree: node/name count mismatch "
            f"({len(flat)} nodes vs {len(names)} names); names omitted"
        )
        names = ["" for _ in flat]

    forms: list = []
    cur_cat = "(未分類)"
    for (kind, val), name in zip(flat, names):
        if kind == "cat":
            cur_cat = name or cur_cat
        else:
            fid, _, vid = val.partition("@")
            forms.append({
                "form_id": fid.lower(),
                "form_version_id": vid.lower(),
                "form_name": name,
                "category": cur_cat,
            })
    return forms


def _mark_filled(filled: dict, caller_key: str, fb: dict, value: str) -> None:
    """Record a successful fill under every key validation may use."""
    filled[caller_key] = value
    code = fb.get("code") or ""
    label = fb.get("label") or ""
    if code:
        filled[code] = value
    if label:
        filled[label] = value


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

            # Required: the ＊ marker. Plugin forms render it as <span id="…lblStart#">＊</span>
            # coloured by CSS class (not inline style); native fields use an inline color:red
            # span. The rule only shows on the rendered page, so read it from the DOM — check
            # both encodings.
            required = False
            for span in block.xpath(".//span[contains(@id,'lblStart')]"):
                text = "".join(span.itertext())
                if "＊" in text or "*" in text:
                    required = True
                    break
            if not required:
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

            # Options for choose-from-a-fixed-list fields (radio / checkbox / dropDown).
            # The agent must validate user input against these before apply_form — a value
            # outside the list is silently dropped by the server (e.g. 地點="台北" lands empty).
            options: list = []
            if input_type in ("radio", "checkbox") and input_type_attr:
                for inp in block.xpath(f".//input[@type='{input_type_attr}']"):
                    val = inp.get("value") or ""
                    iid = inp.get("id") or ""
                    if not val:
                        continue
                    # skip the free-text 其他/Others escape-hatch radio (id …rbOthers, value
                    # "rbOthers", paired with a txtOthers box) — it is not a fixed choice.
                    if "others" in iid.lower() or val.lower() == "rbothers":
                        continue
                    lbl = ""
                    lab_el = block.xpath(".//label[@for=$fid]", fid=iid) if iid else []
                    if lab_el:
                        lbl = lab_el[0].text_content().strip()
                    options.append({"value": val, "label": lbl or val})
            elif input_type == "dropDown" and input_el is not None:
                for o in input_el.xpath(".//option"):
                    val = (o.get("value") or "").strip()
                    txt = o.text_content().strip()
                    if not val or val in ("all", "###***$$$") or txt in ("所有表單", "─請選擇─"):
                        continue
                    options.append({"value": val, "label": txt})

            # Disabled controls ignore posted values; expose the state instead of reporting a
            # value as filled when the server will discard it.
            disabled = input_el is not None and input_el.get("disabled") is not None
            if not disabled and input_type == "datePicker":
                di = block.xpath(".//input[contains(@name,'dateInput')]")
                disabled = bool(di) and di[0].get("disabled") is not None

            field: dict = {
                "code": code,
                "label": label,
                "required": required,
                "input_type": input_type,
                "input_name": input_name,
                "input_title": input_title,
                "dialog_url": dialog_url,
                "options": options,
                "disabled": disabled,
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

    # Some forms use an older template without `table.fieldWidth`; fall back when the primary
    # parser finds no fields.
    if not fields:
        fields = _parse_classic_field_blocks(tree)
    return fields


def _parse_classic_field_blocks(tree) -> list:
    """Fallback field parser for UOF's older "classic" form template.

    Each field is a `<tr>` of two `<td class="ul">` cells: a label cell
    (`<font color="Red">＊</font>` for
    required + a `<span ondblclick="CopyToClipBoard()">label：</span>`) and an input cell
    holding exactly one `versionFieldUC{n}` group. No `table.fieldWidth` wrapper, and — unlike
    the Telerik template — **no field code is exposed anywhere in this page's DOM** (the
    CopyToClipBoard() label span carries no code attribute/argument to recover one from).

    Because the DOM does not expose a field code, `code` uses the DOM-sourced label text, which
    `apply_form_web` also accepts as a match key.
    """
    fields = []
    for td in tree.xpath("//td[@class='ul'][@align='right']"):
        try:
            raw = "".join(td.itertext())
            label = re.sub(r"\s+", " ", raw).strip().lstrip("＊").rstrip("：").strip()
            if not label:
                continue
            required = bool(td.xpath(".//font[@color='Red']"))
            sib = td.getnext()
            if sib is None:
                continue

            input_el = None
            for el in sib.xpath(".//input[@type!='hidden'] | .//select | .//textarea"):
                name = el.get("name") or ""
                if not name or "ClientState" in name or name.endswith("dateInput") or name.endswith(("_SD", "_AD")):
                    continue
                input_el = el
                break
            input_name = input_el.get("name") if input_el is not None else ""
            input_kind = (input_el.tag or "").lower() if input_el is not None else ""
            input_class = (input_el.get("class") or "") if input_el is not None else ""
            input_type_attr = (input_el.get("type") or "").lower() if input_el is not None else ""

            is_datagrid = bool(sib.xpath(
                ".//*[contains(@id,'DataGrid') or contains(@onclick,'SetupDataGridFieldValue')]"
            ))
            is_file = bool(sib.xpath(".//*[contains(@onclick,'RemoteFileDialog') or contains(@onclick,'FileCenter')]"))
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

            options: list = []
            if input_type in ("radio", "checkbox") and input_type_attr:
                for inp in sib.xpath(f".//input[@type='{input_type_attr}']"):
                    val = inp.get("value") or ""
                    iid = inp.get("id") or ""
                    if not val or "others" in iid.lower() or val.lower() == "rbothers":
                        continue
                    lbl = ""
                    lab_el = sib.xpath(".//label[@for=$fid]", fid=iid) if iid else []
                    if lab_el:
                        lbl = lab_el[0].text_content().strip()
                    options.append({"value": val, "label": lbl or val})
            elif input_type == "dropDown" and input_el is not None:
                for o in input_el.xpath(".//option"):
                    val = (o.get("value") or "").strip()
                    txt = o.text_content().strip()
                    if not val or val in ("all", "###***$$$") or txt in ("所有表單", "─請選擇─"):
                        continue
                    options.append({"value": val, "label": txt})

            # datePicker's real `disabled` lives on the visible dateInput sub-element, not the
            # hidden trigger input `input_el` resolves to (same quirk as the Telerik parser above).
            disabled = input_el is not None and input_el.get("disabled") is not None
            if not disabled and input_type == "datePicker":
                di = sib.xpath(".//input[contains(@name,'dateInput')]")
                disabled = bool(di) and di[0].get("disabled") is not None

            field = {
                "code": label,
                "label": label,
                "required": required,
                "input_type": input_type,
                "input_name": input_name or "",
                "input_title": "",
                "dialog_url": "",
                "options": options,
                "disabled": disabled,
            }
            if input_type == "dataGrid":
                # The real internal grid fieldId (e.g. "004") isn't in this row's markup at all —
                # it only shows up inside a RadToolBar client-config JSON blob elsewhere on the
                # page, keyed by this field's add-row button uniqueID. `code` stays the label
                # (for apply_form_web's normal field matching); this is a separate lookup key
                # specifically for _datagrid_dialog_path (see add_datagrid_rows call site).
                uc_idx = None
                m2 = re.search(r"versionFieldUC(\d+)", input_name or "")
                if m2:
                    uc_idx = m2.group(1)
                if uc_idx is None:
                    # dataGrid fields have no direct input; recover the index from the AddDgRow marker
                    idm = sib.xpath(".//*[contains(@id,'AddDgRow')]/@id")
                    if idm:
                        m3 = re.search(r"versionFieldUC(\d+)", idm[0])
                        uc_idx = m3.group(1) if m3 else None
                if uc_idx is not None:
                    page_html = _etree.tostring(tree, encoding="unicode")
                    btn_marker = f"versionFieldUC{uc_idx}$WebImageButton_AddDgRow"
                    p = page_html.find(btn_marker)
                    if p != -1:
                        # The RadButton client config sits between the uniqueID and the `clicking`
                        # handler's open2() URL, so inspect a bounded window after the marker.
                        window = page_html[p:p + 2000]
                        fm = re.search(r"fieldId=([A-Za-z0-9_]+)", window)
                        if fm:
                            field["grid_field_id"] = fm.group(1)
            fields.append(field)
        except Exception as ex:
            _eprint(f"[ops.http_web] ⚠️ classic field block parse error: {type(ex).__name__}: {ex}")
            continue
    return fields


def _picker_search_payload(tree, keyword: str) -> dict:
    """Keyword + search-button entries needed to actually run a picker's search.

    Setting the keyword box alone is not enough: an ASP.NET submit button only fires when its
    `name=value` pair is posted too, so without it the server re-renders the default first page
    and every keyword silently returns identical rows.
    """
    p: dict = {}
    box = ""
    for inp in tree.xpath("//input[@type='text']"):
        name = inp.get("name") or ""
        low = name.lower()
        if name and ("search" in low or "keyword" in low or "key" in low):
            box = name
            break
    if not box:  # single text input on a picker page is the keyword box
        boxes = [i.get("name") for i in tree.xpath("//input[@type='text']") if i.get("name")]
        if len(boxes) == 1:
            box = boxes[0]
    if box:
        p[box] = keyword
    for btn in tree.xpath("//input[@type='submit']"):
        name = btn.get("name") or ""
        val = btn.get("value") or ""
        if not name:
            continue
        if "搜尋" in val or "查詢" in val or "search" in name.lower() or "btnkey" in name.lower():
            p[name] = val
            break
    return p


def _control_label(el) -> tuple:
    """(label, required) for a form control, from the nearest preceding label cell.

    A UOF row often packs several label/control pairs side by side
    (`主旨 [c] ＊付款人 [c] ＊立帳日期 [c]`), so the row's first cell is the wrong answer for
    every pair but the first — walk back from the control's own cell instead. Long cells are
    layout/CSS noise, not labels.
    """
    td = el.getparent()
    while td is not None and td.tag != "td":
        td = td.getparent()
    cands = []
    if td is not None:
        cands = list(td.itersiblings(preceding=True))
        row = td.getparent()
        if row is not None and row.tag == "tr":
            first = row.xpath("./td")
            if first:
                cands.append(first[0])
    for c in cands:
        raw = re.sub(r"\s+", " ", "".join(c.itertext())).replace("\xa0", " ").strip()
        if not raw or len(raw) > 30:
            continue
        return raw.lstrip("*＊ ").strip(), raw.startswith(("*", "＊"))
    return "", False


def _choice_controls(tree, keep) -> list:
    """Radio groups and checkboxes, in the same shape as the text/select controls.

    A radio group collapses into one control whose `options` are its buttons, matching how a
    <select> is reported. This keeps required choices visible even when no option is selected.
    """
    def _opt_text(el) -> str:
        lid = el.get("id") or ""
        for lab in (tree.xpath(f"//label[@for={lid!r}]") if lid else []):
            t = re.sub(r"\s+", " ", "".join(lab.itertext())).strip()
            if t:
                return t
        return el.get("value") or ""

    groups: dict = {}
    out: list = []
    for el in tree.xpath("//input[@type='radio'] | //input[@type='checkbox']"):
        name = el.get("name") or ""
        if not name or name.startswith("__") or not keep(name):
            continue
        label, required = _control_label(el)
        base = {
            "name": name,
            "id": name.split("$")[-1],
            "label": label,
            "required": required,
            "readonly": el.get("disabled") is not None,
            "hidden": "HideMe" in (el.get("class") or ""),
            "lookup_buttons": [],
        }
        if (el.get("type") or "").lower() == "checkbox":
            out.append(dict(base, type="checkbox", options=[],
                            value=el.get("value") or "on" if el.get("checked") is not None else ""))
            continue
        g = groups.get(name)
        if g is None:
            g = dict(base, type="radio", options=[], value="")
            groups[name] = g
            out.append(g)
        g["options"].append({"value": el.get("value") or "", "text": _opt_text(el)})
        if el.get("checked") is not None:
            g["value"] = el.get("value") or ""
    return out


def _parse_inline_controls(tree, uc_prefix: str) -> list:
    """Named controls a plugin renders inline inside one `versionFieldUC<N>` block.

    `_parse_field_blocks` reports the block as a single dialog field, so the controls the plugin
    draws inside it (付款人 / 立帳日期 / 金額 …) are otherwise invisible. Grouping by the UC
    prefix keeps this generic — no form or plugin is named.
    """
    def _t(el) -> str:
        return re.sub(r"\s+", " ", "".join(el.itertext())).replace("\xa0", " ").strip()

    out = []
    for el in tree.xpath("//input[@type='text'] | //select | //textarea"):
        name = el.get("name") or ""
        if uc_prefix not in name or name.startswith("__"):
            continue
        label, required = _control_label(el)
        out.append({
            "name": name,
            "id": (el.get("id") or "").split("_")[-1],
            "label": label,
            "required": required,
            "type": "select" if el.tag == "select" else (el.tag if el.tag == "textarea" else "text"),
            "options": [{"value": o.get("value") or "", "text": _t(o)} for o in el.xpath(".//option")],
            "readonly": el.get("readonly") is not None or el.get("disabled") is not None,
            "hidden": "HideMe" in (el.get("class") or ""),
            "lookup_buttons": [],
        })
    out.extend(_choice_controls(tree, lambda n: uc_prefix in n))
    return out


def _parse_dialog_fields(dialog_html: str) -> list:
    """Parse a plugin dialog page (PRItemDialog / ExpEmpItemDialog / …) as a mini-form.

    Matched structurally, never by URL: any dialog whose controls sit in table rows whose first
    cell is the label (`*` prefix = required). Complements `_parse_datagrid_columns`, which only
    handles the `SetupDataGridFieldValue` versionFieldUC template.

    Returns [{name, id, label, required, type, options, readonly, hidden, lookup_buttons}].
    Every control is returned, including hidden helper companions (料號 has 4), because picking
    the "real" one is form knowledge — a skill's call, not ours.
    """
    tree = _html_fromstring(dialog_html)
    for bad in tree.xpath("//script | //style"):
        bad.getparent().remove(bad)

    def _t(el) -> str:
        return re.sub(r"\s+", " ", "".join(el.itertext())).replace("\xa0", " ").strip()

    out = []
    for el in tree.xpath("//input[@type='text'] | //input[@type='password'] | //select | //textarea"):
        name = el.get("name") or ""
        if not name or name.startswith("__"):
            continue
        label, required = _control_label(el)
        row = el.getparent()
        while row is not None and row.tag != "tr":
            row = row.getparent()
        cls = (el.get("class") or "")
        style = (el.get("style") or "").replace(" ", "").lower()
        options = []
        if el.tag == "select":
            options = [(o.get("value") or "", _t(o)) for o in el.xpath(".//option")]
        out.append({
            "name": name,
            "id": (el.get("id") or "").split("_")[-1],
            "label": label,
            "required": required,
            "type": "select" if el.tag == "select" else (el.tag if el.tag == "textarea" else "text"),
            "options": [{"value": v, "text": t} for v, t in options],
            "readonly": el.get("readonly") is not None or el.get("disabled") is not None,
            "hidden": "HideMe" in cls or "display:none" in style,
            "lookup_buttons": [
                (b.get("id") or "").split("_")[-1]
                for b in (row.xpath(".//input[@type='submit'] | .//input[@type='button']") if row is not None else [])
            ],
        })
    out.extend(_choice_controls(tree, lambda n: True))
    return out


def _parse_filled_form_fields(tree) -> list:
    """Extract the filled-in field values of a rendered form (ViewForm / SignNodeForm).

    Form-agnostic by design: grabs the widest faithful snapshot and interprets nothing —
    per-form meaning is a skill's job. Keyed by the form's own field code, same vocabulary
    `apply_form` writes with.

    Returns [{code, name, required, value, options, inputs, grid, filler}].
    """
    for bad in tree.xpath("//script | //style"):
        bad.getparent().remove(bad)
    colls = tree.xpath("//table[contains(@id,'tbFieldCollection')]")
    if not colls:
        return []

    def _t(el) -> str:
        return re.sub(r"\s+", " ", "".join(el.itertext())).replace("\xa0", " ").strip()

    out = []
    for td in colls[0].xpath("./tr/td | ./tbody/tr/td"):
        name_el = td.xpath(".//span[@class='TitleFont']")
        code_el = td.xpath(".//span[@class='FieldHide']")
        if not name_el and not code_el:
            continue  # spacer / layout cell
        try:
            f = {
                "name": _t(name_el[0]) if name_el else "",
                "code": _t(code_el[0]).strip("()") if code_el else "",
                "required": bool(td.xpath(".//span[contains(@id,'lblStart')]//font[text()='＊']")),
                "value": "", "options": [], "inputs": {}, "grid": [], "filler": "",
            }
            # a field may own several grids (MAINFORM has Grid1+Grid2)
            grids = td.xpath(".//table[contains(@id,'Grid')]")
            for g in grids:
                rows = []
                for tr in g.xpath(".//tr"):
                    cells = [_t(c) for c in tr.xpath("./td | ./th")]
                    if any(cells):
                        rows.append(cells)
                if rows:
                    f["grid"].append({"id": (g.get("id") or "").split("_")[-1], "rows": rows})

            def _in_grid(el, _grids=grids) -> bool:
                return any(g in el.iterancestors() for g in _grids)

            for sp in td.xpath(".//span[@id]"):
                sid = sp.get("id") or ""
                if "_lbl" not in sid or "lblFiller" in sid or "lblStart" in sid:
                    continue
                if _in_grid(sp):
                    continue
                v = _t(sp)
                if v:
                    f["value"] = f"{f['value']} / {v}" if f["value"] else v
            # Composite fields (MAINFORM) render no lbl spans — their data lives only here.
            # Kept raw under the control name: real data and control state (txtHasItems='OK')
            # aren't distinguishable at this layer. Not grid-filtered — grid rows come from
            # itertext(), which never sees an <input value=…>.
            for inp in td.xpath(".//input[@type='text'] | .//textarea"):
                v = (inp.get("value") or inp.text or "").strip()
                lab, req = _control_label(inp)
                nm = (inp.get("id") or "").split("_")[-1] or "?"
                # Empty ones matter too: a blank ＊required sub-field is the difference between
                # "not filled in" and "we couldn't see it" — the browser comparison showed the old
                # output could not tell those apart.
                if v or req:
                    f["inputs"][nm] = {"label": lab, "required": req, "value": v}
            for ch in td.xpath(".//input[@type='radio' or @type='checkbox']"):
                if _in_grid(ch):
                    continue
                v = ch.get("value") or ""
                if not v or v.startswith(("rb", "cbx")):
                    continue  # "其他" toggle sentinel, not a real option
                f["options"].append({"value": v, "selected": ch.get("checked") is not None})
            sel = [o["value"] for o in f["options"] if o["selected"]]
            if sel:
                f["value"] = " / ".join(dict.fromkeys(sel))
            fl = td.xpath(".//span[contains(@id,'lblFiller')]")
            if fl:
                f["filler"] = _t(fl[0])
            out.append(f)
        except Exception as ex:
            _eprint(f"[ops.http_web] ⚠️ filled field parse error: {type(ex).__name__}: {ex}")
            continue
    return out


def _render_filled_fields(fields: list) -> list:
    """Render `_parse_filled_form_fields` output for an agent to read.

    Shows empty fields rather than hiding them, and keeps UOF's own wording, so a skill can
    tell "not filled in" from "we failed to parse it".
    """
    lines = []
    for f in fields:
        mark = "＊" if f["required"] else " "
        head = f"  {mark}{f['name']}({f['code']}): {f['value'] or '(空白)'}"
        if f["options"]:
            opts = " ｜ ".join(
                ("✓" if o["selected"] else "○") + o["value"] for o in f["options"]
            )
            head += f"\n      選項: {opts}"
        lines.append(head)
        if f["inputs"]:
            lines.append("      欄位內控制項：")
            for k, meta in f["inputs"].items():
                mark = "＊" if meta["required"] else " "
                lab = meta["label"] or "(無標籤)"
                lines.append(f"        {mark}{lab} [{k}] = {meta['value'] or '(空白)'}")
        for g in f["grid"]:
            lines.append(f"      [{g['id']}]")
            for row in g["rows"]:
                lines.append("        | " + " | ".join(row))
    return lines


def _form_state_payload(tree) -> dict:
    """Serialize a rendered ASP.NET form's *current* state like a browser would.

    Unlike `_parse_hidden_fields` (hidden only), this captures every named
    input/select/textarea's present value — required for plugin forms whose
    server-side state must round-trip through each synchronous postback.
    Buttons are excluded (the postback trigger is set separately via __EVENTTARGET);
    radios/checkboxes only post when checked; selects post their selected option.
    """
    p: dict = {}
    for el in tree.xpath("//input[@name]"):
        n = el.get("name")
        t = (el.get("type") or "text").lower()
        if t in ("submit", "button", "image", "reset"):
            continue
        if t in ("checkbox", "radio"):
            if el.get("checked") is not None:
                p[n] = el.get("value") or "on"
        else:
            p.setdefault(n, el.get("value") or "")
    for el in tree.xpath("//textarea[@name]"):
        p[el.get("name")] = el.text or ""
    for el in tree.xpath("//select[@name]"):
        sel = [o for o in el.xpath(".//option") if o.get("selected") is not None]
        p[el.get("name")] = (sel[0].get("value") or "") if sel else ""
    return p


def _decode_json_attr(raw: str):
    """Decode a (possibly double-HTML-encoded) jsonData attribute into a dict, or None."""
    import json as _json
    for candidate in (html.unescape(html.unescape(raw)), html.unescape(raw), raw):
        try:
            return _json.loads(candidate)
        except Exception:
            continue
    return None


def _radnumeric_clientstate(prev: str, num) -> str:
    """RadNumericTextBox posts its value via a `_ClientState` JSON, not the text input.

    Merge the numeric value into the control's existing ClientState so the server reads it.
    """
    import json as _json
    try:
        js = _json.loads(prev) if prev else {}
    except Exception:
        js = {}
    js.update({"enabled": True, "validationText": str(float(num)),
               "valueAsString": str(float(num)), "lastSetTextBoxValue": str(num)})
    return _json.dumps(js, separators=(",", ":"))


def _raddate_clientstate(prev: str, ad_date: str) -> str:
    """RadDateInput posts its value via a `_ClientState` JSON (value as 'yyyy-MM-dd-00-00-00')."""
    import json as _json
    try:
        js = _json.loads(prev) if prev else {}
    except Exception:
        js = {}
    iso = ad_date.replace("/", "-") + "-00-00-00"
    js.update({"enabled": True, "validationText": iso, "valueAsString": iso,
               "lastSetTextBoxValue": ad_date})
    return _json.dumps(js, separators=(",", ":"))


def _trigger_control(pay: dict, tree, press: str) -> None:
    """Fire `press` the way the browser would: submit inputs post name=value, everything else
    goes through __EVENTTARGET. Using the wrong one makes the server throw."""
    btn = next((e for e in tree.xpath("//input[@type='submit'] | //input[@type='button']")
                if (e.get("name") or "").split("$")[-1] == press
                or (e.get("id") or "").split("_")[-1] == press), None)
    if btn is not None and (btn.get("type") or "").lower() == "submit":
        pay[btn.get("name")] = btn.get("value") or ""
    else:
        pay["__EVENTTARGET"] = (press if press.startswith("ctl00")
                                else f"ctl00$ContentPlaceHolder1${press}")
        pay["__EVENTARGUMENT"] = ""


def _fill_control_value(payload: dict, tree, name: str, value) -> Optional[str]:
    """Write one control's value into `payload`. Returns an error string, or None on success.

    Handles the three ways UOF stores a value: <select> posts an option value while callers
    naturally supply the label; Telerik keeps the real value in a hidden `_ClientState`, and a
    date belongs to the *inner* `…_dateInput_ClientState` — the outer one is a different schema
    and makes the server throw.
    """
    sel = tree.xpath(f"//select[@name={name!r}]")
    if sel:
        opt = next((o for o in sel[0].xpath(".//option")
                    if (o.get("value") or "") == str(value)
                    or "".join(o.itertext()).strip() == str(value)), None)
        if opt is None:
            return f"值『{value}』不在選項中"
        payload[name] = opt.get("value") or ""
        return None
    radios = tree.xpath(f"//input[@type='radio'][@name={name!r}]")
    if radios:
        # a radio group posts one name=value; callers naturally supply the visible label
        def _lbl(el):
            lid = el.get("id") or ""
            for lab in (tree.xpath(f"//label[@for={lid!r}]") if lid else []):
                t = re.sub(r"\s+", " ", "".join(lab.itertext())).strip()
                if t:
                    return t
            return ""
        hit = next((e for e in radios
                    if (e.get("value") or "") == str(value) or _lbl(e) == str(value)), None)
        if hit is None:
            allowed = "／".join(f"{_lbl(e) or e.get('value')}" for e in radios)
            return f"值『{value}』不是有效選項，只能填：{allowed}"
        payload[name] = hit.get("value") or ""
        return None
    payload[name] = str(value)
    sv = str(value).strip()
    if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", sv):
        slash = sv.replace("-", "/")
        payload[name] = slash
        payload[name + "$dateInput"] = slash
        dcs = name.replace("$", "_") + "_dateInput_ClientState"
        payload[dcs] = _raddate_clientstate(payload.get(dcs, ""), slash)
        return None
    cs = name.replace("$", "_") + "_ClientState"
    if cs in payload:
        try:
            payload[cs] = _radnumeric_clientstate(payload.get(cs, ""), float(sv))
        except ValueError:
            pass
    return None


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


_DATAGRID_DIALOG_RE = r"['\"]([^'\"]*SetupDataGridFieldValue\.aspx\?[^'\"]*fieldId={code}[^'\"]*)['\"]"


def _datagrid_dialog_path(page_html: str, field_code: str) -> str:
    """Extract a dataGrid field's row-editor dialog URL from the FirstSite page HTML.

    The row-add button's `$uof.dialog.open2('…SetupDataGridFieldValue.aspx?…fieldId=<code>…')`
    carries the runtime scriptId/applicantGuid. Returns the (html-unescaped) path, or "".
    """
    m = re.search(_DATAGRID_DIALOG_RE.format(code=re.escape(field_code)), page_html)
    return html.unescape(m.group(1)) if m else ""


def _parse_datagrid_columns(dialog_html: str) -> list:
    """From a SetupDataGridFieldValue.aspx dialog page, return the row's columns in order.

    The dialog is a mini plugin form: each column is a versionFieldUC# UserControl holding one
    input. Returns [{index, label, input_name, input_type, client_state_name}] ordered by column.
    Column labels are HTML-entity-encoded in the page, so unescape before reading.
    """
    cols: list = []
    uc_indices = sorted({int(m.group(1)) for m in re.finditer(r"versionFieldUC(\d+)\b", dialog_html)})
    for i in uc_indices:
        names = re.findall(rf'name="([^"]*versionFieldUC{i}\$[^"]*)"', dialog_html)
        prim = ""
        for n in names:
            base = n.rsplit("$", 1)[-1]
            if "ClientState" in n or n.endswith("dateInput") or base.endswith(("_SD", "_AD")):
                continue
            prim = n
            break
        if not prim:
            continue
        base = prim.rsplit("$", 1)[-1]
        if "RadNumeric" in base:
            itype = "numeric"
        elif "RadDate" in base:
            itype = "date"
        elif "DropDownList" in base:
            itype = "dropDown"
        elif "rbList" in base:
            itype = "radio"
        elif "MultiLine" in base:
            itype = "multiLineText"
        else:
            itype = "text"
        # label: unescaped CJK text near this UC's marker (last non-noise token before the input)
        p = dialog_html.find(f"versionFieldUC{i}")
        region = re.sub(r"<script[\s\S]*?</script>", " ", dialog_html[max(0, p - 700):p + 120])
        region = html.unescape(re.sub(r"<[^>]+>", " ", region))
        label = ""
        for cand in reversed(re.findall(r"[一-鿿]{2,10}", region)):
            if cand not in ("資訊", "說明", "確定", "取消", "注意", "新增", "編輯"):
                label = cand
                break
        # RadNumeric/RadDate values live in a `_ClientState` hidden whose NAME uses underscores
        # (not $), derived from the control name — e.g. name "…$versionFieldUC2$RadNumericTextBox1"
        # → ClientState "…_versionFieldUC2_RadNumericTextBox1_ClientState". Setting only the text
        # input drops the value (this was the 數量 silent-drop bug), so resolve the real name here.
        cs_name = ""
        if itype in ("numeric", "date"):
            base = prim.replace("$", "_")
            cand = base + ("_dateInput_ClientState" if itype == "date" else "_ClientState")
            if cand in dialog_html:
                cs_name = cand
        cols.append({"index": i, "label": label, "input_name": prim,
                     "input_type": itype, "client_state_name": cs_name})
    return cols


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

class HttpSession:
    """httpx client with UOF cookie-session management."""

    def __init__(self) -> None:
        base_raw = os.environ.get("UOF_BASE_URL", "").rstrip("/")
        parsed = urlparse(base_raw)
        # _vpath is the optional virtual path prefix.
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
        self._form_id_version_map: Optional[dict] = None
        self._apply_form_list: Optional[dict] = None
        self._form_cache_at = 0.0
        self._login_lock = threading.Lock()

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

    def _form_cache_valid(self) -> bool:
        return (time.monotonic() - self._form_cache_at) < _FORM_CACHE_TTL_SECONDS

    def _do_login(self) -> None:
        """GET Login.aspx, parse VIEWSTATE, POST credentials."""
        account = os.environ.get("UOF_ACCOUNT", "")
        password = os.environ.get("UOF_PASSWORD", "")
        login_url = self._full_url(self._vpath + _LOGIN_PATH)
        _eprint("[ops.http_web] logging in")
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
        _eprint("[ops.http_web] login succeeded")

    def _relogin_if_still_expired(self) -> None:
        """Avoid duplicate concurrent logins: re-check session after taking the lock."""
        with self._login_lock:
            probe = self._client.get(self._full_url(self._vpath + _HOMEPAGE_PATH))
            if self._is_login_page(probe):
                self._do_login()

    def get(self, path: str) -> "_httpx.Response":
        """GET path (relative to base+vpath), auto-relogin on Login.aspx redirect."""
        url = self._full_url(self._vpath + path)
        resp = self._client.get(url)
        if self._is_login_page(resp):
            _eprint(f"[ops.http_web] 🔄 session expired, re-logging in")
            self._relogin_if_still_expired()
            resp = self._client.get(url)
        return resp

    def post(self, path: str, data: dict, *, retry_on_login: bool = True) -> "_httpx.Response":
        """POST to path (relative to base+vpath), auto-relogin on Login.aspx redirect."""
        url = self._full_url(self._vpath + path)
        resp = self._client.post(url, data=data)
        if self._is_login_page(resp):
            if not retry_on_login:
                return resp
            _eprint(f"[ops.http_web] 🔄 session expired on POST, re-logging in")
            self._relogin_if_still_expired()
            resp = self._client.post(url, data=data)
        return resp

    def _ensure_logged_in(self) -> None:
        """Check homepage; login if redirected."""
        url = self._full_url(self._vpath + _HOMEPAGE_PATH)
        resp = self._client.get(url)
        if self._is_login_page(resp):
            self._relogin_if_still_expired()

    # ── formId ↔ formVersionId mapping ──────────────────────────────

    def scrape_apply_form_list(self) -> dict:
        """List the forms this account can *initiate* — 電子簽核 » 表單申請 (ApplyFormList tree).

        Returns {"ok", "reason", "forms": [{form_id, form_version_id, form_name, category}]}.
        This is the authoritative "what can I start" set. (``scrape_form_list`` reads the
        FormQuery *query* dropdown, which is a broader, version-less set and must not be used
        for the applyable list.)
        """
        if self._apply_form_list is not None and self._form_cache_valid():
            return self._apply_form_list
        resp = self.get(_APPLY_FORM_LIST_PATH)
        if "Login.aspx" in str(resp.url):
            return {"ok": False, "reason": "redirected to Login.aspx", "forms": []}
        forms = _parse_apply_form_tree(resp.text)
        result = {"ok": True, "reason": "", "forms": forms}
        self._apply_form_list = result
        self._form_cache_at = time.monotonic()
        _eprint(f"[ops.http_web] ApplyFormList: {len(forms)} applyable forms")
        return result

    def get_form_id_version_mapping(self) -> dict:
        """{formId: versionId} (lowercase) for the applyable forms in the ApplyFormList tree."""
        if self._form_id_version_map is not None and self._form_cache_valid():
            return self._form_id_version_map
        mapping: dict = {}
        for f in self.scrape_apply_form_list().get("forms", []):
            if f["form_id"] and f["form_version_id"]:
                mapping[f["form_id"]] = f["form_version_id"]
        if not mapping:
            # safety net: the structured tree parse yielded nothing — fall back to the raw
            # value-pair regex over the same page so downstream apply_*/structure still work.
            resp = self.get(_APPLY_FORM_LIST_PATH)
            for form_id, version_id in re.findall(
                r'"value":"([0-9a-f]{8}-[0-9a-f-]{27})@([0-9a-f]{8}-[0-9a-f-]{27})"',
                resp.text,
                re.I,
            ):
                mapping[form_id.lower()] = version_id.lower()
        self._form_id_version_map = mapping
        _eprint(f"[ops.http_web] formId→versionId map: {len(mapping)} entries")
        return mapping

    def _resolve_form_ids(self, form_id_or_version: str) -> tuple:
        """Accept either a formId or a formVersionId; return (formId, versionId) or ("", "").

        Callers (apply_*) may be handed either identifier — an agent typically has the formId
        from get_form_list/get_form_structure_by_id, not the version. `get_form_id_version_mapping`
        is {formId: versionId}, so look the key up as a formId first, then as a versionId.
        """
        mapping = self.get_form_id_version_mapping()
        key = (form_id_or_version or "").lower()
        if key in mapping:                       # given a formId
            return key, mapping[key]
        for fid, vid in mapping.items():         # given a versionId
            if vid == key:
                return fid, vid
        return "", ""

    def _lookup_created_form(self, form_number: str) -> tuple:
        """After 成單, resolve (task_id, real_form_name) by listing recent forms and matching the number.

        `search_forms` keyword (txtKeywordByFormQuery) does NOT match the auto form number, so a
        keyword search returns nothing — list recent forms (no keyword) instead; the just-created
        form is the newest row. Also returns the form's real name (registry's static name may cover
        several formIds and be wrong for the specific one).
        """
        if not form_number:
            return "", ""
        try:
            for rr in self.search_forms(max_results=50).get("rows", []):
                if rr.get("form_number") == form_number:
                    return rr.get("task_id", ""), rr.get("form_name", "")
        except Exception as ex:
            _eprint(f"[ops.http_web] ⚠️ lookup created form failed: {type(ex).__name__}: {ex}")
        return "", ""

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

        resp2 = self.post(_FORM_QUERY_PATH, payload)
        if "Login.aspx" in str(resp2.url):
            return {"ok": False, "reason": "redirected to Login.aspx after search", "rows": []}
        if "ErrorReport" in str(resp2.url):
            return {"ok": False, "reason": "查詢被導向 ErrorReport（伺服器端查詢例外）", "rows": []}

        all_rows = _parse_rows(self._parse(resp2))
        seen = {r["task_id"] for r in all_rows if r["task_id"]}
        kw = (keyword or "").strip().lower()

        # 關鍵字模式要蒐齊全集才能過濾；無關鍵字也要翻到湊滿 max_results。
        if kw or len(all_rows) < max_results:
            cond = {k: v for k, v in payload.items()
                    if k.startswith(date_prefix) and not k.endswith("wibQuery")}
            grid = date_prefix + "grdQuery"
            cur = resp2
            for page in range(2, 40):   # backstop：至多 ~40 頁
                if not kw and len(all_rows) >= max_results:
                    break
                h = _parse_hidden_fields(self._parse(cur))
                h.update(cond)                         # pager postback 必須重帶條件欄位，否則伺服器用預設重繫結
                h.pop(date_prefix + "wibQuery", None)  # 翻頁不按查詢鈕
                h["__EVENTTARGET"] = grid
                h["__EVENTARGUMENT"] = f"Page${page}"
                cur = self.post(_FORM_QUERY_PATH, h)
                if "Login.aspx" in str(cur.url) or "ErrorReport" in str(cur.url):
                    break
                new = [r for r in _parse_rows(self._parse(cur))
                       if r["task_id"] and r["task_id"] not in seen]
                if not new:
                    break
                seen.update(r["task_id"] for r in new)
                all_rows.extend(new)

        if kw:
            matched = [r for r in all_rows if kw in (
                f"{r['form_number']} {r['form_name']} {r['subject']} {r['applicant']}").lower()]
        else:
            matched = all_rows
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
        resp2 = self.post(path_only, payload)
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
        """Picker candidates for one dialog field of a form. {ok, reason, field, rows}."""
        st = self.dialog_structure(form_version_id, field_code)
        if not st.get("ok"):
            return {"ok": False, "reason": st.get("reason", ""), "field": "", "rows": []}
        if not st["fields"]:
            return {"ok": False, "reason": f"找不到對話框欄位 {field_code}", "field": "", "rows": []}
        f = st["fields"][0]
        # dialog_structure only keeps the basename; re-resolve the full url here
        fid, vid = self._resolve_form_ids(form_version_id)
        resp = self.get(f"{_ADD_FORM_SCRIPT_PATH}?formId={fid}&formVersionId={vid}&mode=apply")
        tree = self._parse(self.get(self.strip_vpath(str(resp.url))))
        full = ""
        for fb in _parse_field_blocks(tree, include_dialog_companions=True):
            if (fb.get("code") or "").upper() == field_code.upper():
                full = (fb.get("dialog_url") or "").replace("&amp;", "&")
                break
        if not full:
            return {"ok": False, "reason": f"欄位 {field_code} 取不到查詢視窗位址", "field": f["label"], "rows": []}
        return {"ok": True, "reason": "", "field": f"{f['label']}({f['code']})",
                "rows": self.list_dialog_options(full, keyword, limit)}

    def _dialog_opener_name(self, tree, dialog_url: str) -> str:
        """Name of the parent control that opens `dialog_url` (its `btnAdd`).

        After the dialog confirms a row, the browser's callback returns true and the framework
        posts the parent back through this control; the server then appends the pending row to
        the grid. Skipping that postback leaves the row in limbo — the dialog reports success and
        the form still comes out empty.
        """
        base = dialog_url.split("/")[-1].split("?")[0]
        for el in tree.xpath("//input[@onclick] | //a[@onclick]"):
            if base in html.unescape(el.get("onclick") or ""):
                # submit inputs fire on their name=value pair; anything else via __EVENTTARGET
                return (el.get("name") or "",
                        el.get("value") or "",
                        (el.get("type") or "").lower() == "submit")
        return ("", "", False)

    def add_plugin_dialog_rows(self, dialog_full_url: str, rows: list) -> dict:
        """Persist rows through a plugin row-editor dialog (PRItemDialog, ExpEmpItemDialog, …).

        Same confirm protocol as `add_datagrid_rows` (`__EVENTTARGET=MasterPageRadButton1` +
        `FASTReturnValue=[DefaultNullValue]`), but the row's columns are plain named controls
        rather than the `versionFieldUC` template, so rows are keyed by control name.

        **The url must come from the caller's own apply-page render** — it carries that session's
        GridDataID, and a row written under any other session is not visible to this one's 送出.

        Returns {ok, added, controls, errors}.
        """
        import json as _json
        path = self.strip_vpath(dialog_full_url if dialog_full_url.startswith("/")
                                else "/" + dialog_full_url)

        _trigger = _trigger_control
        added = 0
        errors: list = []
        notes: list = []      # non-fatal: reported, but do not fail the row
        controls: list = []
        returned: list = []
        for ri, row in enumerate(list(rows or [])):
            r = self.get(path)
            if "Login.aspx" in str(r.url):
                return {"ok": False, "added": added, "controls": controls,
                        "errors": errors + ["redirected to Login.aspx"]}
            tree = self._parse(r)
            if not controls:
                controls = [c["name"] for c in _parse_dialog_fields(r.text)]
                if not controls:
                    return {"ok": False, "added": added, "controls": controls,
                            "errors": errors + ["對話框欄位解析失敗，無法驗證列內容"]}
            short = {n.split("$")[-1]: n for n in controls}
            if not isinstance(row, dict):
                errors.append(f"第 {ri + 1} 列需為 dict（欄位名稱→值），收到 {type(row).__name__}")
                continue
            # `_lookups` replays picker selections the same way the browser does: hand the picked
            # entity back through the control that opened it, and the server fills the columns it
            # owns. Required for read-only columns — ASP.NET ignores posted values for those, so
            # 料號 stays blank no matter what is sent. Which picker feeds which button is form
            # knowledge and comes from the caller.
            fields = dict(row)
            fields.pop("_press_after", None)
            # `_fill_before` values ride along with every lookup post. Ordering is the point: a
            # control that drives another (分類 drives 費用項目) must be submitted *with* the
            # lookup, so ASP.NET raises its changed-event first and the button click fills the
            # dependent column afterwards. Sent later instead, the change handler fires last and
            # clears what the lookup just filled — silently.
            prefill = fields.pop("_fill_before", None) or {}
            bad_prefill = [k for k in prefill if k not in short and k not in controls]
            if bad_prefill:
                errors.append(f"第 {ri + 1} 列的 _fill_before 控制項名稱不存在：{bad_prefill}")
                continue
            for lk in (fields.pop("_lookups", None) or []):
                press = (lk or {}).get("press") or ""
                picked = (lk or {}).get("row")
                if not press or picked is None:
                    errors.append(f"第 {ri + 1} 列的 _lookups 需要 press 與 row 兩個欄位")
                    break
                lp = _form_state_payload(tree)
                for pk, pv in prefill.items():
                    perr = _fill_control_value(lp, tree, short.get(pk, pk), pv)
                    if perr:
                        notes.append(f"第 {ri + 1} 列：_fill_before 的 {pk} {perr}")
                lp["DialogReturnValue"] = (picked if isinstance(picked, str)
                                           else _json.dumps(picked, ensure_ascii=False))
                _trigger(lp, tree, press)
                rl = self.post(path, lp, retry_on_login=False)
                tree = self._parse(rl)
            payload = _form_state_payload(tree)
            unknown = [k for k in fields if k not in short and k not in controls]
            if unknown:
                errors.append(f"第 {ri + 1} 列的控制項名稱不存在：{unknown}；"
                              f"有效名稱：{'／'.join(sorted(short)[:20])}")
                continue
            if not any(str(v).strip() for v in fields.values()) and not row.get("_lookups"):
                errors.append(f"第 {ri + 1} 列沒有任何值，未送出")
                continue
            bad_field = False
            for k, v in fields.items():
                err = _fill_control_value(payload, tree, short.get(k, k), v)
                if err:
                    # skip the whole row: confirming it would file a row silently missing this
                    # column, which is the failure this layer exists to prevent
                    errors.append(f"第 {ri + 1} 列：{k} 的{err}，該列未送出")
                    bad_field = True
                    break
            if bad_field:
                continue
            # `_press_after` runs buttons that derive values from what was just typed (計算 etc.),
            # so it has to happen after the fill, unlike `_lookups`.
            for press in (row.get("_press_after") or []):
                cp = dict(payload)
                _trigger(cp, tree, press)
                rc = self.post(path, cp, retry_on_login=False)
                tree = self._parse(rc)
                payload = _form_state_payload(tree)
            payload["__EVENTTARGET"] = "ctl00$MasterPageRadButton1"
            payload["__EVENTARGUMENT"] = ""
            payload["FASTReturnValue"] = "[DefaultNullValue]"
            resp = self.post(path, payload, retry_on_login=False)
            if "Login.aspx" in str(resp.url):
                errors.append(f"第 {ri + 1} 列：session 已過期，未重送列確認，請重試")
                continue
            if "ErrorReport" in str(resp.url):
                errors.append(f"第 {ri + 1} 列：確定時被導向 ErrorReport（可能必填未齊或值不合法）")
                continue
            # Proof the dialog accepted the row: it must hand back a return value or ask the
            # parent to post back. A 200 alone means nothing — the page re-renders unchanged when
            # the confirm is a no-op, which previously counted as success and produced forms whose
            # detail grid was empty. Never count a row without evidence it persisted.
            trv = _temp_return_value(resp.text)
            if trv is None:
                errors.append(
                    f"第 {ri + 1} 列：確定後對話框未回傳列資料，該列未被接受"
                    "（常見原因：查找型欄位只填了代碼、沒帶內部 Id）")
                continue
            returned.append(trv if isinstance(trv, str) else _json.dumps(trv, ensure_ascii=False))
            added += 1
        return {"ok": added == len(list(rows or [])) and not errors,
                "added": added, "controls": controls, "errors": errors,
                "notes": notes, "returned": returned}

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
        for fb in _parse_field_blocks(tree, include_dialog_companions=True):
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
        r2 = self.post(path, payload)
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
        resp2 = self.post(path_only, payload)
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

    def add_datagrid_rows(self, dialog_full_path: str, rows: list) -> dict:
        """Persist general-dataGrid rows over httpx via the row-editor dialog.

        Each row's dialog 確定 is a **synchronous** postback (`__EVENTTARGET=MasterPageRadButton1`
        + `FASTReturnValue=[DefaultNullValue]`) that writes the row into a server-side store keyed
        by this session's scriptId; the parent form's later 儲存/送出 reads the rows from that store
        (same session cookie). The parent's async `AddDgRow` postback is cosmetic and is NOT
        replayed.

        Returns {ok, added, columns, errors}.
        """
        path = self.strip_vpath(dialog_full_path)
        added = 0
        errors: list = []
        columns: list = []
        row_list = list(rows or [])
        for ri, row in enumerate(row_list):
            r = self.get(path)
            if "Login.aspx" in str(r.url):
                return {"ok": False, "added": added, "columns": columns,
                        "errors": errors + ["redirected to Login.aspx"]}
            if not columns:
                columns = _parse_datagrid_columns(r.text)
                if not columns:
                    return {"ok": False, "added": added, "columns": columns,
                            "errors": errors + ["對話框欄位解析失敗（找不到 versionFieldUC 欄位）"]}
            payload = _form_state_payload(self._parse(r))
            mapped, unmatched = _map_row_to_columns(row, columns)
            col_names = "／".join(c["label"] or f"欄{c['index'] + 1}" for c in columns)
            # fail loudly BEFORE posting: unmatched keys or an all-empty row would create a
            # blank detail row on the server (it accepts blanks) — the incomplete-form trap.
            if unmatched:
                errors.append(
                    f"第 {ri + 1} 列的欄名對不上：{unmatched}；此明細每列的有效欄名為：{col_names}，未送出該列"
                )
                continue
            if not any(str(v).strip() for v in mapped.values()):
                errors.append(f"第 {ri + 1} 列沒有任何欄位值（有效欄名：{col_names}），未送出該列")
                continue
            for col in columns:
                v = mapped.get(col["index"])
                if v is None or v == "":
                    continue
                iname = col["input_name"]
                if col["input_type"] == "numeric":
                    payload[iname] = str(v)
                    if col["client_state_name"]:
                        payload[col["client_state_name"]] = _radnumeric_clientstate(
                            payload.get(col["client_state_name"], ""), v)
                elif col["input_type"] == "date":
                    vs = str(v).replace("-", "/")
                    payload[iname] = vs
                    payload[iname + "$dateInput"] = vs
                    if col["client_state_name"]:
                        payload[col["client_state_name"]] = _raddate_clientstate(
                            payload.get(col["client_state_name"], ""), str(v))
                else:
                    payload[iname] = str(v)
            payload["FASTReturnValue"] = "[DefaultNullValue]"
            payload["__EVENTTARGET"] = "ctl00$MasterPageRadButton1"
            payload["__EVENTARGUMENT"] = ""
            rr = self.post(path, payload, retry_on_login=False)
            if "Login.aspx" in str(rr.url):
                errors.append(f"第 {ri + 1} 列：session 已過期，未重送列確認，請重試")
                continue
            if "ErrorReport" in str(rr.url):
                errors.append(f"第 {ri + 1} 列：對話框確定後被導向 ErrorReport")
                continue
            if "NeedPostBack" not in rr.text:
                reds = sorted(set(re.findall(r"必填|請選擇|不可為空|欄位不得|格式", rr.text)))
                errors.append(f"第 {ri + 1} 列：對話框未回 NeedPostBack（{reds or '未知原因'}）")
                continue
            added += 1
        return {"ok": added == len(row_list) and added > 0, "added": added,
                "columns": columns, "errors": errors}

    # ── apply_form_web ────────────────────────────────────────────────

    def apply_form_web(
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
        field_blocks = _parse_field_blocks(tree2, include_dialog_companions=True)
        if not field_blocks:
            # Validation depends on field_blocks. Refuse an unvalidated header-only submission
            # when neither parser can identify fields.
            return {"ok": False, "task_id": "", "form_number": "", "filled": {},
                    "errors": ["此表單目前無法解析出任何欄位（httpx 解析器 bug，非表單本身限制），"
                               "無法驗證內容完整性，已擋下——請改到 UOF 網頁操作，並回報開發面追查。"],
                    "reason": "表單欄位解析失敗，為避免建立無法驗證的空殼單，未送出"}
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

        errors: list = []      # soft: extra/unknown fields, skipped — form still submits
        blocking: list = []    # hard: bad option value / missing required — refuse to submit
        filled: dict = {}
        bad_option_codes: set = set()  # fields flagged as invalid-option — don't also报「未提供」
        datagrid_pending: list = []  # (code, fb, rows) — filled via dialog after the main loop

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
            # Any value the caller supplied that we cannot write is silent data loss: the form
            # would submit looking successful while missing exactly what was asked for. Block.
            if fb is None:
                blocking.append(f"欄位 {code} 在表單中找不到——值『{value}』無法寫入")
                continue

            if fb.get("disabled"):
                errors.append(f"欄位 {code}（{fb.get('label') or ''}）在此表單為停用狀態、起單時不可填，已略過")
                continue

            itype = fb.get("input_type", "text")
            iname = fb.get("input_name") or ""

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

            if not iname:
                blocking.append(f"欄位「{fb.get('label') or code}」找不到可寫入的控制項（解析器缺口），值『{value}』無法寫入")
                continue

            if itype == "datePicker":
                # value can be yyyy-mm-dd or yyyy/mm/dd. RadDatePicker reads its value from a
                # `_ClientState` hidden (underscore name), NOT the text input — filling only the
                # text input silently drops the date (the datePicker silent-drop bug), so write
                # the ClientState too.
                v_slash = str(value).replace("-", "/")
                payload[iname] = v_slash
                payload[iname + "$dateInput"] = v_slash
                cs = iname.replace("$", "_") + "_dateInput_ClientState"
                payload[cs] = _raddate_clientstate(payload.get(cs, ""), v_slash)
                _mark_filled(filled, code, fb, v_slash)

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
                            _mark_filled(filled, code, fb, opt_val)
                            matched = True
                            break
                    if not matched:
                        # label contains match
                        for opt in sel_el.xpath(".//option"):
                            opt_val = opt.get("value") or ""
                            opt_txt = "".join(opt.itertext()).strip()
                            if str_val.lower() in opt_txt.lower():
                                payload[iname] = opt_val
                                _mark_filled(filled, code, fb, opt_val)
                                matched = True
                                break
                if not matched:
                    opts = fb.get("options") or []
                    hint = f"，只能填：{'／'.join(o['value'] for o in opts)}" if opts else ""
                    blocking.append(f"欄位「{fb.get('label') or code}」的值『{value}』不是有效下拉選項{hint}")
                    bad_option_codes.add(fb.get("code") or "")

            elif itype == "radio":
                opts = fb.get("options") or []
                # post the option's *value*, not whatever the caller typed. These happen to be
                # identical on today's forms, so echoing the input looked correct — it would post
                # the label verbatim the moment a form used coded values.
                hit = next((o for o in opts
                            if str(value) in (o["value"], o["label"])), None)
                if opts and hit is None:
                    bad_option_codes.add(fb.get("code") or "")
                    blocking.append(
                        f"欄位「{fb.get('label') or code}」的值『{value}』不是有效選項，"
                        f"只能填：{'／'.join(o['label'] for o in opts)}"
                    )
                else:
                    payload[iname] = hit["value"] if hit else str(value)
                    _mark_filled(filled, code, fb, payload[iname])

            elif itype == "checkbox":
                # An ASP.NET CheckBox posts its name only when ticked; the value is irrelevant.
                # Assigning str(value) therefore *ticked* the box whatever was passed — including
                # 「否」/False — and there was no way to express unticked at all.
                opts = fb.get("options") or []
                on = str(value).strip().lower() not in (
                    "", "false", "0", "no", "n", "否", "未", "unchecked")
                if on:
                    payload[iname] = (opts[0]["value"] if opts else "on")
                else:
                    payload.pop(iname, None)
                _mark_filled(filled, code, fb, "已勾選" if on else "未勾選")

            elif itype == "dialog" or isinstance(value, (dict, list, tuple)):
                # A structured value addresses a plugin block even when the block was inferred as
                # plain text because it exposes no dialog button.
                dialog_url = fb.get("dialog_url") or ""
                # A plugin block can carry its own controls and one or more row grids. A dict
                # addresses the block itself:
                # `_lookups` / `_press_after` / `_rows` are reserved, every other key is a control
                # name inside the block. Which control means what is form knowledge — not MCP's.
                if isinstance(value, dict):
                    inline = dict(value)
                    rows_v = inline.pop("_rows", None)
                    label_ = fb.get("label") or code
                    uc_m = re.search(r"(versionFieldUC\d+)", iname or "")
                    ucp = uc_m.group(1) if uc_m else ""
                    names = {n.split("$")[-1]: n for n in (
                        e.get("name") for e in
                        tree2.xpath("//input[@name]|//select[@name]|//textarea[@name]"))
                        if n and (not ucp or ucp in n)}
                    presses = inline.pop("_press_after", None) or []
                    # Controls consumed by a lookup must be posted with the button press, not after it.
                    prefill_i = inline.pop("_fill_before", None) or {}
                    bad_inline = False
                    for pk, pv in prefill_i.items():
                        if pk not in names:
                            blocking.append(f"欄位「{label_}」區塊內無控制項 {pk}（_fill_before）")
                            bad_inline = True
                    for lk in ([] if bad_inline else (inline.pop("_lookups", None) or [])):
                        press, picked = (lk or {}).get("press"), (lk or {}).get("row")
                        if not press or picked is None:
                            blocking.append(f"欄位「{label_}」的 _lookups 每項需要 press 與 row")
                            bad_inline = True
                            break
                        # same DialogReturnValue protocol as the row editor, one layer up: the
                        # server owns the read-only columns, so posting their text does nothing
                        pp = dict(payload)
                        for pk, pv in prefill_i.items():
                            perr = _fill_control_value(pp, tree2, names[pk], pv)
                            if perr:
                                blocking.append(f"欄位「{label_}」的 _fill_before {pk} {perr}")
                                bad_inline = True
                        pp["DialogReturnValue"] = (picked if isinstance(picked, str)
                                                   else json.dumps(picked, ensure_ascii=False))
                        _trigger_control(pp, tree2, press)
                        before = {k: v for k, v in payload.items() if not ucp or ucp in k}
                        lookup_resp = self.post(first_site_path, pp, retry_on_login=False)
                        if "Login.aspx" in str(lookup_resp.url):
                            blocking.append(f"欄位「{label_}」lookup 時 session 已過期，請重試")
                            bad_inline = True
                            break
                        tree2 = self._parse(lookup_resp)
                        # Take the whole rendered state back, not just the hiddens: the columns
                        # the server just filled are ordinary inputs, and keeping the pre-lookup
                        # payload would post the empty values straight back over them at 儲存.
                        payload.update(_form_state_payload(tree2))
                        # A row the server cannot resolve is accepted without complaint: the id
                        # lands but the display column stays blank, and the form submits looking
                        # filled. Require the block to have actually changed.
                        after = {k: v for k, v in _form_state_payload(tree2).items()
                                 if not ucp or ucp in k}
                        if before and after == before:
                            blocking.append(
                                f"欄位「{label_}」按下 {press} 後區塊沒有任何變化——"
                                f"伺服器無法解析所選項目，請換一筆")
                            bad_inline = True
                            break
                    unknown = [k for k in inline if k not in names]
                    if unknown and not bad_inline:
                        blocking.append(f"欄位「{label_}」區塊內無這些控制項：{unknown}；"
                                        f"有效名稱：{'／'.join(sorted(names)[:20])}")
                        bad_inline = True
                    if not bad_inline:
                        for k, v in inline.items():
                            err = _fill_control_value(payload, tree2, names[k], v)
                            if err:
                                blocking.append(f"欄位「{label_}」的 {k} {err}")
                                bad_inline = True
                        for press in presses:
                            pp = dict(payload)
                            _trigger_control(pp, tree2, press)
                            press_resp = self.post(first_site_path, pp, retry_on_login=False)
                            if "Login.aspx" in str(press_resp.url):
                                blocking.append(f"欄位「{label_}」按下 {press} 時 session 已過期，請重試")
                                bad_inline = True
                                break
                            tree2 = self._parse(press_resp)
                            payload.update(_form_state_payload(tree2))
                    if bad_inline:
                        continue
                    _mark_filled(filled, code, fb, "、".join(f"{k}={v}" for k, v in inline.items()) or "已選取")
                    if rows_v is None:
                        continue
                    value = rows_v
                if isinstance(value, (list, tuple, dict)):
                    # A block can hold several row editors. A list addresses the first one; a dict
                    # keyed by the opener button name addresses them individually.
                    batches = ([(k, v) for k, v in value.items()] if isinstance(value, dict)
                               else [("", list(value))])
                    m_uc = re.search(r"(versionFieldUC\d+)", iname or "")
                    openers = []
                    for el in tree2.xpath("//input[@onclick]"):
                        nm = el.get("name") or ""
                        if m_uc and m_uc.group(1) not in nm:
                            continue
                        oc = html.unescape(el.get("onclick") or "")
                        mu = re.search(r"['\"]([^'\"\s]*Dialog\.aspx[^'\"\s]*)['\"]", oc)
                        # Row-editor dialogs carry GridDataID while picker dialogs do not.
                        if mu and "GridDataID" in mu.group(1):
                            # keep it as path+query: callers below prepend the vpath themselves
                            u = urlparse(urljoin(str(resp2.url), mu.group(1)))
                            openers.append((nm, u.path + (f"?{u.query}" if u.query else "")))
                    if not openers:
                        blocking.append(f"明細「{fb.get('label') or code}」找不到列編輯對話框，無法填列")
                        continue
                    bad_batch = False
                    added_desc = []
                    for hint, rows_list in batches:
                        if hint:
                            pick = next((o for o in openers
                                         if o[0].split("$")[-1] == hint), None)
                            if pick is None:
                                blocking.append(
                                    f"明細「{fb.get('label') or code}」沒有名為 {hint} 的列編輯按鈕；"
                                    f"可用：{'／'.join(o[0].split('$')[-1] for o in openers)}")
                                bad_batch = True
                                break
                        else:
                            pick = openers[0]
                        opener, row_dialog = pick
                        # name the row editor once a block can have more than one, or a failure
                        # reads as "the detail failed" with no way to tell which grid
                        tag = f"{code}/{opener.split('$')[-1]}" if len(batches) > 1 else code
                        res = self.add_plugin_dialog_rows(row_dialog, list(rows_list))
                        for e in res.get("errors", []) + res.get("notes", []):
                            errors.append(f"明細 {tag}: {e}")
                        if not res.get("ok"):
                            blocking.append(
                                f"明細「{fb.get('label') or code}」的 {opener.split('$')[-1]}："
                                f"{len(rows_list)} 列僅有 {res.get('added', 0)} 列被對話框接受，未完整")
                            bad_batch = True
                            break
                        # Replay what the browser does after each dialog confirm: put the returned
                        # row JSON into a `DialogReturnValue` field and post the parent back through
                        # the control that opened the dialog. Without DialogReturnValue the postback
                        # carries no row and the grid stays empty however many times it fires.
                        # (Mechanism read off $uof.dialog.open2's add_close handler.)
                        for row_json in res.get("returned", []):
                            pp = dict(payload)
                            pp["DialogReturnValue"] = row_json
                            pp["__EVENTTARGET"] = opener
                            pp["__EVENTARGUMENT"] = ""
                            rp = self.post(first_site_path, pp, retry_on_login=False)
                            if "Login.aspx" in str(rp.url):
                                blocking.append("session 已過期，未重送明細回填 postback，請重試")
                                bad_batch = True
                                break
                            tree2 = self._parse(rp)
                            payload.update(_parse_hidden_fields(tree2))
                        added_desc.append(f"{res['added']} 列")
                    if bad_batch:
                        continue
                    grid_rows = len([
                        tr for g in tree2.xpath("//table[contains(@id,'Grid')]")
                        for tr in g.xpath(".//tr") if tr.xpath("./td")
                        and "沒有資料" not in "".join(tr.itertext())
                    ])
                    if not grid_rows:
                        blocking.append(
                            f"明細「{fb.get('label') or code}」回填後表格仍為空——列未真正寫入單據")
                        continue
                    _mark_filled(filled, code, fb, "＋".join(added_desc))
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
                # text, textarea, numeric, unknown
                payload[iname] = str(value)
                _mark_filled(filled, code, fb, str(value))
                if itype == "numeric":
                    # a Telerik numeric keeps its real value in `_ClientState`; writing only the
                    # visible input is the bug that made 數量 come back as 0 in the row editors.
                    # No header field on today's forms is numeric, so this is untested here.
                    cs = iname.replace("$", "_") + "_ClientState"
                    try:
                        payload[cs] = _radnumeric_clientstate(payload.get(cs, ""), float(value))
                    except (TypeError, ValueError):
                        pass

        # 4b. Fill general-dataGrid detail rows via their row-editor dialog. Each row's dialog
        #     確定 persists to the server store keyed by this session's scriptId; the 儲存/送出
        #     below (same session) then reads them. Done before validation so required grids count.
        for code, fb, rows in datagrid_pending:
            # Classic templates need grid_field_id because their public code is only the label.
            dlg_path = _datagrid_dialog_path(resp2.text, fb.get("grid_field_id") or fb.get("code") or code)
            if not dlg_path:
                blocking.append(f"明細「{fb.get('label') or code}」找不到列編輯對話框位址，無法填列")
                continue
            res = self.add_datagrid_rows(dlg_path, rows)
            if res.get("added"):
                _mark_filled(filled, code, fb, f"{res['added']} 列")
            for e in res.get("errors", []):
                errors.append(f"明細 {code}: {e}")
            if not res.get("ok"):
                blocking.append(
                    f"明細「{fb.get('label') or code}」{len(rows)} 列僅成功加入 {res.get('added', 0)} 列，未完整"
                )

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
        for fb in field_blocks:
            code_ = fb.get("code") or ""
            # skip if already filled, or already flagged as an invalid-option value (avoid the
            # contradictory「值不合法」＋「未提供」pair on the same field)
            if not fb.get("required") or code_ in filled or code_ in bad_option_codes:
                continue
            label = fb.get("label") or fb.get("code") or "(未命名欄位)"
            itype = fb.get("input_type", "")
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
                       reason="" if task_id else "已成單但未取得 TaskId（可用 query_forms 查）")

    # ── CDS picker helpers ─────────────────────────────────────────────
    def resolve_picker_entity(self, dialog_path: str, search_kw: str,
                              code_field: str, code: str) -> Optional[dict]:
        """Search a CDS picker dialog by keyword; return the result row's full entity JSON.

        UOF CDS pickers (SupplierDialog / ItemDialog) render each result row with a
        `jsonData` attribute holding the entity incl. its numeric `Id`. That JSON is
        exactly what the parent form expects back as `DialogReturnValue` — so we read it
        directly over httpx (no browser dialog needed). Returns the row matching `code`
        on `code_field`, else the first row (when `code` is empty), else None.
        """
        import json as _json
        tree = self._parse(self.get(dialog_path))
        p = _form_state_payload(tree)
        for kf in tree.xpath("//input[@type='text'][@name]"):
            if "key" in (kf.get("name") or "").lower():
                p[kf.get("name")] = search_kw
        for bk in tree.xpath("//input[@type='submit'][@name]"):
            if "搜尋" in (bk.get("value") or "") or (bk.get("name") or "").endswith("btnKey"):
                p[bk.get("name")] = bk.get("value") or "搜尋"
                break
        p["__EVENTTARGET"] = ""
        p["__EVENTARGUMENT"] = ""
        t2 = _html_fromstring(self.post(dialog_path, p).text)
        fallback = None
        for el in t2.xpath("//*[@jsonData] | //*[@jsondata]"):
            jd = _decode_json_attr(el.get("jsonData") or el.get("jsondata") or "")
            if jd is None:
                continue
            if fallback is None:
                fallback = jd
            if code and str(jd.get(code_field) or "").lower() == str(code).lower():
                return jd
        return None if code else fallback

    # Form-specific application recipes intentionally live outside this public MCP package.
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

    def dialog_structure(self, form_version_id: str, field_code: str = "") -> dict:
        """Inner field structure of a form's dialog-backed fields.

        Opens the apply page, finds each dialog field's own page and parses it as a mini-form.
        `field_code` empty ⇒ every dialog field on the form.

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
        out = []
        for fb in _parse_field_blocks(tree, include_dialog_companions=True):
            if fb.get("input_type") != "dialog":
                continue
            code = fb.get("code") or ""
            if want and code.upper() != want:
                continue
            url = (fb.get("dialog_url") or "").replace("&amp;", "&")
            entry = {"code": code, "label": fb.get("label") or "", "dialog": url.split("/")[-1][:60],
                     "inner": [], "inline": [], "row_editor": "", "note": ""}
            # Plugin forms render their real controls inline inside the field's own
            # versionFieldUC block; _parse_field_blocks only reports the block itself, so those
            # controls (付款人 / 立帳日期 / 金額 …) are invisible without this.
            m_uc = re.search(r"(versionFieldUC\d+)", fb.get("input_name") or "")
            if m_uc:
                entry["inline"] = _parse_inline_controls(tree, m_uc.group(1))
            # A row-editor dialog on the page (ExpEmpItemDialog…) means this field owns detail rows.
            for u in set(re.findall(r"['\"]([^'\"]*ItemDialog\.aspx[^'\"]*)['\"]", first.text)):
                entry["row_editor"] = u.replace("&amp;", "&")
                break
            if not url:
                entry["note"] = "取不到對話框位址"
            else:
                try:
                    d = self.get(self.strip_vpath(url if url.startswith("/") else "/" + url))
                    inner = _parse_datagrid_columns(d.text) or _parse_dialog_fields(d.text)
                    entry["inner"] = inner
                    if not inner:
                        entry["note"] = "對話框內容無法解析（版型未知）"
                except Exception as ex:
                    entry["note"] = f"讀取對話框失敗：{type(ex).__name__}: {ex}"
            out.append(entry)
        return {"ok": True, "reason": "", "fields": out}

    # ── 待簽清單 (Homepage 待簽表單 widget) ────────────────────────────
    _SIGN_LINK_RE = (r"SignNodeForm\.aspx\?TASK_ID=([0-9a-f-]{36})"
                     r"&SITE_ID=([0-9a-f-]{36})&NODE_SEQ=(\d+)")

    def pending_sign_list(self, max_pages: int = 20) -> dict:
        """Every form awaiting THIS identity's signature, across all widget pages.

        No pending-list API exists, but the Homepage 待簽表單 widget is a DataGrid whose rows
        each carry TASK_ID/SITE_ID/NODE_SEQ — enough to both list and sign. It pages 10 at a
        time, so follow `Page$N` postbacks until no new task ids appear.

        Returns {ok, reason, rows, total}; row text is left as UOF renders it.
        """
        resp = self.get(_HOMEPAGE_PATH)
        if "Login.aspx" in str(resp.url):
            return {"ok": False, "reason": "redirected to Login.aspx", "rows": [], "total": 0}
        tree = self._parse(resp)
        raw = html.unescape(resp.text)

        # widget control name for paging, e.g. ctl00$...$RadDock<guid>$C$widget$DGFormList
        m_ctl = re.search(r"__doPostBack\('([^']*\$DGFormList)','Page\$", raw)
        ctl = m_ctl.group(1) if m_ctl else ""
        m_total = re.search(r"共\s*(\d+)\s*筆", raw)
        total = int(m_total.group(1)) if m_total else 0

        def _rows_of(tree, raw_text) -> list:
            grids = tree.xpath("//table[contains(@id,'DGFormList')]")
            if not grids:
                return []
            out = []
            for tr in grids[0].xpath("./tr | ./tbody/tr"):
                row_raw = html.unescape(_etree.tostring(tr, encoding="unicode"))
                m = re.search(self._SIGN_LINK_RE, row_raw)
                if not m:
                    continue  # pager / spacer row
                out.append({
                    "task_id": m.group(1), "site_id": m.group(2), "node_seq": m.group(3),
                    "text": re.sub(r"\s+", " ", "".join(tr.itertext())).strip(),
                })
            return out

        rows = _rows_of(tree, raw)
        seen = {r["task_id"] for r in rows}
        page = 2
        while ctl and len(rows) < total and page <= max_pages:
            payload = _form_state_payload(tree)
            payload["__EVENTTARGET"] = ctl
            payload["__EVENTARGUMENT"] = f"Page${page}"
            r2 = self.post(_HOMEPAGE_PATH, payload)
            if "Login.aspx" in str(r2.url):
                break
            tree = self._parse(r2)
            fresh = [r for r in _rows_of(tree, html.unescape(r2.text)) if r["task_id"] not in seen]
            if not fresh:
                break  # paging stopped advancing; stop rather than spin
            rows += fresh
            seen |= {r["task_id"] for r in fresh}
            page += 1
        return {"ok": True, "reason": "", "rows": rows, "total": total or len(rows)}

    # ── sign a pending task (自由流程 web 簽核，純 httpx) ──────────────────
    def _find_pending_sign(self, task_id: str):
        """Return (site_id, node_seq) for a task pending this identity, else None.

        Goes through `pending_sign_list`: scanning only the first Homepage render reported
        "not pending" for anything on page 2+.
        """
        try:
            listing = self.pending_sign_list()
            for r in listing.get("rows", []):
                if r["task_id"].lower() == task_id.lower():
                    return (r["site_id"], r["node_seq"])
        except Exception as ex:
            _eprint(f"[ops.http_web] ⚠️ pending list scan failed, "
                    f"falling back to first page: {type(ex).__name__}: {ex}")
            home = html.unescape(self.get(_HOMEPAGE_PATH).text)
            m = re.search(
                r"SignNodeForm\.aspx\?TASK_ID=" + re.escape(task_id)
                + r"&SITE_ID=([0-9a-f-]{36})&NODE_SEQ=(\d+)", home)
            return (m.group(1), m.group(2)) if m else None
        return None

    def sign_task(self, task_id: str, approve: bool = True, comment: str = "",
                  next_signer_guid: str = "") -> dict:
        """Sign (簽核) a pending free-flow task over httpx. Returns {ok, reason, result}.

        Three synchronous steps mirroring the web UI (no browser):
          1) GET FreeTask/SignNodeForm.aspx?TASK_ID=&SITE_ID=&NODE_SEQ= (site/node from 待簽 widget),
             POST 同意=`MasterPageRadButton3` (or 否決=`MasterPageRadButton4`) with txtComment.
          2) The response carries the confirm-page URL (native `SendOtherSite.aspx`, plugin
             `OtherSiteSend.aspx`) with the server-computed real siteId + signResult.
          3) GET it, set rbListSignResult=Approve/Disapprove + rblEndType=Y(結案) or N(+下一關簽核者),
             POST 送出=`MasterPageRadButton2`.
        Signer identity is the current session's UOF_ACCOUNT; only tasks pending for that
        identity can be signed. `next_signer_guid` empty ⇒ 結案 (ends the flow → 通過).
        """
        import json as _json
        pend = self._find_pending_sign(task_id)
        if not pend:
            return {"ok": False, "reason": f"TaskId {task_id} 不在目前身份的待簽清單（非此人待簽或已結案）",
                    "result": ""}
        site_id, node_seq = pend
        CPH = "ctl00$ContentPlaceHolder1$"
        p = (f"/WKF/FormUse/FreeTask/SignNodeForm.aspx"
             f"?TASK_ID={task_id}&SITE_ID={site_id}&NODE_SEQ={node_seq}")
        pay = _form_state_payload(self._parse(self.get(p)))
        pay[CPH + "txtComment"] = comment or ("同意" if approve else "否決")
        pay["__EVENTTARGET"] = "ctl00$MasterPageRadButton3" if approve else "ctl00$MasterPageRadButton4"
        pay["__EVENTARGUMENT"] = ""
        pay["__LASTFOCUS"] = ""
        r1 = self.post(p, pay, retry_on_login=False)
        if "Login.aspx" in str(r1.url):
            return {"ok": False, "reason": "session 已過期，未重送簽核 postback，請重試", "result": ""}
        if "ErrorReport" in str(r1.url):
            return {"ok": False, "reason": "簽核頁 postback 發生伺服器錯誤（表單本體可能不完整/未填必要內容）",
                    "result": ""}
        # confirm page URL: native SendOtherSite.aspx or plugin OtherSiteSend.aspx
        m = re.search(r"(/WKF/FormUse/[A-Za-z]+/(?:SendOtherSite|OtherSiteSend)\.aspx\?[^'\"]+)",
                      html.unescape(r1.text))
        if not m:
            am = re.search(r'hiddenActionMode"[^>]*value="([^"]*)"', r1.text)
            return {"ok": False, "result": "",
                    "reason": f"未取得簽核確認頁（actionMode={am.group(1) if am else '?'}）；"
                              "同意未生效（可能該站需選填內容或表單不完整）"}
        cpath = m.group(1)
        p2 = _form_state_payload(self._parse(self.get(cpath)))
        p2[CPH + "rbListSignResult"] = "Approve" if approve else "Disapprove"
        if next_signer_guid:
            p2[CPH + "rblEndType"] = "N"  # 往下一站點
            # 指定下一關簽核者（單人）：ChoiceList hiddenJSON
            p2[CPH + "UC_ChoiceList_Signer$hiddenJSON"] = _json.dumps(
                [{"UserGUID": next_signer_guid, "Type": "user"}], separators=(",", ":"))
        else:
            p2[CPH + "rblEndType"] = "Y"  # 結案（此關為最後簽核）
        p2["__EVENTTARGET"] = "ctl00$MasterPageRadButton2"  # 送出
        p2["__EVENTARGUMENT"] = ""
        p2["__LASTFOCUS"] = ""
        r2 = self.post(cpath, p2, retry_on_login=False)
        if "Login.aspx" in str(r2.url):
            return {"ok": False, "reason": "session 已過期，未重送簽核確認，請重試", "result": ""}
        if "ErrorReport" in str(r2.url):
            return {"ok": False, "reason": "送出（簽核確認）發生伺服器錯誤", "result": ""}
        reds = sorted(set(re.findall(r"必填|請選擇|至少|請指定", r2.text)))
        if reds and next_signer_guid == "":
            return {"ok": False, "result": "",
                    "reason": f"送出被擋（{reds}）：此流程可能不允許在此結案，需指定下一關簽核者"}
        return {"ok": True, "reason": "", "result": "同意" if approve else "否決"}

    # ── void / retract a task (作廢/撤單，純 httpx via FormGetBack) ─────────
    def void_task(self, task_id: str, reason: str = "") -> dict:
        """作廢（撤單）一張自己申請、簽核中的表單，純 httpx。對應 UI「表單取回 → 作廢表單」。

        單步同步 postback：GET FormGetBack.aspx?TASK_ID= 撈整頁 hidden state，POST 確定
        （`ctl00$MasterPageRadButton1`）＋`rbGetBack=rbDeleteApplyForm`（作廢表單；另一選項
        `rbSaveApplyForm`＝作廢後存回草稿匣）＋`txtReason`。回 {ok, reason}；是否真的作廢由呼叫端
        重查單狀態確認（此頁 POST 後多半仍停在 FormGetBack，回應字樣不可靠）。
        身份即目前 session（UOF_ACCOUNT）；只有自己申請、未結案的單能作廢。
        """
        CPH = "ctl00$ContentPlaceHolder1$"
        p = f"/WKF/FormUse/FormHandle/FormGetBack.aspx?TASK_ID={task_id}"
        r0 = self.get(p)
        if "Login.aspx" in str(r0.url):
            return {"ok": False, "reason": "redirected to Login.aspx"}
        pay = _parse_hidden_fields(self._parse(r0))
        pay["__EVENTTARGET"] = "ctl00$MasterPageRadButton1"   # 確定
        pay["__EVENTARGUMENT"] = ""
        pay[CPH + "rbGetBack"] = "rbDeleteApplyForm"          # 作廢表單
        pay[CPH + "txtReason"] = reason or "作廢"
        pay[CPH + "tbScriptName"] = ""
        r = self.post(p, pay, retry_on_login=False)
        if "ErrorReport" in str(r.url):
            return {"ok": False, "reason": "作廢 postback 發生伺服器錯誤（可能非本人申請或單已結案）"}
        return {"ok": True, "reason": ""}


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
            data = self._session.scrape_apply_form_list()
        except Exception as ex:
            return f"❌ 取得表單清單時發生錯誤 ({type(ex).__name__}): {ex}"
        if not data.get("ok"):
            return f"❌ 取得表單清單失敗：{data.get('reason', '(unknown)')}"
        forms = data["forms"]
        if not forms:
            return "📋 找不到任何可申請的表單（此帳號在『電子簽核 » 表單申請』沒有可起單的表單）"
        from collections import OrderedDict
        by_cat: OrderedDict = OrderedDict()
        for f in forms:
            by_cat.setdefault(f["category"], []).append(f)
        lines = [f"📋 可申請表單清單（來源：電子簽核 » 表單申請 樹，共 {len(forms)} 個表單）："]
        for cat, items in by_cat.items():
            lines.append(f"\n📁 【{cat}】")
            for f in items:
                lines.append(
                    f"  - {f['form_name']} "
                    f"(formId: {f['form_id']}, formVersionId: {f['form_version_id']})"
                )
        lines.append(
            "\n💡 這是「可起單（表單申請）」的表單；每張都含 formId 與 formVersionId，"
            "起單時 apply_form 直接帶 formVersionId。"
        )
        return "\n".join(lines)

    def get_external_form_list(self) -> str:
        return (
            "⚠️ 網頁機制無法可靠回 `get_external_form_list`。\n\n"
            "「非線上使用」是 UOF 後台「表單管理」中的 admin 旗標，一般 user 在前端\n"
            "（表單申請樹、查詢表單下拉、列表頁）都看不到這個旗標——需在 UOF 後台\n"
            "「表單管理」直接查看。\n\n"
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
            "dataGrid": "明細欄位，起單時帶列清單（見該欄的『每列』）",
            "radio": "單選",
            "checkbox": "多選/勾選",
            "text": "單行文字",
            "dialog": "彈窗選取欄位",
            "unknown": "型別未知（可能是版面元件）",
        }
        unsupported_for_apply = [f for f in fields if f["input_type"] == "fileButton"]
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
            opts = f.get("options") or []
            opt_str = ""
            if opts:
                opt_str = "　可選值：" + "／".join(o["value"] for o in opts)
            lines.append(f"  {mark} [{code}] {f['label']} 〈{f['input_type']}〉 — {hint}{opt_str}")
            if f["input_type"] == "dataGrid":
                cols = f.get("columns") or []
                if cols:
                    col_str = "、".join(f"{c['label'] or ('欄'+str(c['index']+1))}({c['input_type']})" for c in cols)
                    lines.append(f"       每列填：{col_str}；如 {{\"{code}\":[{{列一}}, …]}}，每列用欄名對應")
                else:
                    lines.append(f"       每列帶一個 dict（欄名: 值）或依序的 list；起單時 {{\"{code}\":[…]}}")
        if unsupported_for_apply:
            codes = ", ".join(
                f"{f['code'] or f['label']}({f['input_type']})" for f in unsupported_for_apply
            )
            lines.append(
                f"\n⚠️ 含附檔欄位（{codes}）；apply_form 目前不支援上傳附檔，請於 UOF 網頁操作。"
            )
        lines.append(
            "\n💡 起單時把 fields 帶 `{欄位代碼: 值}` 對應；自動編號欄位帶空字串即可。"
            "\n⚠️ 送出前務必核對：標 ＊ 的欄位一定要填；有『可選值』的欄位只能填清單內的值"
            "（填清單外的值會被伺服器**默默丟棄**、欄位變空、單據不完整卻仍可能回報起單成功）。"
            "\n🔗 來源頁: " + data["url"]
        )
        return "\n".join(lines)

    def _scrape_task_view(self, task_id: str) -> dict:
        """GET ViewFormTemp.aspx?TASK_ID= 並解析申請資訊＋簽核歷程（SignCommentGrid）。

        taskId 直接定址（與查單摘要/歷程同語意）；供 get_task_data / get_task_result 共用。
        """
        resp = self._session.get(f"/WKF/FormUse/ViewFormTemp.aspx?TASK_ID={task_id}")
        if "Login.aspx" in str(resp.url):
            return {"ok": False, "reason": "redirected to Login.aspx"}
        tree = self._session._parse(resp)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", resp.text)).replace("\xa0", " ")
        # 最終結果：GET 頁面有靜態「表單審核結果： 同意/否決/作廢/簽核中」
        m_res = re.search(r"表單審核結果[：:]\s*([^\s<（(]+)", text)
        result = m_res.group(1).strip() if m_res else ""
        # 表單編號使用大寫字母前綴；限制大寫可避免誤中 formVersionId 的小寫十六進位前綴。
        m_no = re.search(r"\b([A-Z]{2,4}\d{6,})\b", text)
        # 簽核歷程：SignCommentGrid <table>，欄序 站點/(icon)/簽核者/簽核意見/簽核時間/簽核狀態（6 欄）
        history = []
        for tbl in tree.xpath("//table[contains(@id,'SignCommentGrid')]"):
            for tr in tbl.xpath(".//tr"):
                tds = tr.xpath("./td")
                if len(tds) < 6:
                    continue
                c = [re.sub(r"\s+", " ", "".join(td.itertext())).replace("\xa0", "").strip() for td in tds]
                history.append({"site": c[0], "signer": c[2], "comment": c[3],
                                 "time": c[4], "status": c[5]})
            break  # 只取第一個 SignCommentGrid 表
        if not result:
            result = "簽核中"
        # 申請者/申請時間＝狀態為「申請」的那一列（否則取首列）
        applicant = apply_time = ""
        for r in history:
            if "申請" in r["status"]:
                applicant, apply_time = r["signer"], r["time"]
                break
        if not applicant and history:
            applicant, apply_time = history[0]["signer"], history[0]["time"]
        close_date = history[-1]["time"] if (history and result != "簽核中") else ""
        # The same page already carries the filled-in form body; parse it here so callers
        # get the actual content, not just who signed when.
        fields = _parse_filled_form_fields(tree)
        return {
            "ok": True, "reason": "", "task_id": task_id,
            "applicant": applicant, "apply_time": apply_time,
            "form_number": (m_no.group(1) if m_no else ""),
            "result": result, "close_date": close_date, "history": history,
            "fields": fields,
        }

    def get_task_data(self, task_id: str) -> str:
        try:
            d = self._scrape_task_view(task_id)
        except Exception as ex:
            return f"❌ 查任務摘要時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 查任務摘要失敗：{d.get('reason', '(unknown)')}"
        if not d["history"] and not d["applicant"]:
            return f"❌ 找不到表單（TaskId {task_id}）或無檢視權限。"
        lines = [
            "📄 表單申請內容（http_web 模式，from ViewFormTemp.aspx）：",
            f"  - 表單編號: {d['form_number'] or '(未取得)'}",
            f"  - 申請者: {d['applicant'] or '(未取得)'}",
            f"  - 申請時間: {d['apply_time'] or '(未取得)'}",
            f"  - 簽核結果: {d['result']}",
        ]
        if d["close_date"]:
            lines.append(f"  - 結案時間: {d['close_date']}")
        if d["fields"]:
            lines += ["", f"📋 表單欄位（{len(d['fields'])} 欄）："]
            lines += _render_filled_fields(d["fields"])
        return "\n".join(lines)

    def get_task_result(self, task_id: str, include_form_data: bool = True) -> str:
        try:
            d = self._scrape_task_view(task_id)
        except Exception as ex:
            return f"❌ 查簽核歷程時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 查簽核歷程失敗：{d.get('reason', '(unknown)')}"
        lines = [
            f"📄 表單 {task_id} 的簽核記錄（http_web 模式，from ViewFormTemp.aspx）：",
            f"  申請者: {d['applicant'] or '(未取得)'} | 最終結果: {d['result']}",
        ]
        lines.append(
            f"  表單編號: {d['form_number'] or '(未取得)'} | 申請時間: {d['apply_time'] or '(未取得)'}"
            + (f" | 結案時間: {d['close_date']}" if d["close_date"] else "")
        )
        if include_form_data:
            if d["fields"]:
                lines += ["", f"📋 表單欄位（{len(d['fields'])} 欄）："]
                lines += _render_filled_fields(d["fields"])
            else:
                lines += ["", "📋 表單欄位：(解析不到欄位；此頁可能非標準表單樣板)"]
        lines += ["", "📝 簽核歷程："]
        if not d["history"]:
            lines.append("  (無歷程或無檢視權限)")
        for r in d["history"]:
            lines.append(
                f"  站點 {r['site']}｜{r['signer']}｜{r['status']}｜{r['time']}"
                + (f"｜意見:{r['comment']}" if r["comment"] else "")
            )
        return "\n".join(lines)

    def get_dialog_structure(self, form_version_id: str, field_code: str = "") -> str:
        try:
            d = self._session.dialog_structure(form_version_id, field_code)
        except Exception as ex:
            return f"❌ 查對話框結構時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 查對話框結構失敗：{d.get('reason', '(unknown)')}"
        if not d["fields"]:
            return ("📋 這張表單沒有對話框型欄位"
                    + (f"（或找不到欄位 {field_code}）" if field_code else "") + "。")
        def _render(cs: list, indent: str = "   ") -> list:
            ls = []
            for c in cs:
                mark = "＊" if c.get("required") else " "
                flags = "".join([
                    "[唯讀]" if c.get("readonly") else "",
                    "[隱藏]" if c.get("hidden") else "",
                ])
                nm = c.get("id") or c.get("name") or "?"
                ls.append(f"{indent}{mark}{c.get('label') or '(無標籤)'} → {nm} 〈{c.get('type', '?')}〉{flags}")
                opts = c.get("options") or []
                if opts:
                    shown = "／".join(o["text"] for o in opts[:8] if o["text"])
                    more = f" …共 {len(opts)} 項" if len(opts) > 8 else ""
                    ls.append(f"{indent}   可選值: {shown}{more}")
                if c.get("lookup_buttons"):
                    ls.append(f"{indent}   查找鈕: {', '.join(c['lookup_buttons'])}")
            return ls

        lines = [f"🗂 對話框欄位結構（{len(d['fields'])} 個）："]
        for f in d["fields"]:
            lines.append(f"\n▸ {f['label']}({f['code']})　挑選器: {f['dialog'] or '(未知)'}")
            if f["note"]:
                lines.append(f"   ⚠️ {f['note']}")
            if f.get("inline"):
                lines.append(f"   ── 欄位區塊內的控制項（{len(f['inline'])}）──")
                lines += _render(f["inline"], "   ")
            if f.get("row_editor"):
                lines.append(f"   ── 明細列編輯器: {f['row_editor'].split('/')[-1][:50]} ──")
            if f["inner"]:
                lines.append(f"   ── 挑選器/列編輯器內欄位（{len(f['inner'])}）──")
            for c in f["inner"]:
                mark = "＊" if c.get("required") else " "
                flags = "".join([
                    "[唯讀]" if c.get("readonly") else "",
                    "[隱藏]" if c.get("hidden") else "",
                ])
                nm = c.get("id") or c.get("name") or "?"
                lines.append(f"   {mark}{c.get('label') or '(無標籤)'} → {nm} 〈{c.get('type', '?')}〉{flags}")
                opts = c.get("options") or []
                if opts:
                    shown = "／".join(o["text"] for o in opts[:8] if o["text"])
                    more = f" …共 {len(opts)} 項" if len(opts) > 8 else ""
                    lines.append(f"      可選值: {shown}{more}")
                if c.get("lookup_buttons"):
                    lines.append(f"      查找鈕: {', '.join(c['lookup_buttons'])}")
        lines.append("\n💡 同一標籤下可能有多個控制項（含隱藏輔助欄）；要填哪一個由表單的 skill 判斷。")
        return "\n".join(lines)

    def search_dialog_options(self, form_version_id: str, field_code: str,
                              keyword: str = "", limit: int = 20) -> str:
        try:
            d = self._session.dialog_options(form_version_id, field_code, keyword, limit)
        except Exception as ex:
            return f"❌ 查詢視窗候選時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 查詢失敗：{d.get('reason', '(unknown)')}"
        rows = d["rows"]
        if not rows:
            return (f"📋 欄位 {d['field']} 以關鍵字「{keyword}」查無候選項目。\n"
                    "💡 換個關鍵字再試；查不到就別硬填，請向使用者確認正確代碼。")
        lines = [f"🔎 {d['field']} 候選項目（關鍵字「{keyword}」，{len(rows)} 筆）："]
        for i, r in enumerate(rows, 1):
            shown = {k: v for k, v in r.items() if v not in (None, "", 0) and not k.startswith("_")}
            head = " ｜ ".join(f"{k}={v}" for k, v in list(shown.items())[:6])
            lines.append(f"  [{i}] {head}")
        lines.append("\n⚠️ 不要盲選第一筆：請確認代碼精確相符或名稱可信，必要時回問使用者。")
        return "\n".join(lines)

    def operate_dialog(self, form_version_id: str, field_code: str,
                       values: Optional[dict] = None, press: str = "") -> str:
        try:
            d = self._session.operate_dialog(form_version_id, field_code, values, press)
        except Exception as ex:
            return f"❌ 操作對話框時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 操作失敗：{d.get('reason', '(unknown)')}"
        lines = [f"🛠 對話框操作完成（{field_code}）"]
        if values:
            lines.append(f"  填入: {values}")
        lines.append(f"  按下: {press or '(未按任何按鈕)'}")
        ch = d["changed"]
        if not ch:
            lines.append("\n⚠️ 伺服器沒有回傳任何欄位變化——動作可能未生效，或此按鈕不影響欄位。")
        else:
            lines.append(f"\n📝 伺服器改動的控制項（{len(ch)}）：")
            for name, mv in list(ch.items())[:30]:
                lines.append(f"  {name.split('$')[-1]}: {mv['from'] or '(空白)'} → {mv['to'] or '(空白)'}")
        lines.append("\n💡 變化清單是判斷「哪些值由系統連帶帶出」的依據；操作順序請依該表單 skill 的定義。")
        return "\n".join(lines)

    def get_pending_sign_list(self) -> str:
        try:
            d = self._session.pending_sign_list()
        except Exception as ex:
            return f"❌ 查待簽清單時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 查待簽清單失敗：{d.get('reason', '(unknown)')}"
        rows = d["rows"]
        if not rows:
            return "📋 目前沒有待簽表單。"
        lines = [f"✍️ 待簽表單（目前身份，共 {len(rows)} 筆）："]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"\n[{i}] {r['text']}"
                f"\n    TaskId: {r['task_id']}"
                f"\n    SiteId: {r['site_id']} | NodeSeq: {r['node_seq']}"
            )
        if d["total"] and d["total"] != len(rows):
            lines.append(f"\n⚠️ 頁面回報共 {d['total']} 筆，實際取得 {len(rows)} 筆（翻頁可能未走完）。")
        lines.append("\n💡 用 get_task_result 看單張欄位內容；terminate_task 可同意/否決。")
        return "\n".join(lines)

    def preview_workflow(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: Optional[dict] = None,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str:
        return (
            "⚠️ 流程預覽（模擬簽核路徑）目前不提供：此功能需在 UOF 網頁上操作。\n"
            "💡 你仍可直接用 apply_form 起單；起單後用 get_task_result 查看實際簽核歷程與目前站點。"
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
        """結案：Cancel＝作廢/撤單（httpx FormGetBack）；Adopt/Reject＝同意/否決（委派 httpx 簽核流程）。

        兩者都先查單狀態攔截「對已結案的單重複操作」（UOF 會覆寫最終結果）。作廢身份即目前 session，
        只有自己申請、未結案的單能作廢；同意/否決只有輪到目前身份待簽的單能做（沿用 sign_task 邊界）。
        """
        if result not in ("Adopt", "Reject", "Cancel"):
            return f"❌ 無效的結案動作: {result}。請使用 Adopt（同意）、Reject（否決）或 Cancel（作廢）"
        # 防護：已結案的單再操作，UOF 端會覆寫最終結果 → 先查狀態攔截
        try:
            d = self._scrape_task_view(task_id)
        except Exception as ex:
            return f"❌ 結案前狀態檢查失敗（{type(ex).__name__}）：{ex}"
        if not d.get("ok"):
            return f"❌ 找不到表單 {task_id}（{d.get('reason', '')}）"
        cur = d.get("result", "")
        if cur in ("同意", "否決", "作廢"):   # 已結案的三種終態（未結案為 簽核中/處理中/空）
            return (f"❌ 表單已結案（結果: {cur}），不可再結案。\n"
                    "⚠️ 注意: 對已結案的單重複操作會覆寫最終結果，已由工具層攔截。")
        if result == "Cancel":
            try:
                r = self._session.void_task(task_id, reason)
            except Exception as ex:
                return f"❌ 作廢執行錯誤（{type(ex).__name__}）：{ex}"
            if not r.get("ok"):
                return f"❌ 表單作廢失敗：{r.get('reason', '(unknown)')}"
            after = self._scrape_task_view(task_id)
            if after.get("ok") and after.get("result") == "作廢":
                return "✅ 表單作廢成功"
            return (f"⚠️ 作廢指令已送出，但狀態未確認為作廢（目前: {after.get('result', '?')}）。\n"
                    "   請用 query_forms / get_task_data 確認，勿直接重送。")
        # Adopt / Reject：委派既有 httpx 簽核流程（同意＝在此關結案；否決）
        approve = (result == "Adopt")
        try:
            r = self._session.sign_task(task_id, approve=approve, comment=reason, next_signer_guid="")
        except Exception as ex:
            return f"❌ 簽核執行錯誤（{type(ex).__name__}）：{ex}"
        if not r.get("ok"):
            return f"❌ 表單{'同意' if approve else '否決'}未完成：{r.get('reason', '')}"
        return f"✅ 表單{'同意' if approve else '否決'}成功"

    def sign_next(self, task_id: str, site_id: str, node_seq: int, signer_guid: str) -> str:
        """簽核目前待簽的一關（純 httpx，自由流程 web 簽核）。

        以目前 MCP 身份（UOF_ACCOUNT）對「自己待簽」的表單按「同意」。site_id/node_seq 由待簽清單
        自動定位、不需呼叫端提供（沿用原簽名以相容工具介面）。`signer_guid` 若提供＝指定下一關
        簽核者（往下一站點）；留空＝在此結案（此關為最後簽核 → 表單結案/通過）。
        """
        try:
            r = self._session.sign_task(task_id, approve=True, comment="",
                                        next_signer_guid=(signer_guid or ""))
        except Exception as ex:
            return f"❌ 簽核執行錯誤（{type(ex).__name__}）：{ex}"
        if not r.get("ok"):
            return f"❌ 簽核未完成：{r.get('reason')}"
        nxt = "指定下一關簽核者" if signer_guid else "結案"
        return (f"✅ 已簽核（同意）TaskId {task_id}（{nxt}）。"
                "可用 query_forms / get_task_result 確認狀態。")

    def query_forms(
        self,
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        max_results: int = 50,
        query_mode: str = "apply",
    ) -> str:
        try:
            result = self._session.search_forms(
                keyword, date_from, date_to, max_results, query_mode)
        except Exception as ex:
            return (
                f"❌ 查詢表單時發生錯誤 ({type(ex).__name__}): {ex}\n"
                f"💡 此清單為自動擷取，遇非預期頁面結構時可能誤判。"
            )
        if not result.get("ok"):
            return f"❌ 查詢失敗：{result.get('reason', '(unknown)')}"
        rows = result["rows"]
        q = result["query"]
        # keyword 時 total_matched＝過濾後命中數；無 keyword 時沿用掃到的列數
        total = result.get("total_matched", result.get("total_scanned", len(rows)))
        header = (
            f"🔍 查詢表單 —"
            f" {q['date_from']} ~ {q['date_to']}"
            + ("（依申請日期）" if q.get("query_mode") == "apply" else "（依簽核日期）")
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
        lines.append("\n💡 UserGuid 可用於 sign_next 的 signer_guid 參數。")
        return "\n".join(lines)
