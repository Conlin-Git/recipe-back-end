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
from app.graph.state import GraphState, RouteDecision, TopicState
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
        topic_state = await context_service.load_topic_state(db, cid)
    # 点火阶段已先把本轮用户消息写入 MySQL；Redis miss 从 MySQL 恢复时将它移除，
    # 避免随后作为 HumanMessage 再注入一次。
    if window and window[-1].get("role") == "user" and window[-1].get("content") == state["question"]:
        window = window[:-1]
    return {
        "summary": summary,
        "window": window,
        "recipe_cache": cache,
        "dev_cache": dev_cache,
        "topic_state": topic_state,
    }


# ---------------- 情感分析 agent ----------------

def _parse_json(text: str) -> dict:
    """解析模型 JSON，避免贪婪正则吞掉多个对象或 Markdown 尾注。"""
    text = (text or "").strip()
    if not text:
        return {}
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    if fenced:
        candidates.insert(0, fenced.group(1))
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    return {}


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "是"}:
            return True
        if normalized in {"false", "0", "no", "否"}:
            return False
    return default


_FOLLOWUP_MARKERS = re.compile(
    r"(刚才|上一条|上面|前面|继续|然后|那|它|这个|那个|这道|这本|怎么|为什么|还有|再说|依赖|配置|改成|补充)"
)
_SWITCH_MARKERS = re.compile(r"(换个话题|换一个问题|另外问|顺便问|不聊这个|新问题)")
_RECIPE_TERMS = re.compile(r"(菜谱|食谱|做法|食材|配料|火候|下锅|炒|煮|炖|烤|蒸|牛肉|鸡|鱼|汤|面|饭)")
_DEV_TERMS = re.compile(r"(代码|编程|前端|后端|接口|API|Docker|Milvus|MySQL|Redis|数据库|依赖|配置|部署|报错|Python|JavaScript|TypeScript|Vue|React|SQL)", re.I)


def _last_domain(state: GraphState) -> str:
    topic = state.get("topic_state") or {}
    if topic.get("domain") in {"recipe", "dev", "chat"}:
        return topic["domain"]
    for message in reversed(state.get("window") or []):
        content = str(message.get("content", ""))
        if _DEV_TERMS.search(content):
            return "dev"
        if _RECIPE_TERMS.search(content):
            return "recipe"
    return "chat"


def _heuristic_route(state: GraphState) -> RouteDecision:
    question = str(state.get("question", "")).strip()
    previous_domain = _last_domain(state)
    explicit_switch = bool(_SWITCH_MARKERS.search(question))
    followup = bool(_FOLLOWUP_MARKERS.search(question)) and not explicit_switch
    if _DEV_TERMS.search(question):
        domain = "dev"
    elif _RECIPE_TERMS.search(question):
        domain = "recipe"
    elif followup:
        domain = previous_domain
    else:
        domain = "chat"
    if followup and not (_DEV_TERMS.search(question) or _RECIPE_TERMS.search(question)):
        domain = previous_domain
    topic_action = "continue" if followup else ("switch" if explicit_switch else "new")
    query = question
    references = {}
    topic = state.get("topic_state") or {}
    topic_id = str(topic.get("topic_id") or "")
    if followup and topic_id:
        for pronoun in ("它", "这个", "那个", "这道", "这本"):
            if pronoun in question:
                references[pronoun] = topic_id
        if references:
            query = f"{topic.get('goal', '')}；追问：{question}".strip("；")
    needs_rag = domain in {"recipe", "dev"} and (not followup or bool(_DEV_TERMS.search(question) or _RECIPE_TERMS.search(question)))
    return {
        "intent_type": "followup" if followup else "new",
        "domain": domain,
        "summary_sufficient": False,
        "needs_rag": needs_rag,
        "reason": "规则路由兜底，避免模型分类失败时误进入菜谱域",
        "confidence": 0.82 if domain in {"recipe", "dev"} else 0.6,
        "topic_action": topic_action,
        "context_scope": ["active_topic", "recent_turns"] if followup else ["active_topic"],
        "retrieval_query": query,
        "resolved_references": references,
        "needs_clarification": False,
        "missing_information": [],
    }


async def sentiment_node(state: GraphState) -> dict:
    """分析用户情感（免费小模型），输出语气建议给问答 agent。异常 fail-open 为中性。"""
    # 知识问答不需要额外的情感模型调用；只在明显情绪或闲聊场景启用，
    # 减少每轮并行调用对延迟和事实提示的干扰。
    question = str(state.get("question", ""))
    if (_DEV_TERMS.search(question) or _RECIPE_TERMS.search(question)) and not re.search(
        r"(生气|焦虑|崩溃|烦|气死|难过|开心|谢谢|救命|急)", question
    ):
        return {"sentiment": DEFAULT_SENTIMENT}
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

