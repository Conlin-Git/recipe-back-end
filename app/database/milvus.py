from pymilvus import MilvusClient
from app.config import settings

# 创建Milvus客户端
milvus_client = MilvusClient(
    uri=f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}"
)

# 初始化集合（开发用，生产手动管理）
def init_milvus_collection():
    if not milvus_client.has_collection(collection_name=settings.MILVUS_COLLECTION_NAME):
        milvus_client.create_collection(
            collection_name=settings.MILVUS_COLLECTION_NAME,
            dimension=settings.MILVUS_EMBEDDING_DIM,
            metric_type="COSINE",
            id_type="int",
            auto_id=False,
        )
        print(f"✅ Milvus集合 {settings.MILVUS_COLLECTION_NAME} 创建成功")
    else:
        print(f"✅ Milvus集合 {settings.MILVUS_COLLECTION_NAME} 已存在")
