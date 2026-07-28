from __future__ import annotations
import json
import os
import re
from typing import Optional
from ..base import OpsBackend
from .session import HttpSession, get_http_session
from .rendering import _render_filled_fields


class HttpWebBackend(OpsBackend):
    """OpsBackend implemented with httpx + lxml (no Playwright)."""

    @property
    def _session(self) -> HttpSession:
        return get_http_session()

    # ── System ──────────────────────────────────────────────────────
    def _identity_label(self, session: Optional["HttpSession"] = None) -> str:
        """目前 session **實際**屬於誰。

        一律以 session 自己記錄的帳號為準，不能退回 UOF_ACCOUNT——使用者可能透過瀏覽器登入
        了另一個人，那時報 UOF_ACCOUNT 就是錯的身份。真的辨識不出來時就明說。
        """
        session = session or self._session
        if session.session_account:
            return session.session_account
        return "（無法辨識登入者；登入頁的帳號欄位不是預期格式，可用 UOF_SESSION_NAMESPACE 區分身份）"

    def check_auth(self) -> str:
        base = os.environ.get("UOF_BASE_URL", "(未設定 UOF_BASE_URL)")
        try:
            session = self._session
            logged_in = session.is_logged_in()
        except Exception as ex:
            return f"❌ http_web session 檢查失敗 ({type(ex).__name__}): {ex}"
        from ...auth.session import has_password_credentials
        if logged_in:
            source = {"browser": "瀏覽器登入", "password": "環境變數帳密自動登入"}.get(
                session.session_source or "", "未知來源"
            )
            out = (
                f"✅ http_web session：{self._identity_label(session)} 已登入\n"
                f"   伺服器: {base}\n"
                f"   認證來源: {source}"
            )
            configured = os.environ.get("UOF_ACCOUNT", "").strip()
            if (configured and session.session_account
                    and session.session_account.lower() != configured.lower()):
                out += (
                    f"\n\n⚠️ 注意：目前操作身份是 **{session.session_account}**，"
                    f"與設定中的 UOF_ACCOUNT（{configured}）不同。\n"
                    "所有操作都會以實際登入的身份送出。請據實告知使用者，不要以設定值稱呼對方。"
                )
            return out
        if session.session_source == "browser_pending":
            return (
                "🔑 http_web session：瀏覽器登入仍在等待使用者完成。\n"
                f"   伺服器: {base}\n\n"
                "完成瀏覽器登入前不會改用環境變數帳密，也不會執行其他受保護工具。"
            )
        if has_password_credentials() and session.session_source != "browser":
            return (
                f"⚠️ http_web session：帳號 {os.environ.get('UOF_ACCOUNT', '')} 未登入"
                f"（下次操作會自動以環境變數帳密登入）\n   伺服器: {base}"
            )
        return (
            f"🔑 http_web session：尚未登入，需要使用者在瀏覽器完成登入。\n"
            f"   伺服器: {base}\n\n"
            "請呼叫 `uof_custom_login` 開啟登入頁，不要向使用者索取帳號密碼。"
        )

    def login(self, force: bool = False) -> str:
        from ...auth import browser_login as _bl
        from .session import session_lifecycle

        session = self._session
        if not force and session.is_logged_in():
            return (
                f"✅ 已經是登入狀態（{self._identity_label()}），不需要重新登入。\n"
                "若要換身份或強制重登，請用 force=True。"
            )
        if force:
            _bl.shutdown_flow()
        token = session_lifecycle().begin_browser(session, force=force)
        flow = _bl.start_login_flow(
            session, reuse=not force, lifecycle_token=token
        )
        opened = _bl.open_in_browser(flow.url)
        wait = _bl.wait_seconds()
        if flow.wait(wait):
            note = ""
            configured = os.environ.get("UOF_ACCOUNT", "").strip()
            if (configured and session.session_account
                    and session.session_account.lower() != configured.lower()):
                note = (
                    f"\n⚠️ 這個身份與設定中的 UOF_ACCOUNT（{configured}）不同，"
                    "後續操作一律以實際登入的身份送出；session 過期時不會自動改用設定的帳密，"
                    "而是再次要求瀏覽器登入。"
                )
            return (
                f"✅ UOF 登入完成，session 已取得（{self._identity_label(session)}）。{note}\n"
                "可以繼續操作其他工具了。"
            )
        opened_line = (
            "已在你的預設瀏覽器開啟 UOF 登入頁。"
            if opened else
            "⚠️ 無法自動開啟瀏覽器，請手動複製下面的網址到瀏覽器開啟："
        )
        return (
            f"🔑 等待使用者完成登入中（已等 {wait:.0f} 秒）。\n"
            f"{opened_line}\n\n"
            f"    {flow.url}\n\n"
            f"這個網址只能在本機開啟、且只有效一次；登入頁最多保留 {_bl.timeout_seconds():.0f} 秒。\n"
            "請告訴使用者去瀏覽器完成登入，完成後再呼叫 `uof_custom_check_auth` 確認。\n"
            "不要向使用者索取帳號密碼——帳密只會輸入在 UOF 自己的登入頁。"
        )

    def logout(self) -> str:
        from ...auth.base import get_session_provider

        get_session_provider().clear()
        return (
            "✅ 已登出：記憶體中的 session 已清除，磁碟上的 session 存檔也已刪除。\n"
            "下次操作需要重新登入（呼叫 `uof_custom_login`）。"
        )

    # ── WKF reads ───────────────────────────────────────────────────
    def get_form_list(self) -> str:
        try:
            data = self._session.scrape_apply_form_list()
        except Exception as ex:
            return f"❌ 取得表單清單時發生錯誤 ({type(ex).__name__}): {ex}"
        if not data.get("ok"):
            return f"❌ 取得表單清單失敗：{data.get('reason', '(unknown)')}"
        forms = data["forms"]
        if not forms:
            return "📋 找不到任何可申請的表單（此帳號在『電子簽核 » 表單申請』沒有可起單的表單）"
        from collections import OrderedDict
        by_cat: OrderedDict = OrderedDict()
        for f in forms:
            by_cat.setdefault(f["category"], []).append(f)
        lines = [f"📋 可申請表單清單（來源：電子簽核 » 表單申請 樹，共 {len(forms)} 個表單）："]
        for cat, items in by_cat.items():
            lines.append(f"\n📁 【{cat}】")
            for f in items:
                lines.append(
                    f"  - {f['form_name']} "
                    f"(formId: {f['form_id']}, formVersionId: {f['form_version_id']})"
                )
        lines.append(
            "\n💡 這是「可起單（表單申請）」的表單；每張都含 formId 與 formVersionId，"
            "起單時 apply_form 直接帶 formVersionId。"
        )
        return "\n".join(lines)

    def get_external_form_list(self) -> str:
        return (
            "⚠️ 網頁機制無法可靠回 `get_external_form_list`。\n\n"
            "「非線上使用」是 UOF 後台「表單管理」中的 admin 旗標，一般 user 在前端\n"
            "（表單申請樹、查詢表單下拉、列表頁）都看不到這個旗標——需在 UOF 後台\n"
            "「表單管理」直接查看。\n\n"
            "可行替代：\n"
            "- 用 `get_form_list` 看「目前帳號可查詢/起單」的所有表單\n"
            "- 「非線上使用」與「可外部起單」並非相同概念，\n"
            "  若是想知道「哪些表單可以起單」，請看 get_form_list 結果。"
        )

    def get_form_structure(self, form_version_id: str) -> str:
        return self._render_form_structure(form_version_id=form_version_id, by_label="formVersionId")

    def get_form_structure_by_id(self, form_id: str) -> str:
        return self._render_form_structure(form_id=form_id, by_label="formId")

    def _render_form_structure(
        self,
        form_id: Optional[str] = None,
        form_version_id: Optional[str] = None,
        by_label: str = "formId",
    ) -> str:
        try:
            data = self._session.scrape_form_structure(form_id=form_id, form_version_id=form_version_id)
        except Exception as ex:
            return f"❌ 取得表單結構時發生錯誤 ({type(ex).__name__}): {ex}"
        if not data.get("ok"):
            return f"❌ 取得表單結構失敗（by {by_label}）：{data.get('reason', '(unknown)')}"
        fields = data["fields"]
        fill_hint = {
            "autoNumber": "系統自動編號（讀取用）",
            "datePicker": "日期欄位 (yyyy/MM/dd)",
            "numeric": "數值欄位",
            "multiLineText": "多行文字",
            "dropDown": "下拉選單，需從選項挑值",
            "fileButton": "附檔欄位（網頁起單流程才能上傳）",
            "dataGrid": "明細欄位，起單時帶列清單（見該欄的『每列』）",
            "radio": "單選",
            "checkbox": "多選/勾選",
            "text": "單行文字",
            "dialog": "彈窗選取欄位",
            "unknown": "型別未知（可能是版面元件）",
        }
        unsupported_for_apply = [f for f in fields if f["input_type"] == "fileButton"]
        lines = [
            f"📝 表單 {form_id or form_version_id} 的欄位清單"
            f"（http_web 模式，from AddFormScript.aspx）",
            f"  formId: {data['form_id']}",
            f"  formVersionId: {data['form_version_id']}",
            f"  共 {len(fields)} 個欄位：",
        ]
        for f in fields:
            mark = "＊" if f["required"] else " "
            code = f["code"] or "—"
            hint = fill_hint.get(f["input_type"], f["input_type"])
            readonly = ""
            if f.get("disabled"):
                reason = f.get("input_title") or ""
                readonly = f" [唯讀：{reason}]" if reason else " [唯讀]"
            opts = f.get("options") or []
            opt_str = ""
            if opts:
                opt_str = "　可選值：" + "／".join(o["value"] for o in opts)
            lines.append(f"  {mark} [{code}] {f['label']} 〈{f['input_type']}〉{readonly} — {hint}{opt_str}")
            if f["input_type"] == "dataGrid":
                cols = f.get("columns") or []
                if cols:
                    col_str = "、".join(f"{c['label'] or ('欄'+str(c['index']+1))}({c['input_type']})" for c in cols)
                    lines.append(f"       每列填：{col_str}；如 {{\"{code}\":[{{列一}}, …]}}，每列用欄名對應")
                else:
                    lines.append(f"       每列帶一個 dict（欄名: 值）或依序的 list；起單時 {{\"{code}\":[…]}}")
        if unsupported_for_apply:
            codes = ", ".join(
                f"{f['code'] or f['label']}({f['input_type']})" for f in unsupported_for_apply
            )
            lines.append(
                f"\n⚠️ 含附檔欄位（{codes}）；apply_form 目前不支援上傳附檔，請於 UOF 網頁操作。"
            )
        lines.append(
            "\n💡 起單時把 fields 帶 `{欄位代碼: 值}` 對應；自動編號欄位帶空字串即可。"
            "\n⚠️ 送出前務必核對：標 ＊ 的欄位一定要填；有『可選值』的欄位只能填清單內的值"
            "（填清單外的值會被伺服器**默默丟棄**、欄位變空、單據不完整卻仍可能回報起單成功）。"
            "\n🔗 來源頁: " + data["url"]
        )
        return "\n".join(lines)

    def get_task_data(self, task_id: str) -> str:
        try:
            d = self._session.lifecycle_operation.inspect(task_id)
        except Exception as ex:
            return f"❌ 查任務摘要時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 查任務摘要失敗：{d.get('reason', '(unknown)')}"
        if not d["history"] and not d["applicant"]:
            return f"❌ 找不到表單（TaskId {task_id}）或無檢視權限。"
        lines = [
            "📄 表單申請內容（http_web 模式，from ViewFormTemp.aspx）：",
            f"  - 表單編號: {d['form_number'] or '(未取得)'}",
            f"  - 申請者: {d['applicant'] or '(未取得)'}",
            f"  - 申請時間: {d['apply_time'] or '(未取得)'}",
            f"  - 簽核結果: {d['result']}",
        ]
        if d["close_date"]:
            lines.append(f"  - 結案時間: {d['close_date']}")
        if d["fields"]:
            lines += ["", f"📋 表單欄位（{len(d['fields'])} 欄）："]
            lines += _render_filled_fields(d["fields"])
        return "\n".join(lines)

    def get_task_result(self, task_id: str, include_form_data: bool = True) -> str:
        try:
            d = self._session.lifecycle_operation.inspect(task_id)
        except Exception as ex:
            return f"❌ 查簽核歷程時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 查簽核歷程失敗：{d.get('reason', '(unknown)')}"
        lines = [
            f"📄 表單 {task_id} 的簽核記錄（http_web 模式，from ViewFormTemp.aspx）：",
            f"  申請者: {d['applicant'] or '(未取得)'} | 最終結果: {d['result']}",
        ]
        lines.append(
            f"  表單編號: {d['form_number'] or '(未取得)'} | 申請時間: {d['apply_time'] or '(未取得)'}"
            + (f" | 結案時間: {d['close_date']}" if d["close_date"] else "")
        )
        if include_form_data:
            if d["fields"]:
                lines += ["", f"📋 表單欄位（{len(d['fields'])} 欄）："]
                lines += _render_filled_fields(d["fields"])
            else:
                lines += ["", "📋 表單欄位：(解析不到欄位；此頁可能非標準表單樣板)"]
        lines += ["", "📝 簽核歷程："]
        if not d["history"]:
            lines.append("  (無歷程或無檢視權限)")
        for r in d["history"]:
            lines.append(
                f"  站點 {r['site']}｜{r['signer']}｜{r['status']}｜{r['time']}"
                + (f"｜意見:{r['comment']}" if r["comment"] else "")
            )
        return "\n".join(lines)

    def get_dialog_structure(self, form_version_id: str, field_code: str = "") -> str:
        try:
            d = self._session.dialog_structure(form_version_id, field_code)
        except Exception as ex:
            return f"❌ 查對話框結構時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 查對話框結構失敗：{d.get('reason', '(unknown)')}"
        if not d["fields"]:
            return ("📋 這張表單沒有對話框型欄位"
                    + (f"（或找不到欄位 {field_code}）" if field_code else "") + "。")
        def _render(cs: list, indent: str = "   ") -> list:
            ls = []
            for c in cs:
                mark = "＊" if c.get("required") else " "
                flags = "".join([
                    "[唯讀]" if c.get("readonly") else "",
                    "[隱藏]" if c.get("hidden") else "",
                ])
                nm = c.get("id") or c.get("name") or "?"
                ls.append(f"{indent}{mark}{c.get('label') or '(無標籤)'} → {nm} 〈{c.get('type', '?')}〉{flags}")
                opts = c.get("options") or []
                if opts:
                    shown = "／".join(o["text"] for o in opts[:8] if o["text"])
                    more = f" …共 {len(opts)} 項" if len(opts) > 8 else ""
                    ls.append(f"{indent}   可選值: {shown}{more}")
                if c.get("lookup_buttons"):
                    ls.append(f"{indent}   查找鈕: {', '.join(c['lookup_buttons'])}")
            return ls

        lines = [f"🗂 對話框欄位結構（{len(d['fields'])} 個）："]
        for f in d["fields"]:
            press = f.get("press") or ""
            hint = (f"　開窗鈕 {press}（用 _lookups[{{press:{press}, row:選中項}}]）"
                    if press and not f.get("row_editors") else
                    f"　開列鈕 {press}" if press else "")
            lines.append(f"\n▸ {f['label']}({f['code']})　挑選器: {f['dialog'] or '(未知)'}{hint}")
            if f["note"]:
                lines.append(f"   ⚠️ {f['note']}")
            if f.get("inline"):
                lines.append(f"   ── 欄位區塊內的控制項（{len(f['inline'])}）──")
                lines += _render(f["inline"], "   ")
            for red in (f.get("row_editors") or []):
                lines.append(f"   ── 明細列編輯器: {red['dialog']}（開列鈕 {red['open_button']}；用 _rows 帶列）──")
                if red.get("note"):
                    lines.append(f"      ⚠️ {red['note']}")
                for c in red["fields"]:
                    mark = "＊" if c.get("required") else " "
                    flags = "".join(["[唯讀]" if c.get("readonly") else "", "[隱藏]" if c.get("hidden") else ""])
                    nm = c.get("id") or c.get("name") or "?"
                    lb = [x for x in (c.get("lookup_buttons") or []) if x]
                    if c.get("picker_dialog"):
                        # a lookup column: query candidates + replay the pick inside the row
                        press = lb[0] if lb else "?"
                        extra = (f"（picker: {c['picker_dialog']}；查候選用 search_dialog_options，"
                                 f"填列時把選中項放進該列的 _lookups[{{press:{press}, row:選中項}}]）")
                    elif lb:
                        # no dialog behind the button → a calc/derive action, run after fill
                        extra = f"（動作鈕 {', '.join(lb)}：填列時放進該列的 _press_after）"
                    else:
                        extra = ""
                    lines.append(f"      {mark}{c.get('label') or '(無標籤)'} → {nm} 〈{c.get('type', '?')}〉{flags}{extra}")
                    opts = c.get("options") or []
                    if opts:
                        shown = "／".join(f"{o['value']}={o['text']}" for o in opts[:12] if o["text"])
                        more = f" …共 {len(opts)} 項" if len(opts) > 12 else ""
                        lines.append(f"         可選值(值=顯示): {shown}{more}")
            if f["inner"]:
                lines.append(f"   ── 挑選器/列編輯器內欄位（{len(f['inner'])}）──")
            for c in f["inner"]:
                mark = "＊" if c.get("required") else " "
                flags = "".join([
                    "[唯讀]" if c.get("readonly") else "",
                    "[隱藏]" if c.get("hidden") else "",
                ])
                nm = c.get("id") or c.get("name") or "?"
                lines.append(f"   {mark}{c.get('label') or '(無標籤)'} → {nm} 〈{c.get('type', '?')}〉{flags}")
                opts = c.get("options") or []
                if opts:
                    shown = "／".join(o["text"] for o in opts[:8] if o["text"])
                    more = f" …共 {len(opts)} 項" if len(opts) > 8 else ""
                    lines.append(f"      可選值: {shown}{more}")
                if c.get("lookup_buttons"):
                    lines.append(f"      查找鈕: {', '.join(c['lookup_buttons'])}")
        lines.append("\n💡 同一標籤下可能有多個控制項（含隱藏輔助欄）；要填哪一個由表單的 skill 判斷。")
        # Some blocks only render their detail editor after their picker is chosen, so a structure
        # read on the blank page cannot show it. Say so rather than let it read as "no detail".
        if any(f.get("press") for f in d["fields"]) and not any(f.get("row_editors") for f in d["fields"]):
            lines.append("💡 這裡沒列出明細列編輯器：有些表單要先按開窗鈕選定主資料（如供應商）後，"
                         "明細區塊才會出現。apply_form 帶 `_lookups` 時會自動先選再填明細；"
                         "若該表單確實有明細，請照該表單 skill 的順序帶。")
        return "\n".join(lines)

    def search_dialog_options(self, form_version_id: str, field_code: str,
                              keyword: str = "", limit: int = 20) -> str:
        try:
            d = self._session.dialog_options(form_version_id, field_code, keyword, limit)
        except Exception as ex:
            return f"❌ 查詢視窗候選時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 查詢失敗：{d.get('reason', '(unknown)')}"
        rows = d["rows"]
        if not rows:
            return (f"📋 欄位 {d['field']} 以關鍵字「{keyword}」查無候選項目。\n"
                    "💡 換個關鍵字再試；查不到就別硬填，請向使用者確認正確代碼。")
        lines = [f"🔎 {d['field']} 候選項目（關鍵字「{keyword}」，{len(rows)} 筆）："]
        for i, r in enumerate(rows, 1):
            shown = {k: v for k, v in r.items() if v not in (None, "", 0) and not k.startswith("_")}
            head = " ｜ ".join(f"{k}={v}" for k, v in list(shown.items())[:6])
            lines.append(f"  [{i}] {head}")
            # The picked entity must go back to the dialog *whole*: the server reads its own keys
            # (internal Id included) off this object. A hand-rebuilt subset drops keys the confirm
            # needs and the row is silently rejected — so hand back the exact JSON to paste as-is.
            lines.append(f"      row={json.dumps({k: v for k, v in r.items() if not k.startswith('_')}, ensure_ascii=False)}")
        lines.append("\n⚠️ 不要盲選第一筆：請確認代碼精確相符或名稱可信，必要時回問使用者。")
        lines.append("👉 選定後，把該筆整個 row=… 的 JSON 原樣放進明細列的 "
                     "_lookups[{press:<開窗鈕，如 btnExpense>, row:<此 JSON>}]；不要自行改寫或只挑幾個鍵。")
        return "\n".join(lines)

    def operate_dialog(self, form_version_id: str, field_code: str,
                       values: Optional[dict] = None, press: str = "") -> str:
        try:
            d = self._session.operate_dialog(form_version_id, field_code, values, press)
        except Exception as ex:
            return f"❌ 操作對話框時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 操作失敗：{d.get('reason', '(unknown)')}"
        lines = [f"🛠 對話框操作完成（{field_code}）"]
        if values:
            lines.append(f"  填入: {values}")
        lines.append(f"  按下: {press or '(未按任何按鈕)'}")
        ch = d["changed"]
        if not ch:
            lines.append("\n⚠️ 伺服器沒有回傳任何欄位變化——動作可能未生效，或此按鈕不影響欄位。")
        else:
            lines.append(f"\n📝 伺服器改動的控制項（{len(ch)}）：")
            for name, mv in list(ch.items())[:30]:
                lines.append(f"  {name.split('$')[-1]}: {mv['from'] or '(空白)'} → {mv['to'] or '(空白)'}")
        lines.append("\n💡 變化清單是判斷「哪些值由系統連帶帶出」的依據；操作順序請依該表單 skill 的定義。")
        return "\n".join(lines)

    def get_pending_sign_list(self) -> str:
        try:
            d = self._session.lifecycle_operation.pending()
        except Exception as ex:
            return f"❌ 查待簽清單時發生錯誤 ({type(ex).__name__}): {ex}"
        if not d.get("ok"):
            return f"❌ 查待簽清單失敗：{d.get('reason', '(unknown)')}"
        rows = d["rows"]
        if not rows:
            return "📋 目前沒有待簽表單。"
        lines = [f"✍️ 待簽表單（目前身份，共 {len(rows)} 筆）："]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"\n[{i}] {r['text']}"
                f"\n    TaskId: {r['task_id']}"
                f"\n    SiteId: {r['site_id']} | NodeSeq: {r['node_seq']}"
            )
        if d["total"] and d["total"] != len(rows):
            lines.append(f"\n⚠️ 頁面回報共 {d['total']} 筆，實際取得 {len(rows)} 筆（翻頁可能未走完）。")
        lines.append("\n💡 用 get_task_result 看單張欄位內容；terminate_task 可同意/否決。")
        return "\n".join(lines)

    def preview_workflow(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: Optional[dict] = None,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str:
        return (
            "⚠️ 流程預覽（模擬簽核路徑）目前不提供：此功能需在 UOF 網頁上操作。\n"
            "💡 你仍可直接用 apply_form 起單；起單後用 get_task_result 查看實際簽核歷程與目前站點。"
        )

    def apply_form(
        self,
        form_version_id: str,
        applicant_account: str,
        first_signer_account: str,
        fields: dict,
        comment: str = "",
        urgent_level: str = "2",
    ) -> str:
        try:
            r = self._session.apply_form_web(form_version_id, fields, comment, urgent_level)
        except Exception as ex:
            return f"❌ 起單時發生錯誤 ({type(ex).__name__}): {ex}"
        errors = r.get("errors") or []
        err_block = ("\n⚠️ 填寫警告：\n" + "\n".join(f"  - {e}" for e in errors)) if errors else ""
        if not r.get("ok"):
            return f"❌ 起單失敗：{r.get('reason', '(unknown)')}{err_block}"
        if r.get("submitted_unconfirmed"):
            return (
                f"⚠️ 起單可能已送出，但 TaskId 未確認\n"
                f"   表單編號：{r.get('form_number') or '(未取得)'}\n"
                f"   說明：{r.get('reason')}\n"
                "   請先用 query_forms 或 UOF 網頁確認，勿直接重送。"
                + err_block
            )
        return (
            f"✅ 起單成功\n"
            f"   表單編號：{r.get('form_number') or '(未取得)'}\n"
            f"   TaskId：{r.get('task_id')}"
            + err_block
        )

    def terminate_task(self, task_id: str, result: str, reason: str) -> str:
        """Render the guarded lifecycle result without owning transaction policy."""
        if result not in ("Adopt", "Reject", "Cancel"):
            return f"❌ 無效的結案動作: {result}。請使用 Adopt（同意）、Reject（否決）或 Cancel（作廢）"
        try:
            outcome = self._session.lifecycle_operation.terminate(task_id, result, reason)
        except Exception as ex:
            return f"❌ 任務操作錯誤（{type(ex).__name__}）：{ex}"
        action = {"Adopt": "同意", "Reject": "否決", "Cancel": "作廢"}[result]
        if outcome.get("ok"):
            return f"✅ 表單{action}成功（已由寫後狀態確認）"
        if outcome.get("unconfirmed"):
            return f"⚠️ {outcome.get('reason')}\n   請用 query_forms / get_task_data 確認。"
        return f"❌ 表單{action}未完成：{outcome.get('reason', '(unknown)')}"

    def sign_next(self, task_id: str, site_id: str, node_seq: int, signer_guid: str) -> str:
        """簽核目前待簽的一關（純 httpx，自由流程 web 簽核）。

        以目前 MCP 身份（UOF_ACCOUNT）對「自己待簽」的表單按「同意」。site_id/node_seq 由待簽清單
        自動定位、不需呼叫端提供（沿用原簽名以相容工具介面）。`signer_guid` 若提供＝指定下一關
        簽核者（往下一站點）；留空＝在此結案（此關為最後簽核 → 表單結案/通過）。
        """
        try:
            r = self._session.lifecycle_operation.sign(
                task_id, approve=True, comment="", next_signer_guid=(signer_guid or "")
            )
        except Exception as ex:
            return f"❌ 簽核執行錯誤（{type(ex).__name__}）：{ex}"
        if not r.get("ok"):
            if r.get("unconfirmed"):
                return f"⚠️ {r.get('reason')}\n   請用 query_forms / get_task_result 確認。"
            return f"❌ 簽核未完成：{r.get('reason')}"
        nxt = "指定下一關簽核者" if signer_guid else "結案"
        return f"✅ 已簽核（同意）TaskId {task_id}（{nxt}，已有寫後 evidence）。"

    def query_forms(
        self,
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        max_results: int = 50,
        query_mode: str = "apply",
    ) -> str:
        try:
            result = self._session.search_forms(
                keyword, date_from, date_to, max_results, query_mode)
        except Exception as ex:
            return (
                f"❌ 查詢表單時發生錯誤 ({type(ex).__name__}): {ex}\n"
                f"💡 此清單為自動擷取，遇非預期頁面結構時可能誤判。"
            )
        if not result.get("ok"):
            return f"❌ 查詢失敗：{result.get('reason', '(unknown)')}"
        rows = result["rows"]
        q = result["query"]
        # total_matched 已套用日期邊界與 keyword；不是抓取列數或頁面大小。
        total = result.get("total_matched", result.get("total_scanned", len(rows)))
        header = (
            f"🔍 查詢表單 —"
            f" {q['date_from']} ~ {q['date_to']}"
            + ("（依申請日期）" if q.get("query_mode") == "apply" else "（依簽核日期）")
            + (f"，關鍵字「{q['keyword']}」" if q["keyword"] else "")
            + "\n"
        )
        if not rows:
            return header + "📋 查無資料"
        lines = [
            header + f"共 {total} 筆"
            + (f"（僅顯示前 {len(rows)} 筆）" if total > len(rows) else "") + "："
        ]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"\n[{i}] {r['form_name']} {r['form_number']}  〈{r['status']}〉"
                f"\n    TaskId: {r['task_id'] or '(無法擷取)'}"
                f"\n    申請者: {r['applicant']}"
                f"\n    申請時間: {r['apply_time']}"
                + (f"\n    結案時間: {r['close_time']}" if r["close_time"] else "")
                + (f"\n    摘要: {r['subject']}" if r["subject"] else "")
            )
        lines.append(
            "\n💡 把 TaskId 帶入 `get_task_data` / `get_task_result` 可查單張詳情。"
        )
        return "\n".join(lines)

    def search_users(self, keyword: str) -> str:
        if not keyword or not keyword.strip():
            return "❌ 請提供查詢關鍵字（姓名或帳號）。"
        try:
            users = self._session.search_users(keyword.strip())
        except Exception as ex:
            return f"❌ 查詢人員時發生錯誤 ({type(ex).__name__}): {ex}"
        if not users:
            return f"📋 找不到符合「{keyword}」的人員。"
        lines = [f"👥 人員查詢結果（關鍵字：「{keyword}」，共 {len(users)} 筆）："]
        for u in users:
            lines.append(
                f"\n  姓名：{u['display_name']}　帳號：{u['account']}"
                f"\n  UserGuid：{u['UserGuid']}"
            )
        lines.append("\n💡 UserGuid 可用於 sign_next 的 signer_guid 參數。")
        return "\n".join(lines)
