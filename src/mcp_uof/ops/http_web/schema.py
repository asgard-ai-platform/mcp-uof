from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .parsing import _parse_field_blocks


@dataclass(frozen=True)
class FieldOption:
    value: str
    label: str


@dataclass(frozen=True)
class FormField:
    code: str
    label: str
    required: bool
    input_type: str
    input_name: str
    input_title: str = ""
    dialog_url: str = ""
    options: tuple[FieldOption, ...] = ()
    disabled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FormField":
        known = {
            "code", "label", "required", "input_type", "input_name", "input_title",
            "dialog_url", "options", "disabled",
        }
        return cls(
            code=value.get("code") or "",
            label=value.get("label") or "",
            required=bool(value.get("required")),
            input_type=value.get("input_type") or "unknown",
            input_name=value.get("input_name") or "",
            input_title=value.get("input_title") or "",
            dialog_url=value.get("dialog_url") or "",
            options=tuple(
                FieldOption(str(option.get("value") or ""), str(option.get("label") or ""))
                for option in value.get("options") or []
            ),
            disabled=bool(value.get("disabled")),
            metadata={key: item for key, item in value.items() if key not in known},
        )

    def as_dict(self) -> dict[str, Any]:
        result = {
            "code": self.code,
            "label": self.label,
            "required": self.required,
            "input_type": self.input_type,
            "input_name": self.input_name,
            "input_title": self.input_title,
            "dialog_url": self.dialog_url,
            "options": [
                {"value": option.value, "label": option.label} for option in self.options
            ],
            "disabled": self.disabled,
        }
        result.update(self.metadata)
        return result


@dataclass(frozen=True)
class FormSchema:
    fields: tuple[FormField, ...]

    @classmethod
    def parse(cls, tree, include_dialog_companions: bool = False) -> "FormSchema":
        return cls(tuple(
            FormField.from_dict(value)
            for value in _parse_field_blocks(tree, include_dialog_companions)
        ))

    def find(self, key: str) -> FormField | None:
        wanted = str(key).casefold()
        return next(
            (field for field in self.fields
             if field.code.casefold() == wanted or field.label.casefold() == wanted),
            None,
        )

    def as_dicts(self) -> list[dict[str, Any]]:
        return [field.as_dict() for field in self.fields]

    def missing_required(
        self,
        filled: dict[str, Any],
        invalid_codes: set[str],
    ) -> tuple[FormField, ...]:
        return tuple(
            field for field in self.fields
            if field.required and field.code not in filled and field.code not in invalid_codes
        )
