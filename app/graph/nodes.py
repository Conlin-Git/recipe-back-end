"""编排图的各节点：上下文加载、情感分析 agent、管理编排 agent、三个问答 agent。

问答 agent 的 LLM 都打了 "final_answer" tag，token 经 astream(stream_mode="messages")
透出到 SSE；编排/情感分析用 TAG_NOSTREAM 的非流式小调用，不会漏给用户。

上下文分层（配合编排的追问判断，只控制发给 LLM 的 token，Redis 读取无差别）：
- 追问且摘要足够 → prompt 只带摘要（+菜谱缓存），不带滑窗
- 追问但摘要不足 → prompt 带摘要 + 完整滑窗
- 全新话题 → 只带摘要，不带滑窗（防跨域污染：做菜历史不进开发回答）
- 追问且上下文仍不足 → 菜谱 agent 在 ReAct 循环里自行调 search_recipe 兜底
"""
import asyncio
import json
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.prebuilt import ToolNode

from app.graph.llms import FINAL_ANSWER_TAG, answer_llm, router_llm, sentiment_llm
from app.graph.prompts import (
    CHAT_RULES,
    DEFAULT_SENTIMENT,
    DEV_CACHE_RULES,
    DEV_NEW_QUERY_RULES,
    DEV_RULES,
    ORCHESTRATOR_PROMPT,
    PERSONA_CHAT,
    PERSONA_DEV,
    PERSONA_RECIPE,
    RECIPE_CACHE_RULES,
    RECIPE_NEW_QUERY_RULES,
    RECIPE_RULES,
    SENTIMENT_PROMPT,
    build_qa_system_prompt,
)
from app.graph.state import GraphState, RouteDecision
from app.graph.tools.dev_book_search import search_dev_book_tool
from app.graph.tools.recipe_search import search_recipe_tool
from app.services import (
    context_service,
    dev_cache_service,
    dev_rag_service,
    rag_service,
    recipe_cache_service,
)


# ---------------- 上下文加载 ----------------

async def load_context(state: GraphState) -> dict:
    """读 Redis 热数据：滚动摘要 + 最近N轮滑窗 + 菜谱/书摘缓存（一轮 gather 并发）。

    摘要是编排分类的输入，滑窗供追问兜底和全新查询的上下文连续性使用，
    菜谱缓存供菜谱 agent 复用，书摘缓存供 dev agent 复用。
    Redis miss 时从 MySQL 恢复（见 context_service）。
    """
    from app.database.mysql import AsyncSessionLocal

    cid = state["conversation_id"]
    async with AsyncSessionLocal() as db:
        summary, window, cache, dev_cache = await asyncio.gather(
            context_service.load_summary(cid, state.get("summary_hint", "")),
            context_service.load_window(db, cid),
            recipe_cache_service.get_cached_recipes(cid),
            dev_cache_service.get_cached_chunks(cid),
        )
    return {"summary": summary, "window": window, "recipe_cache": cache, "dev_cache": dev_cache}


# ---------------- 情感分析 agent ----------------

def _parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else {}


async def sentiment_node(state: GraphState) -> dict:
    """分析用户情感（免费小模型），输出语气建议给问答 agent。异常 fail-open 为中性。"""
    try:
        resp = await sentiment_llm.ainvoke(
            [{"role": "user", "content": SENTIMENT_PROMPT.format(question=state["question"])}]
        )
        result = _parse_json(resp.content)
        if not result.get("emotion"):
            result = DEFAULT_SENTIMENT
    except Exception as e:
        print(f"⚠️ 情感分析异常，按中性处理：{e}")
        result = DEFAULT_SENTIMENT
    sentiment = {
        "emotion": str(result.get("emotion", "neutral")),
        "intensity": str(result.get("intensity", "low")),
        "tone_advice": str(result.get("tone_advice", DEFAULT_SENTIMENT["tone_advice"])),
    }
    if sentiment["emotion"] != "neutral":
        print(f"💗 情感分析：{sentiment['emotion']}（{sentiment['intensity']}）→ {sentiment['tone_advice']}")
    return {"sentiment": sentiment}


# ---------------- 管理编排 agent ----------------

# 编排解析失败时的兜底：复刻重构前「每条消息都尝试菜谱检索」的行为
_FALLBACK_ROUTE: RouteDecision = {
    "intent_type": "new", "domain": "recipe",
    "summary_sufficient": False, "needs_rag": True,
    "reason": "编排分类失败，按菜谱域检索兜底",
}


def _recent_brief(window: list[dict]) -> str:
    """给编排看的最近一轮对话（截断，意图分类不需要全文）。"""
    tail = (window or [])[-2:]
    return "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}：{m['content'][:200]}" for m in tail
    ) or "无"


