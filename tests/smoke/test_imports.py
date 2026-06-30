"""
Smoke — 模組匯入（離線；無網路 / 無 UOF / 無子程序）。

自動探索 src/mcp_uof 下所有模組並逐一 import，確保語法、相依、循環匯入皆無誤。
改用自動探索（而非手動清單）以避免與實際套件結構漂移——例如新增 domain 或漏列 sse_server。
注意：import ops.web 不會載入 Playwright（lazy，至 WebBackend 實例化才載），故此測試無需瀏覽器。

執行：uv run python tests/smoke/test_imports.py
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/ — 供 import _common
import _common

_common.ensure_src_on_path()


def discover_modules() -> list:
    pkg_root = _common.SRC / "mcp_uof"
    mods = []
    for py in sorted(pkg_root.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        rel = py.relative_to(_common.SRC).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        mods.append(".".join(parts))
    return mods


def main() -> int:
    modules = discover_modules()
    errors = []
    for m in modules:
        try:
            importlib.import_module(m)
            print(f"  ✅ {m}")
        except Exception as e:
            print(f"  ❌ {m}: {e}")
            errors.append((m, str(e)))

    print("=" * 50)
    if errors:
        print(f"❌ {len(errors)}/{len(modules)} 個模組匯入失敗")
        return len(errors)
    print(f"✅ 所有 {len(modules)} 個模組匯入成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
