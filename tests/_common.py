"""
tests/_common.py — 三層測試（smoke / e2e / mounted）共用工具。純腳本，無 pytest。

提供：
- 路徑與直譯器（ROOT / SRC / PYTHON）
- .env 載入與「是否具備真實環境」判斷（給真實層 skip 用）
- 三個測試帳號（applicant / manager / admin）
- GetToken 與「採購單」版本動態解析（formVersionId 會隨重新發佈而變，不能寫死）
- 穩健的 TaskId 解析（取代易碎的字串 split）
- 回應成功判定（ok）

被三層共用以避免重複與漂移。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # mcp-uof/
SRC = ROOT / "src"
PYTHON = sys.executable

# 採購單對外中介欄位。e2e（服務層直呼 SOAP）仍用採購單；MEMO 內容由各測試自帶。
PO_FIELDS = {"FORM_NO": "", "PO_Main": "", "MEMO": "", "ATTACH_FILES": ""}

# 工作流程測試（mounted，經 MCP）用的「原生中介」表單：apply_form 對它走 SOAP(快、不觸發外部服務)。
# 採購單在 MCP 層已收斂為「網頁起單」分派(慢)，故工作流程改用使用者建立的原生測試表單。
# 003=客戶名稱(由各測試帶 memo)；A001 autoNumber 空字串；001/100 為日期。SOAP 起單不驗網頁必填，餘可略。
WORKFLOW_FORM_NAME = "MCP 測試申請單"
WORKFLOW_FIELDS = {"A001": "", "001": "2026/07/01", "100": "2026/07/15"}

# 真實環境所需設定鍵；缺任一則真實層（e2e / mounted）自動 skip。
_REQUIRED_ENV = ("UOF_BASE_URL", "UOF_APP_NAME", "UOF_RSA_PUBLIC_KEY", "UOF_PASSWORD")

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
    """(applicant, manager, admin)，對應 .env 的 UOF_ACCOUNT_USER3 / USER2 / USER1（含預設值）。"""
    applicant = os.getenv("UOF_ACCOUNT_USER3", "applicant_account")
    manager = os.getenv("UOF_ACCOUNT_USER2", "manager_account")
    admin = os.getenv("UOF_ACCOUNT_USER1", "admin")
    return applicant, manager, admin


def get_token_for(user: str) -> str:
    """為指定帳號取得 Token（三個測試帳號共用 UOF_PASSWORD）。需先 load_env()。"""
    ensure_src_on_path()
    from mcp_uof.auth import rsa_encrypt
    from mcp_uof.soap_client import uof_client
    from mcp_uof.domains.system.endpoints import AUTH_ENDPOINT

    pk = os.getenv("UOF_RSA_PUBLIC_KEY", "")
    token = uof_client.call(
        endpoint_path=AUTH_ENDPOINT,
        method_name="GetToken",
        params={
            "appName": os.getenv("UOF_APP_NAME", ""),
            "account": rsa_encrypt(pk, user),
            "password": rsa_encrypt(pk, os.getenv("UOF_PASSWORD", "")),
        },
    )
    if not token:
        raise RuntimeError(f"GetToken 失敗：{user}（回應為空，帳號不存在或未授權）")
    return token


def resolve_form(token: str, form_name: str = "採購單") -> tuple:
    """由 GetFormList 動態解析表單目前的 (formId, recentVersionId)。

    formVersionId 會隨表單在後台重新發佈而改變（formId 較穩定），寫死會 VersionIdNoMatchException，
    因此一律動態解析。找不到時回 (None, None)。
    """
    ensure_src_on_path()
    from lxml import etree
    from mcp_uof.soap_client import uof_client
    from mcp_uof.domains.wkf.endpoints import WKF_ENDPOINT

    xml = uof_client.call(
        endpoint_path=WKF_ENDPOINT, method_name="GetFormList", params={"token": token}
    )
    if not xml:
        return None, None
    root = etree.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    for form in root.iter("Form"):
        if form.get("formName") == form_name:
            return form.get("formId"), form.get("recentVersionId")
    return None, None


def extract_task_id(text: str):
    """從 apply_form 回應穩健抽出 TaskId（GUID）。

    取代易碎的 `text.split("TaskId):")[-1]`：service 文案微調或起單失敗時，舊寫法會把整段
    錯誤字串當成 TaskId 往下打。這裡用正則鎖定「TaskId 標記後的 GUID」；找不到回 None。
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
