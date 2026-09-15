"""会话级菜谱缓存：RAG 命中的菜谱片段存 Redis，同会话内追问直接复用。

第一次检索命中后，完整菜谱片段（含步骤和配图）写入当前会话的缓存；
后续追问同一道菜时由菜谱 agent 直接引用缓存内容回答，不再走向量检索
（embed/milvus/rerank/校验全省）。追问的菜和缓存不相关时，agent 自行
调用 search_recipe 工具检索并刷新缓存。

存储：chat:recipes:{conversation_id} -> JSON 列表，按 id 去重、新的在前，
上限 RECIPE_CACHE_MAX_ITEMS 条，TTL 与热上下文一致（7 天）。
"""
import json

from app.config import settings
from app.database.redis import redis_client


def _cache_key(conversation_id: int) -> str:
    return f"chat:recipes:{conversation_id}"


async def get_cached_recipes(conversation_id: int) -> list[dict]:
    """读取本会话缓存的菜谱片段，无缓存返回 []。"""
    raw = await redis_client.get(_cache_key(conversation_id))
    return json.loads(raw) if raw else []


async def set_cached_recipes(conversation_id: int, recipes: list[dict]) -> None:
    """新检索到的菜谱并入缓存：按 id 去重，新命中的排前面，超出上限截断。"""
    if not recipes:
        return
    existing = await get_cached_recipes(conversation_id)
    new_ids = {r["id"] for r in recipes}
    merged = list(recipes) + [r for r in existing if r.get("id") not in new_ids]
    merged = merged[: settings.RECIPE_CACHE_MAX_ITEMS]
    await redis_client.set(
        _cache_key(conversation_id), json.dumps(merged, ensure_ascii=False),
        ex=settings.CHAT_CONTEXT_TTL_SECONDS,
    )


async def clear_cache(conversation_id: int) -> None:
    await redis_client.delete(_cache_key(conversation_id))
