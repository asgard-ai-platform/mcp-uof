"""
UOF MCP Server — SSE HTTP 入口

架構對齊 mcp-uofx/sse_server.py：
- 使用 FastAPI + SSE 作為 HTTP 載體
- AuthMiddleware 攔截 Bearer Token
- contextvars 儲存當前使用者身分
"""

import contextvars
import os
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from mcp.server.fastmcp import FastMCP
from dotenv import load_dotenv
import uvicorn


from ._log import eprint as _eprint  # 診斷一律走 stderr（共用，勿在各檔複製）


load_dotenv(Path.cwd() / ".env", override=False)
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

SSE_HOST = os.getenv("MCP_SSE_HOST", "127.0.0.1")
SSE_PORT = int(os.getenv("MCP_SSE_PORT", "8001"))

# 使用 contextvars 來儲存目前連線的使用者身分 (Thread-safe / Async-safe)
current_user = contextvars.ContextVar("current_user", default=None)

# 建立 FastMCP 實例
mcp = FastMCP("UOF_SSE_Server")

@mcp.tool()
def uof_test_whoami() -> str:
    """測試用的 Tool，示範如何不依賴參數，直接從連線 Context 中得知使用者是誰"""
    user_code = current_user.get()
    if not user_code:
        return "❌ 伺服器內部錯誤：無法取得使用者身分"

    return f"✅ 驗證成功！目前執行指令的使用者是：{user_code}，您可以安全地執行專屬於此使用者的後端操作。"

# 建立 FastAPI app 作為載體
app = FastAPI(title="UOF MCP SSE Server")

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 只有存取 /sse 與 /messages (MCP 通訊通道) 才需要驗證
        if request.url.path.startswith("/sse") or request.url.path.startswith("/messages"):
            # 如果是 CORS Preflight (OPTIONS)，直接放行
            if request.method == "OPTIONS":
                return await call_next(request)

            auth_header = request.headers.get("Authorization")

            # 若無 Token 或格式不對，直接阻擋連線 (HTTP 401)
            if not auth_header or not auth_header.startswith("Bearer "):
                return JSONResponse(
                    status_code=401,
                    content={"error": "Unauthorized. Please provide a valid Bearer token."}
                )

            token = auth_header.split(" ")[1]

            # Demo 驗證：正式部署請改為 JWT 驗證或呼叫 UOF token 驗證端點。
            expected_token = os.getenv("MCP_SSE_TEST_TOKEN", "")
            if not expected_token or token != expected_token:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Invalid token."}
                )
            user_code = os.getenv("MCP_SSE_TEST_USER", "test_user")

            # 將解析出的身分存入 contextvars 中，讓後續的 MCP Tools 可以讀取
            current_user.set(user_code)

        # 放行請求
        response = await call_next(request)
        return response

# 掛載驗證攔截器
app.add_middleware(AuthMiddleware)

# 將 FastMCP 的 SSE 應用程式掛載到 FastAPI
mcp_asgi_app = mcp.sse_app()
app.mount("/", mcp_asgi_app)

def main():
    _eprint(f"啟動 UOF SSE Server ({SSE_HOST}:{SSE_PORT})")
    uvicorn.run(app, host=SSE_HOST, port=SSE_PORT)


if __name__ == "__main__":
    main()
