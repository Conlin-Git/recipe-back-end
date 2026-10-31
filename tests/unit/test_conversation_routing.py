from app.graph.nodes import _heuristic_route, _parse_json


def test_parse_json_accepts_fenced_or_surrounded_output():
    assert _parse_json('说明\n```json\n{"domain":"dev","confidence":0.9}\n```\n') == {
        "domain": "dev",
        "confidence": 0.9,
    }
    assert _parse_json('prefix {"domain":"chat"} trailing') == {"domain": "chat"}


def test_followup_keeps_active_development_domain():
    route = _heuristic_route(
        {
            "question": "那依赖和配置怎么弄？",
            "topic_state": {
                "domain": "dev",
                "topic_id": "docker-local-env",
                "goal": "让工程在本地运行",
            },
            "window": [],
        }
    )
    assert route["intent_type"] == "followup"
    assert route["domain"] == "dev"
    assert "recent_turns" in route["context_scope"]


def test_route_failure_does_not_default_to_recipe():
    route = _heuristic_route({"question": "天气怎么样？", "window": []})
    assert route["domain"] == "chat"
    assert route["needs_rag"] is False


def test_json_parser_never_uses_greedy_tail_as_route():
    assert _parse_json('{"domain":"recipe"}\n{"domain":"dev"}') == {
        "domain": "recipe"
    }
