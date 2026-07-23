"""Operations facade and registry for the current HTTP web backend."""
from __future__ import annotations

from typing import Optional

from .base import OpsBackend

# 工具登記表；目前 OpsRouter 的所有操作都委派給 http_web。
BINDING = {
    "check_auth": "http_web",
    "get_form_list": "http_web",
    "get_external_form_list": "http_web",
    "get_form_structure": "http_web",
    "get_form_structure_by_id": "http_web",
    "get_task_data": "http_web",
    "get_task_result": "http_web",
    "get_pending_sign_list": "http_web",
    "get_dialog_structure": "http_web",
    "search_dialog_options": "http_web",
    "operate_dialog": "http_web",
    "preview_workflow": "http_web",
    "apply_form": "http_web",
    "terminate_task": "http_web",
    "sign_next": "http_web",
    "query_forms": "http_web",
    "search_users": "http_web",
}


def mechanisms_for(op: str) -> list:
    """某工具設計上可走的機制清單，供入口認證閘判斷。

    **未知 op 直接報錯（fail-loud）**，與 `_route` 的 `BINDING[op]` 一致：避免漏綁/改名時靜默
    退回某機制。`require_auth` 在裝飾期就會呼叫本函式驗證，因此這類錯誤在 import server 時即爆。
    """
    if op not in BINDING:
        raise KeyError(
            f"工具 op {op!r} 未在 ops/router.py 的 BINDING 登錄機制綁定。"
            "新增工具請補上綁定；工具改名請同步更新 BINDING。"
        )
    return [BINDING[op]]


class OpsRouter(OpsBackend):
    """將 OpsBackend 操作委派給目前唯一的 HttpWebBackend。"""

    def __init__(self) -> None:
        self._http_web = None

    # ── 機制（惰性、單例）──────────────────────────────────────────
    @property
    def http_web(self) -> OpsBackend:
        if self._http_web is None:
            from .http_web import HttpWebBackend
            self._http_web = HttpWebBackend()
        return self._http_web

    def _route(self, op: str, *args, **kwargs) -> str:
        return getattr(self.http_web, op)(*args, **kwargs)

    # ── 對外操作 ────────────────────────────────────────────────────
    def check_auth(self) -> str:
        """就緒檢查：回報網頁 session 認證狀態（一個程序 = 一個 UOF_ACCOUNT）。"""
        return self._route("check_auth")

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

    def get_pending_sign_list(self) -> str:
        return self._route("get_pending_sign_list")

    def get_dialog_structure(self, form_version_id: str, field_code: str = "") -> str:
        return self._route("get_dialog_structure", form_version_id, field_code)

    def search_dialog_options(self, form_version_id: str, field_code: str,
                              keyword: str = "", limit: int = 20) -> str:
        return self._route("search_dialog_options", form_version_id, field_code, keyword, limit)

    def operate_dialog(self, form_version_id: str, field_code: str,
                       values: Optional[dict] = None, press: str = "") -> str:
        return self._route("operate_dialog", form_version_id, field_code, values, press)

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
        self, keyword: str = "", date_from: str = "", date_to: str = "",
        max_results: int = 50, query_mode: str = "apply",
    ) -> str:
        return self._route("query_forms", keyword, date_from, date_to, max_results, query_mode)

    def search_users(self, keyword: str) -> str:
        return self._route("search_users", keyword)
