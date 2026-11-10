import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.fortune import interpretation as ai


def chart():
    # Hand-authored fixtures, unrelated to a person's birthday or real chart.
    return {"input": {"birth_date": "DO_NOT_SEND", "longitude": "PRIVATE"}, "chart": {"palaces": [
        {"name": name, "stars": [{"name": star, "type": "major"}]} for name, star in
        [("命宫", "天机"), ("官禄", "七杀"), ("夫妻", "天府"), ("仆役", "天同")]]}}


def test_context_excludes_personal_data_and_binds_topics():
    result = chart()
    context = ai.chart_context(result, question="工作")
    assert [t["id"] for t in context["topics"]] == ["career"]
    assert "DO_NOT_SEND" not in json.dumps(context)
    assert "PRIVATE" not in json.dumps(context)
    answer = ai.compose_sections(json.dumps({"sections": [{"id": "career", "paragraphs": ["可以观察自己是否喜欢明确目标。"]}]}), context)
    assert "官禄 · 七杀" in answer
    assert "天府" not in answer
    with pytest.raises(ValueError):
        ai.compose_sections('{"sections":[{"id":"love","paragraphs":["内容"]}]}', context)
    with pytest.raises(ValueError, match="mixed stars"):
        ai.compose_sections('{"sections":[{"id":"career","paragraphs":["天府代表你的工作"]}]}', context)
    question = ai.interpretation_question("公历1990年5月17日 14:30 东经116.4，想看工作")
    assert "1990" not in question and "14:30" not in question and "116.4" not in question
    assert "工作" in question


@pytest.mark.parametrize("reference_ok, status", [(True, "reference"), (False, "unavailable")])
def test_real_http_contract_fallback_and_safe_prose(monkeypatch, reference_ok, status):
    seen = []
    def handler(request):
        seen.append(request)
        if request.url.host == 'raw.githubusercontent.com':
            assert str(request.url) == ai.UPSTREAM_RAW and request.method == 'GET'
            assert 'authorization' not in request.headers
            return httpx.Response(200 if reference_ok else 503, text="'七杀': { keywords: '将星·果决', nature: '凶星', element: '金' }")
        assert str(request.url) == 'https://model.example/v1/chat/completions'
        body = json.loads(request.content)
        assert body['response_format'] == {'type': 'json_object'}
        content = json.loads(body['messages'][1]['content'])
        assert 'birth_date' not in content['chart']
        assert content['chart']['topics'][0]['palace'] == '官禄'
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"sections": [{"id": "career", "paragraphs": ["可以观察自己的做事节奏。[1]", "[假来源](https://fake.example) <script>bad</script>"]}]})}}]})
    client = httpx.AsyncClient
    monkeypatch.setattr(ai.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(ai, 'settings', SimpleNamespace(LLM_API_KEY='test-model', LLM_BASE_URL='https://model.example/v1', LLM_MODEL='test', LLM_THINKING=''))
    result = asyncio.run(ai.interpret(chart(), '工作'))
    assert result['status'] == 'ready'
    assert result['search_status'] == status
    assert '<script' not in result['html'] and 'fake.example' not in result['html']
    assert '<table' not in result['html']
    assert '官禄 · 七杀' in result['markdown']
    assert len(result['sources']) == (0 if status == 'unavailable' else 1)
    assert all('DO_NOT_SEND' not in r.content.decode() for r in seen)


def test_model_failure_does_not_discard_chart_or_claim_success(monkeypatch):
    client = httpx.AsyncClient
    monkeypatch.setattr(ai.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs))
    monkeypatch.setattr(ai, 'settings', SimpleNamespace(LLM_API_KEY='test', LLM_BASE_URL='https://model.example/v1', LLM_MODEL='test', LLM_THINKING=''))
    result = asyncio.run(ai.interpret(chart(), '工作'))
    assert result['status'] == 'unavailable' and not result['html']
    assert '命盘已保留' in result['message']


def test_unexpected_model_table_becomes_paragraphs():
    text = ai.prose_only('| 年份 | 说明 |\n| --- | --- |\n| 2026 | 观察习惯 |\n| 2027 | 再作调整 |')
    assert '年份：2026；说明：观察习惯' in text
    assert '年份：2027；说明：再作调整' in text
    assert '|' not in text


