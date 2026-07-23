"""Helpers for mounting the MCP server as a stdio subprocess."""
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

# Public tool names used by the registration guard.
EXPECTED_TOOLS = {
    "uof_custom_check_auth",
    "uof_custom_get_form_list",
    "uof_custom_get_external_form_list",
    "uof_custom_get_form_structure",
    "uof_custom_get_form_structure_by_id",
    "uof_custom_preview_workflow",
    "uof_custom_apply_form",
    "uof_custom_get_task_data",
    "uof_custom_get_task_result",
    "uof_custom_get_pending_sign_list",
    "uof_custom_get_dialog_structure",
    "uof_custom_search_dialog_options",
    "uof_custom_operate_dialog",
    "uof_custom_terminate_task",
    "uof_custom_query_forms",
    "uof_custom_search_users",
    "uof_custom_sign_next",
}


def env_for(account: str, dotenv: dict, password=None, home=None) -> dict:
    """Build the explicit environment passed to a mounted stdio server."""
    env = {**os.environ, **dotenv, "UOF_ACCOUNT": account}
    if password is not None:
        env["UOF_PASSWORD"] = password
    if home is not None:
        env["HOME"] = home
    env["PYTHONPATH"] = str(_common.SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")
    return env


def params_for(account: str, dotenv: dict, password=None, home=None) -> StdioServerParameters:
    return StdioServerParameters(
        command=_common.PYTHON,
        args=["-m", "mcp_uof.server"],
        env=env_for(account, dotenv, password, home),
        cwd=str(_common.ROOT),
    )


@asynccontextmanager
async def mounted_session(account: str, dotenv: dict, password=None, home=None):
    """掛載一個身份的 server 子程序，yield 已 initialize 的 ClientSession（離開時自動收尾子程序）。"""
    async with stdio_client(params_for(account, dotenv, password, home)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def text(result) -> str:
    return result.content[0].text if result.content else ""


async def call(session, name: str, args: dict = None) -> str:
    """呼叫一個工具並回傳純文字（本專案所有工具回 str）。"""
    return text(await session.call_tool(name, args or {}))


async def tool_names(session) -> set:
    return {t.name for t in (await session.list_tools()).tools}
