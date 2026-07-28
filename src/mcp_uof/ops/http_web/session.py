from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Optional

from ..._log import eprint as _eprint
from .transport import HttpTransport


class HttpSession:
    """Small compatibility facade over explicit transport and operation objects."""

    def __init__(self, runtime=None, lifecycle=None) -> None:
        lifecycle = lifecycle or session_lifecycle()
        self._transport = HttpTransport(
            runtime,
            restore_session=lifecycle.restore,
            password_authenticated=lifecycle.password_authenticated,
        )

    def _target(self):
        # Tests may construct the facade with __new__ and override transport methods.
        return self.__dict__.get("_transport", self)

    @property
    def _client(self):
        target = self._target()
        return target.__dict__["_client"]

    @_client.setter
    def _client(self, value):
        self.__dict__.setdefault("_transport", self).__dict__["_client"] = value

    @property
    def _base(self):
        return self._target().__dict__.get("_base", "")

    @_base.setter
    def _base(self, value):
        self.__dict__.setdefault("_transport", self).__dict__["_base"] = value

    @property
    def _vpath(self):
        return self._target().__dict__.get("_vpath", "")

    @_vpath.setter
    def _vpath(self, value):
        self.__dict__.setdefault("_transport", self).__dict__["_vpath"] = value

    @property
    def session_source(self):
        return self._target().__dict__.get("session_source")

    @session_source.setter
    def session_source(self, value):
        self._target().__dict__["session_source"] = value

    @property
    def session_account(self):
        return self._target().__dict__.get("session_account", "")

    @session_account.setter
    def session_account(self, value):
        self._target().__dict__["session_account"] = value

    @property
    def webforms_runtime(self):
        return HttpTransport.webforms_runtime.__get__(self._target(), HttpTransport)

    @property
    def detail_operation(self):
        operation = self.__dict__.get("_detail_operation")
        if operation is None:
            from .details import DetailOperation
            operation = DetailOperation(self.webforms_runtime, self.strip_vpath)
            self._detail_operation = operation
        return operation

    @property
    def forms_operation(self):
        operation = self.__dict__.get("_forms_operation")
        if operation is None:
            from .forms import FormsOperation
            operation = FormsOperation(self)
            self._forms_operation = operation
        return operation

    @property
    def dialog_operation(self):
        operation = self.__dict__.get("_dialog_operation")
        if operation is None:
            from .dialogs import DialogOperation
            operation = DialogOperation(self)
            self._dialog_operation = operation
        return operation

    @property
    def submission_operation(self):
        operation = self.__dict__.get("_submission_operation")
        if operation is None:
            from .submission import SubmissionOperation
            operation = SubmissionOperation(self)
            self._submission_operation = operation
        return operation

    @property
    def lifecycle_operation(self):
        operation = self.__dict__.get("_lifecycle_operation")
        if operation is None:
            from .lifecycle import TaskLifecycleOperation
            operation = TaskLifecycleOperation(self)
            self._lifecycle_operation = operation
        return operation

    # Transport facade
    def _parse(self, response):
        return HttpTransport._parse(self._target(), response)

    def _full_url(self, path):
        return HttpTransport._full_url(self._target(), path)

    def get(self, path):
        return HttpTransport.get(self._target(), path)

    def post(self, path, data, *, retry_on_login=False):
        return HttpTransport.post(self._target(), path, data, retry_on_login=retry_on_login)

    def strip_vpath(self, url):
        return HttpTransport.strip_vpath(self._target(), url)

    def is_logged_in(self):
        return HttpTransport.is_logged_in(self._target())

    def _do_login(self):
        return HttpTransport._do_login(self._target())

    def _ensure_logged_in(self):
        return HttpTransport._ensure_logged_in(self._target())

    def _relogin_if_still_expired(self):
        return HttpTransport._relogin_if_still_expired(self._target())

    def _form_cache_valid(self):
        return HttpTransport._form_cache_valid(self._target())

    def scrape_apply_form_list(self):
        return HttpTransport.scrape_apply_form_list(self._target())

    def get_form_id_version_mapping(self):
        return HttpTransport.get_form_id_version_mapping(self._target())

    def _resolve_form_ids(self, value):
        return HttpTransport._resolve_form_ids(self._target(), value)

    def _lookup_created_form(self, form_number):
        if not form_number:
            return "", ""
        try:
            for row in self.search_forms(max_results=50).get("rows", []):
                if row.get("form_number") == form_number:
                    return row.get("task_id", ""), row.get("form_name", "")
        except Exception as ex:
            _eprint(
                f"[ops.http_web] lookup created form failed: "
                f"{type(ex).__name__}: {ex}"
            )
        return "", ""

    # Form facade
    def scrape_form_structure(self, *args, **kwargs):
        return self.forms_operation.scrape_form_structure(*args, **kwargs)

    def scrape_form_list(self):
        return self.forms_operation.scrape_form_list()

    def search_forms(self, *args, **kwargs):
        return self.forms_operation.search_forms(*args, **kwargs)

    # Dialog facade
    def dialog_structure(self, *args, **kwargs):
        return self.dialog_operation.dialog_structure(*args, **kwargs)

    def dialog_options(self, *args, **kwargs):
        return self.dialog_operation.dialog_options(*args, **kwargs)

    def list_dialog_options(self, *args, **kwargs):
        return self.dialog_operation.list_dialog_options(*args, **kwargs)

    def operate_dialog(self, *args, **kwargs):
        return self.dialog_operation.operate_dialog(*args, **kwargs)

    def search_dialog(self, *args, **kwargs):
        return self.dialog_operation.search_dialog(*args, **kwargs)

    def datagrid_columns(self, *args, **kwargs):
        return self.dialog_operation.datagrid_columns(*args, **kwargs)

    def search_users(self, *args, **kwargs):
        return self.dialog_operation.search_users(*args, **kwargs)

    # Submission/task compatibility facade
    def apply_form_web(self, form_version_id, fields, comment="", urgent_level="2", submit=True):
        return self.submission_operation.submit(
            form_version_id, fields, comment=comment, urgent_level=urgent_level, submit=submit
        )

    def pending_sign_list(self, max_pages=20):
        return self.lifecycle_operation.pending(max_pages)

    def sign_task(self, task_id, approve=True, comment="", next_signer_guid=""):
        return self.lifecycle_operation.sign(task_id, approve, comment, next_signer_guid)

    def void_task(self, task_id, reason=""):
        return self.lifecycle_operation.void(task_id, reason)


