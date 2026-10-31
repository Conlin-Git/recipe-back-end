"""会话上下文服务：Redis 热上下文（最近N轮）+ 滚动摘要压缩。

存储设计：
- Redis 热缓存（TTL 7天）：
    chat:ctx:{conversation_id}     -> 最近N轮消息 JSON 列表 [{"role","content"},...]
    chat:summary:{conversation_id} -> 滚动摘要 str
- MySQL 持久层：
    messages 表全量消息、conversations.summary 摘要（Redis 丢失时可恢复）

分层加载（配合编排 agent 的追问判断，控制发给 LLM 的 token）：
- load_summary：摘要始终加载（体积小，编排分类和回答都要用）
- load_window：完整滑窗按需加载——追问且摘要足够时不进 prompt，
  摘要不足/全新查询时才由问答 agent 带上

压缩机制：窗口外最早的一轮（一问一答）滑出时，由调用方用 LLM 把
「旧摘要 + 滑出消息」压缩成新摘要（见 chat_service.compress_context）。

发给 LLM 前的三级整形（只影响 Redis 热上下文，MySQL 原文不动）：
- P0 入库瘦身：assistant 消息写热上下文前剥掉步骤配图链接（对后续对话零价值）
- P1 读时整形：最近 CHAT_CONTEXT_FULL_ROUNDS 轮保留全文，更早的截断；
  总量超 CHAT_CONTEXT_TOKEN_BUDGET 再从最旧整轮丢弃
  （丢弃只是这次不带，消息仍在窗口里，日后随窗口溢出正常进摘要）
- P2 摘要：见 chat_service.SUMMARY_PROMPT
"""
import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crud import conversation as conv_crud
from app.database.redis import redis_client
from app.models.conversation import Conversation
from app.models.conversation_state import ConversationState

MAX_MSGS = settings.CHAT_CONTEXT_ROUNDS * 2  # 一轮 = 一问一答
_TOPIC_STATE_PREFIX = "chat:topic:"

_IMG_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def clean_for_context(text: str) -> str:
    """P0 入库瘦身：剥掉 markdown 图片链接和剥完留下的空行。"""
    text = _IMG_PATTERN.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _truncate(text: str) -> str:
    limit = settings.CHAT_CONTEXT_TRUNCATE_CHARS
    return text if len(text) <= limit else text[:limit] + "…"


