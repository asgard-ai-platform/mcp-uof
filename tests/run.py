"""
tests/run.py — 兩層測試統一入口（無 pytest）。

用法：
    uv run python tests/run.py [smoke|mounted|all]

不帶參數 = all。每支測試以獨立子程序執行（隔離模組級單例與環境變數），彙總 exit code。

exit code 約定：
    0        全數通過
    77       SKIP（缺少執行條件，例如 mounted 沒有真實環境設定）——不算失敗，但**必須**與
             PASS 分開顯示：把 skip 顯示成 PASS 會讓人誤以為那一層已經驗過了。
    其他非 0  失敗項數
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent  # tests/
PYTHON = sys.executable
SKIP_CODE = 77

TIERS = {
    "smoke": ["smoke/test_imports.py", "smoke/test_binding.py", "smoke/test_mcp_contract.py",
              "smoke/test_webforms_runtime.py", "smoke/test_http_web_regressions.py",
              "smoke/test_plugin_detail_rows.py", "smoke/test_detail_operation.py",
              "smoke/test_form_submission_objects.py", "smoke/test_apply_form_web.py",
              "smoke/test_task_lifecycle.py", "smoke/test_session_lifecycle.py",
              "smoke/test_session_store.py", "smoke/test_browser_login.py",
              "smoke/test_auth_flow.py"],
    "mounted": ["mounted/test_mcp_stdio.py"],
}
ORDER = ["smoke", "mounted"]


def run_file(rel: str) -> int:
    print("\n" + "#" * 64, flush=True)
    print(f"# {rel}", flush=True)
    print("#" * 64, flush=True)
    return subprocess.run([PYTHON, str(ROOT / rel)]).returncode


def summarize(codes: list) -> tuple:
    """回傳 (顯示字串, 失敗數)。全 skip → SKIP；有失敗 → 失敗數；其餘 → PASS。"""
    failures = sum(c for c in codes if c not in (0, SKIP_CODE))
    if failures:
        return f"❌ {failures} 失敗", failures
    if codes and all(c == SKIP_CODE for c in codes):
        return "⏭️  SKIP — 未執行，缺少執行條件", 0
    if SKIP_CODE in codes:
        return "✅ PASS（部分 SKIP）", 0
    return "✅ PASS", 0


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    if arg == "all":
        tiers = ORDER
    elif arg in TIERS:
        tiers = [arg]
    else:
        print(f"用法：python tests/run.py [smoke|mounted|all]（得到 {arg!r}）")
        return 2

    results = {tier: [run_file(rel) for rel in TIERS[tier]] for tier in tiers}

    print("\n" + "=" * 64, flush=True)
    print("總結", flush=True)
    total = 0
    for tier in tiers:
        label, failures = summarize(results[tier])
        total += failures
        print(f"  {tier:8} {label}", flush=True)
    print("=" * 64, flush=True)
    return total


if __name__ == "__main__":
    sys.exit(main())
