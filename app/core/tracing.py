"""LangSmith 可视化调试（https://smith.langchain.com）。

在 .env 里配好 LANGSMITH_API_KEY 并把 LANGSMITH_TRACING=true 即启用，
不配置时 traceable/wrap_openai 全部空转，对主流程零影响。

注意：langsmith SDK 只认环境变量，不认 pydantic settings，
所以这里在 import langsmith 之前把 .env 里的配置写进 os.environ。

启用后可在 LangSmith 控制台看到每次对话的完整调用树：
stream_chat → retrieve_verified_recipes → search_ranked → embed / milvus
  → rerank → check_recipe_context（候选池逐条过检）→ 豆包流式生成
每个节点的输入、输出、耗时一目了然。
"""
import os

from app.config import settings

if settings.LANGSMITH_API_KEY:
    os.environ.setdefault("LANGSMITH_TRACING", str(settings.LANGSMITH_TRACING).lower())
    os.environ.setdefault("LANGSMITH_API_KEY", settings.LANGSMITH_API_KEY)
    os.environ.setdefault("LANGSMITH_PROJECT", settings.LANGSMITH_PROJECT)

from langsmith import traceable  # noqa: E402
from langsmith.wrappers import wrap_openai  # noqa: E402

__all__ = ["traceable", "wrap_openai"]
