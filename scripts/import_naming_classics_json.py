"""导入公开公版录入的命理古籍 JSON 全文到 naming_knowledge。

JSON 来源为 qiankunyoushu.com/library/<slug>.json，页面标注了书目、底本和公版录入说明。
本脚本不从 CTP 绕过验证抓取；每本书和版本都通过 manifest 登记，便于复核和替换。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from scripts.ingest_common import Embedder, RateLimiter, ensure_collection, milvus_client, stable_id, upsert_batches  # noqa: E402

CHUNK_CHARS = 700
OVERLAP_CHARS = 100


def _heading(line: str) -> bool:
    text = line.strip()
    return bool(text and (len(text) <= 36 or re.match(r"^(卷|第|一、|二、|三、|四、|五、|六、|七、|八、|九、|十、)", text)))


def make_chunks(lines: list[str]) -> list[dict]:
    chunks, buf, start, heading = [], "", 1, ""
    for line_no, raw in enumerate(lines, 1):
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if _heading(line) and len(line) <= 36:
            heading = line
        candidate = f"{buf}\n{line}" if buf else line
        if buf and len(candidate) > CHUNK_CHARS:
            chunks.append({"text": buf, "line_start": start, "line_end": line_no - 1, "heading": heading})
            tail = buf[-OVERLAP_CHARS:]
            buf, start = f"{tail}\n{line}", line_no
        else:
            buf = candidate
            if not start:
                start = line_no
    if buf:
        chunks.append({"text": buf, "line_start": start, "line_end": len(lines), "heading": heading})
    return chunks


def load_doc(path: Path) -> tuple[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data.get("title") or path.stem), list(data.get("lines") or [])


def main() -> None:
    parser = argparse.ArgumentParser(description="导入命理古籍 JSON 全文")
    parser.add_argument("--dir", default="docs/naming/sources/classics/text")
    parser.add_argument("--collection", default=settings.NAMING_KNOWLEDGE_COLLECTION)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--tpm", type=int, default=2_000_000)
    args = parser.parse_args()
    root = Path(args.dir)
    files = sorted(root.glob("*.json"))
    if not files:
        raise SystemExit(f"没有找到 JSON：{root}")
    sources = {
        "sanming-tonghui": {"title":"三命通会", "school":"子平命理；四库全书本系统", "url":"https://qiankunyoushu.com/library/sanming-tonghui", "edition":"殆知阁公版录入；四库全书本系统"},
        "yuanhai-ziping": {"title":"渊海子平", "school":"子平命理；公版录入", "url":"https://qiankunyoushu.com/library/yuanhai-ziping", "edition":"公开古籍录文"},
        "ziping-zhenquan": {"title":"子平真诠评注", "school":"格局/用神；含后人评注，须与原著分层", "url":"https://qiankunyoushu.com/library/ziping-zhenquan", "edition":"公开古籍录文"},
        "ditiansui-chanwei": {"title":"滴天髓阐微", "school":"调候/旺衰；任铁樵注本系统", "url":"https://qiankunyoushu.com/library/ditiansui-chanwei", "edition":"公开古籍录文"},
        "qiongtong-baojian": {"title":"穷通宝鉴", "school":"调候；公版录入", "url":"https://qiankunyoushu.com/library/qiongtong-baojian", "edition":"公开古籍录文"},
    }
    rows = []
    for path in files:
        source_id = path.stem
        title, lines = load_doc(path)
        meta = sources.get(source_id, {"title": title, "school":"待专家复核", "url":f"https://qiankunyoushu.com/library/{source_id}", "edition":"公开古籍录文"})
        chunks = make_chunks(lines)
        print(f"📖 {title}: {len(lines)} 行 → {len(chunks)} 块")
        for index, chunk in enumerate(chunks):
            content_key = f"{source_id}|{index}|{chunk['text']}"
            rows.append({
                "id": stable_id(content_key), "doc_id": path.name, "source_id": source_id,
                "title": title, "heading": chunk["heading"], "chunk_index": index,
                "page_start": chunk["line_start"], "page_end": chunk["line_end"],
                "text": chunk["text"], "source_url": meta["url"], "citation": f"{title}，{meta['edition']}，原文行 {chunk['line_start']}-{chunk['line_end']}",
                "edition": meta["edition"], "school": meta["school"], "knowledge_version":"naming-classics-publictext-v1",
                "license_status":"网站标注公版录入；生产再分发前需逐项复核许可",
                "_embed": f"《{title}》\n{meta['school']}\n{chunk['heading']}\n{chunk['text']}",
            })
    cache = Path("data") / "embeddings_naming_knowledge_publictext.pkl"
    cache.parent.mkdir(parents=True, exist_ok=True)
    vectors = Embedder(RateLimiter(args.tpm), str(cache)).embed_all([{"cid": r["id"], "text": r.pop("_embed")} for r in rows])
    for row in rows:
        row["vector"] = vectors[row["id"]]
    client = milvus_client()
    ensure_collection(client, args.collection, settings.MILVUS_EMBEDDING_DIM, args.fresh)
    upsert_batches(client, args.collection, rows)
    print(f"🎉 全部古籍导入完成：{len(rows)} 块 → {args.collection}")


if __name__ == "__main__":
    main()
