from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[int] = None  # 为空则创建新会话


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    html: Optional[str] = None  # assistant 消息的渲染 HTML，前端直接 v-html 展示
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
