"""採購單（SWUnitechE_POMain plugin）的網頁起單 handler。

填寫頁結構與強韌性規則見 docs/web-apply-design.md。流程：
  表單申請 → 展開 → 選表單節點 → 填寫表單 → FirstSite iframe
  → 主旨 / 供應商(SupplierDialog picker) / 幣別(gate 明細)
  → 逐筆明細(POItemDialog → ItemDialog 料號 picker → 數量/單價 → 確定)
  → 送出
"""
from __future__ import annotations

import os
import re
from typing import Any

from .base import WebApplyHandler
from . import helpers as H


class PurchaseOrderWebApplyHandler(WebApplyHandler):
    form_kind = "purchase_order"

    def describe(self) -> str:
        return (
            "📝 採購單 — 起單時 fields 帶以下內容：\n"
            "  - subject：主旨（必填）\n"
            "  - supplier：供應商代碼（必填，如 C000007）\n"
            "  - supplier_query：供應商名稱關鍵字（選填；代碼不在清單第一頁時需提供，以名稱搜尋定位）\n"
            "  - currency：幣別（選填，預設第一個，如 TWD）\n"
            "  - payment_term / location：付款條件 / 儲存地點（選填，預設各取第一個）\n"
            "  - ship_type_index：運送方式（選填，0 起算）\n"
            "  - request_date：請購要求到貨日 yyyy/MM/dd（選填，預設今天+14 天）\n"
            "  - details：明細（必填，至少一筆）；每筆 {item_code:料號, item_query?:搜尋關鍵字, qty:數量, price:單價, unit:單位}\n"
            "💡 範例：apply_form(form_version_id, fields={'subject':'…','supplier':'C000007',"
            "'details':[{'item_code':'A001','qty':2,'price':100,'unit':'個'}]})"
        )

    def validate(self, payload: dict) -> str | None:
        # 採購單的硬性需求：供應商代碼 + 至少一筆明細（明細是表單必填，空明細送出必被擋）。
        if not str(payload.get("subject", "")).strip():
            return "採購單需要 subject（主旨），payload.subject 不可為空。"
        if not str(payload.get("supplier", "")).strip():
            return "採購單需要 supplier（供應商代碼），payload.supplier 不可為空。"
        if not payload.get("details"):
            return "採購單需要至少一筆明細，payload.details 不可為空。"
        for i, detail in enumerate(payload.get("details", []), start=1):
            if not isinstance(detail, dict):
                return f"採購單明細第 {i} 筆必須是物件。"
            if not str(detail.get("item_code", "")).strip():
                return f"採購單明細第 {i} 筆需要 item_code（料號），不可只靠搜尋結果第一筆。"
        return None

    # ── 主流程 ────────────────────────────────────────────────────────
    def fill_and_submit(self, page: Any, form_name: str, payload: dict, dry_run: bool) -> dict:
        log: list[str] = []
        base = os.environ["UOF_BASE_URL"].rstrip("/")
        self._msgs = self._ensure_dialog_handler(page)  # 自動接受送出 confirm/alert（handler 只裝一次）

        # 0) 送出前先記下「目前已存在的 PO 單號」基準（事後 diff 出新單，避免抓到舊單）
        try:
            self._baseline = self._myformlist_nums(page)[0] if not dry_run else set()
        except Exception as e:
            return {"ok": False, "reason": f"送出前查詢既有 PO 清單失敗，為避免抓錯 TaskId 不送出：{e}", "log": log}

        # 1) 導航到填寫頁
        page.goto(f"{base}/WKF/FormUse/PersonalBox/ApplyFormList.aspx", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        try:
            page.get_by_text("展開", exact=True).first.click(timeout=4000)
        except Exception:
            pass
        page.wait_for_timeout(1200)
        node = page.get_by_text(form_name, exact=True)
        clicked = False
        for i in range(node.count()):
            try:
                node.nth(i).click(timeout=3000, force=True); clicked = True; break
            except Exception:
                continue
        if not clicked:
            return {"ok": False, "reason": f"表單申請樹找不到可點的節點「{form_name}」", "log": log}
        page.wait_for_timeout(600)
        page.get_by_text("填寫表單", exact=True).first.click(timeout=5000)
        fr = H.wait_frame(page, "FirstSite.aspx", timeout=20,
                          ready=lambda f: f.locator("[name$='txtSubject']").count() > 0)
        if fr is None:
            return {"ok": False, "reason": "填寫頁（FirstSite）未載入", "log": log}
        log.append("filled: opened apply form")

        # 2) 主旨
        subject = payload.get("subject", "")
        H.first_site(page).locator("[name$='txtSubject']").first.fill(subject)
        log.append(f"filled: subject={subject!r}")

        # 3) 供應商（readonly → 必走 picker）。picker 的搜尋框是「依名稱」搜，不是代碼；
        #    所以非第一頁的供應商要靠 payload.supplier_query(名稱關鍵字)縮小後再依代碼點選。
        sup = payload.get("supplier", "")
        sup_name = self._pick_supplier(page, sup, payload.get("supplier_query"), log)
        if not sup_name:
            return {"ok": False,
                    "reason": (f"供應商 {sup!r} 選取失敗：picker 依名稱搜尋，此代碼不在第一頁。"
                               "請在 payload 加 supplier_query=（供應商名稱關鍵字）以便定位。"),
                    "log": log}
        log.append(f"filled: supplier={sup}/{sup_name}")

        # 4) 幣別（gate 明細）：先設，否則「新增細項」不啟用
        cur = self._select(H.first_site(page), "ddCurrency", payload.get("currency"))
        log.append(f"filled: currency={cur}")
        page.wait_for_timeout(2500)
        # 等「新增細項」啟用
        for _ in range(30):
            try:
                if H.first_site(page).locator("[name$='btnAdd']").first.is_enabled():
                    break
            except Exception:
                pass
            page.wait_for_timeout(300)

        # 5) 明細（逐筆）
        for k, d in enumerate(payload.get("details", [])):
            ok, code = self._add_detail(page, d, log)
            if not ok:
                return {"ok": False, "reason": f"明細#{k+1} 加入失敗", "log": log}
            log.append(f"filled: detail#{k+1} item={code} qty={d.get('qty')} price={d.get('price')}")

        # 5b) 其餘必填欄位在「明細 postback 之後、緊接送出前」才填，避免被明細的 postback 清掉
        self._fill_po_required(page, payload, log)

        H.no_overlay(page)
        fr = H.first_site(page)
        # 明細是否真的加入：看「原幣金額合計」是否被算出（>0），比偵測「沒有資料」字樣可靠
        total = ""
        try:
            total = fr.locator("[name$='txtOriTotal']").first.input_value()
        except Exception:
            pass
        details_added = (not payload.get("details")) or \
            (total.strip() not in ("", "0", "0.0", "0.00", "0.000"))
        filled = {"subject": subject, "supplier": sup, "supplier_name": sup_name,
                  "currency": cur, "details": payload.get("details", []), "total": total}

        if dry_run:
            shot = os.environ.get("WEBAPPLY_SHOT")
            if shot:
                try: page.screenshot(path=shot, full_page=True)
                except Exception: pass
            return {"ok": True, "dry_run": True, "filled": filled,
                    "details_added": details_added, "log": log}
        if not details_added:
            return {"ok": False, "reason": "明細疑似未加入（金額合計為 0），為避免起空單不送出", "log": log}

        # 6) 先「儲存」：真正把表單存到伺服器（含隱藏 Id），否則「送出」的驗證看到的是空 viewstate
        #    （會 9 個必填全紅、連已填的主旨也紅）。存檔後才送出。
        #    儲存鍵是 Telerik master 按鈕 ctl00$MasterPageRadButton1(autoPostBack)——直接觸發其 postback，
        #    比靠按鈕文字 get_by_text("儲存") 穩(免受文字變動/『送出前需儲存』標籤誤撞/遮罩時序影響)。
        if not self._click_master_button(page, "ctl00_MasterPageRadButton1", "儲存", postback=True):
            return {"ok": False, "log": log,
                    "reason": "找不到「儲存」按鈕(ctl00_MasterPageRadButton1)；當下按鈕線索＝"
                              + self._dump_buttons_diag(page)}
        page.wait_for_timeout(5000)
        log.append(f"after-save msgs={self._msgs[-2:]}")
        if os.environ.get("WEBAPPLY_SAVE_ONLY") == "1":
            shot = os.environ.get("WEBAPPLY_SHOT")
            if shot:
                try: page.screenshot(path=shot, full_page=True)
                except Exception: pass
            ff = H.first_site(page)
            reds = ff.get_by_text("必填欄位").count() if ff else -1
            return {"ok": True, "dry_run": False, "saved_only": True,
                    "filled": filled, "reds_after_save": reds, "log": log}

        # 7) 送出 → 會跳「簽核確認」對話框（下一站點/簽核人員/確定要結案嗎），需按該對話框的「確定」才真正送出。
        #    送出鍵需真實點擊(觸發其客戶端流程開出確認視窗)，用穩定 id ctl00_MasterPageRadButton3，非按鈕文字。
        if not self._click_master_button(page, "ctl00_MasterPageRadButton3", "送出", postback=False):
            return {"ok": False, "log": log,
                    "reason": "找不到「送出」按鈕(ctl00_MasterPageRadButton3)；當下按鈕線索＝"
                              + self._dump_buttons_diag(page)}
        page.wait_for_timeout(3500)
        confirmed = self._confirm_sign_dialog(page)
        log.append(f"sign-confirm 對話框: {'已確定' if confirmed else '未出現'}")
        page.wait_for_timeout(4000)
        import os as _os
        shot = _os.environ.get("WEBAPPLY_SHOT")
        if shot:
            try: page.screenshot(path=shot, full_page=True)
            except Exception: pass
        task_id, form_number = self._capture_submitted(page, self._baseline, subject)
        log.append(f"after-submit new={form_number or '(無新單)'} msgs={self._msgs[-2:]}")
        if not task_id:
            return {"ok": True, "submitted_unconfirmed": True, "dry_run": False, "filled": filled,
                    "task_id": "", "form_number": form_number,
                    "reason": "表單可能已送出，但未能唯一確認本次 TaskId；請用 query_forms 或 UOF 網頁查詢，勿直接重送。",
                    "log": log}
        return {"ok": bool(task_id), "dry_run": False, "filled": filled,
                "task_id": task_id, "form_number": form_number,
                "reason": "" if task_id else "送出後未能唯一確認本次 TaskId（請查 query_forms 確認）",
                "log": log}

    def _ensure_dialog_handler(self, page: Any) -> list:
        """確保 page 的「對話框自動接受」handler **只裝一次**，回傳(清空後的)訊息清單。

        WebRuntime 的 page 是長壽單例(一個 server 程序共用)。若每次起單都 `page.on("dialog", ...)`，
        handler 會累加；之後一個對話框被多個 handler 各 accept 一次 → Playwright 報
        "Cannot accept dialog which is already handled!" 而整個起單崩潰——長時間運行的 Desktop server
        多次起單後必中(這也是本機單次測試漏掉的原因)。故只裝一次、accept 包 try 防重複。"""
        msgs = getattr(page, "_uof_dialog_msgs", None)
        if msgs is None:
            msgs = []
            page._uof_dialog_msgs = msgs

            def _on_dialog(d):
                try:
                    msgs.append(d.message)
                    d.accept()
                except Exception:
                    pass  # 已被處理/已關閉 → 忽略，避免拋出中斷起單
            page.on("dialog", _on_dialog)
        msgs.clear()  # 每次起單清空，log 只看本次
        return msgs

    def _find_button_frame(self, page: Any, btn_id: str, label: str, *, need_visible: bool):
        """跨**所有 frame**找底部 master 按鈕：先用穩定 id `#btn_id`，再用 `.rbText` 文字後備。
        回 (frame, locator) 或 (None, None)。need_visible=False 時只要 DOM 存在即可（postback 不需可見）。"""
        for f in page.frames:
            try:
                loc = f.locator(f"#{btn_id}")
                if loc.count() and (not need_visible or loc.first.is_visible()):
                    return f, loc.first
            except Exception:
                pass
            try:  # 後備：rbText 精確文字（exact 避免誤撞『送出前需儲存』之類標籤）
                t = f.get_by_text(label, exact=True)
                for i in range(min(t.count(), 5)):
                    if not need_visible or t.nth(i).is_visible():
                        return f, t.nth(i)
            except Exception:
                pass
        return None, None

    def _dump_buttons_diag(self, page: Any) -> str:
        """找不到送出區按鈕時，蒐集當下各 frame 的按鈕線索，讓失敗訊息可診斷(而非死路)。"""
        parts = []
        for f in page.frames:
            try:
                rb = f.locator("span.rbText, input[type=submit], input[type=button], button")
                texts = []
                for i in range(min(rb.count(), 15)):
                    e = rb.nth(i)
                    t = (e.inner_text() or e.get_attribute("value") or "").strip()
                    if t:
                        texts.append(t[:10])
                if texts:
                    parts.append(f"[{f.url[-40:]}] {texts}")
            except Exception:
                pass
        return " ; ".join(parts)[:400] or "(無可見按鈕)"

    def _click_master_button(self, page: Any, btn_id: str, label: str, *, postback: bool) -> bool:
        """點 FirstSite 底部 Telerik master 列按鈕(儲存/送出)，用**穩定 id**(跨表單一致)而非按鈕文字。

        為何不用 get_by_text：底部按鈕是 Telerik RadButton，文字在 `.rbText` span，且「送出前需儲存」
        之類核取方塊標籤也含「儲存/送出」字樣，文字定位易誤撞或受 postback 時序 flaky。
        強韌做法：**跨所有 frame 找**(不假設一定在 FirstSite frame)、加長等待(冷啟動/慢環境)、
        儲存走 `__doPostBack`(autoPostBack 按鈕，等同真實存檔 POST，**且不需按鈕可見**——解「按鈕在捲動區外
        未渲染可見」的情況)；送出需真實點擊(觸發其客戶端流程開「簽核確認」視窗)，會先捲到可見再點。

        回 True=已觸發；False=等不到該按鈕。"""
        for _ in range(60):  # ~30s，涵蓋冷啟動/慢 postback
            H.no_overlay(page)
            frame, el = self._find_button_frame(page, btn_id, label, need_visible=not postback)
            if frame is not None:
                if postback:
                    target = btn_id.replace("_", "$", 1)  # ctl00_MasterPageRadButton1 → ctl00$MasterPageRadButton1
                    frame.evaluate(f"__doPostBack('{target}','')")
                    return True
                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    el.click(timeout=5000)
                except Exception:
                    el.click(timeout=4000, force=True)
                return True
            page.wait_for_timeout(500)
        return False

    def _confirm_sign_dialog(self, page: Any) -> bool:
        """送出後的「簽核確認」對話框：找含『結案/下一站點』字樣的 frame，按其「確定」。"""
        for _ in range(12):
            for fr in page.frames:
                try:
                    if fr.get_by_text("確定", exact=True).count() and (
                        fr.get_by_text("下一站點資訊").count() or fr.get_by_text("確定要結案").count()
                        or fr.get_by_text("簽核確認").count()
                    ):
                        fr.get_by_text("確定", exact=True).first.click(timeout=4000)
                        return True
                except Exception:
                    pass
            page.wait_for_timeout(700)
        return False

    # ── 子步驟 ────────────────────────────────────────────────────────
    def _dismiss_dialog(self, page: Any, frame_sub: str = "") -> None:
        """關掉殘留的 Telerik 對話框，避免擋住下一次重試（先點「關閉視窗」，再退而求其次按 Escape）。"""
        if frame_sub:
            fr = H.find_frame(page, frame_sub)
            if fr is not None:
                for kw in ("關閉視窗", "關閉", "取消"):
                    try:
                        b = fr.get_by_text(kw, exact=True).first
                        if b.count() and b.is_visible():
                            b.click(timeout=3000); page.wait_for_timeout(600); return
                    except Exception:
                        pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(800)

    def _pick_supplier(self, page: Any, code: str, query: Any, log: list) -> str:
        for attempt in range(3):
            H.no_overlay(page)
            if H.find_frame(page, "SupplierDialog.aspx") is not None:
                self._dismiss_dialog(page, "SupplierDialog.aspx")
            try:
                H.first_site(page).locator("[name$='btnVendor']").first.click(timeout=5000)
            except Exception as e:
                log.append(f"supplier btnVendor err: {str(e)[:40]}"); continue
            sd = H.wait_frame(page, "SupplierDialog.aspx", timeout=20,
                              ready=lambda f: f.locator("[name$='txtKey']").count() > 0)
            if sd is None:
                log.append(f"supplier attempt {attempt+1}: dialog 未開"); continue
            page.wait_for_timeout(1500)  # 讓 grid 安定，txtKey 綁定完成
            # 若有名稱關鍵字就先搜尋縮小（txtKey 是依名稱搜）；沒有就靠第一頁
            if query:
                try:
                    sd.locator("[name$='txtKey']").first.fill(str(query))
                    sd.get_by_text("搜尋", exact=True).first.click(timeout=4000)
                    sd = H.wait_frame(page, "SupplierDialog.aspx", timeout=12) or sd
                    page.wait_for_timeout(1500)
                except Exception as e:
                    log.append(f"supplier 搜尋 err: {str(e)[:40]}")
            # 在結果列裡點「可見的」代碼格（get_by_text 不會匹配 input 值，故不會誤中搜尋框）
            cells = sd.get_by_text(code, exact=True)
            if not any(cells.nth(i).is_visible() for i in range(cells.count())):
                log.append(f"supplier attempt {attempt+1}: 代碼 {code} 無可見節點"
                           + ("（已用關鍵字搜尋仍無）" if query else "（不在第一頁；需 supplier_query）"))
                self._dismiss_dialog(page, "SupplierDialog.aspx"); continue
            # 點代碼：先試 exact 可見節點，再退而求其次用非 exact
            clicked = False
            for loc in (sd.get_by_text(code, exact=True), sd.get_by_text(code, exact=False)):
                for i in range(loc.count()):
                    c = loc.nth(i)
                    try:
                        if c.is_visible():
                            c.click(timeout=5000); clicked = True; break
                    except Exception:
                        pass
                if clicked:
                    break
            if not clicked:
                log.append(f"supplier attempt {attempt+1}: 代碼 {code} 無可見節點")
                self._dismiss_dialog(page, "SupplierDialog.aspx"); continue
            page.wait_for_timeout(800)
            # 點代碼只是選列；還要按「確定」提交（若代碼點擊已關窗，確定就找不到）
            sd2 = H.find_frame(page, "SupplierDialog.aspx")
            if sd2 is not None:
                try:
                    ok = sd2.get_by_text("確定", exact=True).first
                    if ok.count() and ok.is_visible():
                        ok.click(timeout=4000)
                except Exception as e:
                    log.append(f"supplier 確定 err: {str(e)[:40]}")
            H.wait_frame_gone(page, "SupplierDialog.aspx", timeout=20)
            name = H.poll_value(page, "FirstSite", "txtSupplierName", timeout=8)
            if name:
                return name
            log.append(f"supplier attempt {attempt+1}: clicked but name empty")
            self._dismiss_dialog(page, "SupplierDialog.aspx")
        return ""

    def _select(self, fr: Any, suffix: str, want: Any) -> str:
        """選某 select（name 結尾 suffix）：want 命中 value 或文字則選它，否則選第一個非空選項。"""
        e = fr.locator(f"select[name$='{suffix}']").first
        if not e.count():
            return ""
        opts = e.locator("option")
        chosen = None
        for j in range(opts.count()):
            v = opts.nth(j).get_attribute("value")
            txt = (opts.nth(j).inner_text() or "").strip()
            if not v:
                continue
            if want and (str(want) == v or str(want) in txt):
                chosen = v; break
            if chosen is None:
                chosen = v
        if chosen:
            e.select_option(value=chosen)
            try:  # 觸發 change+blur，讓 ASP.NET 必填驗證重新評估、清掉紅字
                e.evaluate("el=>{el.dispatchEvent(new Event('change',{bubbles:true}));"
                           "el.dispatchEvent(new Event('blur',{bubbles:true}));}")
            except Exception:
                pass
            # 關鍵：同步隱藏的 Id companion（伺服器/驗證讀這個；只設 dropdown 不夠）。
            # 命名規律：ddPaymentTerm→txtPaymentTermId、ddLocation→txtLocationId、ddCurrency→txtCurrencyId。
            if suffix.startswith("dd"):
                hid = "txt" + suffix[2:] + "Id"
                try:
                    fr.locator(f"[name$='{hid}']").first.evaluate(
                        "(el,v)=>{el.value=v;el.dispatchEvent(new Event('change',{bubbles:true}));}",
                        chosen)
                except Exception:
                    pass
        return chosen or ""

    def _selected_value(self, page: Any, suffix: str) -> str:
        try:
            return H.first_site(page).locator(f"select[name$='{suffix}']").first.input_value()
        except Exception:
            return ""

    def _fill_po_required(self, page: Any, payload: dict, log: list) -> None:
        # 下拉彼此的 postback 可能互相清掉對方的值（例如設儲存地點會清掉付款條件）。
        # 因此「設定→驗證→補設」迴圈，直到兩個必填下拉都站得住。
        # 注意：不要在這裡重設幣別——改幣別會觸發金額重算、清掉已加入的明細。
        want = {"ddPaymentTerm": payload.get("payment_term"), "ddLocation": payload.get("location")}
        for _ in range(4):
            empty = [s for s in want if not self._selected_value(page, s).strip()]
            if not empty:
                break
            for s in empty:
                self._select(H.first_site(page), s, want[s])
                page.wait_for_timeout(1500)
        pay = self._selected_value(page, "ddPaymentTerm"); loc = self._selected_value(page, "ddLocation")
        try:
            radios = H.first_site(page).locator("[name$='rbShipType']")
            if radios.count():
                radios.nth(int(payload.get("ship_type_index", 0))).check(timeout=3000)
        except Exception as e:
            log.append(f"運送方式 err {str(e)[:30]}")
        page.wait_for_timeout(1000)
        # 請購要求到貨日（必填）：最後填，並輪詢確認它有值（被 postback 清掉就重填）
        rd = payload.get("request_date")
        if not rd:
            from datetime import date, timedelta
            rd = (date.today() + timedelta(days=14)).strftime("%Y/%m/%d")
        for _ in range(4):
            fr = H.first_site(page)
            try:
                el = fr.locator("[name$='txtRequestDate']").first
                el.fill(rd)
                el.evaluate("e=>{e.dispatchEvent(new Event('change',{bubbles:true}));"
                            "e.dispatchEvent(new Event('blur',{bubbles:true}));}")
            except Exception as e:
                log.append(f"到貨日 err {str(e)[:30]}")
            page.wait_for_timeout(800)
            try:
                if H.first_site(page).locator("[name$='txtRequestDate']").first.input_value().strip():
                    break
            except Exception:
                pass
        log.append(f"filled: 付款條件={pay} 儲存地點={loc} 到貨日={rd}")

    def _add_detail(self, page: Any, d: dict, log: list) -> tuple[bool, str]:
        H.no_overlay(page)
        H.js_click(H.first_site(page).locator("[name$='btnAdd']").first)   # 開窗：JS click 繞遮罩
        dlg = H.wait_frame(page, "POItemDialog.aspx", timeout=20,
                           ready=lambda f: f.locator("[name$='btnQueryItem']").count() > 0)
        if dlg is None:
            log.append("detail: POItemDialog 未開"); return False, ""
        H.js_click(dlg.locator("[name$='btnQueryItem']").first)            # 開料號 picker
        idg = H.wait_frame(page, "/Dialog/ItemDialog.aspx", "POItemDialog", timeout=20,
                           ready=lambda f: f.get_by_text("選取", exact=True).count() > 0)
        if idg is None:
            log.append("detail: ItemDialog 未開或無『選取』"); return False, ""
        want_code = str(d.get("item_code", "")).strip()
        # 以關鍵字搜尋料號；未提供 item_query 時，用精確料號搜尋縮小結果。
        q = d.get("item_query") or want_code
        if q:
            try:
                idg.locator("[name$='txtKey'], [name$='txtKeyword']").first.fill(q)
                idg.get_by_text("搜尋", exact=True).first.click(timeout=4000)
                idg = H.wait_frame(page, "/Dialog/ItemDialog.aspx", "POItemDialog", timeout=15,
                                   ready=lambda f: f.get_by_text("選取", exact=True).count() > 0)
            except Exception:
                pass
        sel = None
        if want_code:
            rows = idg.locator("tr").filter(has_text=want_code)
            for i in range(rows.count()):
                row = rows.nth(i)
                if row.get_by_text(want_code, exact=True).count():
                    pick = row.get_by_text("選取", exact=True).first
                    if pick.count():
                        sel = pick
                        break
            if sel is None:
                log.append(f"detail: 搜尋結果找不到指定料號列 {want_code}")
                return False, ""
        else:
            sel = idg.get_by_text("選取", exact=True).first
        href = sel.get_attribute("href") or ""          # 不可見的 GridView 選取連結 → 直接跑 __doPostBack
        if not href:
            log.append(f"detail: 指定料號列 {want_code} 找不到可執行的選取連結")
            return False, ""
        idg.evaluate(href.replace("javascript:", ""))   # 只是選列；ItemDialog 仍開
        page.wait_for_timeout(1500)
        idg2 = H.find_frame(page, "/Dialog/ItemDialog.aspx", "POItemDialog")
        if idg2 is not None:                            # 按 ItemDialog 的「確定」提交回 POItemDialog
            try:
                idg2.get_by_text("確定", exact=True).first.click(timeout=4000)
            except Exception as e:
                log.append(f"detail: ItemDialog 確定 err {str(e)[:30]}")
        H.wait_frame_gone(page, "/Dialog/ItemDialog.aspx", "POItemDialog", timeout=20)
        code = H.poll_value(page, "POItemDialog.aspx", "txtItemCode", timeout=12)
        if not code:
            log.append("detail: 料號選取後 txtItemCode 仍空"); return False, ""
        if want_code and code.strip() != want_code:
            log.append(f"detail: 料號不符 want={want_code} got={code.strip()}")
            return False, code
        dlg = H.find_frame(page, "POItemDialog.aspx")
        # 單位 / 廠商單位（不會自動帶；換算值=兩單位的換算，預設同單位→比 1）
        unit = str(d.get("unit", "個"))
        vendor_unit = str(d.get("vendor_unit", unit))
        for suf, val in (("txtItemUnit", unit), ("txtVendorUnit", vendor_unit)):
            try:
                el = dlg.locator(f"[name$='{suf}']").first
                if el.count() and not el.get_attribute("readonly"):
                    el.fill(val)
            except Exception:
                pass
        if d.get("qty") is not None:
            dlg.locator("[name$='txtQty']").first.fill(str(d["qty"]))
        pr = dlg.locator("[name$='txtPrice']").first
        if pr.input_value().strip() == "" and d.get("price") is not None:
            pr.fill(str(d["price"]))
        # 計算：算出「採購單位換算值/廠商採購數量/廠商採購單價」(必填、readonly，由此鈕填)
        try:
            H.js_click(dlg.locator("[name$='btnCalc']").first); page.wait_for_timeout(1800)
        except Exception:
            pass
        dlg = H.find_frame(page, "POItemDialog.aspx")
        # 後備：若計算未填出廠商欄位（料號無單位換算主檔），以「換算=1」直接寫值滿足必填
        conv = dlg.locator("[name$='txtConversionRate']").first.input_value()
        if not conv.strip():
            qty = str(d.get("qty", "1")); price = str(d.get("price", "0"))
            dlg.evaluate(
                """({qty, price, unit}) => {
                    const set = (suf, v) => {
                        const el = document.querySelector(`[name$='${suf}']`);
                        if (el) { el.removeAttribute('readonly'); el.value = v;
                                  el.dispatchEvent(new Event('change', {bubbles:true})); }
                    };
                    set('txtConversionRate', '1');
                    set('txtVendorQty', qty);
                    set('txtVendorPrice', price);
                    set('txtVendorUnit', unit);
                }""",
                {"qty": qty, "price": price, "unit": vendor_unit},
            )
            log.append("detail: 計算未填→以換算1 補廠商欄位")
        dlg.get_by_text("確定", exact=True).first.click(timeout=5000, force=True)  # 真實 click
        gone = H.wait_frame_gone(page, "POItemDialog.aspx", timeout=20)
        if not gone:
            log.append("detail: 按確定後 POItemDialog 未關閉（可能必填未滿足）"); return False, code
        return True, code

    def _myformlist_nums(self, page: Any) -> tuple[set, dict]:
        """查詢表單頁：回 (PO單號集合, 單號→TaskId)。供送出前後 diff 出新單。"""
        base = os.environ["UOF_BASE_URL"].rstrip("/")
        page.goto(f"{base}/WKF/FormUse/PersonalBox/MyFormList.aspx?item=FormQuery",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        html = page.content()
        num_to_tid: dict = {}
        for tid, num in re.findall(r"TASK_ID=([0-9a-fA-F-]{36})[\s\S]{0,500}?(PO\d{6,})", html):
            num_to_tid.setdefault(num, tid)
        return set(num_to_tid), num_to_tid

    def _task_contains_subject(self, page: Any, task_id: str, subject: str) -> bool:
        """打開列印頁確認候選 TaskId 確實包含本次主旨，避免並行起單時抓錯單。"""
        base = os.environ["UOF_BASE_URL"].rstrip("/")
        try:
            page.goto(f"{base}/WKF/FormUse/FormPrint.aspx?TASK_ID={task_id}",
                      wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1200)
            text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            title = page.title() or ""
            return bool(subject and (subject in text or subject in title))
        except Exception:
            return False

    def _capture_submitted(self, page: Any, before: set, subject: str) -> tuple[str, str]:
        """送出後到查詢頁找出『新出現且符合本次主旨』的 PO 單號與 TaskId。"""
        try:
            nums, num_to_tid = self._myformlist_nums(page)
        except Exception:
            return "", ""
        new = sorted(n for n in nums if n not in before)
        matches = []
        for n in new:
            tid = num_to_tid.get(n, "")
            if tid and self._task_contains_subject(page, tid, subject):
                matches.append((tid, n))
        if len(matches) == 1:
            return matches[0]
        return "", ""