def _recent_brief(window: list[dict]) -> str:
    """给编排看的最近一轮对话（截断，意图分类不需要全文）。"""
    tail = (window or [])[-2:]
    return "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}：{m['content'][:200]}" for m in tail
    ) or "无"


async def orchestrator_node(state: GraphState) -> dict:
    """意图分类 + 路由决策（主模型，JSON 结构化输出）。"""
    heuristic = _heuristic_route(state)
    cached_titles = "、".join(
        r.get("title", "") for r in state.get("recipe_cache") or []
    ) or "无"
    cached_dev_titles = "、".join(
        r.get("title", r.get("chapter", "")) for r in state.get("dev_cache") or []
    ) or "无"
    try:
        resp = await router_llm.ainvoke([{"role": "user", "content": ORCHESTRATOR_PROMPT.format(
            summary=state.get("summary") or "无",
            recent=_recent_brief(state.get("window")),
            cached_recipes=cached_titles,
            cached_dev=cached_dev_titles,
            topic_state=json.dumps(state.get("topic_state") or {}, ensure_ascii=False),
            question=state["question"],
        )}])
        raw = _parse_json(resp.content)
        if not raw:
            raise ValueError("router returned no valid JSON")
        raw_domain = raw.get("domain")
        domain = raw_domain if raw_domain in ("recipe", "dev", "chat") else heuristic["domain"]
        intent = raw.get("intent_type") if raw.get("intent_type") in ("followup", "new") else heuristic["intent_type"]
        confidence = raw.get("confidence", 0.7)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.7
        # 短追问、代词和明确领域词优先接受规则判断，避免模型把「那依赖怎么配」
        # 重新判成闲聊或菜谱。
        if heuristic["intent_type"] == "followup" and not raw.get("topic_action"):
            intent = "followup"
        if heuristic["domain"] in {"recipe", "dev"} and _FOLLOWUP_MARKERS.search(state["question"]):
            domain = heuristic["domain"]
        route: RouteDecision = {
            "intent_type": intent,
            "domain": domain,
            "summary_sufficient": _as_bool(raw.get("summary_sufficient"), False),
            "needs_rag": _as_bool(raw.get("needs_rag"), heuristic["needs_rag"]),
            "reason": str(raw.get("reason") or heuristic["reason"]),
            "confidence": confidence,
            "topic_action": str(raw.get("topic_action") or heuristic["topic_action"]),
            "context_scope": raw.get("context_scope") if isinstance(raw.get("context_scope"), list) else heuristic["context_scope"],
            "retrieval_query": str(raw.get("retrieval_query") or heuristic["retrieval_query"]),
            "resolved_references": raw.get("resolved_references") if isinstance(raw.get("resolved_references"), dict) else heuristic["resolved_references"],
            "needs_clarification": _as_bool(raw.get("needs_clarification"), False),
            "missing_information": raw.get("missing_information") if isinstance(raw.get("missing_information"), list) else [],
        }
    except Exception as e:
        print(f"⚠️ 编排分类异常，使用规则路由：{e}")
        route = heuristic

    if route["intent_type"] == "new" and route["domain"] in {"recipe", "dev"}:
        route["needs_rag"] = True
    topic = state.get("topic_state") or {}
    continuing = route.get("topic_action") == "continue" and topic.get("domain") == route["domain"]
    topic_state: TopicState = {
        "domain": route["domain"],
        "topic_id": str(topic.get("topic_id") if continuing else f"{route['domain']}:{state['question'][:60]}"),
        "goal": str(topic.get("goal") if continuing else state["question"]),
        "entities": list(topic.get("entities") or []) if continuing else [],
        "constraints": list(topic.get("constraints") or []) if continuing else [],
        "open_loops": list(topic.get("open_loops") or []) if continuing else [],
    }

    print(f"🧭 编排决策：{route['intent_type']}/{route['domain']} "
          f"needs_rag={route['needs_rag']} confidence={route['confidence']:.2f}（{route['reason']}）")
    return {"route": route, "topic_state": topic_state}


def join_node(state: GraphState) -> dict:
    """汇聚点：等 sentiment_agent 和 orchestrator 两个并行分支都完成再路由。"""
    return {}


