"""检查 2022—2026 年 31 省数据包是否完整且已核验。"""
import argparse
import asyncio

from sqlalchemy import select

from app.database.mysql import AsyncSessionLocal
from app.models.gaokao import GaokaoSource

PROVINCES = {
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江", "上海", "江苏", "浙江",
    "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川",
    "贵州", "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
}
DATASETS = {"score_band", "cutoff", "admission"}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2022)
    parser.add_argument("--end-year", type=int, default=2026)
    args = parser.parse_args()
    expected = {(province, year, dataset) for province in PROVINCES for year in range(args.start_year, args.end_year + 1) for dataset in DATASETS}
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(GaokaoSource))).scalars().all()
    valid = {(r.province, r.year, r.dataset_type) for r in rows if r.validation_status == "verified"}
    missing = sorted(expected - valid)
    extra = sorted(valid - expected)
    print({"expected": len(expected), "verified": len(expected & valid), "missing": len(missing), "extra": len(extra)})
    if missing:
        print("missing:")
        for item in missing:
            print("  ", item)
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
