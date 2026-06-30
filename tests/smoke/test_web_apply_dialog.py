"""
Smoke — 網頁起單的對話框 handler 只裝一次（離線；純函式、零網路、不開瀏覽器）。

守住一個實機踩到的 bug：WebRuntime 的 page 是長壽單例，若每次起單都 page.on("dialog",...)，
handler 會累加，之後一個對話框被多個 handler 各 accept 一次 → Playwright「Cannot accept dialog
which is already handled!」整個起單崩潰（長時間運行的 Desktop server 多次起單後必中）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _common

_common.ensure_src_on_path()

from mcp_uof.ops.web_apply.purchase_order import PurchaseOrderWebApplyHandler
from mcp_uof.server import _web_apply_identity_error


class _FakePage:
    def __init__(self):
        self.on_events = []

    def on(self, event, fn):
        self.on_events.append(event)


def main() -> int:
    failures = 0

    def check(label, cond, detail=""):
        nonlocal failures
        if cond:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
            failures += 1

    h = PurchaseOrderWebApplyHandler()
    page = _FakePage()

    m1 = h._ensure_dialog_handler(page)
    m1.append("dummy-msg")
    m2 = h._ensure_dialog_handler(page)   # 第二次起單：不應再裝 handler
    h._ensure_dialog_handler(page)        # 第三次

    check("dialog handler 只註冊一次（多次起單不累加）",
          page.on_events.count("dialog") == 1, f"註冊次數={page.on_events.count('dialog')}")
    check("每次起單清空訊息清單", m2 == [], f"得 {m2}")
    check("回傳的是同一個持久清單", m1 is m2)

    err = h.validate({"supplier": "C000007", "details": [{"qty": 1}]})
    check("採購單缺 subject → fail-fast", err is not None and "subject" in err, str(err))
    err = h.validate({"subject": "測試", "supplier": "C000007", "details": [{"qty": 1}]})
    check("採購單明細缺 item_code → fail-fast", err is not None and "item_code" in err, str(err))
    err = h.validate({"subject": "測試", "supplier": "C000007", "details": [{"item_code": "A001", "qty": 1}]})
    check("採購單明細有 item_code → validate 通過", err is None, str(err))

    import os
    old_account = os.environ.get("UOF_ACCOUNT")
    os.environ["UOF_ACCOUNT"] = "applicant_account"
    try:
        check("web_apply 身份一致 → 放行", _web_apply_identity_error("applicant_account") == "")
        err = _web_apply_identity_error("other_user")
        check("web_apply 身份不一致 → 擋下", "other_user" in err and "applicant_account" in err and err.startswith("❌"), err)
    finally:
        if old_account is None:
            os.environ.pop("UOF_ACCOUNT", None)
        else:
            os.environ["UOF_ACCOUNT"] = old_account

    print("=" * 50)
    print("web_apply dialog handler 測試完成" + (f"（{failures} 項失敗）" if failures else "（全數通過）"))
    return failures


if __name__ == "__main__":
    sys.exit(main())
