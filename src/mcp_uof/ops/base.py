"""
OpsBackend — protocol covering every WKF + system operation exposed as an MCP tool.

Both SoapBackend and WebBackend implement this. The MCP tool layer in server.py
dispatches by calling `get_backend().<method>(...)` — it never reaches into the
implementations directly. That keeps tool definitions stable as backends evolve.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class OpsBackend(ABC):
    """對外操作面（12 個工具）。OpsRouter 實作此介面；每個操作於開發期靜態綁定到某機制
    （SOAP 或 web），見 ops.router。SoapBackend / WebBackend 是機制實作。"""

    # ── System ──────────────────────────────────────────────────────
    @abstractmethod
    def check_auth(self) -> str: ...

    # ── WKF reads ───────────────────────────────────────────────────
    @abstractmethod
    def get_form_list(self) -> str: ...
    @abstractmethod
    def get_external_form_list(self) -> str: ...
    @abstractmethod
    def get_form_structure(self, form_version_id: str) -> str: ...
    @abstractmethod
    def get_form_structure_by_id(self, form_id: str) -> str: ...
    @abstractmethod
    def get_task_data(self, task_id: str) -> str: ...
    @abstractmethod
    def get_task_result(self, task_id: str, include_form_data: bool = True) -> str: ...

    # ── WKF writes ──────────────────────────────────────────────────
    @abstractmethod
    def preview_workflow(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: Optional[dict] = None,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str: ...
    @abstractmethod
    def apply_form(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: dict,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str: ...
    @abstractmethod
    def terminate_task(self, task_id: str, result: str, reason: str) -> str: ...
    @abstractmethod
    def sign_next(
        self, task_id: str, site_id: str, node_seq: int, signer_guid: str
    ) -> str: ...

    # ── WKF search — UOF PublicAPI 無清單/搜尋 API，於 ops.router 綁定到 web 機制 ──
    @abstractmethod
    def query_forms(
        self,
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        max_results: int = 50,
    ) -> str: ...


def web_not_implemented(method: str, hint: str = "") -> str:
    """誠實能力說明：此操作的 web 機制實作尚未完成。

    正常情況不會出現——這些操作在 ops.router 綁定到 SOAP 機制；只有在無 PublicAPI 的部署、
    且該操作尚未補上 web 實作時才會走到。刻意不含任何「切換模式」指示：用哪種機制是 server
    內部決定，使用者不需處理。"""
    msg = f"⚠️ 目前無法提供 `{method}`：此操作的網頁機制實作尚未完成。"
    if hint:
        msg += f"\n💡 {hint}"
    return msg
