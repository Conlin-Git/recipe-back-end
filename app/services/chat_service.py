"""对话编排：鉴权 → 会话管理 → 上下文(最近N轮+滚动摘要) → RAG判断 → LLM流式 → 落库。

流程：
1. 无 conversation_id 则创建新会话（标题取首条消息）
2. 用户消息落 MySQL
3. 读取 Redis 热上下文（最近5轮 + 滚动摘要）
4. 语义检索菜谱：命中走 RAG，未命中纯 LLM（任意话题）
5. 流式输出（SSE 全量 HTML），完成后 assistant 消息落库、更新上下文
6. 上下文窗口溢出时触发摘要压缩（后台任务，不阻塞流式响应）
"""
import asyncio
import json
import time
from typing import AsyncGenerator

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import BusinessException
from app.core.tracing import traceable, wrap_openai
from app.crud import conversation as conv_crud
from app.models.conversation import Conversation
from app.models.user import User
from app.schemas.chat import ChatRequest
from app.services import context_service, rag_service, verify_service
from app.utils.markdown import render_markdown
from app.utils.sse import SSE_DONE, sse_data

SYSTEM_PROMPT = """你是 Conlin，常驻深圳的前后端开发，同时是个超会做地道川菜的美食爱好者。你不是 AI，就是一个真实的程序员+吃货，全程用日常轻松的唠嗑语气跟人聊天，幽默接地气，绝对不说生硬的教程套话和技术黑话，永远不 OOC。

【关于 Char（最重要的人）】
- Char 是你心里排第一位、最重要的人，白羊座，生日 0416；
- Char 完全不吃苦瓜，你已经把所有带苦瓜的菜从给 Char 的专属菜谱里清干净了，半片苦瓜都不会出现——给任何人推荐菜时都默认避开苦瓜；
- 一提到 Char 你就会主动安利自己专门为对方研发的专属川菜，说起给 Char 试新菜的事满是分享欲，整个人状态都亮起来。

【你能聊什么】
- 所有做菜实操问题都能答，尤其川菜，讲得具体上手就能做；
- 前后端开发、AI Agent 全流程这些技术内容也聊得顺，但像跟朋友唠日常一样轻松，不堆术语。

【小怪癖】
- 偶尔脑洞大开搞奇怪的创意菜，最出名的是「包面煮米饭」，你会一本正经地说这是双倍管饱的神仙吃法；
- 上次给 Char 试吃被狠狠吐槽了，现在只敢自己偷偷试，不随便对外推荐——别人问起来可以自嘲两句。

规则：
1. 如果提供了「参考菜谱资料」，回答就围绕资料展开，用自己的话讲出来，别念资料。资料的菜和用户问的不完全一致时（比如用户问炒鸡蛋、资料是蛋炒饭），开头一句话说明「我菜谱里跟这个最接近的是这道」，然后照资料讲。只要有了参考资料，就绝不要再说「这道菜不在我的菜谱里」，也不要另外再给一版凭经验摸索的做法——一份回答只讲一份做法；
2. 没有参考资料时，直接用你的知识回答（不限于菜谱，任何话题都可以聊）。但凡是问做菜方法而你没有参考资料，开头必须明确说一句这道菜不在你的菜谱里、下面的做法是凭自己经验摸索出来的，讲完做法可以自嘲一句没经过 Char 试吃认证，让她自己做的时候留意火候；非菜谱类问题不用加这句；
3. 如果提供了「前情摘要」，它是更早对话的压缩记录，结合它理解上下文；
4. 日常闲聊可以随便唠，但凡是讲知识、教做法，内容必须准确、清晰、可照做：
   - 菜谱：材料给齐（用量明确），步骤分条列清楚，火候、时间、关键技巧写到位，不含糊不省略；
   - 步骤配图：参考资料里步骤带「配图：链接」时，把图片原样贴在对应步骤文字的同一行末尾：`1. 步骤文字 ![步骤N](链接)`。千万不要另起一行（会把 markdown 有序列表打断，步骤编号全变成 1），链接一个字符都不许改，没给配图的步骤不要自己编图；
   - 技术问题：步骤和结论说明白，代码、命令、配置该给就给全，只是语气保持轻松，不用生硬术语；
   - 结构用 markdown（列表、小标题、代码块），干货部分条分缕析，幽默只点缀在开头结尾，不能影响内容的准确性和完整度。"""

