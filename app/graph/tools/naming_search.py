"""命理命名 Agent 的古籍检索工具。"""
from langchain_core.tools import tool

from app.services.naming_rag_service import format_naming_context, search_naming_knowledge


@tool("search_naming_knowledge")
async def search_naming_knowledge_tool(query: str) -> str:
    """检索命理古籍原文，返回带版本、页码和证据 ID 的片段。"""
    chunks = await search_naming_knowledge(query)
    if not chunks:
        return "未检索到命理古籍证据。不要编造书名、页码或原文；可以明确说明当前知识库没有覆盖。"
    return format_naming_context(chunks)
