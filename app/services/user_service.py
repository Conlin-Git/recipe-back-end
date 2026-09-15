"""个人信息 / 头像业务。"""
import asyncio
import io
import uuid
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException
from app.crud import user as user_crud
from app.models.user import User
from app.schemas.user import UserUpdateIn

AVATAR_DIR = Path("uploads/avatars")
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
# 原始图上限放宽到 10MB（手机原图普遍 >2MB），服务端压缩后落盘
MAX_AVATAR_SIZE = 10 * 1024 * 1024
AVATAR_MAX_SIDE = 512
AVATAR_QUALITY = 85


async def update_profile(db: AsyncSession, user: User, data: UserUpdateIn) -> User:
    if data.nickname is not None:
        user.nickname = data.nickname
    if data.email is not None:
        user.email = data.email
    return await user_crud.update(db, user)


def _compress_avatar(content: bytes) -> bytes:
    """缩放 + 转 JPEG：最长边 512，PNG 透明底铺白，按 EXIF 矫正手机拍摄方向。"""
    img = Image.open(io.BytesIO(content))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        # RGBA/P 等转 RGB，透明区域铺白底（直接转 RGB 透明处会变黑）
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    img.thumbnail((AVATAR_MAX_SIDE, AVATAR_MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=AVATAR_QUALITY, optimize=True)
    return out.getvalue()


async def save_avatar(db: AsyncSession, user: User, file: UploadFile) -> User:
    if (file.content_type or "") not in ALLOWED_TYPES:
        raise BusinessException(400, "仅支持 jpg/png/webp 格式头像")
    content = await file.read()
    if len(content) > MAX_AVATAR_SIZE:
        raise BusinessException(400, "头像大小不能超过 10MB")

    try:
        # Pillow 是 CPU 密集同步调用，放线程里避免阻塞事件循环
        compressed = await asyncio.to_thread(_compress_avatar, content)
    except Exception:
        raise BusinessException(400, "图片解析失败，请换一张试试")

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{user.id}_{uuid.uuid4().hex[:8]}.jpg"
    (AVATAR_DIR / filename).write_bytes(compressed)

    user.avatar_url = f"/uploads/avatars/{filename}"
    return await user_crud.update(db, user)
