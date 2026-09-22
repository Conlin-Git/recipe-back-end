"""会话与历史消息业务。多轮对话功能开发时由 api/v1/conversation.py 调用。"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException
from app.crud import conversation as conv_crud
from app.models.conversation import Conversation
from app.models.message import Message
from app.services import context_service, stream_service


async def create(db: AsyncSession, user_id: int, title: str = "新对话") -> Conversation:
    return await conv_crud.create_conversation(db, user_id, title)


async def list_mine(db: AsyncSession, user_id: int) -> list[Conversation]:
    return await conv_crud.list_conversations(db, user_id)


async def get_history(
    db: AsyncSession, user_id: int, conversation_id: int,
    before_id: int | None = None, limit: int = 20,
) -> tuple[list[Message], bool]:
    """历史消息分页：返回 (正序消息, 是否还有更早)。查看即已读。"""
    conv = await conv_crud.get_conversation(db, conversation_id, user_id)
    if not conv:
        raise BusinessException(404, "会话不存在")
    page = await conv_crud.list_messages_page(db, conversation_id, before_id, limit)
    # 查看即已读：清掉该会话的未读标记
    await conv_crud.mark_read(db, conversation_id)
    return page


async def mark_read(db: AsyncSession, user_id: int, conversation_id: int) -> None:
    """显式已读：观看中的生成刚完成时前端补调（消息落库晚于上次查看）。"""
    conv = await conv_crud.get_conversation(db, conversation_id, user_id)
    if not conv:
        raise BusinessException(404, "会话不存在")
    await conv_crud.mark_read(db, conversation_id)


async def delete(db: AsyncSession, user_id: int, conversation_id: int) -> None:
    conv = await conv_crud.get_conversation(db, conversation_id, user_id)
    if not conv:
        raise BusinessException(404, "会话不存在")
    await db.delete(conv)
    await db.commit()
    # 取消进行中的生成任务 + 清理流缓冲和热上下文
    await stream_service.cancel(conversation_id)
    await context_service.clear_context(conversation_id)
