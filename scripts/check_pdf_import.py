"""PDF 导入质量抽检：从 Milvus 随机抽 N 块，用块文本前缀做向量检索，统计自命中率。

自命中 = 用块自己的文本当查询，检索 top-k 里应包含该块本身——
 embedding 模型、向量索引、集合数据三者任何一个出问题都过不了这关。

用法（在 recipe-back-end 目录下）：
    .venv/bin/python scripts/check_pdf_import.py                # 抽 20 条，top-3 判定
    .venv/bin/python scripts/check_pdf_import.py -n 50 --topk 5
"""
import argparse
import os
import random
import sys

from openai import OpenAI
from pymilvus import MilvusClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import settings  # noqa: E402

QUERY_PREFIX_CHARS = 150   # 取块文本前多少字作查询（模拟用户提问的信息量）


def main():
    parser = argparse.ArgumentParser(description="PDF 导入质量抽检（自命中率）")
    parser.add_argument("-n", type=int, default=20, help="抽样条数")
    parser.add_argument("--topk", type=int, default=3, help="检索 top-k 内命中算通过")
    parser.add_argument("--collection", default=settings.DEV_BOOK_COLLECTION)
    parser.add_argument("--seed", type=int, default=42, help="随机种子，固定可复现")
    args = parser.parse_args()

    client = MilvusClient(uri=f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}")
    if not client.has_collection(args.collection):
        print(f"❌ 集合 {args.collection} 不存在，先跑 import_pdf.py 导入")
        sys.exit(1)

    ids = [r["id"] for r in client.query(
        collection_name=args.collection, filter="id >= 0",
        output_fields=["id"], limit=16384,
    )]
    print(f"📚 集合 {args.collection} 共 {len(ids)} 块，随机抽 {args.n} 条自测…")
    sample_ids = random.Random(args.seed).sample(ids, min(args.n, len(ids)))

    rows = client.query(
        collection_name=args.collection,
        filter=f"id in [{','.join(str(i) for i in sample_ids)}]",
        output_fields=["id", "heading", "text", "page_start"],
        limit=len(sample_ids),
    )
    by_id = {r["id"]: r for r in rows}

    embed_client = OpenAI(api_key=settings.EMBEDDING_API_KEY,
                          base_url=settings.EMBEDDING_BASE_URL, timeout=60)

    hits = 0
    for i, cid in enumerate(sample_ids, 1):
        row = by_id[cid]
        query = row["text"][:QUERY_PREFIX_CHARS]
        vector = embed_client.embeddings.create(
            model=settings.EMBEDDING_MODEL, input=[query]).data[0].embedding
        results = client.search(
            collection_name=args.collection, data=[vector], limit=args.topk,
            output_fields=["heading", "page_start"],
        )[0]
        hit_ids = {h["id"] for h in results}
        ok = cid in hit_ids
        hits += ok
        heading = (row.get("heading") or "（无章节）")[:30]
        rank = [h["id"] for h in results].index(cid) + 1 if ok else "-"
        top_score = results[0]["distance"] if results else 0
        print(f"  {'✅' if ok else '❌'} [{i:2d}] p{row.get('page_start', '?'):>4} {heading:<30} "
              f"自命中排名 {rank}  top1相似度 {top_score:.3f}")

    print(f"\n🎯 自命中率：{hits}/{len(sample_ids)}（top-{args.topk}）"
          f"{'，检索链路正常' if hits >= len(sample_ids) * 0.9 else '，⚠️ 偏低，检查 embedding 模型是否与导入时一致'}")


if __name__ == "__main__":
    main()
