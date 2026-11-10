"""图内各 agent 的 LLM 客户端（langchain-openai ChatOpenAI）。

- answer_llm：主模型（智谱 GLM），流式。打 "final_answer" tag——chat_service 只把
  带这个 tag 的 token 透出到 SSE，编排/情感分析等中间调用不会漏给用户。
- router_llm：编排 agent 用的主模型，非流式，TAG_NOSTREAM 双保险。
- sentiment_llm：硅基流动免费小模型，非流式，fail-open。

RAG 检索/校验/embedding 仍用 rag_service / verify_service 里的 OpenAI SDK
客户端（带 LangSmith wrap），不在此处重复建设。
"""
from langchain_openai import ChatOpenAI
from langgraph.constants import TAG_NOSTREAM
from openai import RateLimitError

from app.config import settings

FINAL_ANSWER_TAG = "final_answer"

# 关闭隐藏推理（reasoning 曾占输出 token 61%），生成时间直接砍半以上
_extra_body = {"thinking": {"type": settings.LLM_THINKING}} if settings.LLM_THINKING else {}

_primary_answer_llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,
    streaming=True,
    timeout=30,
    max_retries=0,
    tags=[FINAL_ANSWER_TAG],
    extra_body=_extra_body,
)

# 主模型限流时使用现有硅基流动配置。只兜底 429（发生于首个 token 之前），
# 不重放已输出一半的回答，避免超时后将两份答案拼在一起。
_fallback_answer_llm = (
    ChatOpenAI(
        model=settings.SILICONFLOW_LLM_MODEL,
        api_key=settings.SILICONFLOW_API_KEY,
        base_url=settings.SILICONFLOW_BASE_URL,
        streaming=True,
        timeout=30,
        max_retries=0,
        tags=[FINAL_ANSWER_TAG],
    )
    if settings.LLM_PROVIDER != "siliconflow" and settings.SILICONFLOW_API_KEY
    else None
)


def create_answer_llm(tools=None):
    primary = _primary_answer_llm.bind_tools(tools) if tools else _primary_answer_llm
    if _fallback_answer_llm is None:
        return primary
    fallback = _fallback_answer_llm.bind_tools(tools) if tools else _fallback_answer_llm
    return primary.with_fallbacks([fallback], exceptions_to_handle=(RateLimitError,))


answer_llm = create_answer_llm()

router_llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,
    temperature=0,
    max_tokens=200,
    timeout=8,
    max_retries=0,
    tags=[TAG_NOSTREAM],
    extra_body=_extra_body,
)

sentiment_llm = ChatOpenAI(
    model=settings.SENTIMENT_LLM_MODEL,
    api_key=settings.SILICONFLOW_API_KEY,
    base_url=settings.SILICONFLOW_BASE_URL,
    temperature=0,
    max_tokens=100,
    max_retries=0,
    timeout=settings.RAG_VERIFY_TIMEOUT,  # 免费档抖动时快速 fail-open
    tags=[TAG_NOSTREAM],
)
