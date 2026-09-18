"""
RAG 数据接入公共模块：限流、向量化（带断点缓存）、Milvus 批量写入。

从 import_recipes.py 抽取的通用能力，表格 / PDF 等知识库导入脚本共用。
企业落地要点：
- 向量化是导入链路里最贵最慢的一环：批量 + 并发 + 限流 + 断点缓存，中断重跑不重复花钱
- 主键用确定性内容 hash：同一内容重跑得到同一 id，配合 upsert 保证幂等，不产生重复数据
"""
import hashlib
import os
import pickle
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from openai import OpenAI
from pymilvus import MilvusClient
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import settings  # noqa: E402

EMBED_BATCH_SIZE = 32      # 单次 embedding 请求的文本数
EMBED_WORKERS = 8          # embedding 并发线程数
WRITE_BATCH_SIZE = 500     # Milvus 每批写入条数


def clean_str(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def stable_id(key: str) -> int:
    """内容 hash 生成 63bit 正整数主键：同一内容重跑得到同一 id，配合 upsert 幂等"""
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little") >> 1


class RateLimiter:
    """线程安全令牌桶限流器（按 token 计），遇 429 自动降速"""

    def __init__(self, tpm: int):
        self.rate = tpm / 60.0          # 每秒补充的 token 数
        self.lock = threading.Lock()
        self.capacity = self.rate * 10  # 允许 10 秒突发
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.last_penalize = 0.0

    def acquire(self, n: int):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            time.sleep(min(wait, 5.0))

    def penalize(self):
        """触发 429 时把速率砍到 70%（1 秒内多次触发只生效一次，防止并发线程把速率打穿）"""
        with self.lock:
            now = time.monotonic()
            if now - self.last_penalize < 1.0:
                return
            self.last_penalize = now
            self.rate = max(self.rate * 0.7, 500)   # 保底 500 token/s
            self.capacity = self.rate * 10
            self.tokens = min(self.tokens, self.capacity)
        tqdm.write(f"⚠️  触发 TPM 限流，速率降至 {self.rate * 60:.0f} token/min")


class Embedder:
    def __init__(self, limiter: RateLimiter, cache_path: str):
        self.client = OpenAI(api_key=settings.EMBEDDING_API_KEY,
                             base_url=settings.EMBEDDING_BASE_URL,
                             timeout=120)
        self.limiter = limiter
        self.cache_path = cache_path

    @staticmethod
    def est_tokens(texts: list[str]) -> int:
        # 中文字符≈1 token，用字符数做保守估计
        return sum(len(t) for t in texts)

    def embed_batch(self, texts: list[str], retries: int = 8) -> list[list[float]]:
        for attempt in range(retries):
            self.limiter.acquire(self.est_tokens(texts))
            try:
                resp = self.client.embeddings.create(
                    model=settings.EMBEDDING_MODEL, input=texts)
                return [item.embedding for item in resp.data]
            except Exception as e:
                if attempt == retries - 1:
                    raise
                if "429" in str(e):
                    self.limiter.penalize()
                wait = min(2 ** (attempt + 1), 30)
                tqdm.write(f"⚠️  embedding 批次失败（{e}），{wait}s 后重试…")
                time.sleep(wait)

    def embed_all(self, rows: list[dict]) -> dict[int, list[float]]:
        """rows: [{cid, text}]，带断点缓存，返回 {cid: vector}"""
        cache: dict[int, list[float]] = {}
        if os.path.exists(self.cache_path):
            with open(self.cache_path, "rb") as f:
                cache = pickle.load(f)
            tqdm.write(f"📦 命中缓存 {len(cache)} 条，跳过已向量化的部分")

        todo = [r for r in rows if r["cid"] not in cache]
        batches = [todo[i:i + EMBED_BATCH_SIZE] for i in range(0, len(todo), EMBED_BATCH_SIZE)]
        done_since_save = 0
        with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as pool:
            futures = {pool.submit(self.embed_batch, [r["text"] for r in b]): b
                       for b in batches}
            for fut in tqdm(as_completed(futures), total=len(futures), desc="向量化", unit="批"):
                batch, vectors = futures[fut], fut.result()
                for r, v in zip(batch, vectors):
                    cache[r["cid"]] = v
                done_since_save += 1
                if done_since_save >= 100:   # 每 100 批落一次盘，中断可续跑
                    with open(self.cache_path, "wb") as f:
                        pickle.dump(cache, f)
                    done_since_save = 0
        with open(self.cache_path, "wb") as f:
            pickle.dump(cache, f)
        return cache


def milvus_client() -> MilvusClient:
    return MilvusClient(uri=f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}")


def ensure_collection(client: MilvusClient, name: str, dim: int, fresh: bool):
    if fresh and client.has_collection(name):
        client.drop_collection(name)
        print(f"🗑️  已删除旧 Milvus 集合 {name}")
    if not client.has_collection(name):
        client.create_collection(name, dimension=dim, metric_type="COSINE",
                                 id_type="int", auto_id=False)
        print(f"✅ Milvus 集合 {name} 创建成功（dim={dim}）")


def upsert_batches(client: MilvusClient, name: str, rows: list[dict]):
    for i in tqdm(range(0, len(rows), WRITE_BATCH_SIZE), desc="写入 Milvus", unit="批"):
        client.upsert(collection_name=name, data=rows[i:i + WRITE_BATCH_SIZE])
    client.flush(name)
