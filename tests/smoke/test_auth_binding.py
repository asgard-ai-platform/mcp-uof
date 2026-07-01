"""
Smoke — 認證跟著工具的機制走（離線；純函式、零網路）。

守住修正後的入口認證閘 `require_auth`：依「該工具設計綁定的機制」驗對應認證——
SOAP 工具驗 token、web 工具（`query_forms`）驗 session，**不是一律驗 SOAP token**。
多機制時採 OR（任一通過即放行）；全部不過才回失敗訊息。

（這對應 PR reviewer 指出的 blocking：query_forms 之前被 SOAP token 前置擋住。）

執行：uv run python tests/smoke/test_auth_binding.py
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

_common.ensure_src_on_path()

import mcp_uof.auth.base as ab
from mcp_uof.ops.router import mechanisms_for
from mcp_uof.ops.web_apply.registry import resolve as resolve_web_apply
from mcp_uof.ops.web_apply.router import VersionResolveError, resolve_version


def main() -> int:
    failures = 0
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

    def check(label, cond, detail=""):
        nonlocal failures
        if cond:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
            failures += 1

    # 1) 綁定表（供認證閘判斷）
    check("mechanisms_for(query_forms) = ['http_web']", mechanisms_for("query_forms") == ["http_web"])
    check("mechanisms_for(apply_form) = ['soap']", mechanisms_for("apply_form") == ["soap"])
    check("mechanisms_for(get_form_list) = ['soap']", mechanisms_for("get_form_list") == ["soap"])

    # 1b) 未知 op 必須 fail-loud（不可靜默退回 soap，否則漏綁/改名時 web 工具會被 token 擋）
    try:
        mechanisms_for("__nonexistent_op__")
        check("未知 op fail-loud", False, "mechanisms_for 未拋錯（靜默了）")
    except KeyError:
        check("未知 op fail-loud（mechanisms_for 拋 KeyError）", True)

    # 2) 正常：各工具只驗自己機制對應的 provider
    ab.get_token_provider = lambda: OK("token")
    ab.get_session_provider = lambda: OK("session")
    for name, expect in [
        ("uof_custom_query_forms", "session"),
        ("uof_custom_get_form_list", "token"),
        ("uof_custom_apply_form", "token"),
        ("uof_custom_terminate_task", "token"),
    ]:
        calls.clear()
        r = make(name)()
        check(f"{name} → 驗 {expect}", calls == [expect] and r == "OK", f"驗到 {calls}")

    # 2b) 起單相關工具對 registry 命中的表單，入口認證要跟著表單層分派走 web/session
    po_version = "00000000-0000-0000-0000-000000000002"
    po_form_id = "00000000-0000-0000-0000-000000000001"
    os.environ["UOF_WEB_APPLY_PURCHASE_ORDER_IDS"] = f"{po_form_id},{po_version}"
    check("測試資料：採購單 version 命中 web_apply registry", resolve_web_apply(po_version) is not None)
    check("測試資料：採購單 form_id 命中 web_apply registry", resolve_web_apply(po_form_id) is not None)
    for label, wrapped, call_args in [
        ("get_form_structure 採購單 → 驗 session", make("uof_custom_get_form_structure"), (po_version,)),
        ("apply_form 採購單 → 驗 session", make("uof_custom_apply_form"), (po_version, "u", "m", {})),
        ("preview_workflow 採購單 → 驗 session", make("uof_custom_preview_workflow"), (po_version, "u", "m")),
        ("get_form_structure_by_id 採購單 → 驗 session", make("uof_custom_get_form_structure_by_id"), (po_form_id,)),
    ]:
        calls.clear()
        r = wrapped(*call_args)
        check(label, calls == ["session"] and r == "OK", f"驗到 {calls}, r={r[:40]}")

    # 2c) version 類工具靜態未命中時先驗 session，讓 server 可用 ApplyFormList 反查是否 web_apply
    calls.clear()
    r = make("uof_custom_apply_form")("not-web-version", "u", "m", {})
    check("apply_form 未知 version → 先驗 session", calls == ["session"] and r == "OK", f"驗到 {calls}")

    # 2d) version 未靜態命中時先用 session 進入 server 反查 formId，支援無 PublicAPI 站台的 web_apply 表單
    ab.get_token_provider = lambda: Bad("token")
    ab.get_session_provider = lambda: OK("session")
    calls.clear()
    r = make("uof_custom_apply_form")("new-po-version", "u", "m", {})
    check("apply_form 未知 version → 先驗 session 讓 server 反查", r == "OK" and calls == ["session"], f"calls={calls}, r={r[:40]}")

    ab.get_token_provider = lambda: OK("token")
    ab.get_session_provider = lambda: OK("session")

    # 2e) ApplyFormList 反查不到 version 時必須 fail-loud，不可靜默退 SOAP
    import mcp_uof.ops.web as web
    old_get_web_runtime = web.get_web_runtime
    class EmptyRuntime:
        def form_id_for_version(self, form_version_id): return ""
    web.get_web_runtime = lambda: EmptyRuntime()
    try:
        try:
            resolve_version("missing-version")
            check("version 反查空 formId → fail-loud", False, "未拋 VersionResolveError")
        except VersionResolveError:
            check("version 反查空 formId → fail-loud", True)
    finally:
        web.get_web_runtime = old_get_web_runtime

    # 3) query_forms 的 session 失敗 → 回失敗訊息，且**不會**改去驗 token（不被 SOAP 影響）
    ab.get_token_provider = lambda: OK("token")
    ab.get_session_provider = lambda: Bad("session")
    calls.clear()
    r = make("uof_custom_query_forms")()
    check("query_forms session 失敗 → 回失敗訊息", "🔒" in r or "登入失敗" in r, r[:40])
    check("query_forms 不會改去驗 token", "token" not in calls, f"calls={calls}")

    # 4) 反向：SOAP 工具的 token 失敗時也不會去碰 session
    ab.get_token_provider = lambda: Bad("token")
    ab.get_session_provider = lambda: OK("session")
    calls.clear()
    r = make("uof_custom_get_form_list")()
    check("get_form_list token 失敗 → 回失敗訊息", "🔒" in r or "登入失敗" in r, r[:40])
    check("get_form_list 不會改去驗 session", "session" not in calls, f"calls={calls}")

    # 5) 認證成功後，工具本體例外應原樣拋出，不可被包成「登入失敗」
    ab.get_token_provider = lambda: OK("token")
    calls.clear()
    try:
        make_raising("uof_custom_get_form_list")()
        check("工具本體例外不被吞掉", False, "未拋出例外")
    except ValueError as e:
        check("工具本體例外不被吞掉", str(e) == "tool exploded" and calls == ["token"], f"calls={calls}, e={e}")

    ab.reset_provider_for_tests()  # 還原快取
    print("=" * 50)
    print("認證綁定測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
