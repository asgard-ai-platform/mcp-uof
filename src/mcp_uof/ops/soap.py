"""
SoapBackend — wraps the existing SOAP-based service functions.

This is a thin adapter: every method here fetches the bearer token from the active
TokenAuthProvider, then forwards to the corresponding function in domains/wkf/service.py
(unchanged). Behaviour is identical to mcp-uof v0.1.
"""
from __future__ import annotations

import os
from typing import Optional

from ..auth.base import get_token_provider
from ..domains.wkf import service as wkf
from .base import OpsBackend


def _token(force_refresh: bool = False) -> str:
    return get_token_provider().fetch_token(force_refresh=force_refresh)


class SoapBackend(OpsBackend):
    def _call(self, fn, *args, retry_on_stale: bool = True) -> str:
        """以目前 Token 呼叫 service 函式。

        `retry_on_stale=True`（**讀取類**）：UOF Token 伺服器端有效期可能短於本地快取，失效時
        Wkf.asmx 回 HTTP 500（無明確過期訊息），service 會轉成含「500 Internal Server Error」
        的字串。偵測到就強制重新 GetToken 再跑一次，避免使用者卡在過期 Token。

        `retry_on_stale=False`（**寫入類** apply_form / terminate_task / sign_next）：**不自動重試**。
        500 子字串無法分辨「token 在執行前就被拒（無副作用，重試安全）」與「操作已在伺服器端生效後
        才 500」（例：結案觸發下游而下游 500）。這些寫入沒有 idempotency 保護，盲目重跑會造成重複
        起單/結案/簽核。又因 require_auth 已在進工具前剛驗過 token，寫入當下 token 仍新鮮，幾乎不會
        遇到 stale-token；真失敗就回錯誤，讓呼叫端查狀態後自行決定，比重送安全。
        """
        result = fn(_token(), *args)
        if retry_on_stale and isinstance(result, str) and (
            "500 Internal Server Error" in result or result.startswith("🔒")
        ):
            result = fn(_token(force_refresh=True), *args)
        return result

    # ── System ──────────────────────────────────────────────────────
    def check_auth(self) -> str:
        return get_token_provider().status_report()

    # ── WKF reads ───────────────────────────────────────────────────
    def get_form_list(self) -> str:
        return self._call(wkf.get_form_list)

    def get_external_form_list(self) -> str:
        return self._call(wkf.get_external_form_list)

    def get_form_structure(self, form_version_id: str) -> str:
        return self._call(wkf.get_form_structure, form_version_id)

    def get_form_structure_by_id(self, form_id: str) -> str:
        return self._call(wkf.get_form_structure_by_form_id, form_id)

    def get_task_data(self, task_id: str) -> str:
        return self._call(wkf.get_task_data, task_id)

    def get_task_result(self, task_id: str, include_form_data: bool = True) -> str:
        return self._call(wkf.get_task_result, task_id, include_form_data)

    # ── WKF writes ──────────────────────────────────────────────────
    def preview_workflow(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: Optional[dict] = None,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str:
        return self._call(
            wkf.preview_workflow_structured,
            form_version_id,
            applicant_account,
            first_signer_account,
            fields,
            comment,
            urgent_level,
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
        return self._call(
            wkf.apply_form_structured,
            form_version_id,
            applicant_account,
            first_signer_account,
            fields,
            comment,
            urgent_level,
            retry_on_stale=False,   # 寫入：不自動重試，避免重複起單
        )

    def terminate_task(self, task_id: str, result: str, reason: str) -> str:
        return self._call(
            wkf.terminate_task,
            task_id,
            os.getenv("UOF_ACCOUNT", ""),
            result,
            reason,
            retry_on_stale=False,   # 寫入：不自動重試，避免重複結案
        )

    def sign_next(
        self, task_id: str, site_id: str, node_seq: int, signer_guid: str
    ) -> str:
        return self._call(
            wkf.sign_next, task_id, site_id, node_seq, signer_guid,
            retry_on_stale=False,   # 寫入：不自動重試，避免重複送下一站
        )

    # ── Web-only ─────────────────────────────────────────────────────
    def query_forms(
        self,
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        max_results: int = 50,
    ) -> str:
        # 不會被呼叫：query_forms 在 ops.router 綁定到 web 機制（UOF 一代 PublicAPI 無清單/搜尋 API）。
        # 保留以滿足 OpsBackend 介面；若被誤路由到此，回內部錯誤而非把實作細節丟給使用者。
        return "⚠️ 內部路由錯誤：query_forms 不應由 SOAP 機制處理。"
