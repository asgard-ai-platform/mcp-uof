from __future__ import annotations
import html
import re
from typing import Optional

from .parsing import _is_disabled


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
        if _is_disabled(sel[0]):
            return f"欄位為唯讀，值『{value}』無法寫入"
        opt = next((o for o in sel[0].xpath(".//option")
                    if (o.get("value") or "") == str(value)
                    or "".join(o.itertext()).strip() == str(value)), None)
        if opt is None:
            return f"值『{value}』不在選項中"
        payload[name] = opt.get("value") or ""
        return None
    radios = tree.xpath(f"//input[@type='radio'][@name={name!r}]")
    if radios:
        if any(_is_disabled(el) for el in radios):
            return f"欄位為唯讀，值『{value}』無法寫入"
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
    controls = tree.xpath(f"//input[@name={name!r}] | //textarea[@name={name!r}]")
    if controls and _is_disabled(controls[0]):
        return f"欄位為唯讀，值『{value}』無法寫入"
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

