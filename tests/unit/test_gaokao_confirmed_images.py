from types import SimpleNamespace

from sqlalchemy.dialects import mysql

from app.gaokao.chat_facts import extract_gaokao_facts
from app.gaokao.image_policy import CONFIRMED, SOURCE_KEY
from app.services.gaokao_service import _effective_rank, _row_data_quality, batch_aliases, eligible_admissions
from scripts.enable_sichuan_image_bundle import image_fact


def test_only_signed_source_is_eligible_and_pending_is_excluded():
    sql=str(eligible_admissions().compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds":True}))
    assert SOURCE_KEY in sql and CONFIRMED in sql and "'pending'" not in sql


def test_original_obscured_name_and_bounded_rank_remain_traceable():
    row=dict(track="历史类",batch="本科批B段", university_code="0003",university_name="候选校名",
             group_code="101",min_score=665,min_rank=34,image="640.webp",row_no=7,
             reason_json='["原图校名被裁切","官方分段表未覆盖该分数"]',record_sha="f"*64)
    fact=image_fact(row)
    assert fact[3]=="院校代码 0003（原图校名不全）" and fact[7]==34 and fact[8]=="历史类/640.webp#row7"
    item=SimpleNamespace(verification_status=CONFIRMED,evidence_note=fact[-1],min_rank=34,min_score=665)
    assert _effective_rank(item,{(2026,665):34}) == 34
    assert _row_data_quality(item,34)=="官方位次"


def test_chat_keeps_early_batch_out_of_regular_recommendations():
    batch=extract_gaokao_facts("2027年四川历史类提前批，位次2000")["batch"]
    assert batch=="提前批" and not (batch_aliases(batch) & batch_aliases("本科"))
