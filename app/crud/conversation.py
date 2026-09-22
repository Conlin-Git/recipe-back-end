"""conversations / messages 表的纯数据库操作。"""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message


async def create_conversation(
    db: AsyncSession, user_id: int, title: str = "新对话"
) -> Conversation:
    # 新会话默认已读：未读 = 有消息晚于 last_read_at（见会话列表接口）
    conv = Conversation(user_id=user_id, title=title, last_read_at=datetime.now())
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


async def list_conversations(db: AsyncSession, user_id: int) -> list[Conversation]:
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc())
    )
    return list(result.scalars().all())


async def get_conversation(
    db: AsyncSession, conversation_id: int, user_id: int
) -> Conversation | None:
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def list_recent_messages(
    db: AsyncSession, conversation_id: int, limit: int
) -> list[Message]:
    """最近 limit 条消息，按时间正序返回（用于恢复上下文窗口）。"""
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    return list(reversed(result.scalars().all()))


async def list_messages_page(
    db: AsyncSession, conversation_id: int, before_id: int | None, limit: int
) -> tuple[list[Message], bool]:
    """历史消息分页：before_id 之前（更早）的 limit 条，按时间正序返回。

    用 limit+1 技巧判断是否还有更早的消息（前端据此决定能否继续下拉加载）。
    before_id 为空则返回最新一页（进入会话的首屏）。
    """
    stmt = select(Message).where(Message.conversation_id == conversation_id)
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)
    stmt = stmt.order_by(Message.id.desc()).limit(limit + 1)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    has_more = len(rows) > limit
    return list(reversed(rows[:limit])), has_more


async def add_message(
    db: AsyncSession, conversation_id: int, role: str, content: str
) -> Message:
    msg = Message(conversation_id=conversation_id, role=role, content=content)
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


async def mark_read(db: AsyncSession, conversation_id: int) -> None:
    """查看即已读：把 last_read_at 拨到现在，此前的消息都不再算未读。"""
    conv = await db.get(Conversation, conversation_id)
    if conv:
        conv.last_read_at = datetime.now()
        await db.commit()


async def latest_message_times(
    db: AsyncSession, conversation_ids: list[int]
) -> dict[int, datetime]:
    """批量查每个会话的最新消息时间（会话列表算未读用）。"""
    if not conversation_ids:
        return {}
    result = await db.execute(
        select(Message.conversation_id, func.max(Message.created_at))
        .where(Message.conversation_id.in_(conversation_ids))
        .group_by(Message.conversation_id)
    )
    return {cid: latest for cid, latest in result.all()}
