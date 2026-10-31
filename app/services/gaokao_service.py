"""高考指南：结构化数据优先，模型只负责解释和问答编排。"""
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gaokao import GaokaoAdmission, GaokaoScoreBand, GaokaoSource
from app.schemas.gaokao import DISCLAIMER, GaokaoGuideInput


async def resolve_rank(db: AsyncSession, data: GaokaoGuideInput) -> int | None:
    if data.rank:
        return data.rank
    if data.score is None:
        return None
    row = (
        await db.execute(
            select(GaokaoScoreBand.cumulative_rank)
            .where(
                GaokaoScoreBand.province == data.candidate_province,
                GaokaoScoreBand.year == data.candidate_year,
                GaokaoScoreBand.track == data.track,
                GaokaoScoreBand.score == data.score,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


def _tier(rank: int, historical: list[int]) -> tuple[str, int]:
    """返回档位和相对位次差：正数表示历史最低位次更靠后，风险更低。"""
    median = sorted(historical)[len(historical) // 2]
    gap = median - rank
    ratio = gap / max(rank, 1)
    if ratio >= 0.12:
        return "保", gap
    if ratio >= -0.08:
        return "稳", gap
    return "冲", gap


async def guide(db: AsyncSession, data: GaokaoGuideInput) -> dict:
    rank = await resolve_rank(db, data)
    warnings: list[str] = []
    if rank is None:
        warnings.append("缺少同省同科类的一分一段表，无法把分数可靠换算为位次。请直接填写省内位次。")
        return {
            "disclaimer": DISCLAIMER,
            "data_status": "insufficient",
            "profile": data.model_dump(),
            "recommendations": [],
            "sources": [],
            "warnings": warnings,
        }

    source_ids = set()
    if data.score is not None:
        band_source_id = (
            await db.execute(
                select(GaokaoScoreBand.source_id)
                .where(
                    GaokaoScoreBand.province == data.candidate_province,
                    GaokaoScoreBand.year == data.candidate_year,
                    GaokaoScoreBand.track == data.track,
                    GaokaoScoreBand.score == data.score,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if band_source_id:
            source_ids.add(band_source_id)

    statement = select(GaokaoAdmission).where(
        GaokaoAdmission.province == data.admission_province,
        GaokaoAdmission.track == data.track,
        GaokaoAdmission.batch == data.batch,
        GaokaoAdmission.year == data.reference_year,
        GaokaoAdmission.verification_status == "verified",
    )
    if data.school_provinces:
        statement = statement.where(GaokaoAdmission.school_province.in_(data.school_provinces))
    if data.preferred_regions:
        # 当前表保存 province_code，地域筛选使用明确代码，不让模型猜测。
        statement = statement.where(GaokaoAdmission.province_code.in_(data.preferred_regions))
    if data.keywords:
        like = f"%{data.keywords}%"
        statement = statement.where(
            GaokaoAdmission.university_name.like(like)
            | GaokaoAdmission.group_name.like(like)
            | GaokaoAdmission.major_name.like(like)
        )
    rows = (await db.execute(statement.order_by(GaokaoAdmission.min_rank))).scalars().all()
    if rows and not any(row.min_rank is not None for row in rows):
        warnings.append(
            "已命中官方核验的院校专业组最低分数据，但来源没有发布最低位次；为避免把分数跨省或跨口径臆算为位次，当前不生成按位次的学校推荐。"
        )
    grouped: dict[tuple[str, str, str, str, str], list[GaokaoAdmission]] = defaultdict(list)
    for row in rows:
        grouped[(row.record_kind, row.admission_round, row.university_code, row.group_code, row.major_code)].append(row)

    recommendations = []
    for (record_kind, admission_round, university_code, group_code, major_code), items in grouped.items():
        # 当前阶段使用用户选择的对比年度；后续可扩展为多年度历史聚合。
        by_year = {}
        for item in items:
            if item.year == data.reference_year:
                by_year[item.year] = item
        if not by_year:
            continue
        history = sorted(i.min_rank for i in by_year.values() if i.min_rank)
        if not history:
            continue
        tier, gap = _tier(rank, history)
        rank_status = "已超过" if gap >= 0 else "未达到"
        evidence = [
            {"year": i.year, "min_rank": i.min_rank, "min_score": i.min_score, "source_id": i.source_id}
            for i in sorted(by_year.values(), key=lambda x: x.year, reverse=True)
        ]
        source_ids.update(i.source_id for i in by_year.values())
        confidence = "高" if len(history) >= 4 else "中" if len(history) >= 2 else "低"
        newest = max(by_year)
        item = by_year[newest]
        recommendations.append({
            "tier": tier,
            "rank_status": rank_status,
            "record_kind": item.record_kind,
            "admission_round": item.admission_round,
            "university_name": item.university_name,
            "school_province": item.school_province,
            "group_name": item.group_name or "",
            "major_name": item.major_name or "",
            "subject_requirements": item.subject_requirements,
            "min_score": item.min_score,
            "min_rank": item.min_rank,
            "avg_score": item.avg_score,
            "max_score": item.max_score,
            "historical_years": sorted(by_year),
            "historical_min_ranks": history,
            "rank_gap": gap,
            "plan_count": item.plan_count,
            "admission_count": item.admission_count,
            "submission_count": item.submission_count,
            "admission_status": item.admission_status,
            "confidence": confidence,
            "evidence": evidence,
            "reason": (
                f"{data.reference_year} 年同口径最低位次为 {item.min_rank}，当前位次比投档线靠前 {gap} 位。"
                if gap >= 0
                else f"{data.reference_year} 年同口径最低位次为 {item.min_rank}，当前位次比投档线落后 {-gap} 位。"
            ),
        })

    tier_order = {"稳": 0, "保": 1, "冲": 2}
    recommendations.sort(key=lambda x: (tier_order[x["tier"]], abs(x["rank_gap"] or 0)))
    recommendations = recommendations[: data.limit]
    recommendations_by_school_province: dict[str, list] = defaultdict(list)
    for recommendation in recommendations:
        recommendations_by_school_province.setdefault(recommendation.get("school_province") or "未标注", []).append(recommendation)
    sections = dict(recommendations_by_school_province)
    sources = []
    if source_ids:
        sources = [
            {
                "id": source.id,
                "publisher": source.publisher,
                "year": source.year,
                "dataset_type": source.dataset_type,
                "url": source.source_url,
                "sha256": source.sha256,
                "validation_status": source.validation_status,
            }
            for source in (
                await db.execute(select(GaokaoSource).where(GaokaoSource.id.in_(source_ids)))
            ).scalars().all()
        ]
    if not recommendations and not warnings:
        if data.school_provinces:
            provinces = "、".join(data.school_provinces)
            warnings.append(
                f"当前数据库没有满足招生省份、年份、科类、批次及院校所在地（{provinces}）口径的已核验录取数据；"
                "院校所在地未标注或来源未完成核验的记录不会用于推荐。"
            )
        else:
            warnings.append("当前数据库没有满足省份、年份、科类、批次和位次口径的已核验录取数据，暂不生成学校推荐。")
    return {
        "disclaimer": DISCLAIMER,
        "data_status": "ok" if recommendations else "insufficient",
        "profile": {**data.model_dump(), "resolved_rank": rank},
        "recommendations": recommendations,
        "sections": sections,
        "sources": sources,
        "warnings": warnings,
    }
