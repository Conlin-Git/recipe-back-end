"""个人信息 / 头像业务。"""
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException
from app.crud import user as user_crud
from app.models.user import User
from app.schemas.user import UserUpdateIn

AVATAR_DIR = Path("uploads/avatars")
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_AVATAR_SIZE = 2 * 1024 * 1024  # 2MB


async def update_profile(db: AsyncSession, user: User, data: UserUpdateIn) -> User:
    if data.nickname is not None:
        user.nickname = data.nickname
    if data.email is not None:
        user.email = data.email
    return await user_crud.update(db, user)


async def save_avatar(db: AsyncSession, user: User, file: UploadFile) -> User:
    ext = ALLOWED_TYPES.get(file.content_type or "")
    if not ext:
        raise BusinessException(400, "仅支持 jpg/png/webp 格式头像")
    content = await file.read()
    if len(content) > MAX_AVATAR_SIZE:
        raise BusinessException(400, "头像大小不能超过 2MB")

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{user.id}_{uuid.uuid4().hex[:8]}{ext}"
    (AVATAR_DIR / filename).write_bytes(content)

    user.avatar_url = f"/uploads/avatars/{filename}"
    return await user_crud.update(db, user)
