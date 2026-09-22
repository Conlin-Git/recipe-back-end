"""统一业务异常与异常处理器：错误响应与成功响应同一封装。

格式：{"code": 非0, "data": null, "msg": "可读错误信息"}
HTTP 状态码保留语义（401/404/409/422/429 等），前端既可按状态码分流
（如 401 自动登出），也可直接展示 msg。
"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class BusinessException(Exception):
    def __init__(self, code: int = 400, message: str = "业务错误"):
        self.code = code
        self.message = message


def error_body(code: int, msg: str) -> dict:
    return {"code": code, "data": None, "msg": msg}


async def business_exception_handler(request: Request, exc: BusinessException):
    return JSONResponse(
        status_code=exc.code,
        content=error_body(exc.code, exc.message),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """框架级 HTTP 错误（404 路由不存在、405 等）也统一成标准封装。"""
    msg = exc.detail if isinstance(exc.detail, str) else "请求错误"
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.status_code, msg),
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """入参校验失败：取第一条错误拼成可读信息，不把内部结构暴露给前端。"""
    err = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(
        str(x) for x in err.get("loc", []) if x not in ("body", "query", "path")
    )
    detail = err.get("msg", "参数校验失败")
    msg = f"参数错误（{loc}）：{detail}" if loc else f"参数错误：{detail}"
    return JSONResponse(status_code=422, content=error_body(422, msg))


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(BusinessException, business_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
