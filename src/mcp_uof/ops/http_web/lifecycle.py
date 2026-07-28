from __future__ import annotations

import html
import json
import re

from ..._log import eprint as _eprint
from .constants import _HOMEPAGE_PATH, _etree
from .parsing import _parse_hidden_fields
from .payload import _form_state_payload
from .rendering import _parse_filled_form_fields


class TaskLifecycleOperation:
    """Owns task inspection, pending ownership, and guarded lifecycle writes."""

    _SIGN_LINK_RE = (
        r"SignNodeForm\.aspx\?TASK_ID=([0-9a-f-]{36})"
        r"&SITE_ID=([0-9a-f-]{36})&NODE_SEQ=(\d+)"
    )
    _STATUS = {
        "簽核中": "簽核中",
        "處理中": "簽核中",
        "進行中": "簽核中",
        "同意": "同意",
        "通過": "同意",
        "核准": "同意",
        "否決": "否決",
        "駁回": "否決",
        "作廢": "作廢",
        "撤銷": "作廢",
    }
    _TERMINAL = frozenset(("同意", "否決", "作廢"))

    def __init__(self, session) -> None:
        self._session = session

    def inspect(self, task_id: str) -> dict:
        path = f"/WKF/FormUse/ViewFormTemp.aspx?TASK_ID={task_id}"
        response = self._session.get(path)
        failure = self._response_failure(response, "任務頁")
        if failure:
            return {"ok": False, "reason": failure, "task_id": task_id}

        tree = self._session._parse(response)
        text = re.sub(r"\s+", " ", " ".join(tree.itertext())).replace("\xa0", " ")
        match = re.search(r"表單審核結果[：:]\s*([^\s（(]+)", text)
        raw_status = match.group(1).strip() if match else ""
        result = self._STATUS.get(raw_status)
        if result is None:
            shown = raw_status or "未取得"
            return {
                "ok": False,
                "reason": f"任務頁缺少可辨識的簽核狀態（取得: {shown}）",
                "task_id": task_id,
            }

        history = self._parse_history(tree)
        applicant = apply_time = ""
        for row in history:
            if "申請" in row["status"]:
                applicant, apply_time = row["signer"], row["time"]
                break
        if not applicant and history:
            applicant, apply_time = history[0]["signer"], history[0]["time"]
        number = re.search(r"\b([A-Z]{2,4}\d{6,})\b", text)
        fields = _parse_filled_form_fields(tree)
        if not history and not number and not fields:
            return {
                "ok": False,
                "reason": "任務頁雖含狀態文字，但缺少表單編號、歷程與欄位 evidence",
                "task_id": task_id,
            }
        return {
            "ok": True,
            "reason": "",
            "task_id": task_id,
            "applicant": applicant,
            "apply_time": apply_time,
            "form_number": number.group(1) if number else "",
            "result": result,
            "close_date": history[-1]["time"] if history and result in self._TERMINAL else "",
            "history": history,
            "fields": fields,
        }

    def pending(self, max_pages: int = 20) -> dict:
        response = self._session.get(_HOMEPAGE_PATH)
        failure = self._response_failure(response, "待簽首頁")
        if failure:
            return {"ok": False, "reason": failure, "rows": [], "total": 0}
        tree = self._session._parse(response)
        raw = html.unescape(response.text)
        grids = tree.xpath("//table[contains(@id,'DGFormList')]")
        total_match = re.search(r"共\s*(\d+)\s*筆", raw)
        if not grids and not (total_match and int(total_match.group(1)) == 0):
            return {"ok": False, "reason": "待簽首頁缺少可辨識的清單元件", "rows": [], "total": 0}

        control_match = re.search(r"__doPostBack\('([^']*\$DGFormList)','Page\$", raw)
        control = control_match.group(1) if control_match else ""
        total = int(total_match.group(1)) if total_match else 0
        rows = self._pending_rows(grids[0]) if grids else []
        seen = {row["task_id"] for row in rows}
        page = 2
        while control and len(rows) < total and page <= max_pages:
            payload = _form_state_payload(tree)
            payload.update({"__EVENTTARGET": control, "__EVENTARGUMENT": f"Page${page}"})
            next_response = self._session.post(
                _HOMEPAGE_PATH, payload, retry_on_login=True
            )
            if self._response_failure(next_response, "待簽清單翻頁"):
                return {"ok": False, "reason": "待簽清單翻頁失敗", "rows": [], "total": total}
            tree = self._session._parse(next_response)
            grids = tree.xpath("//table[contains(@id,'DGFormList')]")
            if not grids:
                return {"ok": False, "reason": "待簽清單翻頁缺少清單元件", "rows": [], "total": total}
            fresh = [row for row in self._pending_rows(grids[0]) if row["task_id"] not in seen]
            if not fresh:
                break
            rows.extend(fresh)
            seen.update(row["task_id"] for row in fresh)
            page += 1
        return {"ok": True, "reason": "", "rows": rows, "total": total or len(rows)}

    def terminate(self, task_id: str, result: str, reason: str) -> dict:
        if result not in ("Adopt", "Reject", "Cancel"):
            return {"ok": False, "reason": f"無效的結案動作: {result}"}
        before = self.inspect(task_id)
        guard = self._write_guard(before)
        if guard:
            return guard
        if result == "Cancel":
            owner_guard = self._cancel_owner_guard(before)
            if owner_guard:
                return owner_guard
            return self._void(task_id, reason)
        return self._sign(task_id, result == "Adopt", reason, "")

    def sign(self, task_id: str, approve: bool = True, comment: str = "",
             next_signer_guid: str = "") -> dict:
        before = self.inspect(task_id)
        guard = self._write_guard(before)
        if guard:
            return guard
        return self._sign(task_id, approve, comment, next_signer_guid)

    def void(self, task_id: str, reason: str = "") -> dict:
        before = self.inspect(task_id)
        guard = self._write_guard(before)
        if guard:
            return guard
        owner_guard = self._cancel_owner_guard(before)
        if owner_guard:
            return owner_guard
        return self._void(task_id, reason)

    def _sign(self, task_id: str, approve: bool, comment: str,
              next_signer_guid: str) -> dict:
        listing = self.pending()
        if not listing.get("ok"):
            return {"ok": False, "reason": f"無法確認待簽 ownership：{listing.get('reason', '')}"}
        owned = next((row for row in listing["rows"]
                      if row["task_id"].lower() == task_id.lower()), None)
        if owned is None:
            return {"ok": False, "reason": f"TaskId {task_id} 不在目前身份的待簽清單"}

        path = ("/WKF/FormUse/FreeTask/SignNodeForm.aspx"
                f"?TASK_ID={task_id}&SITE_ID={owned['site_id']}&NODE_SEQ={owned['node_seq']}")
        page = self._session.get(path)
        failure = self._response_failure(page, "簽核頁")
        if failure:
            return {"ok": False, "reason": failure}
        prefix = "ctl00$ContentPlaceHolder1$"
        payload = _form_state_payload(self._session._parse(page))
        payload[prefix + "txtComment"] = comment or ("同意" if approve else "否決")
        payload.update({
            "__EVENTTARGET": "ctl00$MasterPageRadButton3" if approve else "ctl00$MasterPageRadButton4",
            "__EVENTARGUMENT": "",
            "__LASTFOCUS": "",
        })
        first = self._session.post(path, payload, retry_on_login=False)
        failure = self._response_failure(first, "簽核頁 postback")
        if failure:
            return {"ok": False, "reason": failure}
        match = re.search(
            r"(/WKF/FormUse/[A-Za-z]+/(?:SendOtherSite|OtherSiteSend)\.aspx\?[^'\"]+)",
            html.unescape(first.text),
        )
        if not match:
            return {"ok": False, "reason": "未取得簽核確認頁；簽核未確認，請勿直接重送", "unconfirmed": True}

        confirm_path = match.group(1)
        confirm = self._session.get(confirm_path)
        failure = self._response_failure(confirm, "簽核確認頁")
        if failure:
            return {"ok": False, "reason": failure}
        payload = _form_state_payload(self._session._parse(confirm))
        payload[prefix + "rbListSignResult"] = "Approve" if approve else "Disapprove"
        payload[prefix + "rblEndType"] = "N" if next_signer_guid else "Y"
        if next_signer_guid:
            payload[prefix + "UC_ChoiceList_Signer$hiddenJSON"] = json.dumps(
                [{"UserGUID": next_signer_guid, "Type": "user"}], separators=(",", ":")
            )
        payload.update({"__EVENTTARGET": "ctl00$MasterPageRadButton2", "__EVENTARGUMENT": "", "__LASTFOCUS": ""})
        sent = self._session.post(confirm_path, payload, retry_on_login=False)
        failure = self._response_failure(sent, "簽核確認 postback")
        if failure:
            return {"ok": False, "reason": failure}
        blockers = sorted(set(re.findall(r"必填|請選擇|至少|請指定", sent.text)))
        if blockers:
            return {"ok": False, "reason": f"送出被擋（{blockers}）"}
        return self._sign_evidence(task_id, approve, next_signer_guid)

    def _sign_evidence(self, task_id: str, approve: bool, next_signer_guid: str) -> dict:
        after = self.inspect(task_id)
        expected = "同意" if approve else "否決"
        if not next_signer_guid and after.get("ok") and after.get("result") == expected:
            return {"ok": True, "reason": "", "result": expected, "evidence": after}
        if next_signer_guid and after.get("ok"):
            listing = self.pending()
            if listing.get("ok") and all(
                row["task_id"].lower() != task_id.lower() for row in listing["rows"]
            ):
                return {"ok": True, "reason": "", "result": expected, "evidence": after}
        current = after.get("result", "未知") if after.get("ok") else after.get("reason", "無法查證")
        return {
            "ok": False,
            "unconfirmed": True,
            "reason": f"簽核指令已送出，但寫後狀態未確認（目前: {current}）；請勿直接重送",
            "result": "",
        }

    def _void(self, task_id: str, reason: str) -> dict:
        path = f"/WKF/FormUse/FormHandle/FormGetBack.aspx?TASK_ID={task_id}"
        page = self._session.get(path)
        failure = self._response_failure(page, "作廢頁")
        if failure:
            return {"ok": False, "reason": failure}
        tree = self._session._parse(page)
        if not tree.xpath("//input[@value='rbDeleteApplyForm']"):
            return {
                "ok": False,
                "reason": "作廢頁未提供『作廢表單』權限，不送出作廢指令",
            }
        payload = _parse_hidden_fields(tree)
        prefix = "ctl00$ContentPlaceHolder1$"
        payload.update({
            "__EVENTTARGET": "ctl00$MasterPageRadButton1",
            "__EVENTARGUMENT": "",
            prefix + "rbGetBack": "rbDeleteApplyForm",
            prefix + "txtReason": reason or "作廢",
            prefix + "tbScriptName": "",
        })
        sent = self._session.post(path, payload, retry_on_login=False)
        failure = self._response_failure(sent, "作廢 postback")
        if failure:
            return {"ok": False, "reason": failure}
        after = self.inspect(task_id)
        if after.get("ok") and after.get("result") == "作廢":
            return {"ok": True, "reason": "", "result": "作廢", "evidence": after}
        current = after.get("result", "未知") if after.get("ok") else after.get("reason", "無法查證")
        return {
            "ok": False,
            "unconfirmed": True,
            "reason": f"作廢指令已送出，但寫後狀態未確認（目前: {current}）；請勿直接重送",
        }

    def _write_guard(self, snapshot: dict) -> dict | None:
        if not snapshot.get("ok"):
            return {"ok": False, "reason": f"寫入前無法驗證任務頁：{snapshot.get('reason', '')}"}
        if snapshot["result"] in self._TERMINAL:
            return {"ok": False, "reason": f"表單已結案（結果: {snapshot['result']}），不可重送"}
        return None

    def _cancel_owner_guard(self, snapshot: dict) -> dict | None:
        identity = (self._session.session_account or "").strip().casefold()

        # 登入帳號在簽核歷程顯示字串的最後一組括號內，例如
        # 「測試專用帳號 employee02(test_account)」。lblApplicant 只有「別名 (職稱)」，
        # 不能作為帳號來源。
        applicant_account = ""
        shown = (snapshot.get("applicant") or "").strip()
        match = re.search(r"\(([^()]+)\)\s*$", shown)
        if match:
            candidate = match.group(1).strip()
            if (
                candidate
                and not re.search(r"[\s()]", candidate)
                and candidate in shown
            ):
                applicant_account = candidate.casefold()

        if identity and applicant_account and identity != applicant_account:
            return {
                "ok": False,
                "reason": "目前身份不是此表單申請人，不執行作廢",
            }
        if not identity or not applicant_account:
            _eprint(
                "[ops.http_web] 無法解析 Cancel ownership，"
                "交由 UOF 作廢頁 capability 判定"
            )
        return None

    @staticmethod
    def _response_failure(response, page_name: str) -> str:
        url = str(response.url).lower()
        if "login.aspx" in url:
            return f"{page_name}導向 Login.aspx"
        if "errorreport" in url:
            return f"{page_name}導向 ErrorReport"
        if getattr(response, "status_code", 200) >= 400:
            return f"{page_name}回傳 HTTP {response.status_code}"
        return ""

    @staticmethod
    def _parse_history(tree) -> list:
        history = []
        tables = tree.xpath("//table[contains(@id,'SignCommentGrid')]")
        if not tables:
            return history
        for row in tables[0].xpath(".//tr"):
            cells = row.xpath("./td")
            if len(cells) < 6:
                continue
            values = [re.sub(r"\s+", " ", "".join(cell.itertext())).replace("\xa0", "").strip()
                      for cell in cells]
            history.append({"site": values[0], "signer": values[2], "comment": values[3],
                            "time": values[4], "status": values[5]})
        return history

    def _pending_rows(self, grid) -> list:
        rows = []
        for tr in grid.xpath("./tr | ./tbody/tr"):
            raw = html.unescape(_etree.tostring(tr, encoding="unicode"))
            match = re.search(self._SIGN_LINK_RE, raw)
            if match:
                rows.append({
                    "task_id": match.group(1),
                    "site_id": match.group(2),
                    "node_seq": match.group(3),
                    "text": re.sub(r"\s+", " ", "".join(tr.itertext())).strip(),
                })
        return rows
