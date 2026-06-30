"""
OpsRouter — 對外唯一的操作面；每個工具於「開發期」靜態綁定到一種機制。

使用者（與 agent）只面對「有哪些工具」，看不到、也不需要選擇機制。綁定原則由開發者在實作時決定：
能用 SOAP / PublicAPI 做的就用 SOAP；SOAP 沒有該能力的（目前只有清單/搜尋類 `query_forms`，
因為 UOF 一代 PublicAPI 沒有列出/搜尋表單的 API）才改用 web（Playwright 驅動網頁）。

下方 `BINDING` 就是「用哪種方式取得資料」的**唯一決策點**，對使用者完全透明——不會有任何
「請切換模式」這種把實作細節丟回給使用者的行為。要新增或改綁一個工具的機制，只改這張表。

機制實作（SoapBackend / WebBackend）惰性建立並快取；各自取得所需認證（SOAP→token、web→session），
共用同一身份（一個程序 = 一個 UOF_ACCOUNT）。
"""
from __future__ import annotations

from typing import Optional

from .base import OpsBackend

# 每工具 → 機制。唯一決策點（開發期決定）。
BINDING = {
    "check_auth": "soap",
    "get_form_list": "soap",
    "get_external_form_list": "soap",
    "get_form_structure": "soap",
    "get_form_structure_by_id": "soap",
    "get_task_data": "soap",
    "get_task_result": "soap",
    "preview_workflow": "soap",
    "apply_form": "soap",
    "terminate_task": "soap",
    "sign_next": "soap",
    "query_forms": "web",      # UOF PublicAPI 無清單/搜尋 API → 透明改用網頁
}


def mechanisms_for(op: str) -> list:
    """某工具設計上可走的機制清單（SOAP 優先），供入口認證閘判斷。

    認證以此採 OR：任一機制的認證通過即可用。目前每個工具單一機制（即 BINDING 那一個），
    清單只有一個元素；未來某工具若可 fallback（SOAP→web），在此回多個、SOAP 在前即可，
    入口閘會自動變成「token 不行就看 session」。

    **未知 op 直接報錯（fail-loud）**，與 `_route` 的 `BINDING[op]` 一致：避免漏綁/改名時靜默
    退回 soap，讓 web 工具（如 query_forms）被 SOAP token 前置擋住而靜默回歸。`require_auth`
    在裝飾期就會呼叫本函式驗證，因此這類錯誤在 import server 時即爆，不會等到執行期。
    """
    if op not in BINDING:
        raise KeyError(
            f"工具 op {op!r} 未在 ops/router.py 的 BINDING 登錄機制綁定。"
            "新增工具請補上綁定；工具改名請同步更新 BINDING。"
        )
    return [BINDING[op]]


class OpsRouter(OpsBackend):
    """實作 OpsBackend 的 12 個操作；每個依 BINDING 委派到對應機制。"""

    def __init__(self) -> None:
        self._soap = None
        self._web = None

    # ── 機制（惰性、單例）──────────────────────────────────────────
    @property
    def soap(self) -> OpsBackend:
        if self._soap is None:
            from .soap import SoapBackend
            self._soap = SoapBackend()
        return self._soap

    @property
    def web(self) -> OpsBackend:
        if self._web is None:
            from .web import WebBackend
            self._web = WebBackend()
        return self._web

    def _mech(self, op: str) -> OpsBackend:
        return self.web if BINDING[op] == "web" else self.soap

    def _route(self, op: str, *args, **kwargs) -> str:
        return getattr(self._mech(op), op)(*args, **kwargs)

    # ── 對外 12 操作（簽名同 OpsBackend；一律經 _route 依 BINDING 委派）──
    def check_auth(self) -> str:
        """跨機制的「就緒檢查」：分別回報 SOAP(token) 與網頁(session) 兩條認證的狀態，**互相獨立**。

        兩條認證各自獨立——即使一條失敗，另一條若正常，走它的工具仍可用（例如 SOAP 拿不到 token 時，
        query_forms 仍可靠 web session 運作）。因此兩邊都呈現、不短路，讓使用者一眼看出哪些工具可用。
        兩段都直接委派各自 provider 的 status_report（單一來源，避免兩套狀態文案各自維護）。"""
        from ..auth.base import get_session_provider
        token_report = self._route("check_auth")               # SOAP token：soap.check_auth → token status_report
        session_report = get_session_provider().status_report()  # 網頁 session：直接委派 session status_report
        return f"{token_report}\n\n{session_report}"

    def get_form_list(self) -> str:
        return self._route("get_form_list")

    def get_external_form_list(self) -> str:
        return self._route("get_external_form_list")

    def get_form_structure(self, form_version_id: str) -> str:
        return self._route("get_form_structure", form_version_id)

    def get_form_structure_by_id(self, form_id: str) -> str:
        return self._route("get_form_structure_by_id", form_id)

    def get_task_data(self, task_id: str) -> str:
        return self._route("get_task_data", task_id)

    def get_task_result(self, task_id: str, include_form_data: bool = True) -> str:
        return self._route("get_task_result", task_id, include_form_data)

    def preview_workflow(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: Optional[dict] = None,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str:
        return self._route(
            "preview_workflow", form_version_id, applicant_account,
            first_signer_account, fields, comment, urgent_level,
        )

    def apply_form(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: dict,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str:
        return self._route(
            "apply_form", form_version_id, applicant_account,
            first_signer_account, fields, comment, urgent_level,
        )

    def terminate_task(self, task_id: str, result: str, reason: str) -> str:
        return self._route("terminate_task", task_id, result, reason)

    def sign_next(self, task_id: str, site_id: str, node_seq: int, signer_guid: str) -> str:
        return self._route("sign_next", task_id, site_id, node_seq, signer_guid)

    def query_forms(
        self, keyword: str = "", date_from: str = "", date_to: str = "", max_results: int = 50
    ) -> str:
        return self._route("query_forms", keyword, date_from, date_to, max_results)
