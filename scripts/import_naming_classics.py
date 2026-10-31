"""把有来源记录的命理古籍 PDF 导入独立 Milvus 集合。

示例：
  .venv/bin/python scripts/import_naming_classics.py \
    --pdf docs/naming/sources/classics/sanming-tonghui-mirror.pdf \
    --title "三命通会（明·万民英，免费镜像版）" \
    --source-url "https://d.zixueguoxue.com/2020/06/202006230335095.pdf"

脚本默认复用 PDF 清洗、结构切块、embedding 缓存和 Milvus 幂等写入能力；
命理集合与菜谱、开发集合隔离。导入前必须在 manifest 中登记版本和许可状态。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from scripts.import_pdf import clean_pages, extract_pages, pack_chunks, build_embed_text  # noqa: E402
from scripts.ingest_common import (  # noqa: E402
    Embedder, RateLimiter, ensure_collection, milvus_client, stable_id, upsert_batches,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="命理古籍 PDF 导入 Milvus")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--source-id", default="sanming-tonghui")
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--edition", default="待补版本")
    parser.add_argument("--school", default="子平命理；待专家复核")
    parser.add_argument("--knowledge-version", default="naming-classics-bootstrap-v1")
    parser.add_argument("--license-status", default="免费镜像；许可待核，不作为公开再分发授权")
    parser.add_argument("--collection", default=settings.NAMING_KNOWLEDGE_COLLECTION)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tpm", type=int, default=2_000_000)
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    pages = extract_pages(str(pdf_path))
    paragraphs, stats = clean_pages(pages)
    chunks = pack_chunks(paragraphs)
    # OCR/排版差异会让目录页漏过通用检测；命理 RAG 不允许把目录当证据。
    chunks = [c for c in chunks if c["text"].count("⋯") < 5]
    if args.limit > 0:
        chunks = chunks[:args.limit]
    print(f"📖 {args.title}: 页数 {len(pages)}，清洗段落 {len(paragraphs)}，有效块 {len(chunks)}，统计 {stats}")
    if args.dry_run:
        for chunk in chunks[:2]:
            print("─" * 60)
            print(build_embed_text(chunk, args.title))
        return

    doc_id = pdf_path.name
    rows = []
    for index, chunk in enumerate(chunks):
        rows.append({
            "id": stable_id(f"{args.source_id}:{args.knowledge_version}:{doc_id}:{index}"),
            "doc_id": doc_id, "source_id": args.source_id, "title": args.title,
            "heading": chunk["heading"], "chunk_index": index,
            "page_start": chunk["page_start"], "page_end": chunk["page_end"],
            "text": chunk["text"], "source_url": args.source_url,
            "citation": f"{args.title}，{args.edition}，第 {chunk['page_start']}-{chunk['page_end']} 页",
            "edition": args.edition, "school": args.school,
            "knowledge_version": args.knowledge_version, "license_status": args.license_status,
            "_embed": build_embed_text(chunk, args.title),
        })
    cache_path = Path("data") / f"embeddings_{args.collection}_{args.source_id}.pkl"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    vectors = Embedder(RateLimiter(args.tpm), str(cache_path)).embed_all(
        [{"cid": row["id"], "text": row.pop("_embed")} for row in rows]
    )
    for row in rows:
        row["vector"] = vectors[row["id"]]
    client = milvus_client()
    ensure_collection(client, args.collection, settings.MILVUS_EMBEDDING_DIM, args.fresh)
    upsert_batches(client, args.collection, rows)
    print(f"🎉 命理古籍导入完成：{len(rows)} 块 → {args.collection}")


if __name__ == "__main__":
    main()
