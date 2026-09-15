"""安全相关：密码哈希、JWT 编解码、token 吊销（Redis 黑名单）。"""
import hashlib
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import settings
from app.database.redis import redis_client

# token 黑名单前缀：存 token 的 sha256 而非原文，即使 Redis 数据泄漏也无法还原出可用 token
_BLACKLIST_PREFIX = "jwt:blacklist:"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> int | None:
    """解码 JWT，返回 user_id；无效或过期返回 None。"""
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


def _token_remaining_seconds(token: str) -> int:
    """token 剩余有效秒数（用于黑名单 TTL，到期自动清）；无效 token 返回 0。"""
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        return max(0, int(payload["exp"] - datetime.now(timezone.utc).timestamp()))
    except (jwt.PyJWTError, KeyError, ValueError):
        return 0


async def revoke_token(token: str) -> None:
    """吊销 token：写入黑名单，TTL 与 token 剩余有效期一致。"""
    ttl = _token_remaining_seconds(token)
    if ttl > 0:
        await redis_client.set(
            f"{_BLACKLIST_PREFIX}{hashlib.sha256(token.encode()).hexdigest()}",
            "1",
            ex=ttl,
        )


async def is_token_revoked(token: str) -> bool:
    """token 是否已吊销。Redis 异常时 fail-open 视为未吊销（与项目旁路服务风格一致）。"""
    try:
        return bool(await redis_client.exists(
            f"{_BLACKLIST_PREFIX}{hashlib.sha256(token.encode()).hexdigest()}"
        ))
    except Exception as e:
        print(f"⚠️ token 吊销检查异常，按未吊销放行：{e}")
        return False
