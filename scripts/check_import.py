"""
检查 Milvus 和 Neo4j 中已导入的菜谱数据，各展示 10 条

用法（在 recipe-back-end 目录下）：
    .venv/bin/python scripts/check_import.py
"""
import os
import sys

from neo4j import GraphDatabase
from pymilvus import MilvusClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import settings  # noqa: E402


def check_milvus():
    print("=" * 60)
    print("Milvus")
    print("=" * 60)
    client = MilvusClient(uri=f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}")
    name = settings.MILVUS_COLLECTION_NAME
    if not client.has_collection(name):
        print(f"⚠️  集合 {name} 不存在，还没导入数据")
        return
    stats = client.get_collection_stats(name)
    print(f"集合 {name} 总条数：{stats['row_count']}")
    rows = client.query(collection_name=name, filter="id >= 0", limit=10,
                        output_fields=["id", "title", "difficulty", "costtime", "favnum"])
    for r in rows:
        print(f"  [{r['id']}] {r.get('title', '')} | 难度:{r.get('difficulty', '')}"
              f" | 耗时:{r.get('costtime', '')} | 收藏:{r.get('favnum', 0)}")
    # 验证向量维度
    sample = client.query(collection_name=name, filter="id >= 0", limit=1,
                          output_fields=["vector"])
    if sample:
        print(f"向量维度：{len(sample[0]['vector'])}")


def check_neo4j():
    print("\n" + "=" * 60)
    print("Neo4j")
    print("=" * 60)
    driver = GraphDatabase.driver(settings.NEO4J_URI,
                                  auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD))
    with driver.session() as session:
        n_recipe = session.run("MATCH (r:Recipe) RETURN count(r) AS n").single()["n"]
        n_ing = session.run("MATCH (i:Ingredient) RETURN count(i) AS n").single()["n"]
        n_rel = session.run("MATCH ()-[r:HAS_INGREDIENT]->() RETURN count(r) AS n").single()["n"]
        print(f"Recipe 节点：{n_recipe} | Ingredient 节点：{n_ing} | HAS_INGREDIENT 关系：{n_rel}")

        rows = session.run("""
            MATCH (r:Recipe)
            OPTIONAL MATCH (r)-[:HAS_INGREDIENT]->(i:Ingredient)
            RETURN r.cid AS cid, r.title AS title, r.difficulty AS difficulty,
                   r.costtime AS costtime, collect(i.name)[0..8] AS ingredients
            ORDER BY r.cid
            LIMIT 10
        """).data()
        for r in rows:
            ings = "、".join(r["ingredients"]) if r["ingredients"] else "（无食材关系）"
            print(f"  [{r['cid']}] {r['title']} | 难度:{r['difficulty']}"
                  f" | 耗时:{r['costtime']}\n      食材：{ings}")
    driver.close()


if __name__ == "__main__":
    check_milvus()
    check_neo4j()
