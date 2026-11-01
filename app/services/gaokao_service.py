"""高考指南：结构化数据优先，模型只负责解释和问答编排。"""
from collections import defaultdict
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gaokao import GaokaoAdmission, GaokaoScoreBand, GaokaoSource
from app.schemas.gaokao import DISCLAIMER, GaokaoCatalogInput, GaokaoGuideInput


SUPPORTED_SCHOOL_PROVINCES = ("四川", "广东", "上海")


def guide_rule_note(candidate_province: str, reference_year: int) -> str:
    return (
        f"生成规则：使用考生在{candidate_province}省的省内排位，比较学校在 {reference_year} 年"
        f"{candidate_province}招生口径下的最低投档/录取门槛；不会使用学校所在地的本地录取线替代{candidate_province}招生数据。"
    )


def batch_aliases(batch: str | None) -> set[str]:
    """兼容四川官方本科普通批的阶段名称，同时保留数据库原始批次。"""
    if not batch:
        return set()
    if batch in {"本科", "本科批", "本科普通批"}:
        return {"本科", "本科批", "本科普通批", "本科批次B段", "本科批B段", "普通本科批次B段", "普通本科批"}
    return {batch}


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
    """返回档位和相对位次差，位次数值越小越有利。"""
    benchmark = int(median(sorted(historical)))
    gap = benchmark - rank
    # 当前位次落后历史线就明确标为“冲”，避免“未达到但显示稳”。
    if gap < 0:
        return "冲", gap
    if gap / max(rank, 1) >= 0.12:
        return "保", gap
    return "稳", gap


def _rank_status(gap: int | None) -> str:
    if gap is None:
        return "暂无位次"
    return "已超过" if gap >= 0 else "未达到"


def _rank_reason(gap: int | None, benchmark: int | None, reference_year: int) -> str:
    if gap is None or benchmark is None:
        return "官方来源未提供可比最低位次，暂不进行冲稳保分档。"
    if gap > 0:
        return f"{reference_year} 年参考最低位次约为 {benchmark}，当前位次领先 {gap} 位。"
    if gap < 0:
        return f"{reference_year} 年参考最低位次约为 {benchmark}，当前位次落后 {-gap} 位，属于冲刺项。"
    return f"{reference_year} 年参考最低位次约为 {benchmark}，当前位次与历史线接近。"


def _data_quality(item: GaokaoAdmission) -> str:
    if item.min_rank is None:
        return "仅最低分" if item.min_score is not None else "无可用数据"
    if item.verification_note and "换算" in item.verification_note:
        return "换算位次"
    return "官方位次"


def _sort_recommendations(items: list[dict], sort_by: str) -> None:
    if sort_by == "min_rank_asc":
        items.sort(key=lambda x: (x["min_rank"] is None, x["min_rank"] or 10**9, -(x["min_score"] or -1)))
    elif sort_by == "rank_gap_desc":
        items.sort(key=lambda x: (x["rank_gap"] is None, -(x["rank_gap"] or -10**9), -(x["min_score"] or -1)))
    else:
        items.sort(key=lambda x: (x["min_score"] is None, -(x["min_score"] or -1), x["min_rank"] or 10**9))


def _base_admission_query(data: GaokaoGuideInput):
    return select(GaokaoAdmission).where(
        GaokaoAdmission.province == data.admission_province,
        GaokaoAdmission.track == data.track,
        GaokaoAdmission.batch.in_(batch_aliases(data.batch)),
        GaokaoAdmission.year.between(
            max(2022, data.reference_year - data.history_years + 1),
            data.reference_year,
        ),
        GaokaoAdmission.verification_status == "verified",
    )


def _coverage_payload(data: GaokaoGuideInput, rows: list[GaokaoAdmission], history_years: list[int]) -> dict:
    selected = data.school_provinces or list(SUPPORTED_SCHOOL_PROVINCES)
    is_known_partial = data.admission_province == "四川" and data.reference_year == 2026
    return {
        "selected_school_provinces": selected,
        "supported_school_provinces": list(SUPPORTED_SCHOOL_PROVINCES),
        "verified_record_count": len(rows),
        "university_count": len({(row.university_code, row.school_province) for row in rows}),
        "history_years": history_years,
        "is_full_scope": False,
        "scope_note": (
            "当前四川 2026 本科全量院校专业组投档明细尚未公开归档，结果只覆盖已核验官方来源。"
            if is_known_partial
            else "当前目录和推荐仅覆盖已核验官方来源，不代表对应省份全部招生院校。"
        ),
    }


