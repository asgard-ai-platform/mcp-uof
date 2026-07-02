"""WebApplyHandler — 逐單種的網頁起單處理器介面。

對外只有一個共用 MCP 工具；它判斷單種後分派到 apply_web（見 router.py）。
router.py 透過 httpx（ops.http_web.HttpSession.apply_form_web）提交；
handler 的 describe() / validate() 在送出前執行，fill_and_submit 已不再使用。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class WebApplyHandler(ABC):
    #: 單種代號（log / 回報用）
    form_kind: str = ""

    @abstractmethod
    def describe(self) -> str:
        """回傳這種單的「可填欄位說明」，供 get_form_structure 對外呈現（讓 agent 知道 fields 帶什麼）。

        單種與機制是設計期決定、對使用者透明的；此說明只談「能填什麼」，不談背後走 web/SOAP。"""

    def validate(self, payload: dict) -> Optional[str]:
        """送出前的 payload 健檢（不開瀏覽器）。回錯誤訊息字串＝擋下；回 None＝通過。預設不檢查。"""
        return None

    @abstractmethod
    def fill_and_submit(
        self, page: Any, form_name: str, payload: dict, dry_run: bool,
    ) -> dict:
        """在 page 上完成此單種的填寫（dry_run=True 時填到送出前即停）。

        回傳 dict：{ok, dry_run, filled:{...}, task_id, form_number, reason}。
        必須遵守 docs/web-apply-design.md 的強韌性規則（poll-until-state、失敗就停不送半張）。
        """
