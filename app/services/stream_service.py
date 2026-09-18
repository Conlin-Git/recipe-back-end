"""流式生成任务管理器：生成与连接解耦，支持断网续传。

架构：
- POST /chat 只"点火"：起后台 asyncio 任务跑 LangGraph，token 事件 XADD 进
  Redis Stream（chat:stream:{cid}），生成完毕照常落库 MySQL + 更新热上下文
- GET /chat/{cid}/stream 是可重入的"订阅"端点：带 last_id 重放缓冲 + 阻塞
  跟随实时事件。客户端（终止按钮/断网/刷新）随时断开重连，不影响生成
- chat:gen:{cid} 标记"生成中"：POST 防重入（同会话同时只跑一个）、会话列表打标
  （前端刷新后据此自动续看）

单 worker 前提：任务注册表是进程内 dict（部署为 uvicorn 单进程，见 README）。
多 worker 需要把任务投递改成队列方案（如 arq/Dramatiq），暂不支持。
"""
import asyncio
import json
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessException
from app.core.tracing import traceable
from app.crud import conversation as conv_crud
from app.database.mysql import AsyncSessionLocal
from app.database.redis import redis_client
from app.graph import chat_graph
from app.graph.llms import FINAL_ANSWER_TAG
from app.models.conversation import Conversation
from app.models.user import User
from app.schemas.chat import ChatRequest
from app.services import chat_service, context_service
from app.utils.markdown import render_markdown
from app.utils.sse import SSE_DONE, sse_data

GEN_TTL_SECONDS = 30 * 60    # 生成中状态/缓冲的兜底 TTL（防进程崩溃残留脏标记）
REPLAY_TTL_SECONDS = 10 * 60  # done 后缓冲保留窗口：供迟到/刷新重放

_TERMINAL_TYPES = ("done", "error")

# 进程内生成任务注册表：conversation_id -> Task（单 worker 前提）
_tasks: dict[int, asyncio.Task] = {}


def _stream_key(conversation_id: int) -> str:
    return f"chat:stream:{conversation_id}"


def _gen_key(conversation_id: int) -> str:
    return f"chat:gen:{conversation_id}"


async def _emit(conversation_id: int, event: dict) -> None:
    """事件写入流缓冲（单字段 data 存 JSON），并续 TTL（长生成防缓冲先过期）。"""
    await redis_client.xadd(
        _stream_key(conversation_id),
        {"data": json.dumps(event, ensure_ascii=False)},
    )
    await redis_client.expire(_stream_key(conversation_id), GEN_TTL_SECONDS)


async def is_generating(conversation_id: int) -> bool:
    return bool(await redis_client.exists(_gen_key(conversation_id)))


async def generating_flags(conversation_ids: list[int]) -> list[bool]:
    """批量查生成中标记（会话列表打标用），与入参顺序一一对应。"""
    if not conversation_ids:
        return []
    async with redis_client.pipeline(transaction=False) as pipe:
        for cid in conversation_ids:
            pipe.exists(_gen_key(cid))
        return [bool(v) for v in await pipe.execute()]


async def start_generation(
    db: AsyncSession, user: User, req: ChatRequest
) -> Conversation:
    """点火：防重入 → 建/取会话 → 起后台生成任务 → 立即返回（不等生成）。"""
    conv = await chat_service.get_or_create_conversation(db, user, req)
    if await is_generating(conv.id):
        raise BusinessException(409, "上一条还在生成中，等它说完吧")
    await redis_client.set(_gen_key(conv.id), "1", ex=GEN_TTL_SECONDS)
    # 清掉上一轮的缓冲，保证订阅方从 '0' 重放时拿到的都是本轮事件
    await redis_client.delete(_stream_key(conv.id))
    _tasks[conv.id] = asyncio.create_task(
        run_generation(conv.id, user.id, req.message)
    )
    return conv


def _chunk_text(chunk) -> str:
    """AIMessageChunk.content 可能是 str 或 content blocks，统一取文本。"""
    content = chunk.content
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


@traceable(run_type="chain", name="run_generation",
           reduce_fn=lambda result: {"finished": result is None})