def test_topic_order_is_normalized_but_duplicate_or_missing_topics_rejected():
    context = ai.chart_context(chart())
    sections = [{"id": topic["id"], "paragraphs": ["可以留意自己在不同场合的习惯。"]} for topic in context["topics"]]
    report = ai.compose_sections(json.dumps({"sections": sections[::-1]}), context)
    assert report.index('性格与处事') < report.index('工作与发展') < report.index('感情与相处')
    with pytest.raises(ValueError, match='topic mismatch'):
        ai.compose_sections(json.dumps({"sections": sections[:1] * 4}), context)


@pytest.mark.parametrize('bad_text', [
    # Real failure shape: the model closed the root after the first topic.
    '{"sections":[{"id":"career","paragraphs":["第一段"]}]},{"id":"career","paragraphs":["第二段"]}]}',
    '{"sections":[{"id":"career","paragraphs":["天府影响你的工作"]}]}',
])
def test_bad_model_output_is_retried_once_and_validated(monkeypatch, bad_text):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        text = bad_text if len(requests) == 1 else '{"sections":[{"id":"career","paragraphs":["可以留意做事时是否喜欢明确的目标。","与他人合作时，先商量好各自负责的事情。"]}]}'
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": text}}]})
    monkeypatch.setattr(ai, 'settings', SimpleNamespace(LLM_API_KEY='not-logged', LLM_BASE_URL='https://model.example/v1', LLM_MODEL='test', LLM_THINKING=''))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ai.generate_prose(client, ai.chart_context(chart(), question='工作'), '工作', [])
    text = asyncio.run(run())
    assert len(requests) == 2 and all(r['response_format'] == {'type': 'json_object'} for r in requests)
    assert requests[1]['temperature'] == 0
    assert len(requests[1]['messages']) == 3
    assert '官禄 · 七杀' in text and '天府' not in text


