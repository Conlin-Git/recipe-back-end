"""对话接口（SSE 流式，需登录）。

上下文由服务端按 conversation_id 维护（Redis 热缓存 + MySQL 持久），
前端只需传 message 和 conversation_id（新会话传空）。
"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.user import User
from app.schemas.chat import ChatRequest
from app.services import chat_service

router = APIRouter(prefix="/chat", tags=["chat"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("")
async def chat(
    req: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return StreamingResponse(
        chat_service.stream_chat(db, current_user, req),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
