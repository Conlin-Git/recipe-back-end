"""菜谱知识库检索工具：菜谱 agent 的 ReAct 工具。

把检索 → 精排 → 常识校验 → 补步骤 的完整链路封装成一个工具，
命中后把菜谱片段写入当前会话的 Redis 缓存（追问复用，省重复检索）。
"""
import time
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from app.config import settings
from app.core.tracing import traceable
from app.services import rag_service, recipe_cache_service, verify_service


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


@tool("search_recipe")
async def search_recipe_tool(
    query: str,
    state: Annotated[dict[str, Any], InjectedState],
) -> str:
    """检索菜谱知识库，获取菜谱的完整做法（材料、步骤、配图、小贴士）。

    什么时候调：用户问的菜在本会话缓存资料里没有、或缓存资料不足以回答时。
    什么时候别调：缓存资料已经覆盖用户的问题（含对同一道菜的追问）——直接回答。

    Args:
        query: 检索词，用菜品名+关键诉求（如「麻婆豆腐 家常做法」），别整句抄用户的话。
    """
    recipes = await retrieve_verified_recipes(query)
    if not recipes:
        return "未检索到相关菜谱资料。请明确告诉用户这道菜不在你的菜谱里，再凭经验给做法。"

    # 命中即入会话缓存：同会话后续追问直接复用，不再重复向量检索
    await recipe_cache_service.set_cached_recipes(state["conversation_id"], recipes)
    return rag_service.format_recipe_context(recipes)
