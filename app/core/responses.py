"""统一成功响应封装：/api 下 2xx 的 JSON 响应自动包成 {"code": 0, "data": ..., "msg": ""}。

错误响应由 app.core.exceptions 的处理器包装成同一格式（code 非 0），
前后端只认这一种结构。SSE 流、图片二进制、204 No Content 原样放行。

用中间件而非自定义 APIRoute：FastAPI 0.141 起 include_router 改为惰性挂载
（_IncludedRouter），父 router 的 route_class 不会传导给子路由。
"""
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


async def _envelope_middleware(request: Request, call_next):
    response = await call_next(request)
    if (
        not request.url.path.startswith("/api/")
        or response.status_code == 204
        or response.status_code >= 400
        or not response.headers.get("content-type", "").startswith(
            "application/json"
        )
    ):
        return response
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()
    return JSONResponse(
        status_code=response.status_code,
        content={"code": 0, "data": json.loads(body), "msg": ""},
    )


def register_envelope_middleware(app: FastAPI) -> None:
    app.middleware("http")(_envelope_middleware)
