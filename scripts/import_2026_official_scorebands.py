"""导入 2026 年官方附件中可逐行校验的一分一段表。

脚本只接收阳光高考/省级考试机构的原始附件，并对每行执行
累计人数 = 上一行累计人数 + 本段人数 的校验。图片 OCR 不在本脚本内
自动入库，避免错列后污染志愿推荐。
"""
import asyncio
import csv
import hashlib
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import fitz
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from app.database.mysql import AsyncSessionLocal
from app.models.gaokao import GaokaoScoreBand, GaokaoSource

ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "data/gaokao/2026_scorebands/assets"
CONVERTED_DIR = ROOT / "data/gaokao/2026_scorebands/converted"

SPECS = [
    ("000_天津.bin", "天津", "综合", "https://gaokao.chsi.com.cn/gkxx/zc/ss/202606/20260624/2293845980.html", "text"),
    ("001_上海.bin", "上海", "综合", "https://gaokao.chsi.com.cn/gkxx/zc/ss/202606/20260624/2293844300.html", "text"),
    ("026_江西省.bin", "江西", "历史类", "https://gaokao.chsi.com.cn/gkxx/zc/ss/202606/20260626/2293847814.html", "text"),
    ("027_江西省.bin", "江西", "物理类", "https://gaokao.chsi.com.cn/gkxx/zc/ss/202606/20260626/2293847814.html", "text"),
    ("028_山东.bin", "山东", "综合", "https://gaokao.chsi.com.cn/gkxx/zc/ss/202606/20260625/2293847728.html", "shandong"),
    ("031_海南省.bin", "海南", "综合", "https://gaokao.chsi.com.cn/gkxx/zc/ss/202607/20260701/2293870259.html", "hainan"),
]


def _numbers_from_text(path: Path) -> list[dict]:
    """解析每行一个分数/本段人数/累计人数的官方 PDF。"""
    lines = []
    for page in fitz.open(path):
        for value in page.get_text().splitlines():
            value = value.strip()
            parts = value.split()
            # 部分江西 PDF 在分页/字体切换处把“人数 累计人数”放在同一行。
            if len(parts) == 2 and all(re.fullmatch(r"\d+", part) for part in parts):
                lines.extend(parts)
            else:
                lines.append(value)
    rows = []
    for i, token in enumerate(lines):
        m = re.fullmatch(r"(\d{2,3})(?:\s*分)?(?:\s*及以上)?", token)
        if not m:
            continue
        score = int(m.group(1))
        if not 100 <= score <= 900 or i + 2 >= len(lines):
            continue
        if not re.fullmatch(r"\d+", lines[i + 1]) or not re.fullmatch(r"\d+", lines[i + 2]):
            continue
        count, cumulative = int(lines[i + 1]), int(lines[i + 2])
        if count <= 0 or cumulative <= 0 or cumulative < count:
            continue
        # 天津等 PDF 会把页码/表格列顺序插入文本流；真正的行应保持分数递减。
        next_score = None
        if i + 3 < len(lines):
            next_match = re.fullmatch(r"(\d{2,3})(?:\s*分)?(?:\s*及以上)?", lines[i + 3])
            next_score = int(next_match.group(1)) if next_match else None
        if next_score is not None and next_score >= score:
            continue
        if 100 <= cumulative <= 900 and next_score is not None and next_score < 100:
            # 典型误读：把上一行人数当分数、把下一行分数当累计人数。
            continue
        rows.append({"score": score, "candidate_count": count, "cumulative_rank": cumulative})
    return _validate(rows)


def _validate(rows: list[dict]) -> list[dict]:
    seen = set()
    previous = None
    for row in rows:
        if row["score"] in seen:
            continue
        if previous is not None and row["cumulative_rank"] < previous:
            raise ValueError(f"累计人数逆序：{row}")
        if previous is not None and row["cumulative_rank"] != previous + row["candidate_count"]:
            raise ValueError(f"累计人数与本段人数不一致：{row}，上一累计={previous}")
        row["rank_start"] = row["cumulative_rank"] - row["candidate_count"] + 1
        row["rank_end"] = row["cumulative_rank"]
        seen.add(row["score"])
        previous = row["cumulative_rank"]
    return rows