def test_invalid_response_stops_after_two_attempts_with_safe_diagnostic(monkeypatch, caplog):
    count = 0
    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": 'PRIVATE_RESPONSE_NOT_FOR_LOGS'}}]})
    async def no_references(*args):
        return [], 'unavailable'
    client = httpx.AsyncClient
    monkeypatch.setattr(ai, 'online_references', no_references)
    monkeypatch.setattr(ai.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(ai, 'settings', SimpleNamespace(LLM_API_KEY='SECRET_NOT_FOR_LOGS', LLM_BASE_URL='https://model.example/v1', LLM_MODEL='test', LLM_THINKING=''))
    result = asyncio.run(ai.interpret(chart(), '工作'))
    assert count == 2
    assert result['status'] == 'unavailable' and result['error_code'] == 'invalid_json'
    assert '未通过核对' in result['message']
    assert 'reason=invalid_json' in caplog.text
    assert 'PRIVATE_RESPONSE_NOT_FOR_LOGS' not in caplog.text and 'SECRET_NOT_FOR_LOGS' not in caplog.text


@pytest.fixture
def complete_chart():
    from app.fortune.service import analyze
    from app.schemas.fortune import FortuneInput
    return asyncio.run(analyze(FortuneInput(calendar_type='solar', birth_date='1990-05-17', birth_time='14:30', gender='female')))


def test_complete_reading_covers_every_palace_pattern_period_and_current_year(complete_chart):
    result = complete_chart
    context = ai.chart_context(result, question='完整白话解读')
    topics = {t['id']: t for t in context['topics']}
    assert {t[0] for t in ai.TOPICS} <= topics.keys()
    assert {'overview', 'basics', 'transforms', 'connections'} <= topics.keys()
    assert topics['overview']['current_age'] == result['chart']['currentAge']
    assert {k for k in topics if k.startswith('period_')} == {f'period_{i}' for i in range(12)}
    all_topics = ai.chart_context(result, question='全部大限解读')['topics']
    assert len([t for t in all_topics if t['id'].startswith('period_')]) == 12
    for i, p in enumerate(result['details']['patterns']):
        assert topics[f'pattern_{i}']['pattern'] == p  # Including all rule conditions.
    annual = [t for t in topics.values() if t['id'].startswith('year_')]
    assert len(annual) == 1
    assert annual[0]['selected_years'][0]['age'] == result['chart']['currentAge']
    assert annual[0]['palace_data']['branch'] == annual[0]['selected_years'][0]['palace_branch']
    assert 'birth_date' not in json.dumps(context)


def test_selected_period_year_and_childhood_do_not_return_all_periods(complete_chart):
    result = complete_chart
    p = result['details']['periods'][2]
    topics = ai.chart_context(result, question=f"{p['start_age']}岁所属大限的白话解读")['topics']
    assert [t['id'] for t in topics] == ['period_2']
    for age in [1, 40, 80]:
        topics = ai.chart_context(result, question=f'{age}虚岁流年')['topics']
        assert [t['id'] for t in topics] == [f'year_{age}']
        assert topics[0]['selected_years'][0]['age'] == age
    assert [t['id'] for t in ai.chart_context(result, question='童限解读')['topics']] == ['childhood']
    with pytest.raises(ValueError, match='outside chart'):
        ai.chart_context(result, question='150岁流年')
    with pytest.raises(ValueError, match='outside chart'):
        ai.chart_context(result, question='40到20岁流年')


def test_explicit_age_range_uses_each_actual_year(complete_chart):
    topics = ai.chart_context(complete_chart, question='35到44岁的白话解读')['topics']
    assert [t['id'] for t in topics] == [f'year_{age}' for age in range(35, 45)]
    assert topics[0]['period']['index'] != topics[-1]['period']['index'] or topics[0]['period']['start_age'] == 35


def test_range_end_is_not_current_age_or_fixed_125():
    from app.fortune.service import analyze
    from app.schemas.fortune import FortuneInput
    ends = set()
    for date in ['1990-05-17', '2000-01-01', '2023-02-10']:
        r = asyncio.run(analyze(FortuneInput(calendar_type='solar', birth_date=date, birth_time='14:30', gender='female')))
        last = r['details']['years'][-1]['age']
        ends.add(last)
        assert last == r['details']['periods'][0]['start_age'] + 119
        assert r['chart']['currentAge'] != last
        assert f"当前为 {r['chart']['currentAge']} 虚岁" in r['report']['markdown']
    assert len(ends) > 1 and 125 in ends


def test_complete_batches_are_all_validated_before_marking_ready(monkeypatch, complete_chart):
    seen_ids = []
    def handler(request):
        body = json.loads(request.content)
        topics = json.loads(body['messages'][1]['content'])['chart']['topics']
        seen_ids.extend(t['id'] for t in topics)
        assert len(topics) <= 4 and body['max_tokens'] >= len(topics) * 900
        sections = [{'id': t['id'], 'paragraphs': ['传统上可以从生活习惯来理解这个主题。', '可以观察自己的做事节奏，再结合实际情况调整。']} for t in topics]
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'sections': sections})}}]})
    async def no_references(*args):
        return [], 'unavailable'
    client = httpx.AsyncClient
    monkeypatch.setattr(ai, 'online_references', no_references)
    monkeypatch.setattr(ai.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(ai, 'settings', SimpleNamespace(LLM_API_KEY='test', LLM_BASE_URL='https://model.example/v1', LLM_MODEL='test', LLM_THINKING=''))
    result = asyncio.run(ai.interpret(complete_chart))
    assert result['status'] == 'ready'
    expected = [t['id'] for t in ai.chart_context(complete_chart)['topics']]
    assert result['coverage'] == expected
    assert set(seen_ids) == set(expected) and len(seen_ids) == len(expected)
    assert result['version'] == 'reference-v1'
    assert result['markdown'].count('## 五、大运走势') == 1
    assert '← 当前大限' in result['markdown']


def test_reference_style_keeps_grounded_star_names_with_explanation(monkeypatch):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        paragraphs = ['七杀星在官禄，传统上代表做事果断。'] if len(calls) == 1 else ['可以观察自己是否喜欢尽快做出决定。', '遇到分歧时先说明理由，减少口舌。']
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'sections': [{'id': 'career', 'paragraphs': paragraphs}]})}}]})
    monkeypatch.setattr(ai, 'settings', SimpleNamespace(LLM_API_KEY='test', LLM_BASE_URL='https://model.example/v1', LLM_MODEL='test', LLM_THINKING=''))
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ai.generate_prose(client, ai.chart_context(chart(), question='工作'), '工作', [])
    report = asyncio.run(go())
    prose = report.split('命盘依据：')[0]
    assert len(calls) == 1
    assert '七杀' in prose and '做事果断' in prose
    assert '星曜组合' in report and '官禄 · 七杀' in report


