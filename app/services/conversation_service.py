"""会话与历史消息业务。多轮对话功能开发时由 api/v1/conversation.py 调用。"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException
from app.crud import conversation as conv_crud
from app.models.conversation import Conversation
from app.models.message import Message
from app.services import context_service


async def create(db: AsyncSession, user_id: int, title: str = "新对话") -> Conversation:
    return await conv_crud.create_conversation(db, user_id, title)


async def list_mine(db: AsyncSession, user_id: int) -> list[Conversation]:
    return await conv_crud.list_conversations(db, user_id)


async def get_history(db: AsyncSession, user_id: int, conversation_id: int) -> list[Message]:
    conv = await conv_crud.get_conversation(db, conversation_id, user_id)
    if not conv:
        raise BusinessException(404, "会话不存在")
    return await conv_crud.list_messages(db, conversation_id)


async def delete(db: AsyncSession, user_id: int, conversation_id: int) -> None:
    conv = await conv_crud.get_conversation(db, conversation_id, user_id)
    if not conv:
        raise BusinessException(404, "会话不存在")
    await db.delete(conv)
    await db.commit()
    # 清理 Redis 热上下文
    await context_service.clear_context(conversation_id)
