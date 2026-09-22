from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.config import settings


class ChatRequest(BaseModel):
    # 长度上限防成本攻击：超长输入直接 422，不进 LLM 链路
    message: str = Field(min_length=1, max_length=settings.CHAT_MESSAGE_MAX_LENGTH)
    conversation_id: Optional[int] = None  # 为空则创建新会话


class ChatStartOut(BaseModel):
    """POST /chat 点火响应：生成在后台进行，前端凭此 ID 订阅 /chat/{id}/stream。"""
    conversation_id: int


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    html: Optional[str] = None  # assistant 消息的渲染 HTML，前端直接 v-html 展示
    created_at: datetime

    model_config = {"from_attributes": True}


class MessagePageOut(BaseModel):
    """历史消息分页：首屏只给最近一页（够上下文和摘要看），更早的下拉加载。"""
    messages: list[MessageOut]
    has_more: bool  # 是否还有更早的消息


class ConversationOut(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime
    # 是否有后台生成任务在进行（Redis chat:gen 标记）：
    # 前端刷新页面后据此自动重连续看（见 stream_service）
    generating: bool = False
    # 是否有晚于 last_read_at 的消息（后台生成完成的回答等），前端据此打未读红点
    unread: bool = False

    model_config = {"from_attributes": True}
