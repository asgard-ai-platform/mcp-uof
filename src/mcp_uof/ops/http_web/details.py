from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlparse

from .parsing import (
    _matches_uc_prefix,
    _parse_datagrid_columns,
    _parse_dialog_fields,
)
from .payload import (
    _fill_control_value,
    _radnumeric_clientstate,
    _raddate_clientstate,
)
from .runtime import EvidenceKind, HydratedPage, ReplayPolicy, WebFormsRuntime
from .validation import (
    _datagrid_dialog_path,
    _dialog_reject_reason,
    _map_row_to_columns,
    _missing_required_controls,
    _missing_required_dialog_fields,
    _temp_return_value,
)


@dataclass(frozen=True)
class DetailWriteResult:
    page: HydratedPage
    added: int
    errors: list[str]
    notes: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class PluginBlockResult:
    page: HydratedPage
    summary: str
    errors: list[str]
    notes: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


class DetailOperation:
    """Owns the stateful dialog-to-parent detail transaction."""

    def __init__(
        self,
        runtime: WebFormsRuntime,
        normalize_path: Callable[[str], str],
    ) -> None:
        self._runtime = runtime
        self._normalize_path = normalize_path

    def persist_plugin_block(
        self,
        parent_path: str,
        page: HydratedPage,
        payload: Mapping[str, Any],
        owner_prefix: str,
        label: str,
        value: Any,
    ) -> PluginBlockResult:
        """Fill one plugin block, including its inline controls and owned row editors."""
        current = page
        state = dict(payload)
        errors: list[str] = []
        notes: list[str] = []
        summary = "已選取"
        rows_value = value

        if isinstance(value, dict):
            inline = dict(value)
            rows_value = inline.pop("_rows", None)
            presses = inline.pop("_press_after", None) or []
            presses_last = inline.pop("_press_last", None) or []
            prefill = inline.pop("_fill_before", None) or {}
            lookups = inline.pop("_lookups", None) or []
            names = {
                name.split("$")[-1]: name
                for name in (
                    element.get("name")
                    for element in current.tree.xpath(
                        "//input[@name]|//select[@name]|//textarea[@name]"
                    )
                )
                if name and (not owner_prefix or _matches_uc_prefix(name, owner_prefix))
            }
            bad_prefill = [key for key in prefill if key not in names]
            if bad_prefill:
                errors.extend(
                    f"欄位「{label}」區塊內無控制項 {key}（_fill_before）"
                    for key in bad_prefill
                )
            unknown = [key for key in inline if key not in names]
            if unknown and not errors:
                errors.append(
                    f"欄位「{label}」區塊內無這些控制項：{unknown}；"
                    f"有效名稱：{'／'.join(sorted(names)[:20])}"
                )
            if not errors:
                for key, item_value in inline.items():
                    error = _fill_control_value(state, current.tree, names[key], item_value)
                    if error:
                        errors.append(f"欄位「{label}」的 {key} {error}")
                        break
            for press in ([] if errors else presses):
                current = self._runtime.control_postback(
                    parent_path, current, press, values=state, replay=ReplayPolicy.NEVER
                )
                if current.evidence.kind is EvidenceKind.LOGIN:
                    errors.append(f"欄位「{label}」按下 {press} 時 session 已過期，請重試")
                    break
                state.update(current.state)
            for lookup in ([] if errors else lookups):
                press = (lookup or {}).get("press")
                picked = (lookup or {}).get("row")
                if not press or picked is None:
                    errors.append(f"欄位「{label}」的 _lookups 每項需要 press 與 row")
                    break
                lookup_values = dict(state)
                for key, item_value in prefill.items():
                    error = _fill_control_value(
                        lookup_values, current.tree, names[key], item_value
                    )
                    if error:
                        errors.append(f"欄位「{label}」的 _fill_before {key} {error}")
                        break
                if errors:
                    break
                before = {
                    key: item_value
                    for key, item_value in state.items()
                    if not owner_prefix or _matches_uc_prefix(key, owner_prefix)
                }
                current = self.replay_lookup(
                    parent_path, current, state, press, picked, lookup_values
                )
                if current.evidence.kind is EvidenceKind.LOGIN:
                    errors.append(f"欄位「{label}」lookup 時 session 已過期，請重試")
                    break
                state.update(current.state)
                after = {
                    key: item_value
                    for key, item_value in current.state.items()
                    if not owner_prefix or _matches_uc_prefix(key, owner_prefix)
                }
                if before and after == before:
                    errors.append(
                        f"欄位「{label}」按下 {press} 後區塊沒有任何變化——"
                        "伺服器無法解析所選項目，請換一筆"
                    )
                    break
            for press in ([] if errors else presses_last):
                current = self._runtime.control_postback(
                    parent_path, current, press, values=state, replay=ReplayPolicy.NEVER
                )
                if current.evidence.kind is EvidenceKind.LOGIN:
                    errors.append(f"欄位「{label}」按下 {press} 時 session 已過期，請重試")
                    break
                state.update(current.state)
            if not errors and owner_prefix:
                for control in _missing_required_controls(current.tree, owner_prefix, state):
                    control_id = control.get("id") or "?"
                    errors.append(
                        f"欄位「{label}」內的必填控制項「{control.get('label') or control_id}」"
                        f"（{control_id}）未提供"
                    )
            if errors:
                return PluginBlockResult(current, summary, errors, notes)
            summary = "、".join(f"{key}={item_value}" for key, item_value in inline.items()) or summary
            current = HydratedPage(
                current.response, current.tree, state, current.evidence
            )
            if rows_value is None:
                return PluginBlockResult(current, summary, errors, notes)

        if not isinstance(rows_value, (list, tuple, dict)):
            return PluginBlockResult(
                current, summary, [f"欄位「{label}」的明細值需為列清單或按鈕對應"], notes
            )
        batches = (
            [(hint, rows) for hint, rows in rows_value.items()]
            if isinstance(rows_value, dict)
            else [("", list(rows_value))]
        )
        detail = self.persist_plugin_batches(
            parent_path,
            current,
            state,
            owner_prefix,
            [(hint, list(rows)) for hint, rows in batches],
        )
        errors.extend(detail.errors)
        notes.extend(detail.notes)
        if detail.ok:
            summary = f"{detail.added} 列"
        return PluginBlockResult(detail.page, summary, errors, notes)

    def replay_lookup(
        self,
        parent_path: str,
        page: HydratedPage,
        payload: Mapping[str, Any],
        press: str,
        picked: Any,
        values: Mapping[str, Any] | None = None,
    ) -> HydratedPage:
        post_values = dict(payload)
        post_values.update(values or {})
        post_values["DialogReturnValue"] = (
            picked if isinstance(picked, str) else json.dumps(picked, ensure_ascii=False)
        )
        return self._runtime.control_postback(
            parent_path,
            page,
            press,
            values=post_values,
            replay=ReplayPolicy.NEVER,
        )

    def discover_plugin_editors(
        self,
        page: HydratedPage,
        owner_prefix: str,
    ) -> list[tuple[str, str]]:
        editors = []
        for element in page.tree.xpath("//input[@onclick]"):
            name = element.get("name") or ""
            if owner_prefix and not _matches_uc_prefix(name, owner_prefix):
                continue
            onclick = html.unescape(element.get("onclick") or "")
            match = re.search(r"['\"]([^'\"\s]*Dialog\.aspx[^'\"\s]*)['\"]", onclick)
            if not match or "GridDataID" not in match.group(1):
                continue
            parsed = urlparse(urljoin(str(page.response.url), match.group(1)))
            editors.append((name, parsed.path + (f"?{parsed.query}" if parsed.query else "")))
        return editors

    def persist_plugin_batches(
        self,
        parent_path: str,
        page: HydratedPage,
        payload: Mapping[str, Any],
        owner_prefix: str,
        batches: list[tuple[str, list]],
    ) -> DetailWriteResult:
        editors = self.discover_plugin_editors(page, owner_prefix)
        if not editors:
            return DetailWriteResult(page, 0, ["找不到列編輯對話框，無法填列"], [])
        current = page
        added = 0
        errors: list[str] = []
        notes: list[str] = []
        for hint, rows in batches:
            editor = next(
                (item for item in editors if not hint or item[0].split("$")[-1] == hint),
                None,
            )
            if editor is None:
                errors.append(
                    f"沒有名為 {hint} 的列編輯按鈕；可用："
                    + "／".join(item[0].split("$")[-1] for item in editors)
                )
                break
            opener, dialog_path = editor
            row_result = self.persist_plugin_rows(dialog_path, rows)
            errors.extend(row_result.errors)
            notes.extend(row_result.notes)
            if row_result.added != len(rows):
                errors.append(f"{len(rows)} 列僅有 {row_result.added} 列被對話框接受，未完整")
                break
            before_count = self._owned_grid_row_count(current, owner_prefix)
            for returned in row_result.returned:
                current = self.replay_lookup(parent_path, current, payload, opener, returned)
                payload = current.state
                if current.evidence.kind is EvidenceKind.LOGIN:
                    errors.append("session 已過期，未重送明細回填 postback，請重試")
                    break
            if errors:
                break
            after_count = self._owned_grid_row_count(current, owner_prefix)
            if after_count <= before_count:
                errors.append("回填後指定明細表格仍無新增列，列未真正寫入單據")
                break
            added += row_result.added
        return DetailWriteResult(current, added, errors, notes)

    @dataclass(frozen=True)
    class _Rows:
        added: int
        errors: list[str]
        notes: list[str]
        returned: list[str]

    def persist_plugin_rows(self, dialog_url: str, rows: list) -> _Rows:
        path = self._normalize_path(dialog_url if dialog_url.startswith("/") else "/" + dialog_url)
        added = 0
        errors: list[str] = []
        notes: list[str] = []
        returned: list[str] = []
        for index, row in enumerate(list(rows or [])):
            page = self._get_page(path)
            fields = _parse_dialog_fields(page.response.text)
            controls = [field["name"] for field in fields]
            if not controls:
                errors.append("對話框欄位解析失敗，無法驗證列內容")
                break
            short = {name.split("$")[-1]: name for name in controls}
            if not isinstance(row, dict):
                errors.append(f"第 {index + 1} 列需為 dict（欄位名稱→值），收到 {type(row).__name__}")
                continue
            values = dict(row)
            presses = values.pop("_press_after", None) or []
            lookups = values.pop("_lookups", None) or []
            presses_last = values.pop("_press_last", None) or []
            prefill = values.pop("_fill_before", None) or {}
            unknown = [key for key in values if key not in short and key not in controls]
            if unknown:
                errors.append(f"第 {index + 1} 列的控制項名稱不存在：{unknown}；有效名稱：{'／'.join(sorted(short)[:20])}")
                continue
            state = dict(page.state)
            invalid = False
            for key, value in values.items():
                error = _fill_control_value(state, page.tree, short.get(key, key), value)
                if error:
                    errors.append(f"第 {index + 1} 列：{key} 的{error}，該列未送出")
                    invalid = True
                    break
            if invalid:
                continue
            for press in presses:
                page = self._runtime.control_postback(
                    path, page, press, values=state, replay=ReplayPolicy.NEVER
                )
                state = dict(page.state)
            for lookup in lookups:
                press = (lookup or {}).get("press") or ""
                picked = (lookup or {}).get("row")
                if not press or picked is None:
                    errors.append(f"第 {index + 1} 列的 _lookups 需要 press 與 row 兩個欄位")
                    invalid = True
                    break
                lookup_values = dict(state)
                for key, value in prefill.items():
                    error = _fill_control_value(lookup_values, page.tree, short.get(key, key), value)
                    if error:
                        errors.append(f"第 {index + 1} 列：_fill_before 的 {key} {error}，該列未送出")
                        invalid = True
                        break
                if invalid:
                    break
                page = self.replay_lookup(path, page, state, press, picked, lookup_values)
                state = dict(page.state)
            if invalid:
                continue
            for press in presses_last:
                page = self._runtime.control_postback(
                    path, page, press, values=state, replay=ReplayPolicy.NEVER
                )
                state = dict(page.state)
            missing = _missing_required_dialog_fields(fields, state)
            if missing:
                names = "、".join(
                    f"{field.get('label')}（{field.get('id') or field['name'].split('$')[-1]}）"
                    if field.get("label")
                    else field.get("id") or field["name"].split("$")[-1]
                    for field in missing
                )
                errors.append(f"第 {index + 1} 列的必填控制項未提供：{names}，該列未送出")
                continue
            confirm = dict(state)
            confirm.update({
                "__EVENTTARGET": "ctl00$MasterPageRadButton1",
                "__EVENTARGUMENT": "",
                "FASTReturnValue": "[DefaultNullValue]",
            })
            response = self._runtime.post(path, confirm, replay=ReplayPolicy.NEVER)
            evidence = self._runtime.evidence(response)
            if evidence.kind is EvidenceKind.LOGIN:
                errors.append(f"第 {index + 1} 列：session 已過期，未重送列確認，請重試")
                continue
            if evidence.kind is EvidenceKind.ERROR_REPORT:
                errors.append(f"第 {index + 1} 列：確定時被導向 ErrorReport")
                continue
            value = _temp_return_value(response.text)
            if value is None:
                reason = _dialog_reject_reason(response.text)
                errors.append(f"第 {index + 1} 列：確定後對話框未回傳列資料，該列未被接受" + (f"——{reason}" if reason else ""))
                continue
            returned.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
            added += 1
        return self._Rows(added, errors, notes, returned)

    def persist_datagrid_rows(self, dialog_url: str, rows: list) -> tuple[int, list[str]]:
        path = self._normalize_path(dialog_url)
        added = 0
        errors: list[str] = []
        columns = []
        for index, row in enumerate(list(rows or [])):
            page = self._get_page(path)
            columns = columns or _parse_datagrid_columns(page.response.text)
            if not columns:
                return 0, ["對話框欄位解析失敗（找不到 versionFieldUC 欄位）"]
            mapped, unmatched = _map_row_to_columns(row, columns)
            if unmatched:
                errors.append(f"第 {index + 1} 列的欄名對不上：{unmatched}，未送出該列")
                continue
            state = dict(page.state)
            for column in columns:
                value = mapped.get(column["index"])
                if value in (None, ""):
                    continue
                name = column["input_name"]
                if column["input_type"] == "numeric":
                    state[name] = str(value)
                    if column["client_state_name"]:
                        state[column["client_state_name"]] = _radnumeric_clientstate(
                            state.get(column["client_state_name"], ""), value
                        )
                elif column["input_type"] == "date":
                    rendered = str(value).replace("-", "/")
                    state[name] = rendered
                    state[name + "$dateInput"] = rendered
                    if column["client_state_name"]:
                        state[column["client_state_name"]] = _raddate_clientstate(
                            state.get(column["client_state_name"], ""), str(value)
                        )
                else:
                    state[name] = str(value)
            state.update({
                "FASTReturnValue": "[DefaultNullValue]",
                "__EVENTTARGET": "ctl00$MasterPageRadButton1",
                "__EVENTARGUMENT": "",
            })
            response = self._runtime.post(path, state, replay=ReplayPolicy.NEVER)
            if "NeedPostBack" not in response.text:
                errors.append(f"第 {index + 1} 列：對話框未回 NeedPostBack")
                continue
            added += 1
        return added, errors

    @staticmethod
    def discover_datagrid_editor(page: HydratedPage, field_id: str) -> str:
        return _datagrid_dialog_path(page.response.text, field_id)

    def _get_page(self, path: str) -> HydratedPage:
        return self._runtime.hydrate(self._runtime.get(path, replay=ReplayPolicy.SAFE))

    @staticmethod
    def _owned_grid_row_count(page: HydratedPage, owner_prefix: str) -> int:
        candidates = page.tree.xpath(
            f"//*[contains(@id, '{owner_prefix}') and contains(@id, 'Grid')]"
        )
        return sum(
            1
            for grid in candidates
            for row in grid.xpath(".//tr")
            if row.xpath("./td") and "沒有資料" not in "".join(row.itertext())
        )
