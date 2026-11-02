import pytest
from pydantic import ValidationError
from app.gaokao.chat_facts import extract_gaokao_facts
from app.gaokao.school_directory import Directory, school_label
from app.schemas.gaokao import GaokaoCatalogInput, GaokaoGuideInput
from scripts.backfill_school_regions import Resolver, national_entry


def test_source_not_target_in_guide_catalog_and_chat():
    for location in ('福建','北京','浙江'):
        with pytest.raises(ValidationError): GaokaoCatalogInput(admission_province=location)
        with pytest.raises(ValidationError): GaokaoGuideInput(province=location,track='物理类')
        assert GaokaoGuideInput(province='四川',track='物理类',school_provinces=[location]).school_provinces == [location]
    with pytest.raises(ValidationError):
        GaokaoGuideInput(candidate_province='广东',admission_province='四川',track='历史类')
    data=GaokaoGuideInput(province='四川省',track='历史类',preferred_regions=['北京市','江西省'])
    assert data.school_provinces == ['北京','江西']
    assert extract_gaokao_facts('上海有哪些学校招四川考生？')['province']=='四川'
    assert extract_gaokao_facts('上海有哪些学校招四川考生？')['school_provinces']==['上海']
    assert 'province' not in extract_gaokao_facts('想去北京的高校')
    assert extract_gaokao_facts('浙江考生想报上海高校')['province']=='浙江'


def test_moe_and_branch_locations_are_not_applicant_or_affiliation():
    d=Directory()
    assert len(d.master)==2952
    assert d.exact(school_label('哈尔滨工业大学（深圳）（中外合作办学）')).province=='广东'
    assert d.exact('哈尔滨工业大学（威海）').province=='山东'
    assert d.exact('哈尔滨工业大学').province=='黑龙江'
    assert d.exact(school_label('厦门大学（马来西亚分校）')) is None
    for name,region in [('河北工业大学','天津'),('西藏民族大学','陕西')]:
        assert national_entry(next(s for s in d.master if s.name == name),d).province == region


def test_wrong_llm_region_and_wrong_campus_are_not_accepted():
    resolver=Resolver()
    resolver.model['上海交大']={'proposed':'西安交通大学','model':'test'}
    assert resolver.resolve(dict(province='上海',year=2026,university_code='10201',university_name='上海交大(01)')) is None
    r=resolver.resolve(dict(province='上海',year=2026,university_code='79601',university_name='北师珠海(01)'))
    assert r['province']=='广东'
