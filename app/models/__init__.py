# 统一导出，方便 main 启动时 Base.metadata.create_all 收集全部表
from app.models.user import User
from app.models.conversation import Conversation
from app.models.message import Message

__all__ = ["User", "Conversation", "Message"]
