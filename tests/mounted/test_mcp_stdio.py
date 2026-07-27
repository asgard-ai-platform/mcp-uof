"""Mounted read-only stdio JSON-RPC tests against the configured UOF environment."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_dotenv = _common.load_env()
if not _common.has_live_env():
    print(f"⏭️  跳過 mounted：缺少真實環境設定 {_common.missing_env()}（請設定 mcp-uof/.env）")
    sys.exit(77)

# 每段使用獨立 HOME，避免載入開發者 session，也確保壞密碼不會沿用先前的有效 cookie。
_RUN_HOME = tempfile.mkdtemp(prefix="uof-mounted-home-")
_NEG_HOME = tempfile.mkdtemp(prefix="uof-mounted-neg-")

from _client import EXPECTED_TOOLS, mounted_session, call, tool_names  # noqa: E402


async def run() -> int:
    account = os.environ["UOF_ACCOUNT"]
    failures = 0

    def check(label, cond, detail=""):
        nonlocal failures
        if cond:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
            failures += 1

    print("═" * 60)
    print("  1) 單一身份唯讀流程：認證、工具註冊與查詢")
    print("═" * 60)
    async with mounted_session(account, _dotenv, home=_RUN_HOME) as session:
        names = await tool_names(session)
        check(
            f"expose 剛好 {len(EXPECTED_TOOLS)} 個工具（得 {len(names)}）",
            names == EXPECTED_TOOLS,
            f"差異={names ^ EXPECTED_TOOLS}",
        )

        result = await call(session, "uof_custom_check_auth")
        check("check_auth 成功", _common.ok(result), result[:120])

        result = await call(session, "uof_custom_get_form_list")
        check(
            "get_form_list 回傳可用表單",
            _common.ok(result) and "formVersionId" in result,
            result[:120],
        )

        result = await call(session, "uof_custom_query_forms", {"max_results": 5})
        check(
            "query_forms 直接回傳查詢結果",
            _common.ok(result)
            and "查詢表單" in result
            and "切換" not in result
            and "不支援" not in result,
            result[:160],
        )

    print("\n" + "═" * 60)
    print("  2) 負向認證：壞密碼回固定狀態，不造成協定錯誤")
    print("═" * 60)
    async with mounted_session(
        account,
        _dotenv,
        password="__definitely_wrong__",
        home=_NEG_HOME,
    ) as session:
        result = await call(session, "uof_custom_check_auth")
        check("check_auth 回未登入狀態", "未登入" in result, result[:120])

        result = await call(session, "uof_custom_get_form_list")
        check("受保護工具回 🔒 字串", "🔒" in result, result[:120])

    print("\n" + "═" * 60)
    print("  3) 登入態管理：login、logout 與帳密備援")
    print("═" * 60)
    async with mounted_session(account, _dotenv, home=_RUN_HOME) as session:
        result = await call(session, "uof_custom_check_auth")
        check("操作前已登入", "✅" in result, result[:120])

        result = await call(session, "uof_custom_login")
        check("已登入時 login 不重開流程", "已經是登入狀態" in result, result[:120])

        result = await call(session, "uof_custom_logout")
        check("logout 回報成功", "✅" in result, result[:120])

        result = await call(session, "uof_custom_get_form_list")
        check(
            "logout 後以帳密備援自動重登",
            _common.ok(result) and "formVersionId" in result,
            result[:120],
        )

    print("\n" + "═" * 60)
    print("真實掛載 MCP 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    print("═" * 60)
    return failures


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
