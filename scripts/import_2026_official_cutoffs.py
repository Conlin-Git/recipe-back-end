"""把阳光高考 2026 批次线文章中逐省核对的普通类数据写入 MySQL。

该脚本只导入官方文章中可以明确对应到省份、科类和批次的普通类/特殊类型控制线，
艺术体育等专业线仍保留在原始来源文件，避免 OCR 表格错列后进入推荐结果。
"""
import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from app.database.mysql import AsyncSessionLocal
from app.models.gaokao import GaokaoCutoff, GaokaoSource

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = ROOT / "data/gaokao/2026_cutoff_sources.json"


def row(province, track, batch, score, cutoff_type="control"):
    return {"province": province, "year": 2026, "track": track, "batch": batch, "score": score, "cutoff_type": cutoff_type}


ROWS = [
    row("北京", "综合", "本科", 429), row("北京", "综合", "本科", 521, "special"), row("北京", "综合", "专科", 120),
    row("天津", "综合", "本科", 458),
    row("河北", "历史类", "本科", 485), row("河北", "物理类", "本科", 443), row("河北", "历史类", "本科", 542, "special"), row("河北", "物理类", "本科", 510, "special"), row("河北", "历史类", "专科", 200), row("河北", "物理类", "专科", 200),
    row("山西", "历史类", "本科", 409), row("山西", "物理类", "本科", 401), row("山西", "历史类", "本科", 538, "special"), row("山西", "物理类", "本科", 524, "special"),
    row("内蒙古", "历史类", "本科", 403), row("内蒙古", "物理类", "本科", 363), row("内蒙古", "历史类", "本科", 512, "special"), row("内蒙古", "物理类", "本科", 488, "special"), row("内蒙古", "历史类", "专科", 160), row("内蒙古", "物理类", "专科", 160),
    row("辽宁", "历史类", "本科", 442), row("辽宁", "物理类", "本科", 344), row("辽宁", "历史类", "本科", 527, "special"), row("辽宁", "物理类", "本科", 508, "special"), row("辽宁", "历史类", "专科", 150), row("辽宁", "物理类", "专科", 150),
    row("吉林", "历史类", "本科", 343), row("吉林", "物理类", "本科", 321), row("吉林", "历史类", "本科", 478, "special"), row("吉林", "物理类", "本科", 473, "special"), row("吉林", "历史类", "专科", 160), row("吉林", "物理类", "专科", 160),
    row("黑龙江", "历史类", "本科", 385), row("黑龙江", "物理类", "本科", 340), row("黑龙江", "历史类", "本科", 466, "special"), row("黑龙江", "物理类", "本科", 464, "special"), row("黑龙江", "历史类", "专科", 150), row("黑龙江", "物理类", "专科", 150),
    row("上海", "综合", "本科", 403), row("上海", "综合", "本科", 504, "special"),
    row("江苏", "历史类", "本科", 484), row("江苏", "物理类", "本科", 456), row("江苏", "历史类", "本科", 532, "special"), row("江苏", "物理类", "本科", 513, "special"),
    row("浙江", "综合", "第一段", 494), row("浙江", "综合", "第二段", 266), row("浙江", "综合", "本科", 594, "special"),
    row("安徽", "历史类", "本科", 490), row("安徽", "物理类", "本科", 451), row("安徽", "历史类", "本科", 522, "special"), row("安徽", "物理类", "本科", 514, "special"), row("安徽", "历史类", "专科", 200), row("安徽", "物理类", "专科", 200),
    row("福建", "历史类", "本科", 458), row("福建", "物理类", "本科", 446), row("福建", "历史类", "本科", 533, "special"), row("福建", "物理类", "本科", 528, "special"), row("福建", "历史类", "专科", 235), row("福建", "物理类", "专科", 235),
    row("江西", "历史类", "本科", 479), row("江西", "物理类", "本科", 412), row("江西", "历史类", "本科", 535, "special"), row("江西", "物理类", "本科", 505, "special"), row("江西", "历史类", "专科", 220), row("江西", "物理类", "专科", 200),
    row("山东", "综合", "一段", 442), row("山东", "综合", "二段", 150), row("山东", "综合", "本科", 525, "special"),
    row("河南", "历史类", "本科", 459), row("河南", "物理类", "本科", 419), row("河南", "历史类", "本科", 534, "special"), row("河南", "物理类", "本科", 513, "special"), row("河南", "历史类", "专科", 179), row("河南", "物理类", "专科", 179),
    row("湖北", "历史类", "本科", 443), row("湖北", "物理类", "本科", 435), row("湖北", "历史类", "本科", 532, "special"), row("湖北", "物理类", "本科", 529, "special"), row("湖北", "历史类", "专科", 200), row("湖北", "物理类", "专科", 200),
    row("湖南", "历史类", "本科", 446), row("湖南", "物理类", "本科", 400), row("湖南", "历史类", "本科", 494, "special"), row("湖南", "物理类", "本科", 481, "special"), row("湖南", "历史类", "专科", 200), row("湖南", "物理类", "专科", 200),
    row("广东", "历史类", "本科", 440), row("广东", "物理类", "本科", 425), row("广东", "历史类", "本科", 546, "special"), row("广东", "物理类", "本科", 539, "special"), row("广东", "历史类", "专科", 200), row("广东", "物理类", "专科", 200),
    row("广西", "历史类", "本科", 398), row("广西", "物理类", "本科", 368), row("广西", "历史类", "本科", 520, "special"), row("广西", "物理类", "本科", 510, "special"), row("广西", "历史类", "专科", 180), row("广西", "物理类", "专科", 180),
    row("海南", "综合", "本科", 479), row("海南", "综合", "本科", 568, "special"),
    row("重庆", "历史类", "本科", 415), row("重庆", "物理类", "本科", 406), row("重庆", "历史类", "本科", 510, "special"), row("重庆", "物理类", "本科", 496, "special"), row("重庆", "历史类", "专科", 180), row("重庆", "物理类", "专科", 180),
    row("四川", "历史类", "本科", 455), row("四川", "物理类", "本科", 435), row("四川", "历史类", "本科", 525, "special"), row("四川", "物理类", "本科", 519, "special"), row("四川", "历史类", "专科", 150), row("四川", "物理类", "专科", 150),
    row("贵州", "历史类", "本科", 439), row("贵州", "物理类", "本科", 393), row("贵州", "历史类", "本科", 503, "special"), row("贵州", "物理类", "本科", 494, "special"), row("贵州", "历史类", "专科", 200), row("贵州", "物理类", "专科", 200),
    row("云南", "历史类", "本科", 465), row("云南", "物理类", "本科", 435), row("云南", "历史类", "本科", 545, "special"), row("云南", "物理类", "本科", 505, "special"), row("云南", "历史类", "专科", 180), row("云南", "物理类", "专科", 180),
    row("西藏", "文史A类", "本科一批", 330), row("西藏", "文史B类", "本科一批", 400), row("西藏", "文史A类", "本科二批", 294), row("西藏", "文史B类", "本科二批", 304), row("西藏", "文史A类", "专科", 237), row("西藏", "文史B类", "专科", 237),
    row("西藏", "理工A类", "本科一批", 300), row("西藏", "理工B类", "本科一批", 400), row("西藏", "理工A类", "本科二批", 200), row("西藏", "理工B类", "本科二批", 300), row("西藏", "理工A类", "专科", 195), row("西藏", "理工B类", "专科", 195),
    row("陕西", "历史类", "本科", 385), row("陕西", "物理类", "本科", 376), row("陕西", "历史类", "本科", 462, "special"), row("陕西", "物理类", "本科", 451, "special"), row("陕西", "历史类", "专科", 200), row("陕西", "物理类", "专科", 200),
    row("甘肃", "历史类", "本科", 405), row("甘肃", "物理类", "本科", 367), row("甘肃", "历史类", "本科", 508, "special"), row("甘肃", "物理类", "本科", 477, "special"), row("甘肃", "历史类", "专科", 160), row("甘肃", "物理类", "专科", 180),
    row("青海", "历史类", "本科", 376), row("青海", "物理类", "本科", 344), row("青海", "历史类", "本科", 427, "special"), row("青海", "物理类", "本科", 417, "special"), row("青海", "历史类", "专科", 150), row("青海", "物理类", "专科", 150),
    row("宁夏", "历史类", "本科", 393), row("宁夏", "物理类", "本科", 360), row("宁夏", "历史类", "本科", 474, "special"), row("宁夏", "物理类", "本科", 437, "special"), row("宁夏", "历史类", "专科", 150), row("宁夏", "物理类", "专科", 150),
    row("新疆", "文科", "本科一批", 451), row("新疆", "理科", "本科一批", 468), row("新疆", "文科", "本科二批", 315), row("新疆", "理科", "本科二批", 304), row("新疆", "文科", "高职专科", 140), row("新疆", "理科", "高职专科", 140),
]


