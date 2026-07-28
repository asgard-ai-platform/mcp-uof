from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol

from .constants import _html_fromstring
from .payload import _form_state_payload, _trigger_control


class ReplayPolicy(Enum):
    """Whether a request may be repeated after restoring authentication."""

    NEVER = "never"
    SAFE = "safe"


class EvidenceKind(Enum):
    OK = "ok"
    LOGIN = "login"
    ERROR_REPORT = "error_report"


@dataclass(frozen=True)
class ResponseEvidence:
    kind: EvidenceKind
    url: str
    status_code: int


@dataclass(frozen=True)
class HydratedPage:
    response: Any
    tree: Any
    state: dict
    evidence: ResponseEvidence


class WebFormsAdapter(Protocol):
    """Single-attempt transport used by WebFormsRuntime."""

    def get(self, path: str) -> Any: ...

    def post(self, path: str, data: Mapping[str, Any]) -> Any: ...


class HttpxWebFormsAdapter:
    """Thin httpx adapter. It deliberately contains no login or replay policy."""

    def __init__(self, client: Any, url_for: Callable[[str], str]) -> None:
        self._client = client
        self._url_for = url_for

    def get(self, path: str) -> Any:
        return self._client.get(self._url_for(path))

    def post(self, path: str, data: Mapping[str, Any]) -> Any:
        return self._client.post(self._url_for(path), data=data)


class WebFormsRuntime:
    """Owns WebForms state hydration, evidence, control postbacks, and replay."""

    def __init__(
        self,
        adapter: WebFormsAdapter,
        reauthenticate: Optional[Callable[[], None]] = None,
    ) -> None:
        self._adapter = adapter
        self._reauthenticate = reauthenticate

    @staticmethod
    def evidence(response: Any) -> ResponseEvidence:
        url = str(response.url)
        lowered = url.lower()
        if "login.aspx" in lowered:
            kind = EvidenceKind.LOGIN
        elif "errorreport" in lowered:
            kind = EvidenceKind.ERROR_REPORT
        else:
            kind = EvidenceKind.OK
        return ResponseEvidence(kind, url, getattr(response, "status_code", 200))

    @staticmethod
    def hydrate(response: Any) -> HydratedPage:
        tree = _html_fromstring(response.text, base_url=str(response.url))
        return HydratedPage(
            response,
            tree,
            _form_state_payload(tree),
            WebFormsRuntime.evidence(response),
        )

    def get(self, path: str, *, replay: ReplayPolicy = ReplayPolicy.SAFE) -> Any:
        return self._request("GET", path, None, replay)

    def post(
        self,
        path: str,
        data: Mapping[str, Any],
        *,
        replay: ReplayPolicy = ReplayPolicy.NEVER,
    ) -> Any:
        return self._request("POST", path, dict(data), replay)

    def control_postback(
        self,
        path: str,
        page: HydratedPage,
        control: str,
        *,
        values: Optional[Mapping[str, Any]] = None,
        replay: ReplayPolicy = ReplayPolicy.NEVER,
    ) -> HydratedPage:
        payload = dict(page.state)
        payload.update(values or {})
        _trigger_control(payload, page.tree, control)
        return self.hydrate(self.post(path, payload, replay=replay))

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Mapping[str, Any]],
        replay: ReplayPolicy,
    ) -> Any:
        response = self._send_once(method, path, data)
        if (
            self.evidence(response).kind is EvidenceKind.LOGIN
            and replay is ReplayPolicy.SAFE
            and self._reauthenticate is not None
        ):
            self._reauthenticate()
            response = self._send_once(method, path, data)
        return response

    def _send_once(
        self,
        method: str,
        path: str,
        data: Optional[Mapping[str, Any]],
    ) -> Any:
        if method == "GET":
            return self._adapter.get(path)
        return self._adapter.post(path, data or {})