async def orchestrator_node(state: GraphState) -> dict:
    """意图分类 + 路由决策（主模型，JSON 结构化输出）。"""
    cached_titles = "、".join(
        r.get("title", "") for r in state.get("recipe_cache") or []
    ) or "无"
    try:
        resp = await router_llm.ainvoke([{"role": "user", "content": ORCHESTRATOR_PROMPT.format(
            summary=state.get("summary") or "无",
            recent=_recent_brief(state.get("window")),
            cached_recipes=cached_titles,
            question=state["question"],
        )}])
        raw = _parse_json(resp.content)
        route: RouteDecision = {
            "intent_type": raw.get("intent_type") if raw.get("intent_type") in ("followup", "new") else "new",
            "domain": raw.get("domain") if raw.get("domain") in ("recipe", "dev", "chat") else "chat",
            "summary_sufficient": bool(raw.get("summary_sufficient")),
            "needs_rag": bool(raw.get("needs_rag")),
            "reason": str(raw.get("reason", "")),
        }
        if not raw:
            route = _FALLBACK_ROUTE
    except Exception as e:
        print(f"⚠️ 编排分类异常：{e}")
        route = _FALLBACK_ROUTE

    print(f"🧭 编排决策：{route['intent_type']}/{route['domain']} "
          f"needs_rag={route['needs_rag']} 摘要够答={route['summary_sufficient']}（{route['reason']}）")
    return {"route": route}


def join_node(state: GraphState) -> dict:
    """汇聚点：等 sentiment_agent 和 orchestrator 两个并行分支都完成再路由。"""
    return {}


def pick_agent(state: GraphState) -> str:
    return {"recipe": "recipe_agent", "dev": "dev_agent", "chat": "chat_agent"}.get(
        (state.get("route") or {}).get("domain", "chat"), "chat_agent"
    )


# ---------------- 问答 agent 公共 ----------------

def _layered_context(state: GraphState) -> list[dict]:
    """按编排结果分层取上下文，同时做跨域防污染：
    - 追问且摘要够答 → 只带摘要
    - 追问但摘要不足 → 摘要 + 完整滑窗
    - 全新话题 → 只带摘要，不带滑窗：滑窗里是上一个话题的对话原文，
      跨域切换时（做菜 → 编程）带进来会污染回答，dev agent 会学历史
      的口气拿做菜类比技术。摘要只留长期偏好和结论，污染面小。
    """
    route = state.get("route") or {}
    include_window = (
        route.get("intent_type") == "followup" and not route.get("summary_sufficient")
    )
    messages = []
    if state.get("summary"):
        messages.append({"role": "system", "content": f"前情摘要：{state['summary']}"})
    if include_window:
        messages.extend(state.get("window") or [])
    return messages


def _qa_messages(state: GraphState, rules: str, persona: str) -> list:
    system = build_qa_system_prompt(persona, rules, state.get("sentiment"))
    return [
        SystemMessage(content=system),
        *_layered_context(state),
        HumanMessage(content=state["question"]),
    ]


# ---------------- 菜谱知识问答 agent（ReAct + search_recipe 工具） ----------------

recipe_tools_node = ToolNode([search_recipe_tool])
# with_config 双保险：bind_tools 后 tag 仍透出，SSE 只转发带 tag 的 token
_recipe_llm = answer_llm.bind_tools([search_recipe_tool]).with_config(tags=[FINAL_ANSWER_TAG])


