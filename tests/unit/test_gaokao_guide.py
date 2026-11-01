import pytest

from app.gaokao.chat_facts import extract_gaokao_facts
from app.api.v1.gaokao import router
from app.schemas.gaokao import GaokaoGuideInput
from app.schemas.gaokao import GaokaoCatalogInput
from app.services.gaokao_service import _build_school_recommendations, _sort_recommendations, _tier, catalog, guide, guide_rule_note


class _EmptyDbResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _EmptyDb:
    async def execute(self, _statement):
        return _EmptyDbResult()


def test_catalog_get_route_exists_for_direct_page_access():
    catalog_routes = [route for route in router.routes if route.path == "/gaokao/catalog"]
    methods = set().union(*(route.methods for route in catalog_routes))
    assert "GET" in methods
    assert "POST" in methods


def test_extract_gaokao_facts_keeps_official_dimensions():
    facts = extract_gaokao_facts("广东省2026年物理类，省内位次38000，考了610分，报本科计算机")
    assert facts == {
        "province": "广东",
        "year": 2026,
        "rank": 38000,
        "score": 610,
        "track": "物理类",
        "batch": "本科",
        "subjects": ["物理"],
    }


def test_rank_tier_is_based_on_rank_gap():
    assert _tier(38000, [44000, 45000, 46000])[0] == "保"
    assert _tier(38000, [39000, 40000, 41000])[0] == "稳"
    assert _tier(38000, [30000, 32000, 35000])[0] == "冲"


def test_rank_behind_cutoff_is_always_chong():
    # 位次 38000 比历史线 37156 落后 844 位，不能再显示为“稳”。
    assert _tier(38000, [37156])[0] == "冲"


def test_recommendations_default_to_score_descending():
    items = [
        {"min_score": 581, "min_rank": 37000, "rank_gap": 10},
        {"min_score": 620, "min_rank": 21000, "rank_gap": -10},
        {"min_score": None, "min_rank": 10000, "rank_gap": 20},
    ]
    _sort_recommendations(items, "min_score_desc")
    assert [item["min_score"] for item in items] == [620, 581, None]


def test_guide_aggregates_school_and_keeps_major_details():
    schools = _build_school_recommendations(
        [
            {
                "university_name": "测试大学",
                "school_province": "四川",
                "min_score": 610,
                "min_rank": 30000,
                "rank_gap": -1000,
                "data_quality": "官方位次",
                "historical_years": [2026],
                "tier": "冲",
                "major_name": "计算机科学与技术",
            },
            {
                "university_name": "测试大学",
                "school_province": "四川",
                "min_score": 580,
                "min_rank": 38000,
                "rank_gap": 7000,
                "data_quality": "官方位次",
                "historical_years": [2026],
                "tier": "保",
                "major_name": "工商管理",
            },
        ],
        candidate_rank=37000,
        reference_year=2026,
    )
    assert len(schools) == 1
    assert schools[0]["major_count"] == 2
    assert schools[0]["min_score"] == 580
    assert schools[0]["min_rank"] == 38000
    # 学校维度取全校最低门槛；该校最低位次 38000 对当前 37000 为“稳”，
    # 具体专业的“冲”会在展开明细中展示。
    assert schools[0]["tier"] == "稳"


def test_batch_short_name_matches_imported_official_batch():
    data = GaokaoGuideInput(
        province="广东",
        year=2025,
        track="物理类",
        rank=38000,
        batch="本科",
    )
    assert data.batch == "本科普通批"


def test_candidate_and_reference_years_are_independent():
    data = GaokaoGuideInput(
        province="广东",
        candidate_year=2027,
        reference_year=2026,
        track="物理类",
        rank=140000,
        score=400,
        batch="本科",
    )
    assert data.candidate_year == 2027
    assert data.reference_year == 2026
    assert data.model_dump()["batch"] == "本科普通批"


def test_new_province_dimensions_are_mapped_for_school_scope():
    data = GaokaoGuideInput(
        candidate_province="四川",
        admission_province="四川",
        school_provinces=["四川", "广东", "上海"],
        candidate_year=2027,
        reference_year=2026,
        track="物理类",
        rank=140000,
    )
    assert data.province == "四川"
    assert data.school_provinces == ["四川", "广东", "上海"]


def test_rule_note_uses_candidate_local_admission_province():
    note = guide_rule_note("广东", 2026)
    assert "考生在广东省的省内排位" in note
    assert "2026 年广东招生口径" in note
    assert "不会使用学校所在地" in note


@pytest.mark.asyncio
async def test_guide_empty_match_returns_empty_school_list_without_fake_records():
    data = GaokaoGuideInput(
        candidate_province="广东",
        admission_province="广东",
        school_provinces=["四川", "广东", "上海"],
        reference_year=2026,
        track="物理类",
        rank=38000,
        batch="本科",
    )
    result = await guide(_EmptyDb(), data)
    assert result["school_recommendations"] == []
    assert result["recommendations"] == []
    assert result["summary"]["matched_count"] == 0
    assert result["data_status"] == "insufficient"
    assert any("没有满足" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_catalog_empty_match_is_a_valid_empty_page_not_not_found():
    result = await catalog(
        _EmptyDb(),
        GaokaoCatalogInput(
            admission_province="广东",
            school_provinces=["四川", "广东", "上海"],
            year=2026,
            track="物理类",
            batch="本科",
        ),
    )
    assert result["items"] == []
    assert result["total"] == 0
    assert result["page"] == 1
    assert result["coverage"]["is_full_catalog"] is False