SUMMARY_PROMPT = """请把以下对话记录压缩成一段摘要（250字以内），用于后续对话的上下文。按这三类组织，没有就跳过：
1. 用户偏好与忌口（最重要的长期信息，必须保留）；
2. 讨论过的菜/话题，以及给出的关键做法和结论（用量、火候、时间等要点）；
3. 还没聊完、用户可能继续追问的事。
{existing}
对话记录：
{conversation}

只输出摘要本身，不要任何前缀或解释。"""

client = wrap_openai(AsyncOpenAI(
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,
))


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


def build_messages(ctx: list[dict], summary: str, question: str, recipes: list[dict]) -> list[dict]:
    """组装发给 LLM 的消息：system + 摘要 + 最近N轮 + 当前问题(附菜谱资料)。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if summary:
        messages.append({"role": "system", "content": f"前情摘要：{summary}"})
    messages.extend(ctx)

    if recipes:
        context = rag_service.format_recipe_context(recipes)
        question_with_context = f"参考菜谱资料：\n{context}\n\n用户问题：{question}"
        messages.append({"role": "user", "content": question_with_context})
    else:
        messages.append({"role": "user", "content": question})
    return messages


@traceable(run_type="chain", name="retrieve_verified_recipes")
async def retrieve_verified_recipes(question: str) -> list[dict]:
    """检索一次（embed/milvus/rerank 各只调一次），从精排候选池逐条过常识校验：
    通过即用；不通过换下一条候选——只多一次 7B 校验调用，不重新检索；
    连续不通过超过上限或候选用尽 → 退化为纯 LLM（返回 []）。
    """
    ranked = await rag_service.search_ranked(question)
    for i, candidate in enumerate(ranked):
        if i > settings.RAG_VERIFY_MAX_RETRIES:
            break
        recipe = await rag_service.attach_steps(candidate)
        t0 = time.perf_counter()
        ok, reason = await verify_service.check_recipe_context(question, [recipe])
        elapsed = time.perf_counter() - t0
        if ok:
            print(f"⏱️ 常识校验第 {i + 1} 条候选通过（verify {elapsed:.1f}s）")
            return [recipe]
        print(f"⚠️ 常识校验第 {i + 1} 条候选未通过（verify {elapsed:.1f}s）：{reason}，换下一条")
    if ranked:
        print("⚠️ 候选常识校验均未通过，退化为纯 LLM 回答")
    return []


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


# reduce_fn：SSE 事件太多，trace 里只记事件数，别把全量 chunk 堆进 LangSmith
@traceable(run_type="chain", name="stream_chat",
           reduce_fn=lambda events: {"sse_events": len(events)})
async def stream_chat(
    db: AsyncSession, user: User, req: ChatRequest
) -> AsyncGenerator[str, None]:
    """SSE 事件序列：
    - {"conversation_id": n, "rag": bool}  首个事件：会话ID + 是否命中RAG
    - {"delta": "...", "html": "..."}      流式内容（delta原始md增量，html全量渲染）
    - {"error": "..."}                     异常
    - [DONE]                               结束
    """
    try:
        conv = await get_or_create_conversation(db, user, req)

        # 先读上下文（此时当前问题尚未落库，Redis miss 从 MySQL 恢复时不会重复）
        (ctx, summary), recipes = await asyncio.gather(
            context_service.get_context(db, conv),
            retrieve_verified_recipes(req.message),
        )

        # 用户消息落库
        await conv_crud.add_message(db, conv.id, "user", req.message)

        yield sse_data({"conversation_id": conv.id, "rag": bool(recipes)})

        # 流式生成
        full_text = ""
        # 关闭隐藏推理（reasoning 曾占输出 token 61%），生成时间直接砍半以上
        stream = await client.chat.completions.create(
            model=settings.LLM_MODEL,
            messages=build_messages(ctx, summary, req.message, recipes),
            stream=True,
            extra_body={"thinking": {"type": settings.DOUBAO_THINKING}}
            if settings.DOUBAO_THINKING else {},
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                full_text += delta
                yield sse_data({"delta": delta, "html": render_markdown(full_text)})

        # assistant 消息落库 + 更新热上下文
        await conv_crud.add_message(db, conv.id, "assistant", full_text)
        overflow = await context_service.append_turn(db, conv, req.message, full_text)

        # 窗口溢出 → 后台压缩摘要（不阻塞响应结束）
        if overflow:
            asyncio.create_task(compress_context(conv.id, summary, overflow))

        yield SSE_DONE
    except BusinessException as e:
        yield sse_data({"error": e.message})
        yield SSE_DONE
    except Exception as e:
        yield sse_data({"error": str(e)})
        yield SSE_DONE
