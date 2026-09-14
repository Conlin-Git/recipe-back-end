"""接口层公共依赖：数据库会话、当前登录用户。"""
from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException
from app.core.security import decode_access_token
from app.crud import user as user_crud
from app.database.mysql import get_db
from app.models.user import User

__all__ = ["get_db", "get_current_user"]


async def get_current_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从 Authorization: Bearer <token> 解析当前用户。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise BusinessException(401, "未登录或登录已过期")
    user_id = decode_access_token(authorization.removeprefix("Bearer ").strip())
    if user_id is None:
        raise BusinessException(401, "未登录或登录已过期")
    user = await user_crud.get_by_id(db, user_id)
    if not user:
        raise BusinessException(401, "用户不存在")
    return user
