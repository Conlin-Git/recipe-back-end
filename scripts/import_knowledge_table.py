"""
知识库表格导入：CSV/XLSX -> 清洗 -> 硅基流动 bge-m3 向量化 -> Milvus

用法（在 recipe-back-end 目录下）：
    .venv/bin/python scripts/import_knowledge_table.py                    # 导入默认示例 data/dev_faq.csv
    .venv/bin/python scripts/import_knowledge_table.py --file xxx.xlsx    # 任意表格（需含 question/answer 列）
    .venv/bin/python scripts/import_knowledge_table.py --dry-run          # 只跑清洗，打印样本和统计，不调向量/不写库
    .venv/bin/python scripts/import_knowledge_table.py --fresh            # 清集合后重导

表格数据清洗四步（企业落地 checklist）：
1. 结构清洗：列名规范化、必含列校验、关键列为空的行丢弃
2. 内容清洗：HTML 标签与实体、全角转半角、零宽字符、连续空白折叠
3. 去重：按规范化后的问句 exact dedup（近重复可上 simhash / embedding 聚类，本数据量级用不上）
4. 主键：有业务 id 用业务 id，没有用内容 hash——重跑 upsert 幂等，不产生重复数据

向量化原则：只有语义内容进向量（问题/答案/标签拼成带字段标签的自然语言），
URL、统计数字、来源等只进元数据用于展示和过滤——数值类字段进向量是纯噪声，还烧 token。
"""
import argparse
import html
import os
import re
import sys

import nh3
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import settings  # noqa: E402
from scripts.ingest_common import (EMBED_WORKERS, Embedder, RateLimiter,  # noqa: E402
                                   clean_str, ensure_collection, milvus_client,
                                   stable_id, upsert_batches)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_FILE = os.path.join(DATA_DIR, "dev_faq.csv")
EMBED_CACHE_PATH = os.path.join(DATA_DIR, "embeddings_cache_faq.pkl")

MAX_TEXT_CHARS = 1500   # FAQ 答案可能偏长，给足窗口；仍远低于 bge-m3 的 8192 token 上限

QUESTION_COL = "question"
ANSWER_COL = "answer"
# 存入 Milvus 动态字段的元数据列（检索时随结果返回，用于展示和过滤）
META_COLUMNS = ["question", "answer", "category", "tags", "source"]

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"), None)
# 全角字母/数字/空格 -> 半角。不能用 NFKC 一把梭：它会把中文全角标点（，？（））转成 ASCII，
# 中文语料读起来全是半角逗号，反而污染语义
_FULLWIDTH = {ord(c): chr(ord(c) - 0xFEE0) for c in
              "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ０１２３４５６７８９"}
_FULLWIDTH[0x3000] = " "


def clean_text(value) -> str:
    """单元格内容清洗：HTML -> 纯文本，全角字母数字转半角，去零宽字符，折叠空白"""
    s = clean_str(value)
    if not s:
        return ""
    s = nh3.clean(s, tags=set())          # 剥掉所有 HTML 标签保留文本，比手写正则稳
    s = html.unescape(s)                  # &amp; &lt; 这类实体还原
    s = s.translate(_FULLWIDTH)           # ｖａｒ -> var，只动字母数字，中文标点保留
    s = s.translate(_ZERO_WIDTH)          # 零宽字符，网页复制粘贴最常见的污染源
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def normalize_key(question: str) -> str:
    """去重 key：忽略大小写、空白和标点后的问句（'var和let区别？' == 'var 和 let 的区别'）"""
    return re.sub(r"[\s\W_]+", "", question.lower())


def build_embed_text(row: dict) -> str:
    """把一行表格拼成带字段标签的自然语言——比直接塞原始行更贴近用户提问的语义空间"""
    parts = [f"问题：{row['question']}", f"答案：{row['answer']}"]
    if row.get("category"):
        parts.append(f"分类：{row['category']}")
    if row.get("tags"):
        parts.append(f"标签：{row['tags']}")
    return "\n".join(parts)[:MAX_TEXT_CHARS]


