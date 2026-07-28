from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .payload import _raddate_clientstate, _radnumeric_clientstate
from .schema import FormField
from .validation import _resolve_checkbox_value


@dataclass(frozen=True)
class EncodeResult:
    filled_value: str = ""
    warning: str = ""
    blocking: str = ""


class FieldCodec:
    """Encodes scalar form values and rejects values UOF would silently discard."""

    def __init__(self, tree) -> None:
        self._tree = tree

    def encode(self, field: FormField, value: Any, payload: dict) -> EncodeResult:
        label = field.label or field.code
        if field.disabled:
            return EncodeResult(warning=f"欄位 {field.code}（{field.label}）在此表單為停用狀態、起單時不可填，已略過")
        if not field.input_name:
            return EncodeResult(blocking=f"欄位「{label}」找不到可寫入的控制項（解析器缺口），值『{value}』無法寫入")
        if field.input_type == "datePicker":
            return self._encode_date(field, value, payload)
        if field.input_type == "dropDown":
            return self._encode_select(field, value, payload)
        if field.input_type == "radio":
            return self._encode_radio(field, value, payload)
        if field.input_type == "checkbox":
            return self._encode_checkbox(field, value, payload)
        rendered = str(value)
        payload[field.input_name] = rendered
        if field.input_type == "numeric":
            state_name = field.input_name.replace("$", "_") + "_ClientState"
            try:
                payload[state_name] = _radnumeric_clientstate(payload.get(state_name, ""), float(value))
            except (TypeError, ValueError):
                pass
        return EncodeResult(filled_value=rendered)

    def _encode_date(self, field: FormField, value: Any, payload: dict) -> EncodeResult:
        rendered = str(value).replace("-", "/")
        payload[field.input_name] = rendered
        payload[field.input_name + "$dateInput"] = rendered
        state_name = field.input_name.replace("$", "_") + "_dateInput_ClientState"
        payload[state_name] = _raddate_clientstate(payload.get(state_name, ""), rendered)
        return EncodeResult(filled_value=rendered)

    def _encode_select(self, field: FormField, value: Any, payload: dict) -> EncodeResult:
        rendered = str(value)
        exact = next(
            (option for option in field.options
             if rendered in (option.value, option.label)),
            None,
        )
        partial = next(
            (option for option in field.options if rendered.casefold() in option.label.casefold()),
            None,
        )
        selected = exact or partial
        if selected is None:
            hint = f"，只能填：{'／'.join(option.value for option in field.options)}" if field.options else ""
            return EncodeResult(blocking=f"欄位「{field.label or field.code}」的值『{value}』不是有效下拉選項{hint}")
        payload[field.input_name] = selected.value
        return EncodeResult(filled_value=selected.value)

    def _encode_radio(self, field: FormField, value: Any, payload: dict) -> EncodeResult:
        selected = next(
            (option for option in field.options if str(value) in (option.value, option.label)),
            None,
        )
        if field.options and selected is None:
            choices = "／".join(option.label for option in field.options)
            return EncodeResult(blocking=f"欄位「{field.label or field.code}」的值『{value}』不是有效選項，只能填：{choices}")
        rendered = selected.value if selected else str(value)
        payload[field.input_name] = rendered
        return EncodeResult(filled_value=rendered)

    def _encode_checkbox(self, field: FormField, value: Any, payload: dict) -> EncodeResult:
        options = [{"value": option.value, "label": option.label} for option in field.options]
        enabled, posted, error = _resolve_checkbox_value(options, value)
        if error:
            return EncodeResult(blocking=f"欄位「{field.label or field.code}」的{error}")
        if enabled:
            payload[field.input_name] = posted
        else:
            payload.pop(field.input_name, None)
        return EncodeResult(filled_value=posted if enabled else "")
