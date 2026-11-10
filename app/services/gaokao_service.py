"""高考指南：结构化数据优先，模型只负责解释和问答编排。"""
from collections import defaultdict
from statistics import median

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.gaokao.image_policy import CONFIRMED, SOURCE_KEY
from app.models.gaokao import GaokaoAdmission, GaokaoScoreBand, GaokaoSource
from app.schemas.gaokao import DISCLAIMER, GaokaoCatalogInput, GaokaoGuideInput


from app.gaokao.regions import CANDIDATE_REGIONS, SCHOOL_REGIONS as SUPPORTED_SCHOOL_PROVINCES


def eligible_admissions():
    confirmed_source = select(GaokaoSource.id).where(
        GaokaoSource.source_key == SOURCE_KEY, GaokaoSource.validation_status == CONFIRMED
    )
    return or_(GaokaoAdmission.verification_status == "verified",
               and_(GaokaoAdmission.verification_status == CONFIRMED,
                    GaokaoAdmission.source_id.in_(confirmed_source)))


def is_image(item):
    return getattr(item, "verification_status", None) == CONFIRMED


def limited_rank(item):
    # 用户已确认的官方投档截图与院校/考试院发布记录同等参与计算。
    # 历史审计标记仍保留，但不再阻断已确认记录的位次匹配。
    return False


def coverage_counts(rows):
    image_count=sum(is_image(r) for r in rows)
    return {"verified_record_count": len(rows), "eligible_record_count": len(rows)}


def guide_rule_note(candidate_province: str, reference_year: int) -> str:
    area = candidate_province + ("市" if candidate_province == "上海" else "省" if candidate_province in {"四川", "广东"} else "")
    return (
        f"生成规则：使用考生在{area}的省内排位，比较学校在 {reference_year} 年"
        f"{candidate_province}招生口径下的最低投档/录取门槛；不会使用学校所在地的本地录取线替代{candidate_province}招生数据。"
    )


def batch_aliases(batch: str | None) -> set[str]:
    """兼容四川官方本科普通批的阶段名称，同时保留数据库原始批次。"""
    if not batch:
        return set()
    if batch in {"本科", "本科批", "本科普通批"}:
        return {"本科", "本科批", "本科普通批", "本科批次B段", "本科批B段", "普通本科批次B段", "普通本科批"}
    if batch in {"提前批", "本科提前批"}:
        return {"提前批", "本科提前批"}
    return {batch}


async def lookup_score_band(db: AsyncSession, data: GaokaoGuideInput) -> tuple[int, int, int] | None:
    """按分数查一分一段表，返回 (位次, 使用的年份, 来源 id)。

    考生年份通常是“明年”，当年的一分一段表尚未发布；这时回退到不晚于考生年份的
    最近一年（位次法的通用做法），由调用方提示实际使用的年份。
    """
    if data.score is None:
        return None
    row = (
        await db.execute(
            select(GaokaoScoreBand.cumulative_rank, GaokaoScoreBand.year, GaokaoScoreBand.source_id)
            .where(
                GaokaoScoreBand.province == data.candidate_province,
                GaokaoScoreBand.year <= data.candidate_year,
                GaokaoScoreBand.track == data.track,
                GaokaoScoreBand.score == data.score,
            )
            .order_by(GaokaoScoreBand.year.desc())
            .limit(1)
        )
    ).first()
    return (row[0], row[1], row[2]) if row else None


async def resolve_rank(db: AsyncSession, data: GaokaoGuideInput) -> int | None:
    if data.rank:
        return data.rank
    band = await lookup_score_band(db, data)
    return band[0] if band else None


