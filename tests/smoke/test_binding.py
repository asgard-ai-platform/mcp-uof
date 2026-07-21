"""Offline smoke checks for operation registration, routing, and authentication guards."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

_common.ensure_src_on_path()

import mcp_uof.auth.base as ab
from mcp_uof.ops import get_backend
from mcp_uof.ops.base import OpsBackend
from mcp_uof.ops.router import OpsRouter, BINDING, mechanisms_for

# All currently registered operations use the HTTP web backend.
EXPECTED = {op: "http_web" for op in (
    "check_auth", "get_form_list", "get_external_form_list",
    "get_form_structure", "get_form_structure_by_id",
    "get_task_data", "get_task_result", "get_pending_sign_list", "get_dialog_structure", "search_dialog_options", "operate_dialog",
    "preview_workflow", "apply_form", "terminate_task",
    "sign_next", "query_forms", "search_users",
)}


def main() -> int:
    failures = 0

    # ── 1) BINDING 靜態表本身 ──────────────────────────────────────
    b = get_backend()
    failures += _common.check("get_backend() 回傳 OpsRouter", isinstance(b, OpsRouter), type(b).__name__)
    failures += _common.check("BINDING 內容符合預期（全 http_web）", BINDING == EXPECTED,
                               str(set(EXPECTED.items()) ^ set(BINDING.items())))

    ops_methods = {n for n, v in vars(OpsBackend).items()
                   if getattr(v, "__isabstractmethod__", False)}
    failures += _common.check("BINDING 鍵集 = OpsBackend 抽象方法集（17）", set(BINDING) == ops_methods,
                               f"差異={set(BINDING) ^ ops_methods}")

    # ── 2) router 委派到 HttpWebBackend（惰性建立，不連線）──────────
    failures += _common.check("router.http_web 是 HttpWebBackend",
                               type(b.http_web).__name__ == "HttpWebBackend", type(b.http_web).__name__)

    # ── 3) 認證閘：驗 session、未知 op fail-loud、例外傳遞 ──
    calls = []

    class OK:
        def __init__(self, n): self.n = n
        def ensure_valid(self): calls.append(self.n)

    class Bad:
        def __init__(self, n): self.n = n
        def ensure_valid(self): raise RuntimeError(f"{self.n} 認證失敗")

    def make(name):
        def f(*args, **kwargs): return "OK"
        f.__name__ = name
        return ab.require_auth(f)

    def make_raising(name):
        def f(*args, **kwargs): raise ValueError("tool exploded")
        f.__name__ = name
        return ab.require_auth(f)

    # 3a) 未知 op 必須 fail-loud（漏綁/改名時立刻爆，不靜默）
    try:
        mechanisms_for("__nonexistent_op__")
        failures += _common.check("未知 op fail-loud", False, "mechanisms_for 未拋錯（靜默了）")
    except KeyError:
        failures += _common.check("未知 op fail-loud（mechanisms_for 拋 KeyError）", True)

    # 3b) 正常：每個工具都驗 session provider
    ab.get_session_provider = lambda: OK("session")
    for name in ("uof_custom_query_forms", "uof_custom_apply_form",
                 "uof_custom_terminate_task", "uof_custom_get_form_list"):
        calls.clear()
        r = make(name)()
        failures += _common.check(f"{name} → 驗 session", calls == ["session"] and r == "OK", f"驗到 {calls}")

    # 3c) session 認證失敗 → 回固定失敗訊息（🔒）
    ab.get_session_provider = lambda: Bad("session")
    calls.clear()
    r = make("uof_custom_query_forms")()
    failures += _common.check("session 失敗 → 回登入失敗訊息", "🔒" in r or "登入失敗" in r, r[:40])

    # 3d) 認證成功後，工具本體例外應原樣拋出，不可被包成「登入失敗」
    ab.get_session_provider = lambda: OK("session")
    calls.clear()
    try:
        make_raising("uof_custom_terminate_task")()
        failures += _common.check("工具本體例外不被吞掉", False, "未拋出例外")
    except ValueError as e:
        failures += _common.check("工具本體例外不被吞掉", str(e) == "tool exploded" and calls == ["session"],
                                   f"calls={calls}, e={e}")

    ab.reset_provider_for_tests()  # 還原快取
    print("=" * 50)
    print("機制綁定測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
