"""对话接口（生成与连接解耦，需登录）。

- POST /chat：点火。起后台生成任务（token 缓存进 Redis Stream），立即返回会话ID
- GET /chat/{id}/stream?last_id=0：SSE 订阅。last_id 之后的事件重放 + 阻塞跟随，
  可反复断开重连（终止按钮/断网/刷新都不影响后端生成）

上下文由服务端按 conversation_id 维护（Redis 热缓存 + MySQL 持久），
前端只需传 message 和 conversation_id（新会话传空）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.config import settings
from app.core import rate_limit
from app.core.exceptions import BusinessException
from app.crud import conversation as conv_crud
from app.models.user import User
from app.schemas.chat import ChatRequest, ChatStartOut
from app.services import stream_service

router = APIRouter(prefix="/chat", tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("", response_model=ChatStartOut)
async def chat(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 限流必须在点火之前：每次生成会触发编排/情感/（可能）校验多路 LLM 调用
    await rate_limit.check_chat_rate(current_user.id, settings.CHAT_RATE_LIMIT_PER_MINUTE)
    conv = await stream_service.start_generation(db, current_user, req)
    return ChatStartOut(conversation_id=conv.id)


@router.get("/{conversation_id}/stream")
async def chat_stream(
    conversation_id: int,
    last_id: str = "0",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await conv_crud.get_conversation(db, conversation_id, current_user.id)
    if not conv:
        raise BusinessException(404, "会话不存在")
    return StreamingResponse(
        stream_service.subscribe(conversation_id, last_id),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
