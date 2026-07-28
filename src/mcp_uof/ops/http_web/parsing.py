from __future__ import annotations
import html
import json
import re
from ..._log import eprint as _eprint
from .constants import _SKIP_HIDDEN_PREFIXES, _DIALOG_OPEN_RE, _etree, _html_fromstring


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
                # specifically for _datagrid_dialog_path (see DetailOperation write path).
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




def _matches_uc_prefix(control_name: str, uc_prefix: str) -> bool:
    """Match one versionFieldUC group without confusing UC1 with UC10."""
    if not control_name or not uc_prefix:
        return False
    return bool(re.search(
        rf"(?:^|[$_]){re.escape(uc_prefix)}(?:$|[$_])",
        control_name,
    ))




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
        if not _matches_uc_prefix(name, uc_prefix) or name.startswith("__"):
            continue
        label, required = _control_label(el)
        out.append({
            "name": name,
            # Some plugin controls expose only ASP.NET's `$`-separated name.
            "id": re.split(r"[_$]", el.get("id") or name)[-1],
            "label": label,
            "required": required,
            "type": "select" if el.tag == "select" else (el.tag if el.tag == "textarea" else "text"),
            "options": [{"value": o.get("value") or "", "text": _t(o)} for o in el.xpath(".//option")],
            "readonly": el.get("readonly") is not None or el.get("disabled") is not None,
            "hidden": "HideMe" in (el.get("class") or ""),
            "lookup_buttons": [],
        })
    out.extend(_choice_controls(tree, lambda n: _matches_uc_prefix(n, uc_prefix)))
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
                re.split(r"[_$]", b.get("id") or b.get("name") or "")[-1]
                for b in (row.xpath(".//input[@type='submit'] | .//input[@type='button']") if row is not None else [])
            ],
        })
    out.extend(_choice_controls(tree, lambda n: True))
    return out




def _find_row_editor_openers(
    page_html: str,
    exclude_basenames: set,
    owner_prefix: str = "",
) -> list:
    """Buttons on the apply page that open a detail-row editor dialog — read from the DOM.

    A detail grid's "add row" button carries an onclick that opens a `*.aspx` dialog. We collect
    every such (button, dialog-url) except the block's own picker dialog. No form/dialog/field
    name is hardcoded: the linkage is read straight from each button's onclick, so it works for
    any plugin form (ItemDialog / GUIsDialog / whatever the deployment renders).
    """
    tree = _html_fromstring(page_html)
    out, seen = [], set()
    for el in tree.xpath("//input[@onclick] | //a[@onclick] | //button[@onclick]"):
        control_name = el.get("name") or el.get("id") or ""
        if owner_prefix and not _matches_uc_prefix(control_name, owner_prefix):
            continue
        m = _DIALOG_OPEN_RE.search(html.unescape(el.get("onclick") or ""))
        if not m:
            continue
        url = m.group(1).replace("&amp;", "&")
        base = url.split("/")[-1].split("?")[0]
        if base in exclude_basenames or url in seen:
            continue
        seen.add(url)
        out.append({
            "open_button": re.split(r"[_$]", control_name)[-1],
            "control_name": control_name,
            "url": url,
            "basename": base,
        })
    return out




def _lookup_dialog_target(dialog_html: str, button_id: str) -> str:
    """The `*.aspx` dialog a given lookup button (by id suffix) opens, read from its onclick.

    Lets a nested row-editor picker (料號/科目 …) be queried later without any hardcoded name.
    """
    tree = _html_fromstring(dialog_html)
    for el in tree.xpath("//input[@onclick] | //a[@onclick] | //button[@onclick]"):
        control_name = el.get("id") or el.get("name") or ""
        if re.split(r"[_$]", control_name)[-1] != button_id:
            continue
        m = _DIALOG_OPEN_RE.search(html.unescape(el.get("onclick") or ""))
        if m:
            return m.group(1).replace("&amp;", "&")
    return ""




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

