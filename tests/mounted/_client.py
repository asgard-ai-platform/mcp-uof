"""
mounted/_client.py — 把 MCP server 當「真實 OS 子程序」掛載的樣板。

與 Claude Desktop / VS Code 在 mcp.json 綁定的執行路徑位元級一致：
  command=sys.executable, args=["-m","mcp_uof.server"], env=身份, cwd=repo根
經官方 SDK 的 stdio_client + ClientSession 走 stdio JSON-RPC。
身份只由注入的 env 決定（一份設定 = 一個身份）。
"""
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

# 對外工具固定名稱集合（共 12 個）。供註冊護欄斷言。
# 工具是唯一對外面向；每個工具用哪種機制（SOAP/web）對使用者透明：多數由 ops.router 的 BINDING
# 在開發期綁定；起單(apply_form/preview/get_form_structure)則再依 ops/web_apply 的設計期登錄表
# 對「網頁起單的單種(如採購單)」內部分派到 web handler，否則走 SOAP 中介——使用者一律只呼叫同一個工具。
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
    "uof_custom_terminate_task",
    "uof_custom_query_forms",
    "uof_custom_sign_next",
}


def env_for(account: str, dotenv: dict, password=None, home=None) -> dict:
    """模擬 mcp.json 的 env 區塊：注入身份與設定（沒有 mode 可設——機制是內部決定）。

    重點：MCP SDK 對 stdio 子程序只繼承白名單環境變數（HOME/PATH/…），UOF_* 不會自動帶過去。
    這裡直接灌 os.environ 全集 + .env，再覆寫 UOF_ACCOUNT（必要時覆寫密碼以測負向認證）。
    home：覆寫 HOME 以指向一個全新的憑證快取目錄（~/.uof）。負向認證測試需要它，否則子程序會
    命中先前真實執行留下的快取 token，使 require_auth 即使密碼錯也通過。
    """
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
