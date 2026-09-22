from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.crud import conversation as conv_crud
from app.models.user import User
from app.schemas.chat import ConversationOut, MessageOut, MessagePageOut
from app.services import conversation_service, stream_service
from app.utils.markdown import render_markdown

router = APIRouter(prefix="/conversations", tags=["conversation"])


@router.get("", response_model=List[ConversationOut])
async def list_conversations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    convs = await conversation_service.list_mine(db, current_user.id)
    conv_ids = [c.id for c in convs]
    # 批量查生成中标记（Redis pipeline），前端刷新后据此自动续看
    flags = await stream_service.generating_flags(conv_ids)
    # 批量查最新消息时间：晚于 last_read_at 即未读（后台生成完成的回答等）
    latest_times = await conv_crud.latest_message_times(db, conv_ids)
    return [
        ConversationOut(
            id=c.id, title=c.title, created_at=c.created_at,
            updated_at=c.updated_at, generating=flag,
            unread=latest_times.get(c.id, datetime.min)
            > (c.last_read_at or datetime.min),
        )
        for c, flag in zip(convs, flags)
    ]


@router.get("/{conversation_id}/messages", response_model=MessagePageOut)
async def get_history(
    conversation_id: int,
    before_id: int | None = None,  # 分页游标：加载比这更早的一页（下拉加载）
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    messages, has_more = await conversation_service.get_history(
        db, current_user.id, conversation_id, before_id, limit
    )
    # assistant 消息附带渲染好的 HTML，前端直接 v-html 展示
    return MessagePageOut(
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                html=render_markdown(m.content) if m.role == "assistant" else None,
                created_at=m.created_at,
            )
            for m in messages
        ],
        has_more=has_more,
    )


@router.post("/{conversation_id}/read", status_code=204)
async def mark_read(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await conversation_service.mark_read(db, current_user.id, conversation_id)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await conversation_service.delete(db, current_user.id, conversation_id)