async def main():
    source_records = {item["province"]: item for item in json.loads(SOURCE_FILE.read_text(encoding="utf-8"))["records"]}
    assert len(source_records) == 31, len(source_records)
    async with AsyncSessionLocal() as db:
        source_ids = {}
        for province, item in source_records.items():
            raw = json.dumps({"province": province, "year": 2026, "url": item["url"], "title": item["title"]}, ensure_ascii=False).encode()
            sha = hashlib.sha256(raw).hexdigest()
            key = f"chsi-2026-cutoff:{province}"
            source = (await db.execute(select(GaokaoSource).where(GaokaoSource.source_key == key))).scalar_one_or_none()
            if source is None:
                source = GaokaoSource(source_key=key, publisher="教育部阳光高考信息平台/省级考试机构", province=province, year=2026, dataset_type="cutoff", source_url=item["url"], retrieved_at=datetime.now(), sha256=sha, row_count=0, validation_status="verified", notes="普通类和特殊类型文化控制线；艺术体育原始文件另存来源包")
                db.add(source)
                await db.flush()
            source_ids[province] = source.id
        for item in ROWS:
            item = {**item, "source_id": source_ids[item["province"]]}
            existing = (await db.execute(select(GaokaoCutoff).where(GaokaoCutoff.province == item["province"], GaokaoCutoff.year == 2026, GaokaoCutoff.track == item["track"], GaokaoCutoff.batch == item["batch"], GaokaoCutoff.cutoff_type == item["cutoff_type"]))).scalar_one_or_none()
            if existing:
                for key, value in item.items(): setattr(existing, key, value)
            else:
                db.add(GaokaoCutoff(**item))
        for province, source_id in source_ids.items():
            count = await db.scalar(select(func.count()).select_from(GaokaoCutoff).where(GaokaoCutoff.source_id == source_id))
            source = await db.get(GaokaoSource, source_id)
            source.row_count = int(count or 0)
        await db.commit()
    print(f"✅ 已导入 2026 官方普通类/特殊类型批次线 {len(ROWS)} 条，覆盖省份 {len(source_records)} 个")


if __name__ == "__main__":
    asyncio.run(main())
