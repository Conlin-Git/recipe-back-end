import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from langchain_openai import ChatOpenAI

from app.config import settings
from app.graph import llms, nodes
from app.graph.builder import build_graph
from app.services import stream_service as stream


def model(handler):
    return ChatOpenAI(model='fixture', api_key='fixture', base_url='https://fixture.invalid/v1',
                      streaming=True, max_retries=0, timeout=1,
                      tags=[llms.FINAL_ANSWER_TAG],
                      http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def answer(request):
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=(
        'data: ' + json.dumps({'id': 'fixture', 'object': 'chat.completion.chunk',
                              'model': 'fixture', 'choices': [{'index': 0, 'delta': {'content': '阿福收到'}, 'finish_reason': None}]}, ensure_ascii=False)
        + '\n\ndata: [DONE]\n\n').encode())


def limited(request):
    return httpx.Response(429, json={'error': {'message': 'rate limited', 'type': 'rate_limit_error', 'code': '1302'}})


def run_chat(primary_handler, fallback_handler=answer, timeout=1, domain="chat"):
    async def run():
        calls = []
        def primary(request):
            calls.append('primary')
            return primary_handler(request)
        def fallback(request):
            calls.append('fallback')
            return fallback_handler(request)
        conv = SimpleNamespace(id=900001, summary='')
        @asynccontextmanager
        async def session():
            yield SimpleNamespace(get=AsyncMock(return_value=conv))
        events = []
        async def emit(cid, event):
            events.append(event)
        async def context(state):
            return {'window': [], 'topic_state': {}, 'summary': '', 'recipe_cache': [], 'dev_cache': []}
        async def route(state):
            return {'route': {'domain': domain, 'needs_rag': False}, 'topic_state': {}}
        async def sentiment(state):
            return {'sentiment': {}}
        with patch.object(llms, '_primary_answer_llm', model(primary)), \
             patch.object(llms, '_fallback_answer_llm', model(fallback)), \
             patch.object(nodes, 'answer_llm', llms.create_answer_llm()), \
             patch.object(nodes, 'load_context', context), \
             patch.object(nodes, 'orchestrator_node', route), \
             patch.object(nodes, 'sentiment_node', sentiment):
            graph = build_graph()
            redis = SimpleNamespace(expire=AsyncMock(), delete=AsyncMock())
            with patch.object(stream, 'chat_graph', graph), patch.object(stream, 'AsyncSessionLocal', session), \
                 patch.object(stream, '_emit', emit), patch.object(stream, 'redis_client', redis), \
                 patch.object(stream.conv_crud, 'add_message', AsyncMock()) as persist, \
                 patch.object(stream.context_service, 'append_turn', AsyncMock(return_value=[])), \
                 patch.object(settings, 'CHAT_GENERATION_TIMEOUT_SECONDS', timeout):
                await stream.run_generation(conv.id, 1, '你好', 'fixture-turn')
                redis.delete.assert_awaited_once_with('chat:gen:900001')
                assert conv.id not in stream._tasks
                return events, calls, persist
    return asyncio.run(run())


@pytest.mark.parametrize('primary_handler,expected_calls', [(answer, ['primary']), (limited, ['primary', 'fallback'])])
def test_real_graph_emits_answer_and_cleans_generation(primary_handler, expected_calls):
    events, calls, persist = run_chat(primary_handler)
    assert calls == expected_calls
    assert [event['type'] for event in events] == ['meta', 'delta', 'done']
    assert events[1]['delta'] == '阿福收到' and '<p>阿福收到</p>' == events[1]['html']
    assert all(event['turn_id'] == 'fixture-turn' for event in events)
    persist.assert_awaited_once()


def test_both_models_limited_emit_terminal_error_without_retries():
    events, calls, persist = run_chat(limited, limited)
    assert calls == ['primary', 'fallback']
    assert events[-1]['type'] == 'error' and '繁忙' in events[-1]['message']
    persist.assert_not_called()