def _tier(rank: int, historical: list[int]) -> tuple[str, int]:
    """返回档位和相对位次差，位次数值越小越有利。"""
    benchmark = int(median(sorted(historical)))
    gap = benchmark - rank
    # 只有在历史线附近才允许进入“冲”。相差数倍时属于口径/数据不匹配，
    # 不能把明显不可达的院校包装成冲刺项。
    if gap < 0:
        if rank > benchmark * 1.2:
            return "数据不足", gap
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
        return "当前记录没有可比的精确最低位次，暂不进行冲稳保分档。"
    if gap > 0:
        return f"{reference_year} 年参考最低位次约为 {benchmark}，当前位次领先 {gap} 位。"
    if gap < 0:
        return f"{reference_year} 年参考最低位次约为 {benchmark}，当前位次落后 {-gap} 位，属于冲刺项。"
    return f"{reference_year} 年参考最低位次约为 {benchmark}，当前位次与历史线接近。"


def _comparison_mode(comparison_rank: int | None) -> str:
    """只有存在同口径最低位次时才允许进入位次比较。"""
    return "rank" if comparison_rank is not None else "score_only"


def _data_quality(item: GaokaoAdmission) -> str:
    if is_image(item) and item.min_rank is not None:
        return "官方位次"
    if item.min_rank is None:
        return "仅最低分" if item.min_score is not None else "无可用数据"
    if item.verification_note and "换算" in item.verification_note:
        return "换算位次"
    return "官方位次"


def _has_exact_min_score(item: GaokaoAdmission) -> bool:
    """“580 分及以上”等区间值不能被当作精确分数换算位次。"""
    note = item.tie_breaker or ""
    return not any(marker in note for marker in ("及以上", "以上", "≥", ">="))


def _effective_rank(item: GaokaoAdmission, scoreband_ranks: dict[tuple[int, int], int]) -> int | None:
    """返回可比较位次、质量和位次来源 source_id。

    官方录取表没有位次时，只在同一招生省、同一年、同一科类有精确最低分且存在
    官方一分一段表的情况下换算。分数区间（例如上海 580 分及以上）永不换算。
    """
    if limited_rank(item):
        return None
    if item.min_rank is not None:
        return item.min_rank
    if item.min_score is None or not _has_exact_min_score(item):
        return None
    return scoreband_ranks.get((item.year, item.min_score))


def _row_data_quality(item: GaokaoAdmission, effective_rank: int | None) -> str:
    if item.min_rank is not None:
        return _data_quality(item)
    return "换算位次" if effective_rank is not None else _data_quality(item)


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
        eligible_admissions(),
    )


def _school_scope_clause(school_provinces: list[str]):
    """生源省不是学校所在地；未标注地点仅能在不限地区时展示。"""
    if not school_provinces:
        return None
    return GaokaoAdmission.school_province.in_(school_provinces)


def _coverage_payload(data: GaokaoGuideInput, rows: list[GaokaoAdmission], history_years: list[int]) -> dict:
    selected = data.school_provinces
    is_known_partial = data.admission_province == "四川" and data.reference_year == 2026
    return {
        "selected_school_provinces": selected,
        "supported_school_provinces": list(SUPPORTED_SCHOOL_PROVINCES),
        **coverage_counts(rows),
        "university_count": len({(row.university_code, row.school_province) for row in rows}),
        "history_years": history_years,
        "is_full_scope": False,
        "scope_note": (
            "四川 2026 数据按批次分别展示，包含已核验的官方投档记录。"
            if is_known_partial
            else "当前目录仅覆盖已收录的招生记录。"
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
                "comparison_mode": _comparison_mode(school_rank),
                "major_count": len(majors),
                "data_quality": next((item.get("data_quality") for item in majors if item.get("min_rank")==school_rank), "仅最低分"),
                "historical_years": sorted({year for item in majors for year in item.get("historical_years", [])}, reverse=True),
                "confidence": "高" if len(majors) >= 4 else "中" if len(majors) >= 2 else "低",
                "reason": _rank_reason(school_gap, school_rank, reference_year),
                "majors": majors,
            }
        )
    _sort_recommendations(schools, "min_score_desc")
    return schools


_MAJOR_PREVIEW_LIMIT = 8
_MAJOR_PREVIEW_FIELDS = ("major_name", "group_name", "min_score", "min_rank")