async def recipe_agent(state: GraphState) -> dict:
    """菜谱问答：缓存优先，不足时自行调 search_recipe 检索（ReAct 循环）。

    首次进入时把 system + 分层上下文 + 用户问题连同模型回复一起写入
    state["messages"]，工具执行完回到本节点时直接基于完整消息历史续推。
    """
    messages = state.get("messages")
    if not messages:
        # 检索策略按编排结果区分：全新菜谱查询必须第一步调工具（别把决策留给
        # 模型自由发挥）；追问缓存优先，不足才自行检索兜底
        needs_rag = (state.get("route") or {}).get("needs_rag")
        strategy = RECIPE_NEW_QUERY_RULES if needs_rag else RECIPE_CACHE_RULES
        system = build_qa_system_prompt(
            PERSONA_RECIPE, f"{RECIPE_RULES}\n\n{strategy}", state.get("sentiment")
        )
        if state.get("recipe_cache"):
            system += ("\n\n本会话已检索过的菜谱资料（相关就直接用它回答，别重复检索）：\n"
                       + rag_service.format_recipe_context(state["recipe_cache"]))
        base = [
            SystemMessage(content=system),
            *_layered_context(state),
            HumanMessage(content=state["question"]),
        ]
        if needs_rag:
            # 全新菜谱查询：首轮检索不靠模型自觉——实测豆包小概率无视 prompt 的
            # 「必须第一步调工具」、甚至无视 tool_choice="required"，直接凭经验答，
            # 而 token 已带 tag 流给用户，无法回收。改为代码层直接调工具，
            # 补上对应的 AIMessage/ToolMessage，让模型基于检索结果生成回答。
            # 检索词直接用用户原问题（embedding + 精排吃整句没问题），省一轮
            # 「只为决定检索词」的 LLM 调用，首轮行为 100% 确定
            tool_call = {
                "name": "search_recipe",
                "args": {"query": state["question"]},
                "id": "forced_search_1",
                "type": "tool_call",
            }
            tool_msg = await search_recipe_tool.ainvoke(
                {**tool_call, "args": {**tool_call["args"], "state": state}}
            )
            base += [AIMessage(content="", tool_calls=[tool_call]), tool_msg]
        resp = await _recipe_llm.ainvoke(base)
        return {"messages": [*base, resp]}
    resp = await _recipe_llm.ainvoke(messages)
    return {"messages": [resp]}


def recipe_should_continue(state: GraphState) -> str:
    """模型请求调工具 → recipe_tools；给出最终回答 → END。"""
    last = state["messages"][-1]
    return "recipe_tools" if getattr(last, "tool_calls", None) else END


# ---------------- 开发知识问答 agent（ReAct + search_dev_book 工具） ----------------

dev_tools_node = ToolNode([search_dev_book_tool])
# with_config 双保险：bind_tools 后 tag 仍透出，SSE 只转发带 tag 的 token
_dev_llm = answer_llm.bind_tools([search_dev_book_tool]).with_config(tags=[FINAL_ANSWER_TAG])


async def dev_agent(state: GraphState) -> dict:
    """开发问答：缓存优先，不足时自行调 search_dev_book 检索（ReAct 循环）。

    与 recipe_agent 同构：首次进入时把 system + 分层上下文 + 用户问题连同模型回复
    一起写入 state["dev_messages"]，工具执行完回到本节点时直接基于完整消息历史续推。
    """
    messages = state.get("dev_messages")
    if not messages:
        # 检索策略按编排结果区分：全新开发查询必须第一步调工具；追问缓存优先，
        # 不足才自行检索兜底
        needs_rag = (state.get("route") or {}).get("needs_rag")
        strategy = DEV_NEW_QUERY_RULES if needs_rag else DEV_CACHE_RULES
        system = build_qa_system_prompt(
            PERSONA_DEV, f"{DEV_RULES}\n\n{strategy}", state.get("sentiment")
        )
        if state.get("dev_cache"):
            system += ("\n\n本会话已检索过的书籍资料（相关就直接用它回答，别重复检索）：\n"
                       + dev_rag_service.format_book_context(state["dev_cache"]))
        base = [
            SystemMessage(content=system),
            *_layered_context(state),
            HumanMessage(content=state["question"]),
        ]
        if needs_rag:
            # 全新开发查询：首轮检索不靠模型自觉（同菜谱 agent——实测豆包小概率
            # 无视 prompt 和 tool_choice="required"，直接凭经验答，而 token 已带
            # tag 流给用户无法回收）。代码层直接调工具，补上对应的
            # AIMessage/ToolMessage，让模型基于书摘生成回答，首轮行为 100% 确定
            tool_call = {
                "name": "search_dev_book",
                "args": {"query": state["question"]},
                "id": "forced_search_1",
                "type": "tool_call",
            }
            tool_msg = await search_dev_book_tool.ainvoke(
                {**tool_call, "args": {**tool_call["args"], "state": state}}
            )
            base += [AIMessage(content="", tool_calls=[tool_call]), tool_msg]
        resp = await _dev_llm.ainvoke(base)
        return {"dev_messages": [*base, resp]}
    resp = await _dev_llm.ainvoke(messages)
    return {"dev_messages": [resp]}


def dev_should_continue(state: GraphState) -> str:
    """模型请求调工具 → dev_tools；给出最终回答 → END。"""
    last = state["dev_messages"][-1]
    return "dev_tools" if getattr(last, "tool_calls", None) else END


# ---------------- 闲聊/通用 agent ----------------

async def chat_agent(state: GraphState) -> dict:
    await answer_llm.ainvoke(_qa_messages(state, CHAT_RULES, PERSONA_CHAT))
    return {}
