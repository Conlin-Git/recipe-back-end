"""命理古籍 RAG：检索依据，不改写确定性八字计算结果。"""
import asyncio
import time

import httpx

from app.config import settings
from app.core.tracing import traceable
from app.database.milvus import milvus_client
from app.services.rag_service import embedding_client

OUTPUT_FIELDS = [
    "title", "heading", "text", "page_start", "page_end", "chunk_index", "source_id",
    "source_url", "citation", "edition", "school", "knowledge_version",
]


async def _embed(text: str) -> list[float]:
    response = await embedding_client.embeddings.create(
        model=settings.EMBEDDING_MODEL, input=[text[:1200]]
    )
    return response.data[0].embedding


def _rerank_doc(item: dict) -> str:
    return f"{item.get('title', '')}\n{item.get('heading', '')}\n{item.get('text', '')}"[:700]


@traceable(run_type="chain", name="naming_rerank")
async def _rerank(query: str, candidates: list[dict]) -> list[dict]:
    if len(candidates) <= 1:
        return candidates[: settings.NAMING_RAG_TOP_K]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{settings.SILICONFLOW_BASE_URL}/rerank",
                headers={"Authorization": f"Bearer {settings.SILICONFLOW_API_KEY}"},
                json={
                    "model": settings.SILICONFLOW_RERANK_MODEL,
                    "query": query,
                    "documents": [_rerank_doc(item) for item in candidates],
                    "top_n": settings.NAMING_RAG_TOP_K,
                },
            )
            response.raise_for_status()
        ranked = []
        for result in response.json().get("results", []):
            if result.get("relevance_score", 0) >= settings.RAG_RERANK_SCORE_THRESHOLD:
                item = candidates[result["index"]]
                item["rerank_score"] = result["relevance_score"]
                ranked.append(item)
        # 命理古籍扫描/OCR 的语义分数常比现代语料低；若 rerank 全部低于通用阈值，
        # 保留向量 top-k 并把分数带回报告，避免“明明有原文却变成无证据”。
        return ranked or candidates[: settings.NAMING_RAG_TOP_K]
    except Exception as exc:
        print(f"⚠️ 命理古籍 rerank 失败，按向量顺序返回：{exc}")
        return candidates[: settings.NAMING_RAG_TOP_K]


@traceable(run_type="retriever", name="search_naming_classics")
async def search_naming_knowledge(query: str) -> list[dict]:
    """Milvus 粗排 + rerank；失败时返回空列表，命名 Agent 继续遵守证据边界。"""
    try:
        started = time.perf_counter()
        vector = await _embed(query)
        results = await asyncio.to_thread(
            milvus_client.search,
            collection_name=settings.NAMING_KNOWLEDGE_COLLECTION,
            data=[vector],
            limit=settings.RAG_RERANK_CANDIDATES,
            output_fields=OUTPUT_FIELDS,
        )
        hits = results[0] if results else []
        candidates = [
            {**hit["entity"], "id": hit["id"], "score": hit["distance"]}
            for hit in hits
            if hit["distance"] >= settings.NAMING_RAG_MIN_SCORE
        ]
        ranked = await _rerank(query, candidates)
        print(f"⏱️ 命理古籍检索：{time.perf_counter() - started:.1f}s / 候选 {len(candidates)}→{len(ranked)}")
        return ranked
    except Exception as exc:
        print(f"⚠️ 命理古籍 RAG 失败，退化为无古籍证据回答：{exc}")
        return []


def format_naming_context(chunks: list[dict]) -> str:
    blocks = []
    for index, item in enumerate(chunks, 1):
        source_id = item.get("source_id") or "unknown"
        evidence_id = f"naming:{source_id}:{item.get('chunk_index', item.get('id', index))}"
        citation = item.get("citation") or item.get("source_url") or "来源待补充"
        location = ""
        if item.get("page_start"):
            location = f"，页码 {item['page_start']}-{item.get('page_end', item['page_start'])}"
        blocks.append(
            f"【命理古籍证据ID：{evidence_id}】《{item.get('title', '')}》"
            f"{location}\n版本/流派：{item.get('edition', '未标注')} / {item.get('school', '未标注')}"
            f"\n出处：{citation}\n原文：{item.get('text', '')}"
        )
    return "\n\n".join(blocks)


def format_naming_citations(chunks: list[dict]) -> str:
    """生成强制展示给用户的出处尾注，避免模型只读证据却漏写引用。"""
    lines = []
    for index, item in enumerate(chunks, 1):
        source_id = item.get("source_id") or "unknown"
        evidence_id = f"naming:{source_id}:{item.get('chunk_index', item.get('id', index))}"
        citation = item.get("citation") or item.get("source_url") or "来源待补充"
        url = item.get("source_url") or ""
        lines.append(f"- `{evidence_id}` 《{item.get('title', '')}》：{citation}" + (f"（{url}）" if url else ""))
    return "\n".join(lines)
