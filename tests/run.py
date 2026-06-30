"""
tests/run.py — 三層測試統一入口（無 pytest）。

用法：
    uv run python tests/run.py [smoke|e2e|mounted|all]

不帶參數 = all。每支測試以獨立子程序執行（隔離模組級單例與環境變數），彙總 exit code。
真實層（e2e / mounted）需要 mcp-uof/.env 的真實 UOFTEST 設定；缺設定時各測試會自行 skip（不算失敗）。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent  # tests/
PYTHON = sys.executable

TIERS = {
    "smoke": ["smoke/test_imports.py", "smoke/test_routing.py", "smoke/test_auth_binding.py", "smoke/test_form_xml.py", "smoke/test_web_apply_dialog.py"],
    "e2e": ["e2e/test_wkf_purchase_order.py"],
    "mounted": ["mounted/test_mcp_stdio.py"],
}
ORDER = ["smoke", "e2e", "mounted"]


def run_file(rel: str) -> int:
    print("\n" + "#" * 64, flush=True)
    print(f"# {rel}", flush=True)
    print("#" * 64, flush=True)
    return subprocess.run([PYTHON, str(ROOT / rel)]).returncode


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    if arg == "all":
        tiers = ORDER
    elif arg in TIERS:
        tiers = [arg]
    else:
        print(f"用法：python tests/run.py [smoke|e2e|mounted|all]（得到 {arg!r}）")
        return 2

    results = {}
    for tier in tiers:
        results[tier] = sum(run_file(rel) for rel in TIERS[tier])

    print("\n" + "=" * 64, flush=True)
    print("總結", flush=True)
    for tier in tiers:
        print(f"  {tier:8} " + ("✅ PASS" if results[tier] == 0 else f"❌ {results[tier]} 失敗"), flush=True)
    print("=" * 64, flush=True)
    return sum(results.values())


if __name__ == "__main__":
    sys.exit(main())
