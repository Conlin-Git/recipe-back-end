"""v1 路由聚合：新增接口文件后在这里注册。"""
from fastapi import APIRouter

from app.api.v1 import auth, chat, conversation, image_proxy, user

api_v1_router = APIRouter(prefix="/api/v1")

api_v1_router.include_router(chat.router)
api_v1_router.include_router(auth.router)
api_v1_router.include_router(user.router)
api_v1_router.include_router(conversation.router)
api_v1_router.include_router(image_proxy.router)
