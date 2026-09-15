from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.config import settings
from app.core.exceptions import register_exception_handlers
from app.database.milvus import init_milvus_collection
from app.database.mysql import engine

app = FastAPI(
    title="菜谱对话机器人API",
    description="LangGraph 多 Agent 编排（编排/情感/菜谱/开发/闲聊）+ Redis 分层上下文 + Milvus RAG 的菜谱智能助手",
    version="1.0.0",
)

# 跨域配置：只允许白名单前端域名（生产在 .env 配 CORS_ORIGINS 改成正式域名）
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGIN_LIST,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 统一业务异常处理
register_exception_handlers(app)

# 注册路由（接口版本化，新增接口在 app/api/v1/__init__.py 聚合）
from app.api.v1 import api_v1_router

app.include_router(api_v1_router)

# 头像等上传文件静态访问
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# 启动检查
@app.on_event("startup")
async def startup_event():
    print("\n🚀 服务启动中，检查所有组件连接...")

    # 检查MySQL
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        print("✅ MySQL连接成功")
    except Exception as e:
        print(f"❌ MySQL连接失败：{e}")

    # 初始化Milvus
    try:
        init_milvus_collection()
    except Exception as e:
        print(f"❌ Milvus连接失败：{e}")

    # 检查Redis
    try:
        from app.database.redis import redis_client
        await redis_client.ping()
        print("✅ Redis连接成功")
    except Exception as e:
        print(f"❌ Redis连接失败：{e}")

    print("\n🎉 服务启动完成！")


# 健康检查接口
@app.get("/health")
async def health_check():
    result = {"status": "ok", "services": {}}

    # MySQL状态（错误细节只进服务端日志，不外泄连接信息）
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        result["services"]["mysql"] = "ok"
    except Exception as e:
        print(f"❌ /health MySQL 检查失败：{e}")
        result["services"]["mysql"] = "fail"

    # Milvus状态
    try:
        from app.database.milvus import milvus_client
        milvus_client.list_collections()
        result["services"]["milvus"] = "ok"
    except Exception as e:
        print(f"❌ /health Milvus 检查失败：{e}")
        result["services"]["milvus"] = "fail"

    # Neo4j状态
    try:
        from app.database.neo4j import neo4j_graph
        if neo4j_graph:
            neo4j_graph.query("RETURN 1")
            result["services"]["neo4j"] = "ok"
        else:
            result["services"]["neo4j"] = "fail"
    except Exception as e:
        print(f"❌ /health Neo4j 检查失败：{e}")
        result["services"]["neo4j"] = "fail"

    return result