def _slim_school(school: dict) -> dict:
    """列表页只需要专业的名称和分数线，完整证据链留在后端，避免单次返回数百 KB。"""
    return {
        **school,
        "majors": [
            {field: major.get(field) for field in _MAJOR_PREVIEW_FIELDS}
            for major in school["majors"][:_MAJOR_PREVIEW_LIMIT]
        ],
    }


def _select_by_tier(schools: list[dict], per_tier: int) -> list[dict]:
    """smart 模式：每档各取前 per_tier 所，档内按“最值得看”的顺序。

    冲：先看离线最近、最有机会够到的；稳/保：先看分数最高的好学校。
    """
    selected: list[dict] = []
    for tier in ("冲", "稳", "保"):
        in_tier = [item for item in schools if item["tier"] == tier]
        if tier == "冲":
            in_tier.sort(key=lambda item: (-(item["rank_gap"] if item["rank_gap"] is not None else -10**9), -(item["min_score"] or -1)))
        else:
            in_tier.sort(key=lambda item: (item["min_score"] is None, -(item["min_score"] or -1), item["min_rank"] or 10**9))
        selected.extend(_slim_school(item) for item in in_tier[:per_tier])
    return selected


async def guide(db: AsyncSession, data: GaokaoGuideInput) -> dict:
    warnings: list[str] = []
    rank = data.rank
    score_band_year: int | None = None
    score_band_source_id: int | None = None
    if rank is None and data.score is not None:
        band = await lookup_score_band(db, data)
        if band:
            rank, score_band_year, score_band_source_id = band
            if score_band_year != data.candidate_year:
                warnings.append(f"分数已按 {score_band_year} 年一分一段表换算为位次 {rank}，实际位次以当年为准。")
        else:
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
    school_scope = _school_scope_clause(data.school_provinces)
    if school_scope is not None:
        statement = statement.where(school_scope)
    if data.keywords:
        like = f"%{data.keywords}%"
        statement = statement.where(
            GaokaoAdmission.university_name.like(like)
            | GaokaoAdmission.group_name.like(like)
            | GaokaoAdmission.major_name.like(like)
        )
    rows = (await db.execute(statement)).scalars().all()
    scoreband_rows = (
        await db.execute(
            select(GaokaoScoreBand).where(
                GaokaoScoreBand.province == data.admission_province,
                GaokaoScoreBand.year.in_({row.year for row in rows}),
                GaokaoScoreBand.track == data.track,
            )
        )
    ).scalars().all() if rows else []
    scoreband_ranks = {(row.year, row.score): row.cumulative_rank for row in scoreband_rows}
    derived_source_ids = {row.source_id for row in scoreband_rows}
    effective_ranks = {row.id: _effective_rank(row, scoreband_ranks) for row in rows}
    row_quality = {row.id: _row_data_quality(row, effective_ranks[row.id]) for row in rows}
    available_years = sorted({row.year for row in rows}, reverse=True)
    if rows and not any(effective_ranks[row.id] is not None for row in rows):
        warnings.append(
            "部分院校专业组仅提供最低分，无法和考生位次做同口径比较；此类记录不会进入冲、稳、保档位。"
        )
        if data.admission_province == "上海" and data.track == "综合":
            warnings.append(
                "上海 2026 本科普通批官方表按综合改革口径发布，当前来源只有投档最低分和同分排序，未提供可复核的专业组最低位次；"
                "不能把上海分数横向换算成四川或广东位次。"
            )

    source_ids = set()
    source_ids.update(derived_source_ids)
    if score_band_source_id:
        source_ids.add(score_band_source_id)

    grouped: dict[tuple[str, str, str, str, str], list[GaokaoAdmission]] = defaultdict(list)
    for row in rows:
        grouped[(row.record_kind, row.admission_round, row.university_code, row.group_code, row.major_code)].append(row)

    recommendations: list[dict] = []
    for (record_kind, admission_round, university_code, group_code, major_code), items in grouped.items():
        by_year: dict[int, GaokaoAdmission] = {}
        for item in items:
            previous = by_year.get(item.year)
            if previous is None or (effective_ranks[item.id] is not None, not is_image(item), -item.id) > (effective_ranks[previous.id] is not None, not is_image(previous), -previous.id):
                by_year[item.year] = item
        if not by_year:
            continue
        reference_item = by_year.get(data.reference_year) or by_year[max(by_year)]
        rank_values = sorted(effective_ranks[item.id] for item in by_year.values() if effective_ranks[item.id] is not None)
        comparison_rank = int(median(rank_values)) if rank_values else None
        gap = comparison_rank - rank if rank is not None and comparison_rank is not None else None
        tier = _tier(rank, [comparison_rank])[0] if rank is not None and comparison_rank is not None else "数据不足"
        historical_years = sorted(by_year)
        evidence = [
            {
                "year": item.year,
                "min_rank": effective_ranks[item.id],
                "min_score": item.min_score,
                "source_id": item.source_id,
                "source_type": "official",
                "source_locator": None if is_image(item) else item.source_locator,
                "note": None,
                "reported_rank": item.min_rank,
                "data_quality": row_quality[item.id],
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
                "tie_breaker": reference_item.tie_breaker,
                "min_score": reference_item.min_score,
                "min_rank": effective_ranks[reference_item.id],
                "avg_score": reference_item.avg_score,
                "max_score": reference_item.max_score,
                "historical_years": historical_years,
                "historical_min_ranks": [effective_ranks[by_year[year].id] for year in historical_years if effective_ranks[by_year[year].id] is not None],
                "rank_gap": gap,
                "comparison_rank": comparison_rank,
                "comparison_mode": _comparison_mode(comparison_rank),
                "plan_count": reference_item.plan_count,
                "admission_count": reference_item.admission_count,
                "submission_count": reference_item.submission_count,
                "admission_status": reference_item.admission_status,
                "confidence": "高" if len(rank_values) >= 4 else "中" if len(rank_values) >= 2 else "低",
                "data_quality": row_quality[reference_item.id],
                "tie_breaker": reference_item.tie_breaker,
                "evidence": evidence,
                "reason": _rank_reason(gap, comparison_rank, data.reference_year),
            }
        )

    _sort_recommendations(recommendations, data.sort_by)
    total_recommendations = len(recommendations)
    # Apply the requested sort within rank-comparable results before truncation;
    # a large release must not fill the first page with unreachable schools.
    recommendations.sort(key=lambda item: item["tier"] == "数据不足")
    returned = recommendations[: data.limit]
    schools = _build_school_recommendations(recommendations, rank, data.reference_year)
    # 档位计数必须基于全部匹配学校；截断只影响返回条数，不能改变“共有多少所”。
    tier_counts = {tier: sum(1 for item in schools if item["tier"] == tier) for tier in ("冲", "稳", "保", "数据不足")}
    if data.sort_by == "smart":
        school_recommendations = _select_by_tier(schools, data.limit)
    else:
        _sort_recommendations(schools, data.sort_by)
        schools.sort(key=lambda item: item["tier"] == "数据不足")
        school_recommendations = schools[:data.limit]
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
                "url": source.source_url if source.validation_status != CONFIRMED else None,
                "sha256": source.sha256,
                "validation_status": source.validation_status,
            }
            for source in (
                await db.execute(select(GaokaoSource).where(GaokaoSource.id.in_(source_ids)))
            ).scalars().all()
            if source.validation_status != CONFIRMED and source.source_url
        ]
    if not returned:
        provinces = "、".join(data.school_provinces) if data.school_provinces else "当前范围"
        warnings.append(
            f"当前数据库没有满足招生省份、年份、科类、批次及院校所在地（{provinces}）口径的可用投档或录取数据；"
            "院校所在地未标注或来源未完成核验的记录不会用于推荐。"
        )
    return {
        "disclaimer": DISCLAIMER,
        "data_status": "ok" if any(item["tier"] in {"冲", "稳", "保"} for item in recommendations) else "insufficient",
        "profile": {**data.model_dump(), "resolved_rank": rank, "score_band_year": score_band_year},
        "recommendations": returned,
        "sections": dict(recommendations_by_school_province),
        "sources": sources,
        "warnings": warnings,
        "summary": {
            "matched_count": len(schools),
            "returned_count": len(returned),
            "record_count": total_recommendations,
            "school_count": len(schools),
            "tier_counts": tier_counts,
            "returned_school_count": len(school_recommendations),
            "available_years": available_years,
            "sort_by": data.sort_by,
        },
        "coverage": _coverage_payload(data, rows, available_years),
        "school_recommendations": school_recommendations,
        "rule_note": guide_rule_note(data.candidate_province or data.admission_province or "当前", data.reference_year),
    }