class SessionLifecycle:
    """Single authority for session identity transitions and operation ownership."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session: Optional[HttpSession] = None
        self._generation = 0

    @contextmanager
    def operation(self):
        with self._lock:
            yield

    def get(self) -> HttpSession:
        with self._lock:
            if self._session is None:
                self._session = HttpSession()
                try:
                    self._session._ensure_logged_in()
                except Exception as ex:
                    from ...auth.base import BrowserLoginRequired
                    if isinstance(ex, BrowserLoginRequired):
                        _eprint("[ops.http_web] 尚未登入，等待 uof_custom_login 開啟瀏覽器登入")
                    else:
                        _eprint(f"[ops.http_web] ⚠️ initial login failed: {ex}")
            return self._session

    def current(self) -> Optional[HttpSession]:
        with self._lock:
            return self._session

    def restore(self, transport) -> None:
        """Load the one stored artifact selected for this process identity."""
        from ...auth import store
        meta = store.load_session(transport._client)
        transport.session_source = (meta or {}).get("source")
        transport.session_account = (meta or {}).get("actual_account") or ""

    def password_authenticated(self, transport, account: str) -> None:
        """Commit a successful password transition and its owned artifact."""
        with self._lock:
            transport.session_source = "password"
            transport.session_account = account
            from ...auth import store
            store.save_session(transport._client, account=account, source="password")

    def begin_browser(self, session: HttpSession, *, force: bool) -> int:
        with self._lock:
            if self._session is None:
                self._session = session
            self._generation += 1
            token = self._generation
            if force:
                from ...auth import store
                store.clear_session(account=session.session_account)
                session._client.cookies.clear()
                session.session_account = ""
            session.session_source = "browser_pending"
            return token

    def complete_browser(self, session: HttpSession, token: int, account: str) -> bool:
        with self._lock:
            if token != self._generation or session is not self._session:
                return False
            session.session_account = account
            session.session_source = "browser"
            from ...auth import store
            store.save_session(session._client, account=account, source="browser")
            return True

    def reset(self, *, all_identities: bool = False) -> None:
        with self._lock:
            self._generation += 1
            session = self._session
            account = session.session_account if session else ""
            from ...auth.browser_login import shutdown_flow
            from ...auth import store
            shutdown_flow()
            if session is not None:
                session._client.cookies.clear()
                session.session_account = ""
                session.session_source = None
            self._session = None
            if all_identities:
                store.clear_all_sessions()
            else:
                store.clear_session(account=account)

    def discard(self) -> None:
        """Drop process memory for tests/restart simulation without deleting artifacts."""
        with self._lock:
            self._generation += 1
            self._session = None


_lifecycle = SessionLifecycle()


def session_lifecycle() -> SessionLifecycle:
    return _lifecycle


def get_http_session() -> HttpSession:
    return _lifecycle.get()


def current_http_session() -> Optional[HttpSession]:
    return _lifecycle.current()


def reset_http_session() -> None:
    _lifecycle.discard()
