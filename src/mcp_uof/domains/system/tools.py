"""
Domain: System — 認證 MCP Tool 工具層。

`check_auth` 委派給當前 AuthProvider 的 status_report()——
token mode 報 Token 狀態，session mode 報 cookie session 狀態。
"""
from ...auth import get_provider


def check_auth() -> str:
    """檢查目前 auth mode 下的身份與認證狀態（自動觸發必要的更新）。"""
    return get_provider().status_report()
