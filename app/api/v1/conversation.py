from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.chat import ConversationOut, MessageOut
from app.services import conversation_service, stream_service
from app.utils.markdown import render_markdown

router = APIRouter(prefix="/conversations", tags=["conversation"])


@router.get("", response_model=List[ConversationOut])
async def list_conversations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    convs = await conversation_service.list_mine(db, current_user.id)
    # 批量查生成中标记（Redis pipeline），前端刷新后据此自动续看
    flags = await stream_service.generating_flags([c.id for c in convs])
    return [
        ConversationOut(
            id=c.id, title=c.title, created_at=c.created_at,
            updated_at=c.updated_at, generating=flag,
        )
        for c, flag in zip(convs, flags)
    ]


@router.get("/{conversation_id}/messages", response_model=List[MessageOut])
async def get_history(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    messages = await conversation_service.get_history(db, current_user.id, conversation_id)
    # assistant 消息附带渲染好的 HTML，前端直接 v-html 展示
    return [
        MessageOut(
            id=m.id,
            role=m.role,
            content=m.content,
            html=render_markdown(m.content) if m.role == "assistant" else None,
            created_at=m.created_at,
        )
        for m in messages
    ]


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await conversation_service.delete(db, current_user.id, conversation_id)
