"""对话支撑服务：会话创建/查找 + 滚动摘要压缩。

流式生成的主链路已迁移到 stream_service（生成与连接解耦，支持断网续传）：
- stream_service.start_generation：点火，起后台任务
- stream_service.run_generation：跑图、事件进 Redis Stream、落库 + 热上下文
- stream_service.subscribe：SSE 订阅（重放 + 跟随）
本模块保留被其复用的两个支撑函数。
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import BusinessException
from app.crud import conversation as conv_crud
from app.models.conversation import Conversation
from app.models.user import User
from app.schemas.chat import ChatRequest
from app.services import context_service, verify_service

SUMMARY_PROMPT = """请把以下对话记录压缩成一段摘要（250字以内），用于后续对话的上下文。按这三类组织，没有就跳过：
1. 用户透露的偏好、习惯、忌口等重要长期信息（必须保留）；
2. 讨论过的话题及关键结论，按领域分述——菜谱给做法要点（用量、火候、时间），
   技术给方案要点（选型、配置、结论），闲聊给情绪状态和关心的事；
3. 还没聊完、用户可能继续追问的事。
{existing}
对话记录：
{conversation}

只输出摘要本身，不要任何前缀或解释。"""


async def get_or_create_conversation(
    db: AsyncSession, user: User, req: ChatRequest
) -> Conversation:
    if req.conversation_id:
        conv = await conv_crud.get_conversation(db, req.conversation_id, user.id)
        if not conv:
            raise BusinessException(404, "会话不存在")
        return conv
    # 新会话：标题取首条消息前 20 字
    title = req.message.replace("\n", " ")[:20] or "新对话"
    return await conv_crud.create_conversation(db, user.id, title)


async def compress_context(conversation_id: int, old_summary: str, overflow: list[dict]) -> None:
    """后台压缩：旧摘要 + 滑出窗口的消息 → 新摘要。

    后台任务生命周期超过请求，使用独立的 db 会话。
    """
    from app.database.mysql import AsyncSessionLocal

    try:
        # P1：喂给摘要器的消息截断——提取要点不需要完整步骤原文，省输入 token
        conversation_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}："
            f"{m['content'][:settings.SUMMARY_SOURCE_MAX_CHARS]}"
            for m in overflow
        )
        existing = f"已有摘要：{old_summary}\n\n" if old_summary else ""
        # P0：摘要是简单压缩任务，用硅基流动免费 7B，不动付费的豆包
        # 后台任务不阻塞用户，单独放宽超时（client 默认 8s 是给关键路径的校验用的）
        resp = await verify_service.verify_client.chat.completions.create(
            model=settings.SILICONFLOW_LLM_MODEL,
            messages=[{"role": "user", "content": SUMMARY_PROMPT.format(
                existing=existing, conversation=conversation_text)}],
            max_tokens=400,
            timeout=30,
        )
        new_summary = (resp.choices[0].message.content or "").strip()
        if new_summary:
            async with AsyncSessionLocal() as db:
                conv = await db.get(Conversation, conversation_id)
                if conv:
                    await context_service.save_summary(db, conv, new_summary)
    except Exception as e:
        print(f"⚠️ 上下文压缩失败：{e}")
