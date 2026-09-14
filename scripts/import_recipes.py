"""
菜谱数据导入脚本：recipes.xlsx -> 硅基流动 bge-m3 向量化 -> Milvus + Neo4j

用法（在 recipe-back-end 目录下）：
    .venv/bin/python scripts/import_recipes.py                # 增量导入（可重复执行，幂等）
    .venv/bin/python scripts/import_recipes.py --fresh        # 清空 Milvus 集合和 Neo4j 菜谱数据后重新导入

设计：
- Embedding 走硅基流动 OpenAI 兼容接口，批量 + 多线程并发，保证速度
- Milvus 用 upsert、Neo4j 用 MERGE，重复执行不会产生脏数据
- Neo4j 中从 yl（原料）列解析食材，建 Ingredient 节点和 HAS_INGREDIENT 关系
"""
import argparse
import os
import pickle
import re
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from neo4j import GraphDatabase
from openai import OpenAI
from pymilvus import MilvusClient
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import settings  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
EXCEL_PATH = os.path.join(DATA_DIR, "recipes.xlsx")
EMBED_CACHE_PATH = os.path.join(DATA_DIR, "embeddings_cache.pkl")

EMBED_BATCH_SIZE = 32      # 硅基流动单次 embedding 请求的文本数
EMBED_WORKERS = 8          # embedding 并发线程数
WRITE_BATCH_SIZE = 500     # Milvus/Neo4j 每批写入条数
MAX_TEXT_CHARS = 1200      # 向量化文本最大长度：检索主要靠菜名/简介/原料/辅料，文本越短越省 TPM 额度

# 向量化时用到的文本列
TEXT_COLUMNS = ["title", "desc", "difficulty", "costtime", "tip", "yl", "fl", "steptext"]
# 存入 Milvus 动态字段的元数据列（检索时随结果返回）
MILVUS_META_COLUMNS = ["title", "desc", "difficulty", "costtime", "thumb", "videourl", "favnum"]
# 存入 Neo4j Recipe 节点的属性列
NEO4J_PROP_COLUMNS = ["zid", "title", "thumb", "videourl", "desc", "difficulty",
                      "costtime", "tip", "yl", "fl", "steptext", "steppic",
                      "grade", "up", "viewnum", "favnum", "outdate", "status"]

TEXT_LABELS = {"title": "菜名", "desc": "简介", "difficulty": "难度", "costtime": "耗时",
               "tip": "小贴士", "yl": "原料", "fl": "辅料", "steptext": "步骤"}


