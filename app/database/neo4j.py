from neo4j import GraphDatabase
from app.config import settings

neo4j_driver = None
neo4j_graph = None

try:
    neo4j_driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    neo4j_driver.verify_connectivity()

    class Neo4jGraphLite:
        def __init__(self, driver):
            self.driver = driver

        def query(self, cypher, params=None):
            with self.driver.session() as session:
                result = session.run(cypher, params or {})
                return [record.data() for record in result]

        def refresh_schema(self):
            pass

        @property
        def schema(self):
            with self.driver.session() as session:
                labels = session.run("CALL db.labels() YIELD label RETURN collect(label) as labels").single()["labels"]
                rels = session.run("CALL db.relationshipTypes() YIELD relationshipType RETURN collect(relationshipType) as rels").single()["rels"]
                return f"Node labels: {labels}\nRelationship types: {rels}"

    neo4j_graph = Neo4jGraphLite(neo4j_driver)
    print("✅ Neo4j连接成功")
except Exception as e:
    print(f"⚠️  Neo4j连接失败：{e}")
    neo4j_graph = None
