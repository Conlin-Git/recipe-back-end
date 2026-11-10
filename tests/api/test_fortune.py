import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.fortune.service import FortuneUnavailable

spec = importlib.util.spec_from_file_location("fortune_api", Path(__file__).resolve().parents[2] / "app/api/v1/fortune.py")
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)
BASE = {"calendar_type": "solar", "birth_date": "1990-05-17", "birth_time": "14:30", "gender": "female"}


def client(authenticated=True):
    app = FastAPI()
    app.include_router(route.router, prefix="/api/v1")
    async def current_user():
        if not authenticated:
            raise HTTPException(401, "请先登录")
        return SimpleNamespace(id=1)
    app.dependency_overrides[get_current_user] = current_user
    return TestClient(app)


def test_auth_required_and_invalid_input_never_calls_engine():
    with client(False) as c, patch.object(route, "analyze", AsyncMock()) as engine:
        assert c.post("/api/v1/fortune/analyze", json=BASE).status_code == 401
        engine.assert_not_called()
    with client() as c, patch.object(route, "analyze", AsyncMock()) as engine:
        for changes in [{"birth_time": "25:00"}, {"gender": "unknown"}, {"birth_date": "1990-02-30"}, {"unexpected": "field"}]:
            assert c.post("/api/v1/fortune/analyze", json={**BASE, **changes}).status_code == 422
        engine.assert_not_called()


def test_real_engine_response_and_service_errors():
    with client() as c:
        response = c.post("/api/v1/fortune/analyze", json=BASE)
        assert response.status_code == 200
        assert len(response.json()["palaces"]) == 12
        assert "仅供娱乐" in response.json()["disclaimer"]
        with patch.object(route, "analyze", AsyncMock(side_effect=FortuneUnavailable("稍后重试"))):
            assert c.post("/api/v1/fortune/analyze", json=BASE).status_code == 503
        with patch.object(route, "analyze", AsyncMock(side_effect=ValueError("闰月不存在"))):
            assert c.post("/api/v1/fortune/analyze", json=BASE).status_code == 422


def test_ai_requires_auth_validates_birth_and_recomputes_chart():
    with client(False) as c, patch.object(route, "interpret", AsyncMock()) as ai:
        assert c.post("/api/v1/fortune/interpret", json={"birth": BASE}).status_code == 401
        ai.assert_not_called()
    with client() as c, patch.object(route, "interpret", AsyncMock(return_value={"status": "ready"})) as ai:
        assert c.post("/api/v1/fortune/interpret", json={"birth": {**BASE, "birth_time": "25:00"}}).status_code == 422
        assert c.post("/api/v1/fortune/interpret", json={"birth": BASE, "chart": {}}).status_code == 422
        ai.assert_not_called()
        assert c.post("/api/v1/fortune/interpret", json={"birth": BASE}).status_code == 200
        assert len(ai.await_args.args[0]["chart"]["palaces"]) == 12
