"""高考志愿规划 Agent。

这个 Agent 是证据锁定的领域编排器：它不生成院校、分数或位次，
只把结构化服务返回的有来源的记录整理成可解释的志愿分层。
模型解释层可以消费 ``agent_plan``，但不能改变其中的候选记录。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.gaokao import GaokaoGuideInput
from app.services.gaokao_service import guide as retrieve_guide


PLANNER_AGENT_NAME = "gaokao_planner_agent"
PLANNER_AGENT_VERSION = "1.0.0"


def _step(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **extra}


def _agent_plan(result: dict[str, Any], data: GaokaoGuideInput) -> dict[str, Any]:
    summary = result.get("summary") or {}
    tiers = summary.get("tier_counts") or {}
    matched = int(summary.get("matched_count") or 0)
    qualified = sum(int(tiers.get(tier) or 0) for tier in ("冲", "稳", "保"))
    rows = result.get("recommendations") or []
    schools = result.get("school_recommendations") or []
    has_rank = any(item.get("comparison_mode") == "rank" for item in rows)
    data_status = "ready" if qualified else "insufficient"

    steps = [
        _step(
            "profile_validation",
            "passed",
            "已锁定考生省份、参考招生省份、年份、科类和批次；候选人位次优先于分数。",
            candidate_province=data.candidate_province,
            admission_province=data.admission_province,
            candidate_year=data.candidate_year,
            reference_year=data.reference_year,
            track=data.track,
            batch=data.batch,
        ),
        _step(
            "official_evidence_retrieval",
            "passed" if matched else "empty",
            "读取已核验的官方投档/录取记录，并保留每条记录的可复核出处。",
            verified_record_count=int((result.get("coverage") or {}).get("verified_record_count") or 0),
            matched_school_count=matched,
            available_years=summary.get("available_years") or [],
        ),
        _step(
            "rank_comparability_gate",
            "passed" if has_rank else "blocked",
            (
                "存在同生源省、同科类的最低位次，允许进行位次分层。"
                if has_rank
                else "没有可复核的同口径最低位次，只展示来源所列最低分，不进行冲稳保。"
            ),
            comparison_mode="rank" if has_rank else "score_only",
        ),
        _step(
            "tier_organization",
            "passed" if qualified else "insufficient",
            "按位次差整理冲、稳、保；数据不足记录保留在明细中但不作为志愿建议。",
            tier_counts={tier: int(tiers.get(tier) or 0) for tier in ("冲", "稳", "保", "数据不足")},
            qualified_school_count=qualified,
            school_count=len(schools),
        ),
    ]
    return {
        "agent_name": PLANNER_AGENT_NAME,
        "agent_version": PLANNER_AGENT_VERSION,
        "status": data_status,
        "mode": "evidence_locked_deterministic",
        "llm_used": False,
        "evidence_locked": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "steps": steps,
        "selection_policy": "采用已核验的官方投档/录取数据；先按同口径位次比较，再按冲/稳/保整理；缺位次的记录不进入档位推荐。",
        "selected_tiers": {tier: int(tiers.get(tier) or 0) for tier in ("冲", "稳", "保")},
        "warnings": list(result.get("warnings") or []),
    }


class GaokaoPlannerAgent:
    """高考志愿规划 Agent 的唯一入口。"""

    name = PLANNER_AGENT_NAME
    version = PLANNER_AGENT_VERSION

    async def run(self, db: AsyncSession, data: GaokaoGuideInput) -> dict[str, Any]:
        result = await retrieve_guide(db, data)
        result["agent_plan"] = _agent_plan(result, data)
        return result


gaokao_planner_agent = GaokaoPlannerAgent()


async def plan_gaokao(db: AsyncSession, data: GaokaoGuideInput) -> dict[str, Any]:
    """兼容函数式调用，供 API 和对话 Agent 共用。"""
    return await gaokao_planner_agent.run(db, data)
