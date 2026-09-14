"""RAG 检索服务：用户提问 → bge-m3 向量化 → Milvus 语义检索 → bge-reranker 精排。

search_ranked 只检索一次（embed/milvus/rerank 各调一次），返回按相关性排序的
完整候选池；常识校验不通过时由调用方直接取下一条候选，不重新检索——
校验重试的开销从「4 轮全量检索」降为「多一次 7B 校验调用」。

候选被采用后才用 attach_steps 按 cid 去 Neo4j 补 steptext/steppic
（这两列只存在 Neo4j），解析成「步骤文字 + 步骤配图」对随参考资料给 LLM。
"""
import asyncio
import re
import time

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.core.tracing import traceable, wrap_openai
from app.database.milvus import milvus_client
from app.database.neo4j import neo4j_graph

embedding_client = wrap_openai(AsyncOpenAI(
    api_key=settings.EMBEDDING_API_KEY,
    base_url=settings.EMBEDDING_BASE_URL,
))

OUTPUT_FIELDS = ["title", "desc", "difficulty", "costtime", "tip"]


async def _embed(text: str) -> list[float]:
    resp = await embedding_client.embeddings.create(
        model=settings.EMBEDDING_MODEL,
        input=[text[:1200]],
    )
    return resp.data[0].embedding


async def _fetch_steps(cids: list[int]) -> dict[int, dict]:
    """按 cid 从 Neo4j 取 steptext/steppic（同步驱动，放线程池）。"""
    if not neo4j_graph or not cids:
        return {}
    try:
        rows = await asyncio.to_thread(
            neo4j_graph.query,
            "UNWIND $cids AS cid "
            "MATCH (r:Recipe {cid: cid}) "
            "RETURN r.cid AS cid, r.steptext AS steptext, r.steppic AS steppic",
            {"cids": cids},
        )
        return {r["cid"]: r for r in rows}
    except Exception as e:
        # 步骤取不到不阻塞，退化为无步骤的资料
        print(f"⚠️ Neo4j 取步骤失败：{e}")
        return {}


def parse_steps(steptext: str, steppic: str) -> list[dict]:
    """steptext 按 # 切步骤、steppic 按 # 切配图，按下标一一配对。

    steptext 形如「1. ▲把茄子……。#\\n2. ▲……#」，steppic 是 # 分隔的 URL。
    """
    texts = []
    for chunk in (steptext or "").split("#"):
        # 去掉开头的序号和 ▲ 标记
        t = re.sub(r"^\s*\d+\s*[.、]?\s*▲?\s*", "", chunk).strip()
        if t:
            texts.append(t)
    # 去掉 URL 里的 200_ 缩略图前缀，取原图（加载失败时前端会回退缩略图）
    pics = [u.replace("/200_", "/")
            for u in (s.strip() for s in (steppic or "").split("#"))
            if u.startswith("http")]
    return [
        {"text": t, "image": pics[i] if i < len(pics) else ""}
        for i, t in enumerate(texts)
    ]


def _rerank_doc(r: dict) -> str:
    """拼 rerank 打分用的文档文本：菜名 + 简介 + 小贴士（截断，省 token）。"""
    return f"{r.get('title', '')}\n{r.get('desc', '')}\n{r.get('tip', '')}"[:500]


@traceable(run_type="chain", name="rerank")
async def _rerank(query: str, candidates: list[dict]) -> list[dict]:
    """交叉编码器重排：一次调用给全部候选打分，返回过阈值的完整有序列表。

    top_n=候选数（不是 RAG_TOP_K），调用方拿到整个精排候选池，校验不通过
    换下一条时无需重新检索/重排。rerank 服务异常时 fail-open 按向量分原序返回。
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
                    "documents": [_rerank_doc(r) for r in candidates],
                    "top_n": len(candidates),
                },
            )
            resp.raise_for_status()
        reranked = []
        for item in resp.json().get("results", []):
            if item.get("relevance_score", 0) >= settings.RAG_RERANK_SCORE_THRESHOLD:
                r = candidates[item["index"]]
                r["rerank_score"] = item["relevance_score"]
                reranked.append(r)
        return reranked
    except Exception as e:
        print(f"⚠️ rerank 失败，按向量相似度原序返回：{e}")
        return candidates


@traceable(run_type="retriever", name="search_ranked")
async def search_ranked(query: str, exclude_ids: list[int] | None = None) -> list[dict]:
    """语义检索候选池：Milvus 粗排 over-fetch → rerank 全量精排，返回有序候选（不含步骤）。

    步骤配图由调用方对实际采用的候选用 attach_steps 按需补，省 Neo4j 查询。
    未命中 / 检索异常返回 []，退化为纯 LLM，不影响主流程。
    """
    try:
        t0 = time.perf_counter()
        vector = await _embed(query)
        t_embed = time.perf_counter() - t0

        search_kwargs = {
            "collection_name": settings.MILVUS_COLLECTION_NAME,
            "data": [vector],
            "limit": settings.RAG_RERANK_CANDIDATES,
            "output_fields": OUTPUT_FIELDS,
        }
        if exclude_ids:
            search_kwargs["filter"] = f"id not in [{','.join(str(i) for i in exclude_ids)}]"
        # pymilvus 同步客户端，放线程池避免阻塞事件循环
        t0 = time.perf_counter()
        results = await asyncio.to_thread(milvus_client.search, **search_kwargs)
        t_milvus = time.perf_counter() - t0

        hits = results[0] if results else []
        matched = [
            {**h["entity"], "id": h["id"], "score": h["distance"]}
            for h in hits
            if h["distance"] >= settings.RAG_SCORE_THRESHOLD
        ]
        if not matched:
            print(f"⏱️ 检索：embed {t_embed:.1f}s / milvus {t_milvus:.1f}s / 无候选")
            return []

        t0 = time.perf_counter()
        ranked = await _rerank(query, matched)
        print(f"⏱️ 检索：embed {t_embed:.1f}s / milvus {t_milvus:.1f}s / "
              f"rerank {time.perf_counter() - t0:.1f}s / 候选 {len(matched)}→{len(ranked)}")
        return ranked
    except Exception as e:
        # 检索失败不阻塞对话，退化为纯 LLM
        print(f"⚠️ RAG检索失败，退化为纯LLM回答：{e}")
        return []


async def attach_steps(recipe: dict) -> dict:
    """给单条候选补 Neo4j 步骤+配图（候选实际被采用前才调，本地查询很快）。"""
    rows = await _fetch_steps([recipe["id"]])
    row = rows.get(recipe["id"])
    recipe["steps"] = parse_steps(row["steptext"], row["steppic"]) if row else []
    return recipe


def format_recipe_context(recipes: list[dict]) -> str:
    """把检索到的菜谱组装成 prompt 资料（含步骤和每步配图链接）。"""
    blocks = []
    for i, r in enumerate(recipes, 1):
        parts = [f"【菜谱{i}】{r.get('title', '')}"]
        if r.get("desc"):
            parts.append(f"简介：{r['desc']}")
        if r.get("difficulty"):
            parts.append(f"难度：{r['difficulty']}")
        if r.get("costtime"):
            parts.append(f"耗时：{r['costtime']}")
        if r.get("steps"):
            lines = ["做法步骤（每步后的配图链接需原样跟随该步骤输出）："]
            for j, s in enumerate(r["steps"], 1):
                line = f"{j}. {s['text']}"
                if s["image"]:
                    line += f"（配图：{s['image']}）"
                lines.append(line)
            parts.append("\n".join(lines))
        if r.get("tip"):
            parts.append(f"小贴士：{r['tip']}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)
