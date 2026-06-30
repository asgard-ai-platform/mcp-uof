"""
Smoke — build_form_xml 的欄位序列化（離線；純函式、零網路）。

守住兩件事：
1. 一般欄位 → <FieldItem fieldId fieldValue>。
2. 明細(dataGrid)欄位（value 是「列的清單」）→ <FieldItem><DataGrid><Row order><Cell fieldId fieldValue></>。
   對應 UOF SendForm 文件 p51 的明細格式（apply_form 的明細支援靠這段）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_common.ensure_src_on_path()

from mcp_uof.domains.wkf.service import build_form_xml
from lxml import etree


def main() -> int:
    failures = 0

    def check(label, cond, detail=""):
        nonlocal failures
        if cond:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
            failures += 1

    xml = build_form_xml(
        "VER", "applicant", "signer",
        {
            "A001": "",                       # autoNumber：空字串
            "003": "客戶 & <他>",              # 一般欄位 + 需跳脫的特殊字元
            "004": [                           # 明細：兩列
                {"004_1": "品名A", "004_3": "5"},
                {"004_1": "品名B", "004_3": None},  # None → 空字串
            ],
        },
        comment="意見", urgent_level="2",
    )
    root = etree.fromstring(xml.encode("utf-8"))

    # 一般欄位
    items = root.findall(".//FormFieldValue/FieldItem")
    by_id = {e.get("fieldId"): e for e in items}
    check("一般欄位 003 以 fieldValue 帶值", by_id["003"].get("fieldValue") == "客戶 & <他>",
          "特殊字元未正確跳脫/還原")
    check("autoNumber A001 帶空字串", by_id.get("A001") is not None and by_id["A001"].get("fieldValue") == "")

    # 明細欄位
    detail = by_id["004"]
    check("明細欄位 004 不是用 fieldValue 平鋪", detail.get("fieldValue") is None)
    grid = detail.find("DataGrid")
    check("明細含 <DataGrid>", grid is not None)
    rows = grid.findall("Row") if grid is not None else []
    check("明細有 2 列", len(rows) == 2, f"得 {len(rows)}")
    if len(rows) == 2:
        check("第一列 order=0", rows[0].get("order") == "0")
        check("第二列 order=1", rows[1].get("order") == "1")
        cells0 = {c.get("fieldId"): c.get("fieldValue") for c in rows[0].findall("Cell")}
        check("列0 子欄位 004_1=品名A", cells0.get("004_1") == "品名A")
        check("列0 子欄位 004_3=5", cells0.get("004_3") == "5")
        cells1 = {c.get("fieldId"): c.get("fieldValue") for c in rows[1].findall("Cell")}
        check("列1 None → 空字串", cells1.get("004_3") == "")

    # 防呆：清單但元素「不是 dict」→ 不可當明細、且不可崩潰（當一般欄位處理）
    try:
        xml2 = build_form_xml("V", "a", "s", {"multi": ["X", "Y"]})
        r2 = etree.fromstring(xml2.encode("utf-8"))
        item = {e.get("fieldId"): e for e in r2.findall(".//FormFieldValue/FieldItem")}["multi"]
        check("非 dict 清單不被當明細(無 DataGrid)", item.find("DataGrid") is None)
        check("非 dict 清單以 fieldValue 平鋪(不崩潰)", item.get("fieldValue") is not None)
    except Exception as e:
        check("非 dict 清單不崩潰", False, f"{type(e).__name__}: {e}")

    print("=" * 50)
    print("build_form_xml 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
