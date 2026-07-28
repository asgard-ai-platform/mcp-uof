from __future__ import annotations
import re


# ── Page path constants ───────────────────────────────────────────────
_LOGIN_PATH = "/Login.aspx"
_HOMEPAGE_PATH = "/Homepage.aspx"
_FORM_QUERY_PATH = "/WKF/FormUse/PersonalBox/MyFormList.aspx?item=FormQuery"
_APPLY_FORM_LIST_PATH = "/WKF/FormUse/PersonalBox/ApplyFormList.aspx"
_ADD_FORM_SCRIPT_PATH = "/WKF/FormUse/AddFormScript.aspx"
_FORM_CACHE_TTL_SECONDS = 300.0

_SKIP_HIDDEN_PREFIXES = ("__VIEWSTATE", "__EVENT", "ClientState", "TSM", "TSSM")


# ── lxml import (fail loudly so the error is obvious) ────────────────
try:
    from lxml import etree as _etree
    from lxml.html import fromstring as _html_fromstring
except ImportError as _e:
    raise ImportError(
        "lxml is required for http_web mode. Install: `uv add lxml`. "
        f"Original error: {_e}"
    ) from _e

try:
    import httpx as _httpx
except ImportError as _e:
    raise ImportError(
        "httpx is required for http_web mode. Install: `uv add httpx`. "
        f"Original error: {_e}"
    ) from _e


# ── Module-level regexes ──
_DIALOG_OPEN_RE = re.compile(r"open2?\(\s*['\"]([^'\"]+?\.aspx[^'\"]*)['\"]", re.I)
_DATAGRID_DIALOG_RE = r"['\"]([^'\"]*SetupDataGridFieldValue\.aspx\?[^'\"]*fieldId={code}[^'\"]*)['\"]"