def _hainan(path: Path) -> list[dict]:
    """按官方 PDF 的坐标列读取“全体考生”两列，跳过首行合并文本。"""
    rows = [{"score": 800, "candidate_count": 111, "cumulative_rank": 111}]
    for page in fitz.open(path):
        grouped = {}
        for x0, y0, _x1, _y1, text, *_ in page.get_text("words"):
            grouped.setdefault(round(y0, 1), []).append((x0, text))
        for words in grouped.values():
            words.sort()
            if not words or not re.fullmatch(r"\d{3}", words[0][1]):
                continue
            score = int(words[0][1])
            if not 200 <= score <= 900:
                continue
            # x≈60 为全体本段人数，x≈92 为全体累计人数。
            nums = [(x, int(t)) for x, t in words[1:] if re.fullmatch(r"\d+", t)]
            if len(nums) < 2:
                continue
            rows.append({"score": score, "candidate_count": nums[0][1], "cumulative_rank": nums[1][1]})
    rows.sort(key=lambda x: x["score"], reverse=True)
    return _validate(rows)


def _soffice() -> str | None:
    for value in (shutil.which("soffice"), "/Users/conlinchen/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice"):
        if value and Path(value).exists():
            return value
    return None


def _shandong(path: Path) -> list[dict]:
    try:
        import pandas as pd

        frame = pd.read_excel(path, header=None, engine="xlrd")
        rows = []
        for raw in frame.itertuples(index=False, name=None):
            values = ["" if value != value else str(value).strip() for value in raw]
            if not values or not re.fullmatch(r"\d{3}", values[0]) or len(values) < 3:
                continue
            if not values[1].isdigit() or not values[2].isdigit():
                continue
            rows.append({"score": int(values[0]), "candidate_count": int(values[1]), "cumulative_rank": int(values[2])})
        if rows:
            rows.sort(key=lambda x: x["score"], reverse=True)
            return _validate(rows)
    except Exception:
        # 本地/极简部署没有 xlrd 时回退到 soffice 转 CSV。
        pass
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = CONVERTED_DIR / f"{path.stem}.csv"
    if not csv_path.exists():
        command = _soffice()
        if not command:
            raise RuntimeError("山东 XLS 需要 LibreOffice/soffice 才能解析")
        subprocess.run([command, "--headless", "--convert-to", "csv", "--outdir", str(CONVERTED_DIR), str(path)], check=True, stdout=subprocess.DEVNULL)
    rows = []
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.reader(handle):
            if not raw or not re.fullmatch(r"\d{3}", raw[0].strip()):
                continue
            if len(raw) < 3 or not raw[1].strip().isdigit() or not raw[2].strip().isdigit():
                continue
            rows.append({"score": int(raw[0]), "candidate_count": int(raw[1]), "cumulative_rank": int(raw[2])})
    rows.sort(key=lambda x: x["score"], reverse=True)
    return _validate(rows)


def _source_key(province: str, url: str, sha256: str) -> str:
    return f"chsi-2026-score-band:{province}:{hashlib.sha256((url + sha256).encode()).hexdigest()[:16]}"


async def main():
    imported = 0
    async with AsyncSessionLocal() as db:
        for filename, province, track, url, parser in SPECS:
            path = ASSET_DIR / filename
            if not path.exists():
                raise FileNotFoundError(path)
            raw = path.read_bytes()
            sha256 = hashlib.sha256(raw).hexdigest()
            rows = _numbers_from_text(path) if parser == "text" else _shandong(path) if parser == "shandong" else _hainan(path)
            if not rows:
                raise ValueError(f"未解析到有效行：{path}")
            key = _source_key(province, url, sha256)
            source = (await db.execute(select(GaokaoSource).where(GaokaoSource.source_key == key))).scalar_one_or_none()
            if source is None:
                source = GaokaoSource(
                    source_key=key,
                    publisher="教育部阳光高考信息平台/省级考试机构",
                    province=province,
                    year=2026,
                    dataset_type="score_band",
                    source_url=url,
                    retrieved_at=datetime.now(),
                    sha256=sha256,
                    row_count=len(rows),
                    validation_status="verified",
                    notes="官方附件逐行解析；普通类分数、人数和累计人数校验通过",
                )
                db.add(source)
                await db.flush()
            else:
                source.row_count = len(rows)
                source.sha256 = sha256
                source.validation_status = "verified"
            values = [{**row, "province": province, "year": 2026, "track": track, "source_id": source.id} for row in rows]
            stmt = insert(GaokaoScoreBand).values(values)
            stmt = stmt.on_duplicate_key_update(**{k: getattr(stmt.inserted, k) for k in values[0] if k not in {"province", "year", "track", "score"}})
            await db.execute(stmt)
            imported += len(values)
        await db.commit()
    print(f"✅ 已导入 2026 官方一分一段表 {imported} 行，覆盖 {len(SPECS)} 个省级附件口径")


if __name__ == "__main__":
    asyncio.run(main())
