"""網頁起單的「設計期登錄結構」。

精神：哪些表單要走網頁起單(plugin 本體、SOAP 中介填不到)，是**部署時明確設定**的事，
不在 runtime 去打 SOAP 讀結構來猜。對應 ops/router.py 的 BINDING 精神
（單一決策點、明確分派）。其餘表單一律走 SOAP 中介起單。

key 同時收 form_id(代碼) 與該表單已知的 version 代號：起單工具拿到的 form_version_id 不論是
哪一種，都對得上。表單若改版、version 變動，更新這裡即可。
"""
from __future__ import annotations

import os
from typing import Optional

from .base import WebApplyHandler
from .purchase_order import PurchaseOrderWebApplyHandler


class FormApplyEntry:
    """一筆登錄：某表單 → 用哪個 web handler 起單、導航用的表單名稱、可填欄位說明。"""

    def __init__(self, handler: WebApplyHandler, form_name: str, ids: set[str]) -> None:
        self.handler = handler
        self.form_name = form_name
        self.ids = {i.lower() for i in ids}


# 共用一個採購單 handler 實例（無狀態，可重用）。
_PO = PurchaseOrderWebApplyHandler()

def _configured_registry() -> list[FormApplyEntry]:
    ids = {
        i.strip()
        for i in os.getenv("UOF_WEB_APPLY_PURCHASE_ORDER_IDS", "").split(",")
        if i.strip()
    }
    if not ids:
        return []
    return [
        FormApplyEntry(
            _PO,
            form_name=os.getenv("UOF_WEB_APPLY_PURCHASE_ORDER_NAME", "採購單"),
            ids=ids,
        )
    ]


def resolve(form_id_or_version: Optional[str]) -> Optional[FormApplyEntry]:
    """依 form_id 或 version 找對應的網頁起單 handler；找不到回 None（=該走 SOAP 中介）。"""
    if not form_id_or_version:
        return None
    key = form_id_or_version.lower()
    for e in _configured_registry():
        if key in e.ids:
            return e
    return None
