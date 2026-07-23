"""
OpsBackend — protocol covering every WKF + system operation exposed as an MCP tool.

`HttpWebBackend` (httpx + lxml 網頁機制) implements this. The MCP tool layer in server.py
dispatches by calling `get_backend().<method>(...)` — it never reaches into the
implementation directly. That keeps tool definitions stable as the backend evolves.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class OpsBackend(ABC):
    """Backend contract implemented by OpsRouter and HttpWebBackend."""

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
    @abstractmethod
    def get_pending_sign_list(self) -> str: ...
    @abstractmethod
    def get_dialog_structure(self, form_version_id: str, field_code: str = "") -> str: ...
    @abstractmethod
    def search_dialog_options(self, form_version_id: str, field_code: str,
                              keyword: str = "", limit: int = 20) -> str: ...
    @abstractmethod
    def operate_dialog(self, form_version_id: str, field_code: str,
                       values: Optional[dict] = None, press: str = "") -> str: ...

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

    # ── WKF search ──────────────────────────────────────────────────
    @abstractmethod
    def query_forms(
        self,
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        max_results: int = 50,
        query_mode: str = "apply",
    ) -> str: ...
    @abstractmethod
    def search_users(self, keyword: str) -> str: ...