def _build_school_recommendations(items: list[dict], candidate_rank: int | None, reference_year: int) -> list[dict]:
    """把专业/专业组记录聚合成学校行，专业明细只在点击学校后查看。"""
    grouped: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
    for item in items:
        grouped[(item["university_name"], item.get("school_province"))].append(item)

    schools: list[dict] = []
    for (university_name, school_province), majors in grouped.items():
        rank_values = [item["min_rank"] for item in majors if item.get("min_rank") is not None]
        score_values = [item["min_score"] for item in majors if item.get("min_score") is not None]
        # “学校最低投档/录取线”取该校可核验记录中的最低分和对应最靠后的位次，
        # 用来回答考生“这所学校有没有可报入口”，专业风险在展开明细中单独查看。
        school_rank = max(rank_values) if rank_values else None
        school_score = min(score_values) if score_values else None
        school_gap = school_rank - candidate_rank if school_rank is not None and candidate_rank is not None else None
        school_tier = _tier(candidate_rank, [school_rank])[0] if school_gap is not None else "数据不足"
        majors.sort(key=lambda item: (item.get("min_score") is None, -(item.get("min_score") or -1), item.get("min_rank") or 10**9))
        schools.append(
            {
                "tier": school_tier,
                "rank_status": _rank_status(school_gap),
                "university_name": university_name,
                "school_province": school_province,
                "min_score": school_score,
                "min_rank": school_rank,
                "rank_gap": school_gap,
                "comparison_rank": school_rank,
                "major_count": len(majors),
                "data_quality": "官方位次" if any(item.get("data_quality") == "官方位次" for item in majors) else ("换算位次" if any(item.get("data_quality") == "换算位次" for item in majors) else "仅最低分"),
                "historical_years": sorted({year for item in majors for year in item.get("historical_years", [])}, reverse=True),
                "confidence": "高" if len(majors) >= 4 else "中" if len(majors) >= 2 else "低",
                "reason": _rank_reason(school_gap, school_rank, reference_year),
                "majors": majors,
            }
        )
    _sort_recommendations(schools, "min_score_desc")
    return schools