def _estimate_tokens(text: str) -> int:
    """轻量 token 估算，避免把字符数直接当 token 数。

    中文和代码中的非 ASCII 字符通常接近一个 token；连续 ASCII 文本按四字符
    估算。它不是模型 tokenizer，但比统一按字符截断稳定，也不增加运行时依赖。
    """
    if not text:
        return 0
    ascii_count = sum(ord(ch) < 128 for ch in text)
    other_count = len(text) - ascii_count
    return other_count + max(1, (ascii_count + 3) // 4)


def shape_context(ctx: list[dict]) -> list[dict]:
    """P1 读时整形：分级截断 + token 预算，控制发给 LLM 的历史体积。"""
    full_msgs = settings.CHAT_CONTEXT_FULL_ROUNDS * 2
    if len(ctx) <= full_msgs:
        return ctx
    shaped = [{**m, "content": _truncate(m["content"])} for m in ctx[:-full_msgs]]
    shaped += ctx[-full_msgs:]
    # 预算仍超 → 从最旧开始整轮丢弃（一轮 = 一问一答两条，成对删）
    budget = settings.CHAT_CONTEXT_TOKEN_BUDGET
    while len(shaped) > full_msgs and sum(_estimate_tokens(m["content"]) for m in shaped) > budget:
        del shaped[:2]
    return shaped


# append_turn 的原子实现：读窗口 → 追加一问一答 → 裁剪溢出 → 写回 + 续 TTL，
# 整个读-改-写在 Redis 内一次执行完，同一会话并发追加不会互相覆盖丢消息。
# ARGV: [user_msg_json, assistant_msg_json, max_msgs, ttl_seconds]
_APPEND_TURN_LUA = """
local raw = redis.call('GET', KEYS[1])
local ctx = {}
if raw then
    ctx = cjson.decode(raw)
end
table.insert(ctx, cjson.decode(ARGV[1]))
table.insert(ctx, cjson.decode(ARGV[2]))
local max = tonumber(ARGV[3])
local overflow = {}
while #ctx > max do
    table.insert(overflow, table.remove(ctx, 1))
end
local encoded = '[]'
if #ctx > 0 then
    encoded = cjson.encode(ctx)
end
redis.call('SET', KEYS[1], encoded, 'EX', tonumber(ARGV[4]))
if #overflow == 0 then
    return '[]'
end
return cjson.encode(overflow)
"""

_append_turn_script = redis_client.register_script(_APPEND_TURN_LUA)


def _ctx_key(conversation_id: int) -> str:
    return f"chat:ctx:{conversation_id}"


def _summary_key(conversation_id: int) -> str:
    return f"chat:summary:{conversation_id}"


async def load_summary(conversation_id: int, db_hint: str = "") -> str:
    """读取滚动摘要。Redis miss 时用 MySQL 里的摘要（db_hint）恢复并回填。"""
    summary = await redis_client.get(_summary_key(conversation_id))
    if summary is None:
        summary = db_hint
        if summary:
            await _set_summary(conversation_id, summary)
    return summary


async def load_window(db: AsyncSession, conversation_id: int) -> list[dict]:
    """读取最近N轮滑窗消息（P1 整形后）。Redis miss 时从 MySQL 恢复并回填。"""
    ctx_raw = await redis_client.get(_ctx_key(conversation_id))
    if ctx_raw is None:
        messages = await conv_crud.list_recent_messages(db, conversation_id, MAX_MSGS)
        # MySQL 里是原文，恢复进热上下文时同样过一遍 P0 清洗
        ctx = [{"role": m.role,
                "content": clean_for_context(m.content) if m.role == "assistant" else m.content}
               for m in messages]
        await _set_ctx(conversation_id, ctx)
    else:
        ctx = json.loads(ctx_raw)
    return shape_context(ctx)


async def append_turn(
    db: AsyncSession, conv: Conversation, question: str, answer: str
) -> list[dict]:
    """把新的一问一答写入上下文，返回溢出窗口的消息列表（供调用方判断是否需要压缩）。

    通过 Lua 脚本原子完成（见 _APPEND_TURN_LUA），同一会话并发调用只会
    影响追加顺序，不会丢失轮次。
    """
    user_msg = json.dumps({"role": "user", "content": question}, ensure_ascii=False)
    # P0：assistant 消息瘦身后再进热上下文（MySQL 已由调用方存了原文）
    assistant_msg = json.dumps(
        {"role": "assistant", "content": clean_for_context(answer)},
        ensure_ascii=False,
    )
    overflow_raw = await _append_turn_script(
        keys=[_ctx_key(conv.id)],
        args=[user_msg, assistant_msg, MAX_MSGS, settings.CHAT_CONTEXT_TTL_SECONDS],
    )
    return json.loads(overflow_raw)


async def save_summary(db: AsyncSession, conv: Conversation, summary: str) -> None:
    """新摘要双写：Redis 热缓存 + MySQL 持久。"""
    await _set_summary(conv.id, summary)
    conv.summary = summary
    await db.commit()


async def clear_context(conversation_id: int) -> None:
    """删除会话时清理所有上下文缓存。"""
    from app.services import dev_cache_service, recipe_cache_service

    await redis_client.delete(
        _ctx_key(conversation_id),
        _summary_key(conversation_id),
        f"{_TOPIC_STATE_PREFIX}{conversation_id}",
    )
    await recipe_cache_service.clear_cache(conversation_id)
    await dev_cache_service.clear_cache(conversation_id)


async def load_topic_state(db: AsyncSession, conversation_id: int) -> dict:
    """读取当前主题状态，Redis miss 时从 MySQL 恢复。"""
    raw = await redis_client.get(f"{_TOPIC_STATE_PREFIX}{conversation_id}")
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                return value
        except (TypeError, json.JSONDecodeError):
            pass
    try:
        result = await db.execute(
            select(ConversationState).where(
                ConversationState.conversation_id == conversation_id
            )
        )
        row = result.scalar_one_or_none()
        if row and row.state_json:
            value = json.loads(row.state_json)
            if isinstance(value, dict):
                await redis_client.set(
                    f"{_TOPIC_STATE_PREFIX}{conversation_id}",
                    json.dumps(value, ensure_ascii=False),
                    ex=settings.CHAT_CONTEXT_TTL_SECONDS,
                )
                return value
    except Exception as exc:
        # 兼容尚未执行 init_db 的旧环境，Redis 状态仍可工作。
        await db.rollback()
        print(f"⚠️ 读取会话主题状态失败：{exc}")
    return {}


async def save_topic_state(
    db: AsyncSession, conversation_id: int, state: dict, commit: bool = True
) -> None:
    """双写当前主题状态；MySQL 作为持久层，Redis 负责热读取。"""
    await redis_client.set(
        f"{_TOPIC_STATE_PREFIX}{conversation_id}",
        json.dumps(state, ensure_ascii=False),
        ex=settings.CHAT_CONTEXT_TTL_SECONDS,
    )
    try:
        result = await db.execute(
            select(ConversationState).where(
                ConversationState.conversation_id == conversation_id
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = ConversationState(
                conversation_id=conversation_id,
                state_json=json.dumps(state, ensure_ascii=False),
                revision=1,
            )
            db.add(row)
        else:
            row.state_json = json.dumps(state, ensure_ascii=False)
            row.revision += 1
            row.updated_at = datetime.now()
        if commit:
            await db.commit()
    except Exception as exc:
        await db.rollback()
        print(f"⚠️ 持久化会话主题状态失败：{exc}")


async def _set_ctx(conversation_id: int, ctx: list[dict]) -> None:
    await redis_client.set(
        _ctx_key(conversation_id), json.dumps(ctx, ensure_ascii=False),
        ex=settings.CHAT_CONTEXT_TTL_SECONDS,
    )


async def _set_summary(conversation_id: int, summary: str) -> None:
    await redis_client.set(
        _summary_key(conversation_id), summary,
        ex=settings.CHAT_CONTEXT_TTL_SECONDS,
    )
