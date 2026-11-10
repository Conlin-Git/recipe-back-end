import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.naming_service import to_output


@pytest.mark.parametrize('mode', ['generate', 'score'])
def test_saved_naming_reports_are_rendered_without_modifying_storage(mode):
    stored = json.dumps({'mode': mode, 'report_markdown': '# 命名报告\n\n## 姓名评分\n\n- **林清**：85 / 100\n\n[参考来源](https://example.com/reference)'}, ensure_ascii=False)
    record = SimpleNamespace(id=1, input_fingerprint='fixture', purpose='person_name',
                             ruleset_version='fixture', knowledge_snapshot_id='fixture',
                             variant_index=0, created_at=datetime(2026, 1, 1), result_json=stored)
    output = to_output(record)
    assert '<h1>命名报告</h1>' in output['result']['report_html']
    assert '<h2>姓名评分</h2>' in output['result']['report_html']
    assert '<strong>林清</strong>' in output['result']['report_html']
    assert 'href="https://example.com/reference"' in output['result']['report_html']
    assert record.result_json == stored


def test_report_html_is_regenerated_and_sanitized():
    record = SimpleNamespace(id=1, input_fingerprint='fixture', purpose='person_name',
                             ruleset_version='fixture', knowledge_snapshot_id='fixture',
                             variant_index=0, created_at=datetime(2026, 1, 1),
                             result_json=json.dumps({'report_markdown': '## 安全报告\n\n<script>alert(1)</script>\n\n[链接](javascript:alert(1))', 'report_html': '<img src=x onerror=alert(1)>'}))
    html = to_output(record)['result']['report_html']
    assert '<h2>安全报告</h2>' in html
    assert '<script' not in html and 'onerror' not in html and 'javascript:' not in html


def test_same_input_caches_generation_and_scoring_separately():
    import asyncio
    from unittest.mock import AsyncMock
    from app.services import naming_service

    async def check():
        records = []
        async def execute(statement):
            params = statement.compile().params
            match = next((record for record in records if record.input_fingerprint == params['input_fingerprint_1']), None)
            return SimpleNamespace(scalar_one_or_none=lambda: match)
        def add(record):
            record.id = len(records) + 1
            record.created_at = datetime(2026, 1, 1)
            records.append(record)
        db = SimpleNamespace(execute=execute, add=add, commit=AsyncMock(), refresh=AsyncMock())
        payload = {'birth_date': '1990-01-01', 'birth_time': '08:00', 'surname': '陈', 'name': '陈清'}
        generated = await naming_service.run(db, 7, payload, 'analyze')
        scored = await naming_service.run(db, 7, payload, 'score')
        assert generated.id != scored.id
        assert generated.input_fingerprint != scored.input_fingerprint
        for record, mode in [(generated, 'generate'), (scored, 'score')]:
            result = to_output(record)['result']
            assert result['mode'] == mode
            assert result['input_fingerprint'] == record.input_fingerprint
            assert '<h1>' in result['report_html']
        assert 'candidates' not in to_output(scored)['result']
        assert await naming_service.run(db, 7, payload, 'analyze') is generated
        assert await naming_service.run(db, 7, payload, 'score') is scored
        assert db.commit.await_count == 2
    asyncio.run(check())
