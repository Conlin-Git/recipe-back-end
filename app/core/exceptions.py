"""统一业务异常与异常处理器，保证接口错误格式一致：{"code": xxx, "message": "..."}"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class BusinessException(Exception):
    def __init__(self, code: int = 400, message: str = "业务错误"):
        self.code = code
        self.message = message


async def business_exception_handler(request: Request, exc: BusinessException):
    return JSONResponse(
        status_code=exc.code,
        content={"code": exc.code, "message": exc.message},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(BusinessException, business_exception_handler)