async def run_generation(conversation_id: int, user_id: int, message: str) -> None:
    """后台生成任务：跑图 → 事件进 Redis Stream → 落库 + 热上下文。

    与请求生命周期完全无关（独立 db session），客户端断开不影响生成。
    事件序列：meta（编排决策后，rag 可能修正重发）→ delta*N → done / error。
    """
    user_persisted = False
    full_text = ""
    meta_sent = False
    rag_hit = False

    async def emit_meta(rag: bool) -> None:
        await _emit(conversation_id, {
            "type": "meta",
            "conversation_id": conversation_id,
            "rag": rag,
            # question 给刷新恢复用：此时用户消息尚未落库，前端靠它重建用户气泡
            "question": message,
        })

    try:
        async with AsyncSessionLocal() as db:
            conv = await db.get(Conversation, conversation_id)
            if not conv:
                return  # 会话已被删除（cancel 已取消任务，这里是双保险）

            init_state = {
                "question": message,
                "conversation_id": conv.id,
                "summary_hint": conv.summary or "",
                "messages": [],
            }

            async for mode, payload in chat_graph.astream(
                init_state, stream_mode=["messages", "updates"]
            ):
                if mode == "updates":
                    if "orchestrator" in payload and not meta_sent:
                        route = payload["orchestrator"].get("route") or {}
                        rag_hit = bool(route.get("needs_rag"))
                        await emit_meta(rag_hit)
                        meta_sent = True
                    if ("recipe_tools" in payload or "dev_tools" in payload) and not rag_hit:
                        # 追问时 agent 自行兜底检索（菜谱/书籍）：修正 rag 标志
                        rag_hit = True
                        if meta_sent:
                            await emit_meta(True)
                    continue

                chunk, metadata = payload
                if FINAL_ANSWER_TAG not in (metadata.get("tags") or []):
                    continue
                delta = _chunk_text(chunk)
                if not delta:
                    continue
                full_text += delta
                await _emit(conversation_id, {
                    "type": "delta",
                    "delta": delta,
                    "html": render_markdown(full_text),
                })

            # 编排都没跑到的极端场景（断流等）也要把 meta 补上
            if not meta_sent:
                await emit_meta(rag_hit)

            # 消息落库 + 更新热上下文（先用户后助手，顺序即时间序）
            # 用户消息在图跑完后才落库：避免 Redis miss 从 MySQL 恢复滑窗时把当前问题读重
            await conv_crud.add_message(db, conv.id, "user", message)
            user_persisted = True
            if full_text:
                await conv_crud.add_message(db, conv.id, "assistant", full_text)
                overflow = await context_service.append_turn(
                    db, conv, message, full_text
                )
                # 窗口溢出 → 后台压缩摘要（不阻塞 done 事件）
                if overflow:
                    summary = await context_service.load_summary(
                        conv.id, conv.summary or ""
                    )
                    asyncio.create_task(
                        chat_service.compress_context(conv.id, summary, overflow)
                    )

            await _emit(conversation_id, {"type": "done"})
            # 生成完毕：缓冲只需保留短窗口供迟到重放
            await redis_client.expire(
                _stream_key(conversation_id), REPLAY_TTL_SECONDS
            )
    except asyncio.CancelledError:
        # 会话删除触发的取消：无需落库（会话已删），直接退出
        raise
    except Exception as e:
        print(f"❌ 对话生成异常（conv={conversation_id}）：{e}")
        # 生成失败也尽力把用户消息留下来，别连问题都丢了
        try:
            if not user_persisted:
                async with AsyncSessionLocal() as db:
                    await conv_crud.add_message(db, conversation_id, "user", message)
        except Exception:
            pass
        await _emit(conversation_id, {
            "type": "error", "message": "服务出了点问题，请稍后重试",
        })
    finally:
        _tasks.pop(conversation_id, None)
        await redis_client.delete(_gen_key(conversation_id))


async def subscribe(conversation_id: int, last_id: str) -> AsyncGenerator[str, None]:
    """SSE 订阅生成过程：last_id 之后的事件重放 + 阻塞跟随，直到终态事件。

    事件 payload 内嵌 "id"（流 entry id）作为下次断点——前端手动解析 SSE，
    只读 data: 行，不走原生 id: 字段。
    """
    key = _stream_key(conversation_id)
    current = last_id or "0"

    # 缓冲和生成标记都没了（TTL 过期或从未生成）→ 直接结束，前端回退拉历史
    if not await redis_client.exists(key) and not await is_generating(conversation_id):
        yield SSE_DONE
        return

    while True:
        entries = await redis_client.xread({key: current}, block=15000, count=200)
        if not entries:
            # 阻塞超时：发心跳保活；生成已结束且无新事件可读 → 收尾
            yield ": ping\n\n"
            if not await is_generating(conversation_id):
                yield SSE_DONE
                return
            continue

        for _stream, messages in entries:
            for entry_id, fields in messages:
                current = entry_id
                payload = json.loads(fields.get("data", "{}"))
                payload["id"] = entry_id
                yield sse_data(payload)
                if payload.get("type") in _TERMINAL_TYPES:
                    yield SSE_DONE
                    return


async def cancel(conversation_id: int) -> None:
    """删除会话时清理：取消生成任务 + 删缓冲和标记。"""
    task = _tasks.pop(conversation_id, None)
    if task and not task.done():
        task.cancel()
    await redis_client.delete(_stream_key(conversation_id), _gen_key(conversation_id))