async def guide(db: AsyncSession, data: GaokaoGuideInput) -> dict:
    rank = await resolve_rank(db, data)
    warnings: list[str] = []
    if data.reference_year == 2026 and data.admission_province == "四川":
        warnings.append(
            "2026 年四川本科批次 B 段全量院校专业组投档明细尚未由四川省教育考试院公开归档；"
            "以下结果仅覆盖数据库中已核验的官方院校来源，不代表全部招生院校。"
        )
    if data.rank is None and data.score is not None and rank is None:
        warnings.append("当前分数没有匹配到同省同科类的一分一段表；结果仍可查看最低分，但不会按位次分档。")
    if data.rank is None and data.score is None:
        warnings.append("请填写省内位次或高考分数；没有成绩无法生成可比较的结果。")
        return {
            "disclaimer": DISCLAIMER,
            "data_status": "insufficient",
            "profile": data.model_dump(),
            "recommendations": [],
            "sections": {},
            "sources": [],
            "warnings": warnings,
            "summary": {"matched_count": 0, "returned_count": 0, "tier_counts": {"冲": 0, "稳": 0, "保": 0, "数据不足": 0}},
            "coverage": _coverage_payload(data, [], []),
            "school_recommendations": [],
            "rule_note": guide_rule_note(data.candidate_province or data.admission_province or "当前", data.reference_year),
        }

    statement = _base_admission_query(data)
    if data.school_provinces:
        statement = statement.where(GaokaoAdmission.school_province.in_(data.school_provinces))
    if data.preferred_regions:
        statement = statement.where(GaokaoAdmission.province_code.in_(data.preferred_regions))
    if data.keywords:
        like = f"%{data.keywords}%"
        statement = statement.where(
            GaokaoAdmission.university_name.like(like)
            | GaokaoAdmission.group_name.like(like)
            | GaokaoAdmission.major_name.like(like)
        )
    rows = (await db.execute(statement)).scalars().all()
    available_years = sorted({row.year for row in rows}, reverse=True)
    if rows and not any(row.min_rank is not None for row in rows):
        warnings.append(
            "已命中官方核验的院校专业组最低分数据，但来源没有发布最低位次；结果会保留在表格中，但不会生成冲稳保推荐。"
        )

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

    grouped: dict[tuple[str, str, str, str, str], list[GaokaoAdmission]] = defaultdict(list)
    for row in rows:
        grouped[(row.record_kind, row.admission_round, row.university_code, row.group_code, row.major_code)].append(row)

    recommendations: list[dict] = []
    for (record_kind, admission_round, university_code, group_code, major_code), items in grouped.items():
        by_year: dict[int, GaokaoAdmission] = {}
        for item in items:
            previous = by_year.get(item.year)
            if previous is None or (item.min_rank is not None and previous.min_rank is None):
                by_year[item.year] = item
        if not by_year:
            continue
        reference_item = by_year.get(data.reference_year) or by_year[max(by_year)]
        rank_values = sorted(item.min_rank for item in by_year.values() if item.min_rank is not None)
        comparison_rank = int(median(rank_values)) if rank_values else None
        gap = comparison_rank - rank if rank is not None and comparison_rank is not None else None
        tier = _tier(rank, [comparison_rank])[0] if rank is not None and comparison_rank is not None else "数据不足"
        historical_years = sorted(by_year)
        evidence = [
            {
                "year": item.year,
                "min_rank": item.min_rank,
                "min_score": item.min_score,
                "source_id": item.source_id,
                "data_quality": _data_quality(item),
            }
            for item in sorted(by_year.values(), key=lambda x: x.year, reverse=True)
        ]
        source_ids.update(item.source_id for item in by_year.values())
        recommendations.append(
            {
                "tier": tier,
                "rank_status": _rank_status(gap),
                "record_kind": reference_item.record_kind,
                "admission_round": reference_item.admission_round,
                "university_name": reference_item.university_name,
                "school_province": reference_item.school_province,
                "group_name": reference_item.group_name or "",
                "major_name": reference_item.major_name or "",
                "subject_requirements": reference_item.subject_requirements,
                "min_score": reference_item.min_score,
                "min_rank": reference_item.min_rank,
                "avg_score": reference_item.avg_score,
                "max_score": reference_item.max_score,
                "historical_years": historical_years,
                "historical_min_ranks": [by_year[year].min_rank for year in historical_years if by_year[year].min_rank is not None],
                "rank_gap": gap,
                "comparison_rank": comparison_rank,
                "plan_count": reference_item.plan_count,
                "admission_count": reference_item.admission_count,
                "submission_count": reference_item.submission_count,
                "admission_status": reference_item.admission_status,
                "confidence": "高" if len(rank_values) >= 4 else "中" if len(rank_values) >= 2 else "低",
                "data_quality": _data_quality(reference_item),
                "evidence": evidence,
                "reason": _rank_reason(gap, comparison_rank, data.reference_year),
            }
        )

    _sort_recommendations(recommendations, data.sort_by)
    total_recommendations = len(recommendations)
    returned = recommendations[: data.limit]
    school_recommendations = _build_school_recommendations(recommendations, rank, data.reference_year)[: data.limit]
    returned_school_recommendations = _build_school_recommendations(returned, rank, data.reference_year)
    tier_counts = {tier: sum(1 for item in school_recommendations if item["tier"] == tier) for tier in ("冲", "稳", "保", "数据不足")}
    recommendations_by_school_province: dict[str, list] = defaultdict(list)
    for recommendation in returned:
        recommendations_by_school_province.setdefault(recommendation.get("school_province") or "未标注", []).append(recommendation)

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
    if not returned and not warnings:
        provinces = "、".join(data.school_provinces) if data.school_provinces else "当前范围"
        warnings.append(
            f"当前数据库没有满足招生省份、年份、科类、批次及院校所在地（{provinces}）口径的已核验录取数据；"
            "院校所在地未标注或来源未完成核验的记录不会用于推荐。"
        )
    return {
        "disclaimer": DISCLAIMER,
        "data_status": "ok" if any(item["tier"] in {"冲", "稳", "保"} for item in recommendations) else "insufficient",
        "profile": {**data.model_dump(), "resolved_rank": rank},
        "recommendations": returned,
        "sections": dict(recommendations_by_school_province),
        "sources": sources,
        "warnings": warnings,
        "summary": {
            "matched_count": len(school_recommendations),
            "returned_count": len(returned),
            "record_count": total_recommendations,
            "school_count": len(school_recommendations),
            "tier_counts": tier_counts,
            "available_years": available_years,
            "sort_by": data.sort_by,
        },
        "coverage": _coverage_payload(data, rows, available_years),
        "school_recommendations": returned_school_recommendations,
        "rule_note": guide_rule_note(data.candidate_province or data.admission_province or "当前", data.reference_year),
    }


