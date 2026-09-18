"""开发书籍 RAG 检索：dev_book_chunks 集合的语义检索 + bge-reranker 精排。

与菜谱检索（rag_service.search_ranked）同构，但候选是书摘块而非整份菜谱：
- 不做常识校验、不查 Neo4j——书摘块导入时已拼「书名 + 章节」上下文前缀，
  块本身自包含，rerank 过阈值即可采用，省一轮 7B 校验调用；
- 命中后写入会话缓存（dev_cache_service），追问复用。

未命中 / 检索异常返回 []，退化为纯 LLM（dev agent 本来就是 JS 专家，兜底体验不差）。
"""
import asyncio
import time

import httpx

from app.config import settings
from app.core.tracing import traceable
from app.database.milvus import milvus_client
from app.services.rag_service import embedding_client

OUTPUT_FIELDS = ["title", "heading", "text", "page_start", "page_end"]


async def _embed(text: str) -> list[float]:
    resp = await embedding_client.embeddings.create(
        model=settings.EMBEDDING_MODEL,
        input=[text[:1200]],
    )
    return resp.data[0].embedding


def _rerank_doc(chunk: dict) -> str:
    """拼 rerank 打分用的文档文本：章节标题 + 正文（截断，省 token）。"""
    return f"{chunk.get('heading', '')}\n{chunk.get('text', '')}"[:500]


@traceable(run_type="chain", name="dev_rerank")
async def _rerank(query: str, candidates: list[dict]) -> list[dict]:
    """交叉编码器重排：一次调用给全部候选打分，返回过阈值的 top_k 块。

    rerank 服务异常时 fail-open 按向量分原序返回（截断到 top_k）。
    """
    if len(candidates) <= 1:
        return candidates
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.SILICONFLOW_BASE_URL}/rerank",
                headers={"Authorization": f"Bearer {settings.SILICONFLOW_API_KEY}"},
                json={
                    "model": settings.SILICONFLOW_RERANK_MODEL,
                    "query": query,
                    "documents": [_rerank_doc(c) for c in candidates],
                    "top_n": settings.DEV_RAG_TOP_K,
                },
            )
            resp.raise_for_status()
        reranked = []
        for item in resp.json().get("results", []):
            if item.get("relevance_score", 0) >= settings.RAG_RERANK_SCORE_THRESHOLD:
                c = candidates[item["index"]]
                c["rerank_score"] = item["relevance_score"]
                reranked.append(c)
        return reranked
    except Exception as e:
        print(f"⚠️ dev rerank 失败，按向量相似度原序返回：{e}")
        return candidates[: settings.DEV_RAG_TOP_K]


@traceable(run_type="retriever", name="search_book_chunks")
async def search_book_chunks(query: str) -> list[dict]:
    """检索开发书籍知识库：Milvus 粗排 over-fetch → rerank 精排，返回 top_k 书摘块。

    未命中 / 检索异常返回 []，退化为纯 LLM，不影响主流程。
    """
    try:
        t0 = time.perf_counter()
        vector = await _embed(query)
        t_embed = time.perf_counter() - t0

        # pymilvus 同步客户端，放线程池避免阻塞事件循环
        t0 = time.perf_counter()
        results = await asyncio.to_thread(
            milvus_client.search,
            collection_name=settings.DEV_BOOK_COLLECTION,
            data=[vector],
            limit=settings.RAG_RERANK_CANDIDATES,
            output_fields=OUTPUT_FIELDS,
        )
        t_milvus = time.perf_counter() - t0

        hits = results[0] if results else []
        matched = [
            {**h["entity"], "id": h["id"], "score": h["distance"]}
            for h in hits
            if h["distance"] >= settings.RAG_SCORE_THRESHOLD
        ]
        if not matched:
            print(f"⏱️ 书籍检索：embed {t_embed:.1f}s / milvus {t_milvus:.1f}s / 无候选")
            return []

        t0 = time.perf_counter()
        ranked = await _rerank(query, matched)
        print(f"⏱️ 书籍检索：embed {t_embed:.1f}s / milvus {t_milvus:.1f}s / "
              f"rerank {time.perf_counter() - t0:.1f}s / 候选 {len(matched)}→{len(ranked)}")
        return ranked
    except Exception as e:
        # 检索失败不阻塞对话，退化为纯 LLM
        print(f"⚠️ 书籍RAG检索失败，退化为纯LLM回答：{e}")
        return []


def format_book_context(chunks: list[dict]) -> str:
    """把检索到的书摘块组装成 prompt 资料（书名 + 章节 + 页码 + 正文）。"""
    blocks = []
    for i, c in enumerate(chunks, 1):
        parts = [f"【书籍资料{i}】《{c.get('title', '')}》"]
        if c.get("heading"):
            parts.append(f"章节：{c['heading']}")
        if c.get("page_start"):
            parts.append(f"页码：{c['page_start']}-{c.get('page_end', c['page_start'])}")
        parts.append(c.get("text", ""))
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)