def _catalog_coverage(data: GaokaoCatalogInput, rows: list[GaokaoAdmission], total: int) -> dict:
    return {
        "selected_school_provinces": data.school_provinces,
        "supported_school_provinces": list(SUPPORTED_SCHOOL_PROVINCES),
        **coverage_counts(rows),
        "university_count": total,
        "is_full_catalog": False,
        "note": "院校总表展示已收录的官方投档/录取记录，并标注可复核来源。",
    }


async def catalog(db: AsyncSession, data: GaokaoCatalogInput) -> dict:
    statement = select(GaokaoAdmission).where(
        GaokaoAdmission.province == data.admission_province,
        eligible_admissions(),
    )
    school_scope = _school_scope_clause(data.school_provinces)
    if school_scope is not None:
        statement = statement.where(school_scope)
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
        ranks = [row.min_rank for row in university_rows if row.min_rank is not None and not limited_rank(row)]
        items.append(
            {
                "university_code": university_code,
                "university_name": university_name,
                "school_province": school_province,
                "latest_year": latest.year,
                "tracks": sorted({row.track for row in university_rows}),
                "years": sorted({row.year for row in university_rows}, reverse=True),
                "record_count": len(university_rows),
                # 学校维度的“最低分/最低位次”必须取最低门槛：分数取最小值，
                # 位次取最靠后的值（数值越大越靠后）。此前取反导致同济大学
                # 634/643/655 被错误聚合成 655，页面与官方口径不一致。
                "min_score": min(scores) if scores else None,
                "min_rank": max(ranks) if ranks else None,
                "data_levels": sorted({"专业录取线" if row.record_kind == "major_admit" else "院校专业组投档/录取线" for row in university_rows}),
                "data_status": "官方记录",
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
            select(
                GaokaoAdmission.province,
                GaokaoAdmission.school_province,
                GaokaoAdmission.year,
                GaokaoAdmission.track,
                func.count(GaokaoAdmission.id),
            )
            .where(
                eligible_admissions(), GaokaoAdmission.province.in_(CANDIDATE_REGIONS),
            )
            .group_by(
                GaokaoAdmission.province,
                GaokaoAdmission.school_province,
                GaokaoAdmission.year,
                GaokaoAdmission.track,
            )
        )
    ).all()
    counts: dict[str, dict[str, int]] = defaultdict(dict)
    admission_counts: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    for admission_province, school_province, year, track, count in rows:
        if school_province in SUPPORTED_SCHOOL_PROVINCES:
            counts[school_province][str(year)] = counts[school_province].get(str(year), 0) + count
        admission_counts[admission_province][str(year)][track] = admission_counts[admission_province][str(year)].get(track, 0) + count
    return {
        "supported_school_provinces": list(SUPPORTED_SCHOOL_PROVINCES),
        "counts_by_school_province_year": dict(counts),
        "counts_by_admission_province_year_track": {
            province: {year: dict(tracks) for year, tracks in years.items()}
            for province, years in admission_counts.items()
        },
        "is_full_catalog": False,
        "note": "当前范围包含已核验的官方投档/录取记录；缺少全量公开来源的院校不会被补写。四川、广东、上海的科类按各省官方口径分别保存，上海综合改革不拆分为物理/历史。",
    }
