import pytest

from app.agents.gaokao_planner import PLANNER_AGENT_NAME, PLANNER_AGENT_VERSION, plan_gaokao
from app.schemas.gaokao import GaokaoGuideInput


class _EmptyDbResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _EmptyDb:
    async def execute(self, _statement):
        return _EmptyDbResult()


@pytest.mark.asyncio
async def test_planner_agent_adds_auditable_evidence_locked_plan():
    result = await plan_gaokao(
        _EmptyDb(),
        GaokaoGuideInput(
            candidate_province="四川",
            admission_province="四川",
            school_provinces=["四川", "广东", "上海"],
            candidate_year=2027,
            reference_year=2026,
            track="物理类",
            rank=38000,
            batch="本科",
        ),
    )
    plan = result["agent_plan"]
    assert plan["agent_name"] == PLANNER_AGENT_NAME
    assert plan["agent_version"] == PLANNER_AGENT_VERSION
    assert plan["evidence_locked"] is True
    assert plan["llm_used"] is False
    assert plan["status"] == "insufficient"
    assert [step["name"] for step in plan["steps"]] == [
        "profile_validation",
        "official_evidence_retrieval",
        "rank_comparability_gate",
        "tier_organization",
    ]
