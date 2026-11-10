import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

from app.graph import nodes
from app.graph.builder import build_graph
from app.services import stream_service as stream


@pytest.mark.parametrize("ai_result", [
    {"status": "unavailable", "message": "AI暂不可用"},
    {"status": "ready", "markdown": "## 工作与发展\n\n可以留意自己的做事节奏。", "search_status": "unavailable", "sources": []},
])
def test_actual_graph_local_answer_is_emitted_and_persisted_once(ai_result):
    async def go():
        conv = SimpleNamespace(id=999999, summary="")
        db = SimpleNamespace(get=AsyncMock(return_value=conv))
        @asynccontextmanager
        async def session():
            yield db
        events = []
        async def emit(cid, event):
            events.append(event)
        async def context(state):
            return {"window": [], "topic_state": {}, "summary": "", "recipe_cache": [], "dev_cache": []}
        # Run the real LangGraph nodes and real engine. Only storage / LLM are isolated.
        with patch.object(nodes, "load_context", context):
            graph = build_graph()
        forbidden = SimpleNamespace(ainvoke=AsyncMock(side_effect=AssertionError("unexpected model call")))
        with patch.object(stream, "chat_graph", graph), patch.object(stream, "AsyncSessionLocal", session), \
             patch.object(stream, "_emit", emit), patch.object(stream, "redis_client", SimpleNamespace(expire=AsyncMock(), delete=AsyncMock())), \
             patch.object(stream.conv_crud, "add_message", AsyncMock()) as persist, \
             patch.object(stream.context_service, "append_turn", AsyncMock(return_value=[])), \
             patch.object(stream.context_service, "save_topic_state", AsyncMock()) as save_topic, \
             patch("app.fortune.chat.interpret", AsyncMock(return_value=ai_result)), \
             patch.object(nodes, "router_llm", forbidden), patch.object(nodes, "sentiment_llm", forbidden):
            await stream.run_generation(conv.id, 1, "紫微排盘，女，公历1990-05-17 14:30", "test-fortune")
            assert [event["type"] for event in events] == ["meta", "delta", "done"]
            assert events[0]["domain"] == "fortune" and not events[0]["rag"]
            assert "仅供娱乐" in events[1]["delta"] and "<h2>" in events[1]["html"]
            persist.assert_awaited_once()
            assert persist.await_args.args[3] == events[1]["delta"]
            assert save_topic.await_args.args[2]["fortune_facts"]["birth_time"] == "14:30"
            forbidden.ainvoke.assert_not_called()
    asyncio.run(go())
