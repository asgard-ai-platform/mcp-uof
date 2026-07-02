"""
Smoke — 工具→機制綁定（離線；純函式、零網路）。

本專案**沒有「模式」**：每個工具用 SOAP 還是 web，是開發期決定、寫死在 `ops.router.BINDING`
的靜態綁定，對使用者透明。本測試守住這張綁定表與 router 委派的正確性，並確認對外是單一 OpsRouter。

執行：uv run python tests/smoke/test_routing.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

_common.ensure_src_on_path()

from mcp_uof.ops import get_backend
from mcp_uof.ops.router import OpsRouter, BINDING
from mcp_uof.ops.base import OpsBackend

# 預期綁定：query_forms→http_web（UOF PublicAPI 無清單/搜尋 API），其餘 11 個→soap。
EXPECTED = {
    "check_auth": "soap",
    "get_form_list": "soap",
    "get_external_form_list": "soap",
    "get_form_structure": "soap",
    "get_form_structure_by_id": "soap",
    "get_task_data": "soap",
    "get_task_result": "soap",
    "preview_workflow": "soap",
    "apply_form": "soap",
    "terminate_task": "soap",
    "sign_next": "soap",
    "query_forms": "http_web",
    "search_users": "http_web",
}


def main() -> int:
    failures = 0

    def check(label, cond, detail=""):
        nonlocal failures
        if cond:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
            failures += 1

    # 1) 對外是單一 OpsRouter（使用者只面對工具，不選機制）
    b = get_backend()
    check("get_backend() 回傳 OpsRouter", isinstance(b, OpsRouter), type(b).__name__)

    # 2) 綁定表 = 預期
    check("BINDING 內容符合預期", BINDING == EXPECTED, str(set(EXPECTED.items()) ^ set(BINDING.items())))

    # 3) 綁定鍵 = OpsBackend 全部對外操作（防漏綁/改名/多綁）
    ops_methods = {n for n, v in vars(OpsBackend).items()
                   if getattr(v, "__isabstractmethod__", False)}
    check("BINDING 鍵集 = OpsBackend 抽象方法集（13）", set(BINDING) == ops_methods,
          f"差異={set(BINDING) ^ ops_methods}")

    # 4) router 依綁定委派到正確機制（離線，不連線）
    check("query_forms 委派 HttpWebBackend", type(b._mech("query_forms")).__name__ == "HttpWebBackend")
    check("search_users 委派 HttpWebBackend", type(b._mech("search_users")).__name__ == "HttpWebBackend")
    check("apply_form 委派 SoapBackend", type(b._mech("apply_form")).__name__ == "SoapBackend")
    check("check_auth 委派 SoapBackend", type(b._mech("check_auth")).__name__ == "SoapBackend")

    print("=" * 50)
    print("綁定測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
