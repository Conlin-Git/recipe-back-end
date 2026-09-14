"""会话上下文服务：Redis 热上下文（最近N轮）+ 滚动摘要压缩。

存储设计：
- Redis 热缓存（TTL 7天）：
    chat:ctx:{conversation_id}     -> 最近N轮消息 JSON 列表 [{"role","content"},...]
    chat:summary:{conversation_id} -> 滚动摘要 str
- MySQL 持久层：
    messages 表全量消息、conversations.summary 摘要（Redis 丢失时可恢复）

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

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crud import conversation as conv_crud
from app.database.redis import redis_client
from app.models.conversation import Conversation

MAX_MSGS = settings.CHAT_CONTEXT_ROUNDS * 2  # 一轮 = 一问一答

_IMG_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def clean_for_context(text: str) -> str:
    """P0 入库瘦身：剥掉 markdown 图片链接和剥完留下的空行。"""
    text = _IMG_PATTERN.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _truncate(text: str) -> str:
    limit = settings.CHAT_CONTEXT_TRUNCATE_CHARS
    return text if len(text) <= limit else text[:limit] + "…"


def shape_context(ctx: list[dict]) -> list[dict]:
    """P1 读时整形：分级截断 + token 预算，控制发给 LLM 的历史体积。"""
    full_msgs = settings.CHAT_CONTEXT_FULL_ROUNDS * 2
    if len(ctx) <= full_msgs:
        return ctx
    shaped = [{**m, "content": _truncate(m["content"])} for m in ctx[:-full_msgs]]
    shaped += ctx[-full_msgs:]
    # 预算仍超 → 从最旧开始整轮丢弃（一轮 = 一问一答两条，成对删）
    budget = settings.CHAT_CONTEXT_TOKEN_BUDGET
    while len(shaped) > full_msgs and sum(len(m["content"]) for m in shaped) > budget:
        del shaped[:2]
    return shaped


def _ctx_key(conversation_id: int) -> str:
    return f"chat:ctx:{conversation_id}"


def _summary_key(conversation_id: int) -> str:
    return f"chat:summary:{conversation_id}"


async def get_context(db: AsyncSession, conv: Conversation) -> tuple[list[dict], str]:
    """读取会话上下文，返回 (最近N轮消息, 滚动摘要)。Redis miss 时从 MySQL 恢复并回填。"""
    ctx_raw = await redis_client.get(_ctx_key(conv.id))
    summary = await redis_client.get(_summary_key(conv.id))

    if ctx_raw is None:
        messages = await conv_crud.list_recent_messages(db, conv.id, MAX_MSGS)
        # MySQL 里是原文，恢复进热上下文时同样过一遍 P0 清洗
        ctx = [{"role": m.role,
                "content": clean_for_context(m.content) if m.role == "assistant" else m.content}
               for m in messages]
        await _set_ctx(conv.id, ctx)
    else:
        ctx = json.loads(ctx_raw)

    if summary is None:
        summary = conv.summary or ""
        if summary:
            await _set_summary(conv.id, summary)

    return shape_context(ctx), summary


async def append_turn(
    db: AsyncSession, conv: Conversation, question: str, answer: str
) -> list[dict]:
    """把新的一问一答写入上下文，返回窗口内的消息列表（供调用方判断是否需要压缩）。"""
    ctx_raw = await redis_client.get(_ctx_key(conv.id))
    ctx = json.loads(ctx_raw) if ctx_raw else []
    ctx.append({"role": "user", "content": question})
    # P0：assistant 消息瘦身后再进热上下文（MySQL 已由调用方存了原文）
    ctx.append({"role": "assistant", "content": clean_for_context(answer)})

    overflow: list[dict] = []
    if len(ctx) > MAX_MSGS:
        overflow = ctx[: len(ctx) - MAX_MSGS]
        ctx = ctx[len(ctx) - MAX_MSGS:]

    await _set_ctx(conv.id, ctx)
    return overflow


async def save_summary(db: AsyncSession, conv: Conversation, summary: str) -> None:
    """新摘要双写：Redis 热缓存 + MySQL 持久。"""
    await _set_summary(conv.id, summary)
    conv.summary = summary
    await db.commit()


async def clear_context(conversation_id: int) -> None:
    """删除会话时清理缓存。"""
    await redis_client.delete(_ctx_key(conversation_id), _summary_key(conversation_id))


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
