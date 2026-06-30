"""網頁起單子系統：共用接口 → 單種分派 → 逐單 handler。

網頁起單沒有獨立對外工具：起單一律經 server 的 `apply_form`，它用 router.resolve 查設計期登錄表
決定走 web handler 還是 SOAP 中介（對使用者透明）。設計與強韌性規則見 docs/web-apply-design.md。
"""
from .router import resolve, resolve_version, describe, describe_version, apply_web, resolve_error_message

__all__ = ["resolve", "resolve_version", "describe", "describe_version", "apply_web", "resolve_error_message"]