def _catalog_coverage(data: GaokaoCatalogInput, rows: list[GaokaoAdmission], total: int) -> dict:
    return {
        "selected_school_provinces": data.school_provinces or list(SUPPORTED_SCHOOL_PROVINCES),
        "supported_school_provinces": list(SUPPORTED_SCHOOL_PROVINCES),
        "verified_record_count": len(rows),
        "university_count": total,
        "is_full_catalog": False,
        "note": "本科院校总表当前基于已核验官方录取数据，暂无录取记录的院校不会被推断或补写。",
    }


async def catalog(db: AsyncSession, data: GaokaoCatalogInput) -> dict:
    statement = select(GaokaoAdmission).where(
        GaokaoAdmission.province == data.admission_province,
        GaokaoAdmission.verification_status == "verified",
    )
    if data.school_provinces:
        statement = statement.where(GaokaoAdmission.school_province.in_(data.school_provinces))
    if data.year is not None:
        statement = statement.where(GaokaoAdmission.year == data.year)
    if data.track:
        statement = statement.where(GaokaoAdmission.track == data.track)
    if data.batch:
        statement = statement.where(GaokaoAdmission.batch.in_(batch_aliases(data.batch)))
    if data.keywords:
        like = f"%{data.keywords}%"
        statement = statement.where(
            GaokaoAdmission.university_name.like(like)
            | GaokaoAdmission.group_name.like(like)
            | GaokaoAdmission.major_name.like(like)
        )
    rows = (await db.execute(statement)).scalars().all()
    grouped: dict[tuple[str, str, str | None], list[GaokaoAdmission]] = defaultdict(list)
    for row in rows:
        grouped[(row.university_code, row.university_name, row.school_province)].append(row)
    items = []
    for (university_code, university_name, school_province), university_rows in grouped.items():
        latest = max(university_rows, key=lambda row: (row.year, row.min_score or -1))
        scores = [row.min_score for row in university_rows if row.min_score is not None]
        ranks = [row.min_rank for row in university_rows if row.min_rank is not None]
        items.append(
            {
                "university_code": university_code,
                "university_name": university_name,
                "school_province": school_province,
                "latest_year": latest.year,
                "tracks": sorted({row.track for row in university_rows}),
                "years": sorted({row.year for row in university_rows}, reverse=True),
                "record_count": len(university_rows),
                "min_score": max(scores) if scores else None,
                "min_rank": min(ranks) if ranks else None,
                "data_levels": sorted({"专业录取线" if row.record_kind == "major_admit" else "院校专业组投档/录取线" for row in university_rows}),
                "data_status": "已核验录取数据",
            }
        )
    items.sort(key=lambda item: (item["min_score"] is None, -(item["min_score"] or -1), item["university_name"]))
    total = len(items)
    start = (data.page - 1) * data.page_size
    return {
        "items": items[start : start + data.page_size],
        "total": total,
        "page": data.page,
        "page_size": data.page_size,
        "coverage": _catalog_coverage(data, rows, total),
    }


async def coverage_snapshot(db: AsyncSession) -> dict:
    rows = (
        await db.execute(
            select(GaokaoAdmission.school_province, GaokaoAdmission.year, func.count(GaokaoAdmission.id))
            .where(
                GaokaoAdmission.province == "四川",
                GaokaoAdmission.school_province.in_(SUPPORTED_SCHOOL_PROVINCES),
                GaokaoAdmission.verification_status == "verified",
            )
            .group_by(GaokaoAdmission.school_province, GaokaoAdmission.year)
        )
    ).all()
    counts: dict[str, dict[str, int]] = defaultdict(dict)
    for school_province, year, count in rows:
        counts[school_province][str(year)] = count
    return {
        "supported_school_provinces": list(SUPPORTED_SCHOOL_PROVINCES),
        "counts_by_school_province_year": dict(counts),
        "is_full_catalog": False,
        "note": "当前范围只展示已核验官方录取数据；缺少全量公开来源的院校不会被补写。",
    }
