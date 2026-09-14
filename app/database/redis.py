"""Redis 客户端：会话热上下文缓存（最近N轮 + 滚动摘要）。"""
import redis.asyncio as redis

from app.config import settings

redis_client = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    password=settings.REDIS_PASSWORD or None,
    decode_responses=True,  # 直接返回 str，免 decode
)
