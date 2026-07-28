from __future__ import annotations
import re
from ..._log import eprint as _eprint
from .parsing import _control_label


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


