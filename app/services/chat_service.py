"""对话支撑服务：会话创建/查找 + 结构化滚动摘要压缩。

流式生成的主链路已迁移到 stream_service（生成与连接解耦，支持断网续传）：
- stream_service.start_generation：点火，起后台任务
- stream_service.run_generation：跑图、事件进 Redis Stream、落库 + 热上下文
- stream_service.subscribe：SSE 订阅（重放 + 跟随）
本模块保留被其复用的两个支撑函数。
"""
import json
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import BusinessException
from app.crud import conversation as conv_crud
from app.database.redis import redis_client
from app.models.conversation import Conversation
from app.models.user import User
from app.schemas.chat import ChatRequest
from app.services import context_service, verify_service

SUMMARY_PROMPT = """请把以下对话压缩成结构化记忆，用于后续多轮对话。只保留能帮助回答后续问题的事实，不要猜测。
字段要求：
- stable_facts：用户明确表达的长期偏好、限制、环境信息；
- topic_brief：当前话题和已确认结论；
- decisions：已经做出的方案、配置或选择；
- open_loops：尚未解决、用户可能继续追问的问题；
- entities：当前话题涉及的实体名称。
每个数组最多 8 项，每项简短具体；没有内容就输出空数组。
{existing}
对话记录：
{conversation}

只输出 JSON，不要 Markdown 或解释：
{{"stable_facts":[],"topic_brief":"","decisions":[],"open_loops":[],"entities":[]}}"""


def _parse_summary(text: str) -> str:
    """接受模型偶尔返回的 Markdown 包裹，统一保存为稳定 JSON 字符串。"""
    text = (text or "").strip()
    match = re.search(r"\{.*?\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                normalized = {
                    "stable_facts": data.get("stable_facts", []) if isinstance(data.get("stable_facts", []), list) else [],
                    "topic_brief": str(data.get("topic_brief", "")),
                    "decisions": data.get("decisions", []) if isinstance(data.get("decisions", []), list) else [],
                    "open_loops": data.get("open_loops", []) if isinstance(data.get("open_loops", []), list) else [],
                    "entities": data.get("entities", []) if isinstance(data.get("entities", []), list) else [],
                }
                return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        except json.JSONDecodeError:
            pass
    return text


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

    lock = None
    try:
        # 同一会话的摘要任务必须串行。否则上一轮慢任务可能在下一轮完成后
        # 才写回，把更新后的摘要覆盖掉。
        lock = redis_client.lock(
            f"chat:summary-lock:{conversation_id}",
            timeout=120,
            blocking_timeout=30,
        )
        acquired = await lock.acquire()
        if not acquired:
            print(f"⚠️ 上下文压缩跳过：会话 {conversation_id} 正在压缩")
            return

        # 等待锁期间可能已经有更新，重新读取最新摘要作为合并基线。
        old_summary = await context_service.load_summary(conversation_id, old_summary)
        # P1：喂给摘要器的消息截断——提取要点不需要完整步骤原文，省输入 token
        conversation_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}："
            f"{m['content'][:settings.SUMMARY_SOURCE_MAX_CHARS]}"
            for m in overflow
        )
        existing = f"已有摘要：{old_summary}\n\n" if old_summary else ""
        # P0：摘要是简单压缩任务，用硅基流动免费 7B，不动付费的主模型
        # 后台任务不阻塞用户，单独放宽超时（client 默认 8s 是给关键路径的校验用的）
        resp = await verify_service.verify_client.chat.completions.create(
            model=settings.SILICONFLOW_LLM_MODEL,
            messages=[{"role": "user", "content": SUMMARY_PROMPT.format(
                existing=existing, conversation=conversation_text)}],
            max_tokens=400,
            timeout=30,
        )
        new_summary = _parse_summary(resp.choices[0].message.content or "")
        if new_summary:
            async with AsyncSessionLocal() as db:
                conv = await db.get(Conversation, conversation_id)
                if conv:
                    await context_service.save_summary(db, conv, new_summary)
    except Exception as e:
        print(f"⚠️ 上下文压缩失败：{e}")
    finally:
        if lock is not None:
            try:
                await lock.release()
            except Exception:
                pass
