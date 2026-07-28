"""Offline snapshot of the meaningful MCP tool surface."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_common.ensure_src_on_path()

from mcp_uof.server import mcp  # noqa: E402


EXPECTED = {
    "uof_custom_apply_form": {
        "required": ["applicant_account", "fields", "first_signer_account", "form_version_id"],
        "properties": {"applicant_account": {"type": "string"}, "comment": {"type": "string", "default": ""}, "fields": {"type": "object"}, "first_signer_account": {"type": "string"}, "form_version_id": {"type": "string"}, "urgent_level": {"type": "string", "default": "2"}},
    },
    "uof_custom_check_auth": {"required": [], "properties": {}},
    "uof_custom_get_dialog_structure": {"required": ["form_version_id"], "properties": {"field_code": {"type": "string", "default": ""}, "form_version_id": {"type": "string"}}},
    "uof_custom_get_external_form_list": {"required": [], "properties": {}},
    "uof_custom_get_form_list": {"required": [], "properties": {}},
    "uof_custom_get_form_structure": {"required": ["form_version_id"], "properties": {"form_version_id": {"type": "string"}}},
    "uof_custom_get_form_structure_by_id": {"required": ["form_id"], "properties": {"form_id": {"type": "string"}}},
    "uof_custom_get_pending_sign_list": {"required": [], "properties": {}},
    "uof_custom_get_task_data": {"required": ["task_id"], "properties": {"task_id": {"type": "string"}}},
    "uof_custom_get_task_result": {"required": ["task_id"], "properties": {"include_form_data": {"type": "boolean", "default": True}, "task_id": {"type": "string"}}},
    "uof_custom_login": {"required": [], "properties": {"force": {"type": "boolean", "default": False}}},
    "uof_custom_logout": {"required": [], "properties": {}},
    "uof_custom_operate_dialog": {"required": ["field_code", "form_version_id"], "properties": {"field_code": {"type": "string"}, "form_version_id": {"type": "string"}, "press": {"type": "string", "default": ""}, "values": {"type": "null|object", "default": None}}},
    "uof_custom_preview_workflow": {"required": ["applicant_account", "first_signer_account", "form_version_id"], "properties": {"applicant_account": {"type": "string"}, "comment": {"type": "string", "default": ""}, "fields": {"type": "null|object", "default": None}, "first_signer_account": {"type": "string"}, "form_version_id": {"type": "string"}, "urgent_level": {"type": "string", "default": "2"}}},
    "uof_custom_query_forms": {"required": [], "properties": {"date_from": {"type": "string", "default": ""}, "date_to": {"type": "string", "default": ""}, "keyword": {"type": "string", "default": ""}, "max_results": {"type": "integer", "default": 50}, "query_mode": {"type": "string", "default": "apply"}}},
    "uof_custom_search_dialog_options": {"required": ["field_code", "form_version_id"], "properties": {"field_code": {"type": "string"}, "form_version_id": {"type": "string"}, "keyword": {"type": "string", "default": ""}, "limit": {"type": "integer", "default": 20}}},
    "uof_custom_search_users": {"required": ["keyword"], "properties": {"keyword": {"type": "string"}}},
    "uof_custom_sign_next": {"required": ["task_id"], "properties": {"node_seq": {"type": "integer", "default": 0}, "signer_guid": {"type": "string", "default": ""}, "site_id": {"type": "string", "default": ""}, "task_id": {"type": "string"}}},
    "uof_custom_terminate_task": {"required": ["reason", "result", "task_id"], "properties": {"reason": {"type": "string"}, "result": {"type": "string"}, "task_id": {"type": "string"}}},
}


def _property_contract(schema: dict) -> dict:
    types = []
    if "type" in schema:
        types.append(schema["type"])
    types.extend(part["type"] for part in schema.get("anyOf", []) if "type" in part)
    result = {"type": "|".join(sorted(types))}
    if "default" in schema:
        result["default"] = schema["default"]
    return result


async def _tools():
    return await mcp.list_tools()


def main() -> int:
    failures = 0
    tools = asyncio.run(_tools())
    actual = {
        tool.name: {
            "required": sorted(tool.inputSchema.get("required", [])),
            "properties": {
                name: _property_contract(schema)
                for name, schema in tool.inputSchema.get("properties", {}).items()
            },
        }
        for tool in tools
    }
    failures += _common.check(
        "MCP tool 名稱、參數型別、required/default 符合公開契約 snapshot",
        actual == EXPECTED,
        "\n" + json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True),
    )
    missing_descriptions = [tool.name for tool in tools if not (tool.description or "").strip()]
    missing_parameter_descriptions = [
        f"{tool.name}.{name}"
        for tool in tools
        for name, schema in tool.inputSchema.get("properties", {}).items()
        if not (schema.get("description") or "").strip()
    ]
    failures += _common.check(
        "所有公開 tool 與參數保有供 MCP client 使用的說明",
        not missing_descriptions and not missing_parameter_descriptions,
        f"tools={missing_descriptions}, params={missing_parameter_descriptions}",
    )
    print("=" * 50)
    print("MCP 公開契約測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
