"""会话级开发书籍缓存：RAG 命中的书摘块存 Redis，同会话内追问直接复用。

与 recipe_cache_service 同构：第一次检索命中后书摘块写入当前会话缓存，
后续追问同一知识点时由 dev agent 直接引用缓存内容回答，不再走向量检索
（embed/milvus/rerank 全省）。追问和缓存不相关时，agent 自行调用
search_dev_book 工具检索并刷新缓存。

存储：chat:devbook:{conversation_id} -> JSON 列表，按 id 去重、新的在前，
上限 DEV_CACHE_MAX_ITEMS 条，TTL 与热上下文一致（7 天）。
"""
import json

from app.config import settings
from app.database.redis import redis_client


def _cache_key(conversation_id: int) -> str:
    return f"chat:devbook:{conversation_id}"


async def get_cached_chunks(conversation_id: int) -> list[dict]:
    """读取本会话缓存的书摘块，无缓存返回 []。"""
    raw = await redis_client.get(_cache_key(conversation_id))
    return json.loads(raw) if raw else []


async def set_cached_chunks(conversation_id: int, chunks: list[dict]) -> None:
    """新检索到的书摘块并入缓存：按 id 去重，新命中的排前面，超出上限截断。"""
    if not chunks:
        return
    existing = await get_cached_chunks(conversation_id)
    new_ids = {c["id"] for c in chunks}
    merged = list(chunks) + [c for c in existing if c.get("id") not in new_ids]
    merged = merged[: settings.DEV_CACHE_MAX_ITEMS]
    await redis_client.set(
        _cache_key(conversation_id), json.dumps(merged, ensure_ascii=False),
        ex=settings.CHAT_CONTEXT_TTL_SECONDS,
    )


async def clear_cache(conversation_id: int) -> None:
    await redis_client.delete(_cache_key(conversation_id))