def test_generation_deadline_emits_error_and_unlocks():
    async def never_finishes(request):
        await asyncio.Event().wait()
    events, calls, persist = run_chat(never_finishes, timeout=0.02)
    assert events[-1]['type'] == 'error' and '超时' in events[-1]['message']
    assert calls == ['primary']
    persist.assert_not_called()


def test_tool_schema_is_preserved_when_switching_models():
    async def run():
        payloads = []
        def fallback(request):
            payloads.append(json.loads(request.content))
            return answer(request)
        tool = {'type': 'function', 'function': {'name': 'search_recipe', 'description': 'Search', 'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}}
        with patch.object(llms, '_primary_answer_llm', model(limited)), patch.object(llms, '_fallback_answer_llm', model(fallback)):
            result = await llms.create_answer_llm([tool]).ainvoke('查菜谱')
        assert result.content == '阿福收到'
        assert payloads[0]['tools'] == [tool]
    asyncio.run(run())


def test_gaokao_clarification_without_model_tokens_reaches_the_user():
    events, calls, persist = run_chat(answer, domain='gaokao')
    assert calls == []
    assert [event['type'] for event in events] == ['meta', 'delta', 'done']
    assert '请补充' in events[1]['delta'] and '科类' in events[1]['delta']
    assert '<p>' in events[1]['html']
    assert persist.await_args.args[3] == events[1]['delta']


def test_naming_citations_added_after_model_stream_are_emitted():
    with patch.object(nodes, 'search_naming_knowledge', AsyncMock(return_value=[])):
        events, calls, persist = run_chat(answer, domain='naming')
    assert calls == ['primary']
    text = ''.join(event.get('delta', '') for event in events)
    assert text.startswith('阿福收到') and '参考出处' in text and '未命中' in text
    assert events[-1]['type'] == 'done'
    assert persist.await_args.args[3] == text


def test_stop_cancels_blocked_generation_and_unlocks_before_returning():
    async def check():
        started = asyncio.Event()
        async def blocked_graph(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
            yield ('updates', {})
        conv = SimpleNamespace(id=900002, summary='')
        @asynccontextmanager
        async def session():
            yield SimpleNamespace(get=AsyncMock(return_value=conv))
        events = []
        async def emit(cid, event):
            events.append(event)
        redis = SimpleNamespace(exists=AsyncMock(return_value=True), delete=AsyncMock(), expire=AsyncMock())
        with patch.object(stream, 'chat_graph', SimpleNamespace(astream=blocked_graph)), \
             patch.object(stream, 'AsyncSessionLocal', session), patch.object(stream, '_emit', emit), \
             patch.object(stream, 'redis_client', redis), \
             patch.object(stream.conv_crud, 'add_message', AsyncMock()) as persist:
            task = asyncio.create_task(stream.run_generation(conv.id, 1, '你好', 'stopped-turn'))
            stream._tasks[conv.id] = task
            await asyncio.wait_for(started.wait(), 1)
            assert await asyncio.wait_for(stream.request_stop(conv.id), .5)
            assert task.done() and conv.id not in stream._tasks
            assert [event['type'] for event in events] == ['stopped']
            assert events[0]['turn_id'] == 'stopped-turn'
            redis.delete.assert_awaited_once_with('chat:gen:900002')
            persist.assert_not_called()
            assert not await stream.request_stop(conv.id)
    asyncio.run(check())


def test_stop_before_generation_task_starts_also_unlocks():
    async def check():
        cid = 900003
        redis = SimpleNamespace(exists=AsyncMock(return_value=True), delete=AsyncMock())
        with patch.object(stream, 'redis_client', redis), \
             patch.object(stream, 'AsyncSessionLocal') as session:
            task = asyncio.create_task(stream.run_generation(cid, 1, '你好', 'not-started'))
            stream._tasks[cid] = task
            assert await stream.request_stop(cid)
            assert task.done() and cid not in stream._tasks and cid not in stream._stop_requested
            redis.delete.assert_awaited_once_with('chat:gen:900003')
            session.assert_not_called()
    asyncio.run(check())
