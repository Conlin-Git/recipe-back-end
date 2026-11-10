import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.fortune.chat import extract_facts, fortune_agent
from app.fortune.service import analyze, FortuneUnavailable, render_report, render_cycle_report
from app.graph import nodes
from app.schemas.fortune import FortuneInput
from app.fortune.report import build_engine_report

BASE = dict(calendar_type="solar", birth_date="1990-05-17", birth_time="14:30", gender="female")


@pytest.fixture(autouse=True)
def isolate_ai():
    with patch("app.fortune.chat.interpret", AsyncMock(return_value={"status": "unavailable", "message": "AI暂不可用"})):
        yield


def run(data=None, **changes):
    return asyncio.run(analyze(FortuneInput(**{**(data or BASE), **changes})))


def test_known_chart_has_twelve_palaces_four_mutagens_and_expected_basics():
    result = run()
    assert len(result["palaces"]) == len({p["name"] for p in result["palaces"]}) == 12
    assert sum(p["is_ming"] for p in result["palaces"]) == 1
    assert sum(p["is_shen"] for p in result["palaces"]) == 1
    assert result["summary"]["ming_branch"] == "戌"
    assert result["summary"]["wuxing"] == "土五局"
    assert {s["type"] for s in result["sihua"]} == {"禄", "权", "科", "忌"}
    assert "仅供娱乐" in render_report(result)


def test_lunar_and_solar_dates_produce_same_chart():
    solar = run()
    lunar = run(calendar_type="lunar", birth_date="1990-04-23")
    assert solar["palaces"] == lunar["palaces"]
    assert solar["summary"] == lunar["summary"]


@pytest.mark.parametrize("change", [dict(birth_date="1990-02-30"), dict(birth_time="24:00"), dict(birth_time="12:60"), dict(gender="unknown"), dict(longitude=181), dict(utc_offset_minutes=481), dict(lunar_leap_month=True), dict(calendar_type="lunar", birth_date="1990-01-31")])
def test_invalid_birth_input_is_rejected(change):
    with pytest.raises(ValidationError):
        FortuneInput(**{**BASE, **change})


def test_nonexistent_lunar_leap_month_and_future_date_rejected():
    with pytest.raises(ValueError, match="闰月不存在"):
        run(calendar_type="lunar", birth_date="1990-01-10", lunar_leap_month=True)
    with pytest.raises(ValueError, match="晚于今天"):
        run(birth_date="2050-01-01")


def test_valid_leap_month_and_late_zi_hour_rollover():
    leap = run(calendar_type="lunar", birth_date="2023-02-10", lunar_leap_month=True)
    assert len(leap["palaces"]) == 12
    assert leap["summary"]["lunar_date"] == "2023年闰2月10日"
    late = run(birth_time="23:30")
    next_day = run(birth_date="1990-05-18", birth_time="00:30")
    assert late["palaces"] == next_day["palaces"]
    assert late["timing"]["chart_date"] == "1990-05-18"
    assert run(birth_time="23:30", late_zishi="current-day")["timing"]["chart_date"] == "1990-05-17"


def test_solar_time_correction_and_missing_node():
    assert run(longitude=116.4074)["timing"]["corrected"] is True
    with patch("app.fortune.service.shutil.which", return_value=None):
        with pytest.raises(FortuneUnavailable):
            run()


def turn(question, previous=None):
    async def go():
        state = {"question": question, "topic_state": (previous or {}).get("topic_state", {}), "window": []}
        forbidden = SimpleNamespace(ainvoke=AsyncMock(side_effect=AssertionError("No external LLM for local fortune turns")))
        with patch.object(nodes, "router_llm", forbidden), patch.object(nodes, "sentiment_llm", forbidden):
            decision = await nodes.orchestrator_node(state)
            await nodes.sentiment_node(state)
            assert decision["route"]["domain"] == "fortune"
            assert not forbidden.ainvoke.called
        state.update(decision)
        return await fortune_agent(state)
    return asyncio.run(go())


def test_chat_multi_turn_asks_for_missing_time_and_matches_api_report():
    first = turn("帮我算命，女，公历1990年5月17日")
    assert "出生时间" in first["local_answer"]
    assert "birth_time" not in first["topic_state"]["fortune_facts"]
    second = turn("下午2点30分", first)
    assert second["local_answer"].endswith(render_report(run()))
    assert len(second["local_answer"]) < 1000
    assert "| ---" not in second["local_answer"]
    follow = turn("我的事业宫呢", second)
    assert "### 官禄" in follow["local_answer"]
    assert "### 命宫" not in follow["local_answer"]


