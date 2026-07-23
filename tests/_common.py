"""
tests/_common.py — 兩層測試（smoke / mounted）共用工具。純腳本，無 pytest。

提供：
- 路徑與直譯器（ROOT / SRC / PYTHON）
- .env 載入與「是否具備真實環境」判斷（給真實層 skip 用）
- 三個測試帳號（applicant / manager / admin）
- 表單版本動態解析（httpx 網頁機制；formVersionId 會隨重新發佈而變，不能寫死）
- TaskId 解析
- 回應成功判定（ok）

由兩層測試共用以避免重複與漂移。
"""
from __future__ import annotations

import os
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # mcp-uof/
SRC = ROOT / "src"
PYTHON = sys.executable

def workflow_form_name() -> str:
    """mounted 工作流程表單名（讀 env，於 load_env() 後呼叫；未設定回空字串→該情境 skip）。

    以函式而非模組常數，避免在 load_env() 之前（import 時）就把值定死為空。
    """
    return os.getenv("UOF_TEST_WORKFLOW_FORM_NAME", "")


def workflow_fields(memo: str) -> dict:
    """Load the deployment-specific mounted-test payload from ignored environment settings."""
    fields = json.loads(os.getenv("UOF_TEST_WORKFLOW_FIELDS", "{}"))
    memo_field = os.getenv("UOF_TEST_WORKFLOW_MEMO_FIELD", "")
    if memo_field:
        fields[memo_field] = memo
    return fields


_REQUIRED_ENV = (
    "UOF_BASE_URL", "UOF_ACCOUNT", "UOF_PASSWORD",
    "UOF_ACCOUNT_USER1", "UOF_ACCOUNT_USER2", "UOF_ACCOUNT_USER3",
    "UOF_TEST_WORKFLOW_FORM_NAME", "UOF_TEST_WORKFLOW_FIELDS",
    "UOF_TEST_WORKFLOW_MEMO_FIELD",
)

_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_TASK_ID_RE = re.compile(r"TaskId\)\s*[:：]?\s*(" + _GUID + r")")


def ensure_src_on_path() -> None:
    """把 src 加到 sys.path，讓 `import mcp_uof.*` 在以腳本方式執行時也可用。"""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))


def load_env() -> dict:
    """載入 mcp-uof/.env 到 os.environ（不覆寫既有），並回傳其 dict（給子程序 env 注入用）。"""
    from dotenv import dotenv_values, load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    return {k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}


def missing_env() -> list:
    """回傳缺少的真實環境設定鍵（空 list 代表齊備）。"""
    return [k for k in _REQUIRED_ENV if not os.getenv(k)]


def has_live_env() -> bool:
    return not missing_env()


def accounts() -> tuple:
    """Return applicant, approver, and cleanup accounts from the mounted-test environment."""
    applicant = os.environ["UOF_ACCOUNT_USER3"]
    manager = os.environ["UOF_ACCOUNT_USER2"]
    admin = os.environ["UOF_ACCOUNT_USER1"]
    return applicant, manager, admin


def resolve_form_httpx(form_name: str) -> tuple:
    """由 httpx 網頁機制動態解析表單目前的 (formId, formVersionId)。

    formVersionId 會隨表單在後台重新發佈而改變（formId 較穩定），寫死會失敗，因此一律動態解析。
    以目前 .env 的帳號登入（版本為全域、與身份無關）；找不到時回 (None, None)。需先 load_env()。
    """
    ensure_src_on_path()
    from mcp_uof.ops.http_web import get_http_session

    s = get_http_session()
    fid = None
    for f in s.scrape_form_list().get("forms", []):
        if f.get("form_name") == form_name:
            fid = (f.get("form_id") or "").lower()
            break
    if not fid:
        return None, None
    return fid, s.get_form_id_version_mapping().get(fid)


def extract_task_id(text: str):
    """從 apply_form 回應穩健抽出 TaskId（GUID）。

    以正則鎖定「TaskId 標記後的 GUID」；找不到回 None。
    """
    if not text:
        return None
    m = _TASK_ID_RE.search(text)
    if m:
        return m.group(1)
    # 退路：回應中第一個 GUID（apply_form 成功訊息只含 TaskId 一個 GUID）。
    m = re.search(_GUID, text)
    return m.group(0) if m else None


def ok(text: str) -> bool:
    """回應視為成功：非空、且不含失敗標記 ❌ / 🔒。"""
    return bool(text) and "❌" not in text and "🔒" not in text


def check(label: str, cond: bool, detail: str = "") -> int:
    """印出一行 ✅/❌ 並回傳失敗數（0 或 1）；呼叫端用 `failures += check(...)` 累加。

    呼叫端用 `failures += check(...)` 累加結果。
    """
    if cond:
        print(f"  ✅ {label}")
        return 0
    print(f"  ❌ {label}{(' — ' + detail) if detail else ''}")
    return 1
