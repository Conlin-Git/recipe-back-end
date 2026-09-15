"""图内各 agent 的 LLM 客户端（langchain-openai ChatOpenAI）。

- answer_llm：主模型（豆包），流式。打 "final_answer" tag——chat_service 只把
  带这个 tag 的 token 透出到 SSE，编排/情感分析等中间调用不会漏给用户。
- router_llm：编排 agent 用的主模型，非流式，TAG_NOSTREAM 双保险。
- sentiment_llm：硅基流动免费小模型，非流式，fail-open。

RAG 检索/校验/embedding 仍用 rag_service / verify_service 里的 OpenAI SDK
客户端（带 LangSmith wrap），不在此处重复建设。
"""
from langchain_openai import ChatOpenAI
from langgraph.constants import TAG_NOSTREAM

from app.config import settings

FINAL_ANSWER_TAG = "final_answer"

# 关闭隐藏推理（reasoning 曾占输出 token 61%），生成时间直接砍半以上
_extra_body = {"thinking": {"type": settings.DOUBAO_THINKING}} if settings.DOUBAO_THINKING else {}

answer_llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,
    streaming=True,
    tags=[FINAL_ANSWER_TAG],
    extra_body=_extra_body,
)

router_llm = ChatOpenAI(
    model=settings.LLM_MODEL,
    api_key=settings.LLM_API_KEY,
    base_url=settings.LLM_BASE_URL,
    temperature=0,
    max_tokens=200,
    tags=[TAG_NOSTREAM],
    extra_body=_extra_body,
)

sentiment_llm = ChatOpenAI(
    model=settings.SENTIMENT_LLM_MODEL,
    api_key=settings.SILICONFLOW_API_KEY,
    base_url=settings.SILICONFLOW_BASE_URL,
    temperature=0,
    max_tokens=100,
    timeout=settings.RAG_VERIFY_TIMEOUT,  # 免费档抖动时快速 fail-open
    tags=[TAG_NOSTREAM],
)