def test_chat_date_correction_new_person_unknown_time_and_glossary():
    first = turn("紫微排盘，女，公历1990-05-17 14:30")
    changed = turn("日期改成1990-05-18", first)
    assert changed["topic_state"]["fortune_facts"]["birth_time"] == "14:30"
    another = turn("帮朋友算命，男，公历1995-02-02", first)
    assert "birth_time" not in another["topic_state"]["fortune_facts"]
    unknown = turn("出生时间不确定", first)
    assert "出生时间" in unknown["local_answer"]
    assert "birth_time" not in unknown["topic_state"]["fortune_facts"]
    calendar_change = turn("改为农历", first)
    assert "birth_date" not in calendar_change["topic_state"]["fortune_facts"]
    assert "出生日期" in calendar_change["local_answer"]
    corrected = turn("生辰改为公历1990-05-18", first)
    assert corrected["topic_state"]["fortune_facts"]["birth_date"] == "1990-05-18"
    glossary = turn("四化是什么意思")
    assert "化禄、化权、化科、化忌" in glossary["local_answer"]


def test_fortune_topic_does_not_capture_recipe_or_naming_questions():
    state = {"topic_state": {"domain": "fortune", "fortune_facts": BASE}, "question": "油焖大虾怎么做"}
    assert nodes._local_fortune_turn(state) is False
    assert nodes._heuristic_route(state)["domain"] == "recipe"
    state["question"] = "帮我取名"
    assert nodes._local_fortune_turn(state) is False
    assert nodes._heuristic_route(state)["domain"] == "naming"


def test_extract_time_does_not_confuse_birth_date_or_utc_offset():
    assert extract_facts("公历1990-05-17") == {"calendar_type": "solar", "lunar_leap_month": False, "birth_date": "1990-05-17"}
    assert extract_facts("我是男，公历1990-05-17 00:00")["birth_time"] == "00:00"
    assert "birth_time" not in extract_facts("UTC+08:00")
    assert extract_facts("UTC-05:00，下午2点半")["birth_time"] == "14:30"


def test_complete_chart_retains_stars_periods_and_every_age():
    result = run()
    chart, detail = result["chart"], result["details"]
    assert len(chart["palaces"]) == len(detail["palace_details"]) == 12
    assert len(detail["periods"]) == 12
    assert len(detail["years"]) >= 80
    assert [y["age"] for y in detail["years"]] == list(range(1, len(detail["years"]) + 1))
    assert len(chart["lunarInfo"]["fourPillars"]) == 4
    for year in detail["years"]:
        assert year["year"] == chart["lunarInfo"]["lunarYear"] + year["age"] - 1
        assert {s["type"] for s in year["sihua"]} == {"禄", "权", "科", "忌"}
        period = next((p for p in detail["periods"] if p["start_age"] <= year["age"] <= p["end_age"]), None)
        assert year["period_index"] == (period["index"] if period else -1)
        assert all(m["palace"] for m in year["sihua"])
    first = detail["periods"][0]
    assert detail["years"][first["start_age"] - 2]["period_index"] == -1
    assert detail["years"][first["start_age"] - 1]["period_index"] == 0
    assert detail["years"][first["end_age"]]["period_index"] == 1


def test_pre_lunar_new_year_age_and_childhood_are_not_shifted():
    result = run(birth_date="1990-01-10")
    assert result["chart"]["lunarInfo"]["lunarYear"] == 1989
    assert result["details"]["years"][0]["year"] == 1989
    assert result["details"]["years"][79]["year"] == 2068
    assert "80 虚岁" in render_cycle_report(result, age=80)
    assert "1–80 虚岁" in render_cycle_report(result, age=1, end_age=80)
    assert "请选择范围内" in render_cycle_report(result, age=0)


def test_chat_can_follow_up_on_year_and_age_without_external_llm():
    first = turn("紫微排盘，女，公历1990-05-17 14:30")
    eighty = turn("看我80岁的流年", first)
    assert "80 虚岁" in eighty["local_answer"]
    assert "2069" in eighty["local_answer"]
    yearly = turn("看看1到80岁逐年排盘", first)
    assert "### 1990年 · 1 虚岁" in yearly["local_answer"]
    assert "### 2069年 · 80 虚岁" in yearly["local_answer"]
    assert "|" not in yearly["local_answer"]
    assert yearly["topic_state"]["fortune_facts"]["birth_date"] == "1990-05-17"


def test_full_text_report_serializes_every_palace_star_period_and_year():
    result = run()
    report = result["report"]["markdown"]
    assert report == build_engine_report(result)
    assert "排盘口径与来源" not in result["report"]["html"]
    assert result["disclaimer"] not in result["report"]["html"]
    assert "传统星曜意象中" not in report
    for palace in result["chart"]["palaces"]:
        assert f"### {palace['name']}" in report
        for star in palace["stars"]:
            assert star["name"] in report
    for year in result["details"]["years"]:
        assert f"| {year['age']} | {year['year']} {year['ganzhi']} |" in report
    for pattern in result["details"]["patterns"]:
        assert pattern["description"] in report
    assert '<div class="report-table"><table>' in result["report"]["html"]
    assert '<script' not in result["report"]["html"]
