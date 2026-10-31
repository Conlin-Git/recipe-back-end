from app.gaokao.chat_facts import extract_gaokao_facts
from app.schemas.gaokao import GaokaoGuideInput
from app.services.gaokao_service import _tier


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
