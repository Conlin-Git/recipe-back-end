"""接口层公共依赖：数据库会话、当前登录用户。"""
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException
from app.core.filing_review import FILING_REVIEW_SCOPE, filing_review_allows
from app.core.security import decode_access_token_payload, is_token_revoked
from app.crud import user as user_crud
from app.database.mysql import get_db
from app.models.user import User

__all__ = ["get_db", "get_current_user"]


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从 Authorization: Bearer <token> 解析当前用户。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise BusinessException(401, "未登录或登录已过期")
    token = authorization.removeprefix("Bearer ").strip()
    payload = decode_access_token_payload(token)
    if payload is None or await is_token_revoked(token):
        raise BusinessException(401, "未登录或登录已过期")
    if payload.get("scope") == FILING_REVIEW_SCOPE:
        route = request.scope.get("route")
        if not filing_review_allows(request.method, getattr(route, "path", "")):
            raise BusinessException(403, "审核账号仅支持内容查看和问答")
    user_id = int(payload["sub"])
    user = await user_crud.get_by_id(db, user_id)
    if not user:
        raise BusinessException(401, "用户不存在")
    return user
