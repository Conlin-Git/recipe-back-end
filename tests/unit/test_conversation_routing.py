import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.graph import nodes
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


def naming_context(question):
    return {
        "question": question,
        "topic_state": {"domain": "naming", "topic_id": "naming:取名", "goal": "给孩子取名"},
        "window": [],
        "recipe_cache": [],
    }


def route_with_model(state, **decision):
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(decision))))
    with patch.object(nodes, "router_llm", model):
        return asyncio.run(nodes.orchestrator_node(state))


def test_shrimp_question_switches_from_naming_to_recipe():
    route = _heuristic_route(naming_context("油焖大虾怎么做"))
    assert route["domain"] == "recipe"
    assert route["intent_type"] == "new"
    assert route["topic_action"] == "switch"
    assert route["needs_rag"] is True


def test_old_naming_route_cannot_disable_recipe_search():
    result = route_with_model(
        naming_context("油焖大虾怎么做啊"), domain="naming", intent_type="followup",
        topic_action="continue", needs_rag=False, retrieval_query="给孩子取名",
        resolved_references={"这个": "naming:取名"},
    )
    route = result["route"]
    assert route["domain"] == "recipe"
    assert route["needs_rag"] is True
    assert route["intent_type"] == "new"
    assert route["retrieval_query"] == "油焖大虾怎么做啊"
    assert route["resolved_references"] == {}
    assert result["topic_state"]["goal"] == "油焖大虾怎么做啊"
    assert nodes.pick_agent(result) == "recipe_agent"


def test_inherited_domain_does_not_override_semantic_model_route():
    # 菜名不在关键词表里时，也不能用旧命理领域覆盖模型识别的菜谱领域。
    result = route_with_model(naming_context("佛跳墙怎么做"), domain="recipe",
                              intent_type="followup", topic_action="continue", needs_rag=False)
    assert result["route"]["domain"] == "recipe"
    assert result["route"]["intent_type"] == "new"
    assert result["route"]["needs_rag"] is True


def test_real_naming_followup_preserves_topic():
    result = route_with_model(naming_context("上面那个再给我三个"), domain="naming",
                              intent_type="followup", topic_action="continue", needs_rag=False)
    assert result["route"]["domain"] == "naming"
    assert result["route"]["intent_type"] == "followup"
    assert result["topic_state"]["topic_id"] == "naming:取名"


def test_recipe_followup_without_cached_evidence_still_searches():
    state = naming_context("这个怎么做")
    state["topic_state"] = {"domain": "recipe", "topic_id": "recipe:油焖大虾", "goal": "油焖大虾"}
    result = route_with_model(state, domain="recipe", intent_type="followup", topic_action="continue",
                              needs_rag=False, retrieval_query="油焖大虾做法")
    assert result["route"]["needs_rag"] is True
    assert result["route"]["retrieval_query"] == "油焖大虾做法"


def test_cached_recipe_followup_does_not_force_another_search():
    state = naming_context("这个要焖多久")
    state["topic_state"] = {"domain": "recipe", "topic_id": "recipe:油焖大虾", "goal": "油焖大虾"}
    state["recipe_cache"] = [{"title": "油焖大虾"}]
    result = route_with_model(state, domain="recipe", intent_type="followup", topic_action="continue", needs_rag=False)
    assert result["route"]["needs_rag"] is False


def test_router_failure_uses_current_recipe_evidence():
    with patch.object(nodes, "router_llm", SimpleNamespace(ainvoke=AsyncMock(side_effect=ValueError("test failure")))):
        result = asyncio.run(nodes.orchestrator_node(naming_context("油焖大虾怎么做")))
    assert result["route"]["domain"] == "recipe"
    assert result["route"]["needs_rag"] is True
