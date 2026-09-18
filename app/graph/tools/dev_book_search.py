"""开发书籍检索工具：dev agent 的 ReAct 工具。

与 search_recipe 同构：把 检索 → 精排 的链路封装成一个工具，
命中后把书摘块写入当前会话的 Redis 缓存（追问复用，省重复检索）。
书摘块自带「书名 + 章节」上下文前缀、内容自包含，不需要菜谱那套
常识校验——rerank 过阈值即采用。
"""
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.core.tracing import traceable
from app.services import dev_cache_service, dev_rag_service


@traceable(run_type="chain", name="retrieve_book_chunks")
async def retrieve_book_chunks(query: str) -> list[dict]:
    """检索一次（embed/milvus/rerank 各只调一次），过 rerank 阈值即采用。"""
    return await dev_rag_service.search_book_chunks(query)


@tool("search_dev_book")
async def search_dev_book_tool(
    query: str,
    state: Annotated[dict[str, Any], InjectedState],
) -> str:
    """检索《JavaScript高级程序设计》（红宝书）电子书知识库，获取 JS 概念、原理、API 用法的权威书摘。

    什么时候调：用户问的 JS 知识点在本会话缓存书摘里没有、或缓存书摘不足以回答时。
    什么时候别调：缓存书摘已经覆盖用户的问题（含对同一知识点的追问）——直接回答。

    Args:
        query: 检索词，用知识点+关键诉求（如「闭包 词法作用域」「事件循环 宏任务微任务」），别整句抄用户的话。
    """
    chunks = await retrieve_book_chunks(query)
    if not chunks:
        return "未检索到相关书籍资料。直接用你的专家知识回答，别跟用户提检索失败的事。"

    # 命中即入会话缓存：同会话后续追问直接复用，不再重复向量检索
    await dev_cache_service.set_cached_chunks(state["conversation_id"], chunks)
    return dev_rag_service.format_book_context(chunks)
