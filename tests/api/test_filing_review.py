"""审核态鉴权回归：无需连接 MySQL / Redis / 大模型。"""
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import jwt
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

# 使用独立测试配置，不读取或输出实际部署密钥。
with patch.dict(os.environ, {
    "JWT_SECRET_KEY": "filing-review-test-secret-at-least-32-bytes",
    "MYSQL_HOST": "127.0.0.1", "MYSQL_PORT": "3306", "MYSQL_USER": "test",
    "MYSQL_PASSWORD": "test", "MYSQL_DATABASE": "test",
    "MILVUS_HOST": "127.0.0.1", "MILVUS_PORT": "19530",
    "MILVUS_COLLECTION_NAME": "test", "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "test", "NEO4J_PASSWORD": "test",
}):
    from app.api.deps import get_current_user, get_db
    from app.config import settings
    from app.core import security
    from app.core.exceptions import register_exception_handlers
    from app.models.user import User

from scripts.create_filing_review_session import write_frontend_env


class FilingReviewTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        register_exception_handlers(self.app)

        async def fake_db():
            yield object()

        self.app.dependency_overrides[get_db] = fake_db

        async def endpoint(user=Depends(get_current_user)):
            return {"user_id": user.id}

        routes = [
            ("GET", "/api/v1/users/me"),
            ("PUT", "/api/v1/users/me"),
            ("POST", "/api/v1/users/me/avatar"),
            ("POST", "/api/v1/auth/logout"),
            ("GET", "/api/v1/conversations"),
            ("GET", "/api/v1/conversations/{conversation_id}/messages"),
            ("POST", "/api/v1/conversations/{conversation_id}/read"),
            ("DELETE", "/api/v1/conversations/{conversation_id}"),
            ("POST", "/api/v1/chat"),
            ("GET", "/api/v1/chat/{conversation_id}/stream"),
            ("POST", "/api/v1/chat/{conversation_id}/stop"),
            ("POST", "/api/v1/other"),
        ]
        api = APIRouter(prefix="/api/v1")
        for method, path in routes:
            # 与应用一致：模块路由 → v1 聚合路由 → app，多层 prefix。
            suffix = path.removeprefix("/api/v1")
            group, _, leaf = suffix.lstrip("/").partition("/")
            router = APIRouter(prefix=f"/{group}")
            router.add_api_route(f"/{leaf}" if leaf else "", endpoint, methods=[method])
            api.include_router(router)
        self.app.include_router(api)
        self.client = TestClient(self.app)
        self.revoked = patch("app.api.deps.is_token_revoked", AsyncMock(return_value=False))
        self.user = patch("app.api.deps.user_crud.get_by_id", AsyncMock(return_value=User(id=42, username="review")))
        self.revoked.start()
        self.user.start()
        self.addCleanup(self.revoked.stop)
        self.addCleanup(self.user.stop)
        self.addCleanup(self.client.close)

    def request(self, method, path, token):
        return self.client.request(method, path, headers={"Authorization": f"Bearer {token}"})

    def test_review_expires_after_30_days_and_regular_lifetime_is_unchanged(self):
        claims = security.decode_access_token_payload(security.create_filing_review_token(42))
        self.assertEqual(claims["exp"] - claims["iat"], 30 * 24 * 3600)
        self.assertEqual(claims["scope"], "filing-review")
        regular = security.decode_access_token_payload(security.create_access_token(42))
        remaining = regular["exp"] - datetime.now(timezone.utc).timestamp()
        self.assertAlmostEqual(remaining, settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60, delta=2)
        self.assertNotIn("scope", regular)

    def test_review_allows_content_and_chat(self):
        token = security.create_filing_review_token(42)
        for method, path in [
            ("GET", "/api/v1/users/me"), ("GET", "/api/v1/conversations"),
            ("GET", "/api/v1/conversations/1/messages"),
            ("POST", "/api/v1/conversations/1/read"),
            ("POST", "/api/v1/chat"), ("GET", "/api/v1/chat/1/stream"),
            ("POST", "/api/v1/chat/1/stop"),
        ]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, token).status_code, 200)

    def test_review_cannot_modify_profile_delete_history_or_revoke_shared_token(self):
        token = security.create_filing_review_token(42)
        for method, path in [
            ("PUT", "/api/v1/users/me"), ("POST", "/api/v1/users/me/avatar"),
            ("DELETE", "/api/v1/conversations/1"), ("POST", "/api/v1/auth/logout"),
            ("POST", "/api/v1/other"),
        ]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, token).status_code, 403)
                self.assertEqual(self.request(method, path, security.create_access_token(42)).status_code, 200)

    def test_expired_tampered_and_revoked_review_tokens_are_rejected(self):
        now = datetime.now(timezone.utc)
        expired = jwt.encode(
            {"sub": "42", "scope": "filing-review", "exp": now - timedelta(seconds=1)},
            settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM,
        )
        self.assertEqual(self.request("GET", "/api/v1/users/me", expired).status_code, 401)
        forged = jwt.encode(
            {"sub": "42", "scope": "filing-review", "exp": now + timedelta(days=30)},
            "different-signing-key-at-least-32-bytes", algorithm=settings.JWT_ALGORITHM,
        )
        self.assertEqual(self.request("GET", "/api/v1/users/me", forged).status_code, 401)
        with patch("app.api.deps.is_token_revoked", AsyncMock(return_value=True)):
            self.assertEqual(self.request("GET", "/api/v1/users/me", security.create_filing_review_token(42)).status_code, 401)

    def test_env_writer_preserves_settings_and_replaces_only_review_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.production.local"
            path.write_text("OTHER=value\nVITE_FILING_REVIEW_TOKEN=old\n", encoding="utf-8")
            write_frontend_env(path, "new")
            self.assertEqual(path.read_text(), "OTHER=value\nVITE_FILING_REVIEW_TOKEN=new\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