def pick_agent(state: GraphState) -> str:
    return {"recipe": "recipe_agent", "dev": "dev_agent", "chat": "chat_agent"}.get(
        (state.get("route") or {}).get("domain", "chat"), "chat_agent"
    )


# ---------------- 问答 agent 公共 ----------------

def _layered_context(state: GraphState) -> list[dict]:
    """按主题状态和最近消息组装上下文，路由不再拥有丢弃精确事实的权限。"""
    route = state.get("route") or {}
    messages = []
    if state.get("topic_state"):
        messages.append({
            "role": "system",
            "content": "当前主题状态（仅用于消解指代，事实以当前问题和证据为准）："
            + json.dumps(state["topic_state"], ensure_ascii=False),
        })
    if state.get("summary"):
        messages.append({"role": "system", "content": f"前情摘要：{state['summary']}"})
    # 追问始终携带最近两轮；跨域新话题只带结构化主题状态，避免旧领域污染。
    include_window = route.get("intent_type") == "followup" or route.get("topic_action") == "continue"
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
            # 全新菜谱查询：首轮检索不靠模型自觉——实测小概率无视 prompt 的
            # 「必须第一步调工具」、甚至无视 tool_choice="required"，直接凭经验答，
            # 而 token 已带 tag 流给用户，无法回收。改为代码层直接调工具，
            # 补上对应的 AIMessage/ToolMessage，让模型基于检索结果生成回答。
            # 检索词直接用用户原问题（embedding + 精排吃整句没问题），省一轮
            # 「只为决定检索词」的 LLM 调用，首轮行为 100% 确定
            tool_call = {
                "name": "search_recipe",
                "args": {"query": (state.get("route") or {}).get("retrieval_query") or state["question"]},
                "id": "forced_search_1",
                "type": "tool_call",
            }
            tool_msg = await search_recipe_tool.ainvoke(
                {**tool_call, "args": {**tool_call["args"], "state": state}}
            )
            base += [AIMessage(content="", tool_calls=[tool_call]), tool_msg]
        resp = await _recipe_llm.ainvoke(base)
        return {"messages": [*base, resp]}
    tool_count = sum(getattr(m, "type", "") == "tool" for m in messages)
    # 达到工具上限后禁用工具生成最终答复，避免以空内容结束一轮对话。
    resp = await (answer_llm if tool_count >= 3 else _recipe_llm).ainvoke(messages)
    return {"messages": [resp]}


def recipe_should_continue(state: GraphState) -> str:
    """模型请求调工具 → recipe_tools；给出最终回答 → END。"""
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if not tool_calls:
        return END
    # 工具循环有硬上限，防止模型重复检索或在异常结果上自旋。
    tool_messages = [m for m in state["messages"] if getattr(m, "type", "") == "tool"]
    if len(tool_messages) >= 3:
        return END
    return "recipe_tools"


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
            # 全新开发查询：首轮检索不靠模型自觉（同菜谱 agent——实测小概率
            # 无视 prompt 和 tool_choice="required"，直接凭经验答，而 token 已带
            # tag 流给用户无法回收）。代码层直接调工具，补上对应的
            # AIMessage/ToolMessage，让模型基于书摘生成回答，首轮行为 100% 确定
            tool_call = {
                "name": "search_dev_book",
                "args": {"query": (state.get("route") or {}).get("retrieval_query") or state["question"]},
                "id": "forced_search_1",
                "type": "tool_call",
            }
            tool_msg = await search_dev_book_tool.ainvoke(
                {**tool_call, "args": {**tool_call["args"], "state": state}}
            )
            base += [AIMessage(content="", tool_calls=[tool_call]), tool_msg]
        resp = await _dev_llm.ainvoke(base)
        return {"dev_messages": [*base, resp]}
    tool_count = sum(getattr(m, "type", "") == "tool" for m in messages)
    resp = await (answer_llm if tool_count >= 3 else _dev_llm).ainvoke(messages)
    return {"dev_messages": [resp]}


def dev_should_continue(state: GraphState) -> str:
    """模型请求调工具 → dev_tools；给出最终回答 → END。"""
    last = state["dev_messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if not tool_calls:
        return END
    tool_messages = [m for m in state["dev_messages"] if getattr(m, "type", "") == "tool"]
    if len(tool_messages) >= 3:
        return END
    return "dev_tools"


# ---------------- 闲聊/通用 agent ----------------

async def chat_agent(state: GraphState) -> dict:
    resp = await answer_llm.ainvoke(_qa_messages(state, CHAT_RULES, PERSONA_CHAT))
    return {"messages": [resp]}