def to_cid(value) -> int:
    """cid 转 int，转不了就用 crc32 兜底，保证主键是 int64"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return zlib.crc32(str(value).encode("utf-8"))


def clean_str(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def build_embed_text(row: pd.Series) -> str:
    parts = [f"{TEXT_LABELS[col]}：{clean_str(row[col])}"
             for col in TEXT_COLUMNS if clean_str(row[col])]
    return "\n".join(parts)[:MAX_TEXT_CHARS]


def parse_ingredients(yl: str) -> list[str]:
    """从 yl（原料）列解析食材名。兼容 '、,，;；/|' 分隔，去掉数量部分"""
    if not yl:
        return []
    names = []
    for token in re.split(r"[、,，;；/|\n]+", yl):
        token = token.strip()
        # 从数字、空白或括号处截断，去掉 "2个" "(适量)" 之类的数量
        name = re.split(r"[\d\s(（【\[]", token)[0].strip()
        if 1 <= len(name) <= 20 and name not in ("适量", "少许", "若干"):
            names.append(name)
    # 去重保序
    return list(dict.fromkeys(names))


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
    def __init__(self, limiter: RateLimiter):
        self.client = OpenAI(api_key=settings.EMBEDDING_API_KEY,
                             base_url=settings.EMBEDDING_BASE_URL,
                             timeout=120)
        self.limiter = limiter

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

    def embed_all(self, rows: list[dict]) -> list[list[float]]:
        """rows: [{cid, text}]，带断点缓存，返回 {cid: vector}"""
        cache: dict[int, list[float]] = {}
        if os.path.exists(EMBED_CACHE_PATH):
            with open(EMBED_CACHE_PATH, "rb") as f:
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
                    with open(EMBED_CACHE_PATH, "wb") as f:
                        pickle.dump(cache, f)
                    done_since_save = 0
        with open(EMBED_CACHE_PATH, "wb") as f:
            pickle.dump(cache, f)
        return cache


def import_milvus(client: MilvusClient, rows: list[dict], fresh: bool):
    name = settings.MILVUS_COLLECTION_NAME
    if fresh and client.has_collection(name):
        client.drop_collection(name)
        print(f"🗑️  已删除旧 Milvus 集合 {name}")
    if not client.has_collection(name):
        client.create_collection(name, dimension=settings.MILVUS_EMBEDDING_DIM,
                                 metric_type="COSINE", id_type="int", auto_id=False)
        print(f"✅ Milvus 集合 {name} 创建成功（dim={settings.MILVUS_EMBEDDING_DIM}）")

    for i in tqdm(range(0, len(rows), WRITE_BATCH_SIZE), desc="写入 Milvus", unit="批"):
        client.upsert(collection_name=name, data=rows[i:i + WRITE_BATCH_SIZE])
    client.flush(name)


def import_neo4j(driver: GraphDatabase.driver, rows: list[dict], fresh: bool):
    cypher = """
    UNWIND $rows AS row
    MERGE (r:Recipe {cid: row.cid})
    SET r += row.props
    WITH r, row
    UNWIND row.ingredients AS ing_name
    MERGE (i:Ingredient {name: ing_name})
    MERGE (r)-[:HAS_INGREDIENT]->(i)
    """
    with driver.session() as session:
        if fresh:
            session.run("MATCH (r:Recipe) DETACH DELETE r")
            session.run("MATCH (i:Ingredient) WHERE NOT (i)--() DELETE i")
            print("🗑️  已清空 Neo4j 中的 Recipe/Ingredient 数据")
        session.run("CREATE CONSTRAINT recipe_cid IF NOT EXISTS "
                    "FOR (r:Recipe) REQUIRE r.cid IS UNIQUE")
        for i in tqdm(range(0, len(rows), WRITE_BATCH_SIZE), desc="写入 Neo4j", unit="批"):
            session.run(cypher, rows=rows[i:i + WRITE_BATCH_SIZE])


def main():
    parser = argparse.ArgumentParser(description="菜谱数据导入 Milvus + Neo4j")
    parser.add_argument("--excel", default=EXCEL_PATH, help="xlsx 文件路径")
    parser.add_argument("--fresh", action="store_true", help="清空 Milvus/Neo4j 旧数据后重新导入（不影响 embedding 缓存）")
    parser.add_argument("--tpm", type=int, default=2_000_000,
                        help="初始 TPM 限流值（默认 200 万，触发 429 会自动降速，按账户实际额度调小更稳）")
    parser.add_argument("--limit", type=int, default=0,
                        help="只导入前 N 条（0 或负数表示全量），用于先跑通验证")
    args = parser.parse_args()

    t0 = time.time()
    print(f"📖 读取 {args.excel} …")
    df = pd.read_excel(args.excel)
    df = df[df["title"].notna() & (df["title"].astype(str).str.strip() != "")]
    if args.limit > 0:
        df = df.head(args.limit)
    print(f"✅ 本次导入 {len(df)} 条菜谱")

    # ---------- 准备数据 ----------
    milvus_rows, neo4j_rows, embed_inputs = [], [], []
    for _, row in df.iterrows():
        cid = to_cid(row["cid"])
        embed_inputs.append({"cid": cid, "text": build_embed_text(row)})
        favnum = row["favnum"]
        milvus_rows.append({
            "id": cid,
            **{col: clean_str(row[col]) for col in MILVUS_META_COLUMNS if col != "favnum"},
            "favnum": int(favnum) if pd.notna(favnum) and str(favnum).strip() != "" else 0,
        })
        neo4j_rows.append({
            "cid": cid,
            "props": {col: clean_str(row[col]) for col in NEO4J_PROP_COLUMNS},
            "ingredients": parse_ingredients(clean_str(row["yl"])),
        })

    # ---------- 向量化（带缓存，可断点续跑） ----------
    print(f"🔢 调用硅基流动 {settings.EMBEDDING_MODEL} 向量化（{EMBED_WORKERS} 线程并发，TPM 上限 {args.tpm}）…")
    vectors = Embedder(RateLimiter(args.tpm)).embed_all(embed_inputs)
    for r in milvus_rows:
        r["vector"] = vectors[r["id"]]

    # ---------- 写入 Milvus ----------
    milvus_client = MilvusClient(uri=f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}")
    import_milvus(milvus_client, milvus_rows, args.fresh)
    print(f"✅ Milvus 导入完成：{len(milvus_rows)} 条")

    # ---------- 写入 Neo4j ----------
    driver = GraphDatabase.driver(settings.NEO4J_URI,
                                  auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD))
    import_neo4j(driver, neo4j_rows, args.fresh)
    driver.close()
    n_ing = sum(len(r["ingredients"]) for r in neo4j_rows)
    print(f"✅ Neo4j 导入完成：{len(neo4j_rows)} 个菜谱节点，{n_ing} 条食材关系")

    print(f"🎉 全部完成，总耗时 {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
