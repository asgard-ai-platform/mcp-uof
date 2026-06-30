"""網頁起單分派：依「設計期登錄結構」(registry) 決定某表單是否走網頁 handler。

起單對外只有一個入口（server 的 apply_form）；它先問這裡「這張表單有沒有登錄網頁 handler」，
有就走 web、沒有就走 SOAP 中介——對使用者透明。**判斷不打 SOAP，只查 registry**（設計期知識）。
"""
from __future__ import annotations

from typing import Optional

from . import registry
from .registry import FormApplyEntry


class VersionResolveError(RuntimeError):
    """formVersionId 反查 formId 失敗；不可靜默退回 SOAP。"""


def resolve(form_version_id: str) -> Optional[FormApplyEntry]:
    """這張表單是否登錄為網頁起單？回 entry 或 None（None＝走 SOAP 中介）。"""
    return registry.resolve(form_version_id)


def resolve_version(form_version_id: str) -> Optional[FormApplyEntry]:
    """依 formVersionId 找 web handler；靜態 version miss 時，從起單頁反查 formId 再比對 registry。"""
    entry = registry.resolve(form_version_id)
    if entry:
        return entry
    if not form_version_id:
        return None
    try:
        from ..web import get_web_runtime
        form_id = get_web_runtime().form_id_for_version(form_version_id)
    except Exception as e:
        raise VersionResolveError(f"無法反查 formVersionId={form_version_id} 的 formId：{e}") from e
    if not form_id:
        raise VersionResolveError(f"ApplyFormList 查不到 formVersionId={form_version_id} 對應的 formId")
    return registry.resolve(form_id)


def describe(form_version_id: str) -> Optional[str]:
    """登錄為網頁起單的表單 → 回它的可填欄位說明；否則 None（由 SOAP get_form_structure 處理）。"""
    entry = registry.resolve(form_version_id)
    return entry.handler.describe() if entry else None


def describe_version(form_version_id: str) -> Optional[str]:
    """同 describe，但 version 改版時可透過 ApplyFormList mapping 反查 formId。"""
    entry = resolve_version(form_version_id)
    return entry.handler.describe() if entry else None


def apply_web(entry: FormApplyEntry, payload: dict, dry_run: bool = False) -> str:
    """執行某表單的網頁起單 handler（在 WebRuntime 的 worker thread 內）。"""
    err = entry.handler.validate(payload)
    if err:
        return f"❌ 起單參數不足（{entry.form_name}）：{err}"
    from ..web import get_web_runtime
    try:
        result = get_web_runtime().web_apply(entry.handler, entry.form_name, payload, dry_run)
    except Exception as e:
        return f"❌ 網頁起單執行錯誤（{type(e).__name__}）：{e}"
    return _format(entry, result)


def resolve_error_message(form_version_id: str, error: Exception) -> str:
    return (
        "❌ 無法確認此表單版本是否為網頁起單表單，為避免錯誤退回 SOAP 起單已停止。"
        f"formVersionId：{form_version_id}；原因：{error}"
    )


def _format(entry: FormApplyEntry, r: dict) -> str:
    name = entry.form_name
    if not r.get("ok"):
        return (f"❌ 起單失敗（{name}）：{r.get('reason', '(unknown)')}\n"
                f"   步驟：{' → '.join(r.get('log', []))}")
    if r.get("dry_run"):
        f = r.get("filled", {})
        return (f"✅ 試填成功（dry_run，未送出）：{name}\n"
                f"   主旨：{f.get('subject')}｜供應商：{f.get('supplier')} / {f.get('supplier_name')}\n"
                f"   幣別：{f.get('currency')}｜明細 {len(f.get('details', []))} 筆"
                f"｜金額合計：{f.get('total') or '(未取得)'}｜明細加入：{'是' if r.get('details_added') else '否'}")
    if r.get("submitted_unconfirmed"):
        return (f"⚠️ 起單可能已送出，但 TaskId 未確認：{name}\n"
                f"   表單編號：{r.get('form_number') or '(未取得)'}\n"
                f"   主旨：{r.get('filled', {}).get('subject')}\n"
                f"   說明：{r.get('reason')}\n"
                "   請先用 query_forms 或 UOF 網頁確認，勿直接重送。")
    return (f"✅ 起單成功：{name}\n"
            f"   表單編號：{r.get('form_number') or '(未取得)'}\n"
            f"   TaskId：{r.get('task_id')}\n"
            f"   主旨：{r.get('filled', {}).get('subject')}")
