"""網頁起單共用 Playwright 助手 — 全部遵守 docs/web-apply-design.md 的強韌性規則。

核心原則：**poll-until-state，不用固定 sleep**。每個動作幾乎都觸發 ASP.NET postback、會把 iframe
重建；所以一律輪詢「目標狀態出現」（frame 就緒 / frame 消失 / 欄位被帶值 / 遮罩散去）再往下。

這些函式都操作 runtime 的單一 page（呼叫端必須已在 WebRuntime 的 worker thread 內）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional


def find_frame(page: Any, sub: str, *exclude: str) -> Optional[Any]:
    """回傳 url 含 sub、且非 dialog2 包裝、且不含任一 exclude 的 frame（找不到回 None）。"""
    for f in page.frames:
        if sub in f.url and "dialog2" not in f.url and all(e not in f.url for e in exclude):
            return f
    return None


def wait_frame(
    page: Any, sub: str, *exclude: str,
    timeout: float = 15, ready: Optional[Callable[[Any], bool]] = None,
) -> Optional[Any]:
    """輪詢直到 frame 出現（且 ready(frame) 為真）。回傳 frame 或 None。"""
    for _ in range(int(timeout * 4)):
        f = find_frame(page, sub, *exclude)
        if f is not None:
            try:
                if ready is None or ready(f):
                    return f
            except Exception:
                pass
        page.wait_for_timeout(250)
    return find_frame(page, sub, *exclude)


def wait_frame_gone(page: Any, sub: str, *exclude: str, timeout: float = 15) -> bool:
    """輪詢直到該 frame 消失（=對話框關閉，常是「選取/送出完成」的訊號）。"""
    for _ in range(int(timeout * 4)):
        if find_frame(page, sub, *exclude) is None:
            return True
        page.wait_for_timeout(250)
    return False


def first_site(page: Any) -> Optional[Any]:
    """採購單填寫頁的主 iframe。"""
    return find_frame(page, "FirstSite.aspx")


def no_overlay(page: Any, timeout: float = 12) -> None:
    """等 Telerik 遮罩散去（遮罩會攔截點擊）。"""
    for _ in range(int(timeout * 4)):
        ov = page.locator(".TelerikModalOverlay")
        if all(not ov.nth(i).is_visible() for i in range(ov.count())):
            return
        page.wait_for_timeout(250)


def js_click(locator: Any) -> None:
    """JS click：只用於被遮罩擋住的『開啟』按鈕（會回呼父視窗的選取/確定不要用這個）。"""
    locator.evaluate("e => e.click()")


def poll_value(
    page: Any, frame_sub: str, suffix: str, *exclude: str, timeout: float = 15,
) -> str:
    """輪詢某 frame 內某欄位（name 以 suffix 結尾）直到有值；每輪重抓 frame（postback 會換 frame）。"""
    for _ in range(int(timeout * 4)):
        f = first_site(page) if frame_sub == "FirstSite" else find_frame(page, frame_sub, *exclude)
        if f is not None:
            try:
                v = f.locator(f"[name$='{suffix}']").first.input_value()
                if v.strip():
                    return v
            except Exception:
                pass
        page.wait_for_timeout(250)
    return ""
