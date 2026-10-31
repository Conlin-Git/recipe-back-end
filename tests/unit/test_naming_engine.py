from datetime import date, time

from app.naming.engine import NamingEngine, input_fingerprint


def payload():
    return {
        "birth_date": date(1990, 1, 1),
        "birth_time": time(8, 0),
        "calendar_type": "solar",
        "purpose": "person_name",
        "surname": "林",
        "given_length": 2,
        "variant_index": 0,
    }


def test_same_user_input_is_deterministic():
    engine = NamingEngine()
    first = engine.analyze(7, payload())
    second = engine.analyze(7, payload())
    assert first["input_fingerprint"] == second["input_fingerprint"]
    assert [x["name"] for x in first["candidates"]] == [x["name"] for x in second["candidates"]]
    assert "参考文献" in first["report_markdown"]
    assert first["bazi"]["pillars"] == ["己巳", "丙子", "丙寅", "壬辰"]


def test_user_salt_changes_candidate_order_but_not_bazi():
    engine = NamingEngine()
    one = engine.analyze(7, payload())
    two = engine.analyze(8, payload())
    assert one["bazi"]["pillars"] == two["bazi"]["pillars"]
    assert one["input_fingerprint"] != two["input_fingerprint"]
    assert [x["name"] for x in one["candidates"]] != [x["name"] for x in two["candidates"]]


def test_score_contains_stroke_and_risk_evidence():
    data = {**payload(), "name": "林病", "purpose": "pen_name"}
    result = NamingEngine().score(7, data)
    assert result["score_breakdown"]["risk_findings"]
    assert any(item["rule_id"] == "KANGXI-001" for item in result["evidence"])
    assert "康熙笔画" in result["report_markdown"] or "笔画" in result["report_markdown"]