@pytest.mark.parametrize('missing', ['LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL'])
def test_missing_model_configuration_has_actionable_reason(monkeypatch, caplog, missing):
    values = dict(LLM_API_KEY='SECRET_TEST_KEY', LLM_BASE_URL='https://model.example/v1', LLM_MODEL='test')
    values[missing] = ' '
    monkeypatch.setattr(ai, 'settings', SimpleNamespace(**values))
    result = asyncio.run(ai.interpret(chart()))
    assert result['error_code'] == 'model_not_configured'
    assert '需要管理员' in result['message']
    assert 'SECRET_TEST_KEY' not in caplog.text


def test_complete_reading_keeps_all_periods_at_any_current_age(complete_chart):
    for age in [1, 120, 150]:
        complete_chart['chart']['currentAge'] = age
        topics = ai.chart_context(complete_chart)['topics']
        assert {t['id'] for t in topics if t['id'].startswith('period_')} == {f'period_{i}' for i in range(12)}
        assert any(t['id'] == 'childhood' for t in topics)


@pytest.fixture(autouse=True)
def clear_reading_cache():
    ai._batch_cache.clear()
    yield
    ai._batch_cache.clear()


def test_failed_batch_preserves_completed_sections_and_retry_uses_cache(monkeypatch, complete_chart):
    calls = []
    fail = True
    async def generate(client, context, question, sources):
        ids = [t['id'] for t in context['topics']]
        calls.append(ids)
        if fail and 'overview' in ids:
            raise httpx.ReadTimeout('slow')
        return '成功解读：' + ','.join(ids)
    async def references(client):
        return [], 'unavailable'
    monkeypatch.setattr(ai, 'generate_prose', generate)
    monkeypatch.setattr(ai, 'online_references', references)
    monkeypatch.setattr(ai, 'settings', SimpleNamespace(LLM_API_KEY='test', LLM_BASE_URL='https://example.test', LLM_MODEL='test'))
    first = asyncio.run(ai.interpret(complete_chart))
    assert first['status'] == 'partial' and first['html'] and first['missing_sections']
    assert 'overview' not in first['coverage']
    calls.clear()
    fail = False
    second = asyncio.run(ai.interpret(complete_chart))
    assert second['status'] == 'ready' and not second['missing_sections']
    assert len(calls) == 1 and 'overview' in calls[0]


def test_reference_report_chapters_and_actual_wuxing(complete_chart):
    from app.fortune.reference_format import layout_report
    topics = ai.chart_context(complete_chart)['topics']
    chapters = list(dict.fromkeys(t['chapter'] for t in topics))
    assert chapters == ['一、命宫核心格局', '二、身宫分析', '三、突出格局与组合', '四、重要宫位分析', '五、大运走势', '六、五行局分析', '七、人生总体评价']
    assert [t['id'] for t in topics[-4:]] == ['strengths', 'cautions', 'directions', 'conclusion']
    basics = next(t for t in topics if t['id'] == 'basics')
    assert basics['title'] == complete_chart['summary']['wuxing'] == '土五局'
    body = next(t for t in topics if t['id'] == 'body')
    palace = next(p for p in complete_chart['chart']['palaces'] if p.get('isShenGong'))
    assert body['stars'] == palace['stars']
    report = layout_report('## 五、大运走势\n阶段一\n## 五、大运走势\n阶段二', complete_chart)
    assert report.count('## 五、大运走势') == 1
    assert '1990-05-17' in report and '土五局' in report and '阶段一' in report and '阶段二' in report
