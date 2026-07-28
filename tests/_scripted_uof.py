"""Reusable, fail-loud HTTP scripts for offline UOF transaction tests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mcp_uof.ops.http_web import HttpSession


@dataclass(frozen=True)
class ScriptedResponse:
    url: str
    text: str = ""
    status_code: int = 200


@dataclass(frozen=True)
class ScriptedStep:
    method: str
    path: str
    response: ScriptedResponse
    retry_on_login: Optional[bool] = None


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    data: Optional[dict]
    retry_on_login: Optional[bool]


def get(path: str, response: ScriptedResponse) -> ScriptedStep:
    return ScriptedStep("GET", path, response)


def post(
    path: str,
    response: ScriptedResponse,
    *,
    retry_on_login: bool,
) -> ScriptedStep:
    return ScriptedStep("POST", path, response, retry_on_login)


class ScriptedHttpSession(HttpSession):
    """HttpSession whose transport and external lookups follow a strict script."""

    def __init__(
        self,
        steps: list[ScriptedStep],
        *,
        form_ids: tuple[str, str],
        virtual_path: str = "",
        created_form: tuple[str, str] = ("", ""),
    ) -> None:
        self._steps = list(steps)
        self._vpath = virtual_path.rstrip("/")
        self._form_ids = form_ids
        self._created_form = created_form
        self.requests: list[RecordedRequest] = []

    def get(self, path: str) -> ScriptedResponse:
        return self._consume("GET", path, None, None)

    def post(
        self,
        path: str,
        data: dict,
        *,
        retry_on_login: bool = True,
    ) -> ScriptedResponse:
        return self._consume("POST", path, dict(data), retry_on_login)

    def assert_finished(self) -> None:
        if self._steps:
            remaining = ", ".join(f"{s.method} {s.path}" for s in self._steps)
            raise AssertionError(f"script has unconsumed requests: {remaining}")

    def _resolve_form_ids(self, _form_id_or_version: str) -> tuple[str, str]:
        return self._form_ids

    def _lookup_created_form(self, _form_number: str) -> tuple[str, str]:
        return self._created_form

    def _consume(
        self,
        method: str,
        path: str,
        data: Optional[dict],
        retry_on_login: Optional[bool],
    ) -> ScriptedResponse:
        if not self._steps:
            raise AssertionError(f"unexpected request after script end: {method} {path}")
        expected = self._steps.pop(0)
        actual = RecordedRequest(method, path, data, retry_on_login)
        self.requests.append(actual)
        if (method, path) != (expected.method, expected.path):
            raise AssertionError(
                f"unexpected request: got {method} {path}; "
                f"expected {expected.method} {expected.path}"
            )
        if expected.retry_on_login is not None and retry_on_login != expected.retry_on_login:
            raise AssertionError(
                f"unexpected retry policy for {method} {path}: "
                f"got {retry_on_login}, expected {expected.retry_on_login}"
            )
        return expected.response
