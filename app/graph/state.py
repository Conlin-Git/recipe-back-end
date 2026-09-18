"""多 Agent 编排图的共享状态。

并行分支（sentiment_agent / orchestrator）各写各的 key，无需自定义 reducer；
messages 是菜谱 agent ReAct 循环的消息列表，用 add_messages 累加。
"""
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class RouteDecision(TypedDict):
    """编排 agent 的路由决策（JSON 结构化输出的解析结果）。"""
    intent_type: str        # followup（追问） | new（全新查询）
    domain: str             # recipe | dev | chat
    summary_sufficient: bool  # 追问时：仅凭摘要是否足以回答
    needs_rag: bool         # 是否需要菜谱 RAG 检索
    reason: str             # 一句话决策理由（排查用）


class SentimentResult(TypedDict):
    emotion: str      # happy | sad | anxious | frustrated | angry | excited | neutral
    intensity: str    # low | medium | high
    tone_advice: str  # 给问答 agent 的语气建议


class GraphState(TypedDict, total=False):
    question: str
    conversation_id: int
    summary_hint: str              # MySQL conversations.summary（Redis miss 时的兜底）
    summary: str                   # 滚动摘要（load_context 加载）
    window: list[dict]             # 最近N轮滑窗消息 [{"role","content"}]（load_context 加载）
    recipe_cache: list[dict]       # 本会话 Redis 缓存的菜谱片段
    dev_cache: list[dict]          # 本会话 Redis 缓存的书摘块
    sentiment: SentimentResult
    route: RouteDecision
    messages: Annotated[list, add_messages]      # 菜谱 agent 的 ReAct 循环消息
    dev_messages: Annotated[list, add_messages]  # dev agent 的 ReAct 循环消息
