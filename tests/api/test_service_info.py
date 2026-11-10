"""模型公示接口：匿名访问、切换配置与敏感配置隔离，无需数据库或模型请求。"""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

TEST_CONFIG = {
    "JWT_SECRET_KEY": "service-info-test-secret-at-least-32-bytes",
    "MYSQL_HOST": "127.0.0.1", "MYSQL_PORT": "3306", "MYSQL_USER": "test",
    "MYSQL_PASSWORD": "private-database-password", "MYSQL_DATABASE": "test",
    "MILVUS_HOST": "127.0.0.1", "MILVUS_PORT": "19530", "MILVUS_COLLECTION_NAME": "test",
    "NEO4J_URI": "bolt://localhost:7687", "NEO4J_USER": "test", "NEO4J_PASSWORD": "test",
}

with patch.dict(os.environ, TEST_CONFIG):
    from app.config import Settings

# 只加载待测路由，不导入会初始化数据库及大模型客户端的 v1 聚合模块。
spec = importlib.util.spec_from_file_location(
    "public_service_info", Path(__file__).resolve().parents[2] / "app/api/v1/service_info.py",
)
service_info = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service_info)


class ServiceInfoTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {}, clear=True):
            self.settings = Settings(
                _env_file=None, **TEST_CONFIG,
                SILICONFLOW_API_KEY="private-api-key", ZHIPU_API_KEY="private-zhipu-key",
                SILICONFLOW_BASE_URL="https://private-gateway.example/v1",
                ZHIPU_LLM_MODEL="glm-4.5-flash", LLM_PROVIDER="siliconflow",
            )
        app = FastAPI()
        app.include_router(service_info.router, prefix="/api/v1")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        config_patch = patch.object(service_info, "settings", self.settings)
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_anonymous_access_reports_the_active_provider_and_model(self):
        response = self.client.get("/api/v1/service-info")
        self.assertEqual(response.status_code, 200)
        source = response.json()["models"][0]
        self.assertEqual(source["provider"], "硅基流动")
        self.assertEqual(source["model"], self.settings.LLM_MODEL)
        self.settings.LLM_PROVIDER = "zhipu"
        source = self.client.get("/api/v1/service-info").json()["models"][0]
        self.assertEqual(source["provider"], "智谱 BigModel")
        self.assertEqual(source["model"], "glm-4.5-flash")

    def test_embedding_provider_and_auxiliary_models_follow_configuration(self):
        self.settings.EMBEDDING_PROVIDER = "zhipu"
        self.settings.SENTIMENT_LLM_MODEL = "auxiliary-model"
        sources = self.client.get("/api/v1/service-info").json()["models"]
        embedding = next(source for source in sources if source["purpose"] == "知识库检索")
        self.assertEqual(embedding["provider"], "智谱 BigModel")
        self.assertEqual(embedding["model"], "embedding-3")
        self.assertIn("auxiliary-model", sources[1]["model"])

    def test_only_public_names_are_returned_without_credentials_or_connection_details(self):
        response = self.client.get("/api/v1/service-info", headers={"Authorization": "Bearer expired"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"models"})
        for source in response.json()["models"]:
            self.assertEqual(set(source), {"purpose", "provider", "model"})
        for private_value in ["private-api-key", "private-zhipu-key", "private-gateway", "private-database-password", TEST_CONFIG["JWT_SECRET_KEY"]]:
            self.assertNotIn(private_value, response.text)


if __name__ == "__main__":
    unittest.main()
