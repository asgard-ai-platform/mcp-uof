from __future__ import annotations
import os
import re
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlparse, urlunparse
from ..._log import eprint as _eprint
from .constants import (
    _LOGIN_PATH,
    _HOMEPAGE_PATH,
    _APPLY_FORM_LIST_PATH,
    _FORM_CACHE_TTL_SECONDS,
    _httpx,
)
from .parsing import _parse_apply_form_tree, _parse_hidden_fields
from .runtime import (
    EvidenceKind,
    HttpxWebFormsAdapter,
    ReplayPolicy,
    WebFormsRuntime,
)


class HttpTransport:
    """Owns HTTP transport, authentication probes, and form-id caches."""

    def __init__(
        self,
        runtime: Optional[WebFormsRuntime] = None,
        *,
        restore_session: Optional[Callable[["HttpTransport"], None]] = None,
        password_authenticated: Optional[Callable[["HttpTransport", str], None]] = None,
    ) -> None:
        base_raw = os.environ.get("UOF_BASE_URL", "").rstrip("/")
        parsed = urlparse(base_raw)
        # _vpath is the optional virtual path prefix.
        self._vpath = parsed.path.rstrip("/")
        # _base is scheme+host only
        self._base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        verify_env = os.environ.get("UOF_SSL_VERIFY", "true").lower()
        self._verify = verify_env not in ("false", "0", "no")
        self._client = _httpx.Client(
            verify=self._verify,
            follow_redirects=True,
            timeout=30.0,
        )
        self._form_id_version_map: Optional[dict] = None
        self._apply_form_list: Optional[dict] = None
        self._form_cache_at = 0.0
        self._login_lock = threading.Lock()
        self._webforms_runtime = runtime
        self._password_authenticated = password_authenticated
        if restore_session is not None:
            restore_session(self)

    # ── Internal helpers ─────────────────────────────────────────────

    def _full_url(self, path: str) -> str:
        """Prepend base (scheme+host) to an absolute path."""
        return self._base + path

    def strip_vpath(self, url: str) -> str:
        """Remove virtual path prefix; always returns path+query (no scheme/host).

        Safe to pass result directly to get() / post(), which re-prepend base+vpath.
        """
        parsed = urlparse(url)
        path = parsed.path
        if self._vpath and path.startswith(self._vpath):
            path = path[len(self._vpath):]
            if not path.startswith("/"):
                path = "/" + path
        query_part = ("?" + parsed.query) if parsed.query else ""
        return path + query_part

    def _parse(self, resp: "_httpx.Response"):
        """Parse response body with lxml and return tree."""
        return WebFormsRuntime.hydrate(resp).tree

    @property
    def webforms_runtime(self) -> WebFormsRuntime:
        """Replaceable runtime behind the legacy HttpSession facade."""
        runtime = getattr(self, "_webforms_runtime", None)
        if runtime is None:
            adapter = HttpxWebFormsAdapter(
                self._client,
                lambda path: self._full_url(self._vpath + path),
            )
            runtime = WebFormsRuntime(adapter, self._relogin_if_still_expired)
            self._webforms_runtime = runtime
        return runtime

    def hydrate(self, resp: "_httpx.Response"):
        return self.webforms_runtime.hydrate(resp)

    def control_postback(self, path: str, page, control: str, *, values=None,
                         retry_on_login: bool = False):
        replay = ReplayPolicy.SAFE if retry_on_login else ReplayPolicy.NEVER
        return self.webforms_runtime.control_postback(
            path, page, control, values=values, replay=replay
        )

    def _is_login_page(self, resp: "_httpx.Response") -> bool:
        return self.webforms_runtime.evidence(resp).kind is EvidenceKind.LOGIN

    def _form_cache_valid(self) -> bool:
        return (time.monotonic() - self._form_cache_at) < _FORM_CACHE_TTL_SECONDS

    def is_logged_in(self) -> bool:
        """探測目前 cookie 是否仍是有效登入態；**不會**觸發任何登入。"""
        try:
            resp = self._client.get(
                self._full_url(self._vpath + _HOMEPAGE_PATH), follow_redirects=False
            )
        except Exception as ex:
            _eprint(f"[ops.http_web] 登入狀態探測失敗（{type(ex).__name__}: {ex}）")
            return False
        location = (resp.headers.get("location") or "").lower()
        if "login.aspx" in location or "login.aspx" in str(resp.url).lower():
            return False
        return resp.status_code < 400

    def _do_login(self) -> None:
        """GET Login.aspx, parse VIEWSTATE, POST credentials.

        沒有設定帳密時不是錯誤——那代表這個部署走瀏覽器登入，改拋 `BrowserLoginRequired`
        讓工具層引導使用者呼叫 `uof_custom_login`。
        """
        from ...auth.base import BrowserLoginRequired

        # 已經是瀏覽器登入的身份時，session 過期一律要求重新登入，**不能**改用環境變數帳密
        # 自動登入：那會讓操作身份在程序中途從「實際登入的人」悄悄變成 UOF_ACCOUNT。
        if self.session_source in ("browser", "browser_pending"):
            pending = self.session_source == "browser_pending"
            raise BrowserLoginRequired(
                ("瀏覽器登入仍在等待使用者完成，不能改用環境變數帳密登入"
                 if pending else
                 f"目前是瀏覽器登入的身份（{self.session_account or '未辨識'}），session 已失效。"
                 "為避免中途換成環境變數帳號，不會自動重登，請重新呼叫 uof_custom_login")
            )
        account = os.environ.get("UOF_ACCOUNT", "")
        password = os.environ.get("UOF_PASSWORD", "")
        if not (account and password):
            raise BrowserLoginRequired("未設定 UOF_ACCOUNT / UOF_PASSWORD，且沒有可用的既有 session")
        login_url = self._full_url(self._vpath + _LOGIN_PATH)
        _eprint("[ops.http_web] logging in")
        resp = self._client.get(login_url)
        tree = self._parse(resp)
        hidden = _parse_hidden_fields(tree)
        payload = {
            **hidden,
            "txtAccount": account,
            "txtPwd": password,
            "btnSubmit": "登入",
            "hdflag": "false",
            "hfIsAdAuth": "false",
        }
        resp2 = self._client.post(login_url, data=payload)
        if "Login.aspx" in str(resp2.url):
            raise RuntimeError(
                f"UOF login failed, still on Login.aspx. "
                f"Check UOF_ACCOUNT / UOF_PASSWORD."
            )
        _eprint("[ops.http_web] login succeeded")
        if self._password_authenticated is not None:
            self._password_authenticated(self, account)

    def _relogin_if_still_expired(self) -> None:
        """Avoid duplicate concurrent logins: re-check session after taking the lock."""
        with self._login_lock:
            probe = self._client.get(self._full_url(self._vpath + _HOMEPAGE_PATH))
            if self._is_login_page(probe):
                self._do_login()

    def get(self, path: str) -> "_httpx.Response":
        """GET path (relative to base+vpath), auto-relogin on Login.aspx redirect."""
        return self.webforms_runtime.get(path, replay=ReplayPolicy.SAFE)

    def post(self, path: str, data: dict, *, retry_on_login: bool = False) -> "_httpx.Response":
        """POST once by default; known-safe query callers must explicitly opt into replay."""
        replay = ReplayPolicy.SAFE if retry_on_login else ReplayPolicy.NEVER
        return self.webforms_runtime.post(path, data, replay=replay)

    def _ensure_logged_in(self) -> None:
        """Check homepage; login if redirected."""
        url = self._full_url(self._vpath + _HOMEPAGE_PATH)
        resp = self._client.get(url)
        if self._is_login_page(resp):
            self._relogin_if_still_expired()

    # ── formId ↔ formVersionId mapping ──────────────────────────────

    def scrape_apply_form_list(self) -> dict:
        """List the forms this account can *initiate* — 電子簽核 » 表單申請 (ApplyFormList tree).

        Returns {"ok", "reason", "forms": [{form_id, form_version_id, form_name, category}]}.
        This is the authoritative "what can I start" set. (``scrape_form_list`` reads the
        FormQuery *query* dropdown, which is a broader, version-less set and must not be used
        for the applyable list.)
        """
        if self._apply_form_list is not None and self._form_cache_valid():
            return self._apply_form_list
        resp = self.get(_APPLY_FORM_LIST_PATH)
        if "Login.aspx" in str(resp.url):
            return {"ok": False, "reason": "redirected to Login.aspx", "forms": []}
        forms = _parse_apply_form_tree(resp.text)
        result = {"ok": True, "reason": "", "forms": forms}
        self._apply_form_list = result
        self._form_cache_at = time.monotonic()
        _eprint(f"[ops.http_web] ApplyFormList: {len(forms)} applyable forms")
        return result

    def get_form_id_version_mapping(self) -> dict:
        """{formId: versionId} (lowercase) for the applyable forms in the ApplyFormList tree."""
        if self._form_id_version_map is not None and self._form_cache_valid():
            return self._form_id_version_map
        mapping: dict = {}
        for f in self.scrape_apply_form_list().get("forms", []):
            if f["form_id"] and f["form_version_id"]:
                mapping[f["form_id"]] = f["form_version_id"]
        if not mapping:
            # safety net: the structured tree parse yielded nothing — fall back to the raw
            # value-pair regex over the same page so downstream apply_*/structure still work.
            resp = self.get(_APPLY_FORM_LIST_PATH)
            for form_id, version_id in re.findall(
                r'"value":"([0-9a-f]{8}-[0-9a-f-]{27})@([0-9a-f]{8}-[0-9a-f-]{27})"',
                resp.text,
                re.I,
            ):
                mapping[form_id.lower()] = version_id.lower()
        self._form_id_version_map = mapping
        _eprint(f"[ops.http_web] formId→versionId map: {len(mapping)} entries")
        return mapping

    def _resolve_form_ids(self, form_id_or_version: str) -> tuple:
        """Accept either a formId or a formVersionId; return (formId, versionId) or ("", "").

        Callers (apply_*) may be handed either identifier — an agent typically has the formId
        from get_form_list/get_form_structure_by_id, not the version. `get_form_id_version_mapping`
        is {formId: versionId}, so look the key up as a formId first, then as a versionId.
        """
        mapping = self.get_form_id_version_mapping()
        key = (form_id_or_version or "").lower()
        if key in mapping:                       # given a formId
            return key, mapping[key]
        for fid, vid in mapping.items():         # given a versionId
            if vid == key:
                return fid, vid
        return "", ""

    def _lookup_created_form(self, form_number: str) -> tuple:
        """After 成單, resolve (task_id, real_form_name) by listing recent forms and matching the number.

        `search_forms` keyword (txtKeywordByFormQuery) does NOT match the auto form number, so a
        keyword search returns nothing — list recent forms (no keyword) instead; the just-created
        form is the newest row. Also returns the form's real name (registry's static name may cover
        several formIds and be wrong for the specific one).
        """
        if not form_number:
            return "", ""
        try:
            for rr in self.search_forms(max_results=50).get("rows", []):
                if rr.get("form_number") == form_number:
                    return rr.get("task_id", ""), rr.get("form_name", "")
        except Exception as ex:
            _eprint(f"[ops.http_web] ⚠️ lookup created form failed: {type(ex).__name__}: {ex}")
        return "", ""

    # ── Form structure ───────────────────────────────────────────────