def load_and_clean(path: str) -> tuple[list[dict], dict]:
    """读表 + 四步清洗，返回 (干净行, 清洗统计)"""
    df = pd.read_csv(path) if path.lower().endswith(".csv") else pd.read_excel(path)
    stats = {"原始行数": len(df)}

    # 1. 结构清洗：列名规范化 + 必含列校验
    df.columns = [str(c).strip().lower() for c in df.columns]
    for col in (QUESTION_COL, ANSWER_COL):
        assert col in df.columns, f"表格缺少必含列 {col}（现有列：{list(df.columns)}）"

    rows, seen = [], set()
    dropped_empty = dropped_dup = 0
    for _, raw in df.iterrows():
        # 2. 内容清洗
        row = {col: clean_text(raw[col]) if col in df.columns else "" for col in META_COLUMNS}
        if not row["question"] or not row["answer"]:
            dropped_empty += 1
            continue
        # 3. 去重（规范化问句 exact dedup）
        key = normalize_key(row["question"])
        if key in seen:
            dropped_dup += 1
            continue
        seen.add(key)
        # 4. 主键：优先业务 id，否则内容 hash（幂等的关键）
        biz_id = clean_str(raw["id"]) if "id" in df.columns else ""
        row["cid"] = int(biz_id) if biz_id.isdigit() else stable_id(f"faq:{key}")
        rows.append(row)

    stats["空问题/空答案丢弃"] = dropped_empty
    stats["重复问句丢弃"] = dropped_dup
    stats["清洗后行数"] = len(rows)
    return rows, stats


def main():
    parser = argparse.ArgumentParser(description="知识库表格导入 Milvus")
    parser.add_argument("--file", default=DEFAULT_FILE, help="CSV/XLSX 文件路径")
    parser.add_argument("--collection", default="knowledge_faq", help="Milvus 集合名")
    parser.add_argument("--fresh", action="store_true", help="清空集合后重新导入（不影响 embedding 缓存）")
    parser.add_argument("--limit", type=int, default=0, help="只导入前 N 条（0 或负数表示全量）")
    parser.add_argument("--dry-run", action="store_true", help="只跑清洗，打印样本和统计，不调向量/不写库")
    parser.add_argument("--tpm", type=int, default=2_000_000, help="初始 TPM 限流值")
    args = parser.parse_args()

    print(f"📖 读取 {args.file} …")
    rows, stats = load_and_clean(args.file)
    for k, v in stats.items():
        print(f"   {k}: {v}")
    if args.limit > 0:
        rows = rows[:args.limit]

    if args.dry_run:
        print("\n🔍 清洗后样本（前 3 条）：")
        for r in rows[:3]:
            print("─" * 60)
            print(build_embed_text(r))
        print(f"\n✅ dry-run 完成，{len(rows)} 行通过清洗（未向量化、未写库）")
        return

    embed_inputs = [{"cid": r["cid"], "text": build_embed_text(r)} for r in rows]
    print(f"🔢 调用硅基流动 {settings.EMBEDDING_MODEL} 向量化（{EMBED_WORKERS} 线程并发）…")
    vectors = Embedder(RateLimiter(args.tpm), EMBED_CACHE_PATH).embed_all(embed_inputs)

    milvus_rows = [{"id": r["cid"],
                    **{col: r[col] for col in META_COLUMNS},
                    "vector": vectors[r["cid"]]} for r in rows]
    client = milvus_client()
    ensure_collection(client, args.collection, settings.MILVUS_EMBEDDING_DIM, args.fresh)
    upsert_batches(client, args.collection, milvus_rows)
    print(f"🎉 完成：{len(milvus_rows)} 条知识导入 {args.collection}")


if __name__ == "__main__":
    main()
