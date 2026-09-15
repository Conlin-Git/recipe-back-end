from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core import security
from app.core.exceptions import BusinessException
from app.models.user import User
from app.schemas.auth import LoginIn, RegisterIn, TokenOut
from app.schemas.user import UserOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
async def register(data: RegisterIn, db: AsyncSession = Depends(get_db)):
    return await auth_service.register(db, data)


@router.post("/login", response_model=TokenOut)
async def login(data: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    token = await auth_service.login(db, data, client_ip)
    return TokenOut(access_token=token)


@router.post("/logout", status_code=204)
async def logout(
    authorization: str | None = Header(default=None),
    current_user: User = Depends(get_current_user),
):
    """登出：吊销当前 token（黑名单 TTL 与 token 剩余有效期一致，到期自动清）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise BusinessException(401, "未登录或登录已过期")
    await security.revoke_token(authorization.removeprefix("Bearer ").strip())
