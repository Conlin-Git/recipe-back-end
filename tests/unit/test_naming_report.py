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
