"""导入经核验的官方高考数据到 MySQL。

示例：
  .venv/bin/python scripts/import_gaokao.py --input data/gaokao/2026-gd-score_band.csv \
    --dataset-type score_band --source-url https://eea.gd.gov.cn/... \
    --publisher 广东省教育考试院 --year 2026 --province 广东 --dry-run

输入文件必须包含 province/year/track，其他字段按 dataset-type 校验；不提供来源 URL、发布机构
或 SHA 校验时拒绝写库。admission 默认进入 pending 审核态，只有显式 --confirm-official 且填写
--verified-by 才会进入 verified；指南服务只使用 verified 数据。

面向“某招生省份中的指定院校所在地”查询时，admission 导入必须加
--require-school-province，逐行提供 school_province（不能从 province 推断）。
"""
import argparse
import asyncio
import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from app.database.mysql import AsyncSessionLocal
from app.models.gaokao import GaokaoAdmission, GaokaoCutoff, GaokaoScoreBand, GaokaoSource


def _args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--dataset-type", choices=["score_band", "cutoff", "admission"], required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--province", required=True)
    parser.add_argument("--school-province", default="", help="院校所在地省份；不得用 --province 自动推断")
    parser.add_argument("--require-school-province", action="store_true", help="admission 导入时强制每行提供院校所在地省份")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--license-note", default="")
    parser.add_argument("--validation-status", choices=["pending", "verified"], default="pending")
    parser.add_argument("--verified-by", default="")
    parser.add_argument("--verification-note", default="")
    parser.add_argument("--confirm-official", action="store_true", help="确认原始文件已人工对照官方来源")
    parser.add_argument("--source-sha256", default="", help="原始官方文件 SHA-256；admission 正式导入必填")
    parser.add_argument("--source-artifact", default="", help="原始文件在 collection run 中的相对路径")
    return parser.parse_args()


def _read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else value.get("rows", [])
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if path.suffix.lower() in {".xlsx", ".xls"}:
        import pandas as pd
        return pd.read_excel(path).fillna("").to_dict(orient="records")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _int(value, *, name: str, required: bool = False):
    if value in (None, ""):
        if required:
            raise ValueError(f"缺少必填数值字段：{name}")
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"字段 {name} 不是整数：{value}") from exc


def _common(row: dict, args) -> dict:
    province = str(row.get("province") or args.province).strip()
    year = _int(row.get("year") or args.year, name="year", required=True)
    track = str(row.get("track") or "").strip()
    if province != args.province or year != args.year or not track:
        raise ValueError(f"第 {row.get('_row', '?')} 行的 province/year/track 与导入参数不一致")
    return {"province": province, "year": year, "track": track}


def _normalize(rows: list[dict], args):
    result = []
    seen = set()
    for index, raw in enumerate(rows, 2):
        raw["_row"] = index
        common = _common(raw, args)
        if args.dataset_type == "score_band":
            item = {
                **common,
                "score": _int(raw.get("score"), name="score", required=True),
                "candidate_count": _int(raw.get("candidate_count"), name="candidate_count") or 0,
                "cumulative_rank": _int(raw.get("cumulative_rank"), name="cumulative_rank", required=True),
                "rank_start": _int(raw.get("rank_start"), name="rank_start"),
                "rank_end": _int(raw.get("rank_end"), name="rank_end"),
            }
            key = (*common.values(), item["score"])
        elif args.dataset_type == "cutoff":
            item = {
                **common,
                "batch": str(raw.get("batch") or "本科").strip(),
                "cutoff_type": str(raw.get("cutoff_type") or "control").strip(),
                "score": _int(raw.get("score"), name="score", required=True),
                "note": str(raw.get("note") or "").strip() or None,
            }
            key = (*common.values(), item["batch"], item["cutoff_type"])
        else:
            item = {
                **common,
                "batch": str(raw.get("batch") or "本科").strip(),
                "admission_round": str(raw.get("admission_round") or "regular").strip(),
                "record_kind": str(raw.get("record_kind") or "major_admit").strip(),
                "school_province": str(raw.get("school_province") or args.school_province).strip() or None,
                "university_code": str(raw.get("university_code") or "").strip(),
                "university_name": str(raw.get("university_name") or "").strip(),
                "group_code": str(raw.get("group_code") or "").strip(),
                "group_name": str(raw.get("group_name") or "").strip(),
                "major_code": str(raw.get("major_code") or "").strip(),
                "major_name": str(raw.get("major_name") or "").strip(),
                "subject_requirements": str(raw.get("subject_requirements") or "").strip() or None,
                "min_score": _int(raw.get("min_score"), name="min_score"),
                "min_rank": _int(raw.get("min_rank"), name="min_rank"),
                "avg_score": _int(raw.get("avg_score"), name="avg_score"),
                "max_score": _int(raw.get("max_score"), name="max_score"),
                "plan_count": _int(raw.get("plan_count"), name="plan_count"),
                "admission_count": _int(raw.get("admission_count"), name="admission_count"),
                "submission_count": _int(raw.get("submission_count"), name="submission_count"),
                "tie_breaker": str(raw.get("tie_breaker") or "").strip() or None,
                "plan_type": str(raw.get("plan_type") or "").strip() or None,
                "province_code": str(raw.get("province_code") or "").strip() or None,
                "source_locator": str(raw.get("source_locator") or "").strip() or None,
                "source_sha256": str(raw.get("source_sha256") or "").strip().lower() or None,
                "source_artifact": str(raw.get("source_artifact") or "").strip() or None,
                "raw_record_hash": str(raw.get("raw_record_hash") or "").strip() or None,
                "verification_status": str(raw.get("verification_status") or "pending").strip(),
                "verified_at": None,
                "verification_note": str(raw.get("verification_note") or "").strip() or None,
                "admission_status": str(raw.get("admission_status") or "admitted").strip(),
                "evidence_note": str(raw.get("evidence_note") or "").strip() or None,
            }
            if item["record_kind"] not in {"group投档", "group_admit", "major_admit"}:
                raise ValueError(f"第 {index} 行 record_kind 不支持：{item['record_kind']}")
            if args.require_school_province and not item["school_province"]:
                raise ValueError(f"第 {index} 行缺少 school_province；不能把招生省份 province 当作院校所在地")
            if not item["university_name"] or (
                item["min_rank"] is None
                and item["min_score"] is None
                and item["admission_status"] != "not_reported"
            ):
                raise ValueError(f"第 {index} 行缺少 university_name 以及 min_rank/min_score")
            key = (*common.values(), item["batch"], item["record_kind"], item["admission_round"], item["university_code"], item["group_code"], item["major_code"])
        if key in seen:
            raise ValueError(f"第 {index} 行与前面记录重复：{key}")
        seen.add(key)
        result.append(item)
    if not result:
        raise ValueError("输入文件没有数据行")
    return result


async def _run(args):
    path = Path(args.input).resolve()
    raw = path.read_bytes()
    rows = _normalize(_read_rows(path), args)
    if args.dataset_type == "admission" and args.validation_status == "verified":
        if not args.confirm_official or not args.verified_by.strip():
            raise ValueError("admission 进入 verified 必须同时提供 --confirm-official 和 --verified-by")
    sha256 = hashlib.sha256(raw).hexdigest()
    source_sha256 = args.source_sha256.strip().lower() or str(rows[0].get("source_sha256") or "").strip().lower()
    if args.dataset_type == "admission" and not source_sha256:
        raise ValueError("admission 必须提供原始官方文件 SHA-256（--source-sha256 或 staging source_sha256）")
    if source_sha256 and (len(source_sha256) != 64 or any(c not in "0123456789abcdef" for c in source_sha256)):
        raise ValueError(f"source_sha256 格式错误：{source_sha256}")
    if any(row.get("source_sha256") and row["source_sha256"] != source_sha256 for row in rows):
        raise ValueError("staging 行的 source_sha256 不一致")
    print(json.dumps({"rows": len(rows), "sha256": sha256, "dry_run": args.dry_run}, ensure_ascii=False))
    if args.dry_run:
        return
    async with AsyncSessionLocal() as db:
        source_key = f"{args.publisher}:{args.province}:{args.year}:{args.dataset_type}:{(source_sha256 or sha256)[:16]}"
        source = (await db.execute(select(GaokaoSource).where(GaokaoSource.source_key == source_key))).scalar_one_or_none()
        if source and source.sha256 != (source_sha256 or sha256):
            raise ValueError(f"source_key 已存在但原始文件 SHA-256 不一致：{source_key}")
        if source is None:
            source = GaokaoSource(
                source_key=source_key,
                publisher=args.publisher,
                province=args.province,
                year=args.year,
                dataset_type=args.dataset_type,
                source_url=args.source_url,
                retrieved_at=datetime.now(),
                sha256=source_sha256 or sha256,
                row_count=len(rows),
                validation_status=args.validation_status,
                license_note=args.license_note or None,
                notes=(f"artifact={args.source_artifact}; verified_by={args.verified_by}; {args.verification_note}" if args.verified_by or args.source_artifact else args.verification_note) or None,
            )
            db.add(source)
            await db.flush()
        elif args.validation_status == "verified":
            source.validation_status = "verified"
            verification_note = f"verified_by={args.verified_by}; {args.verification_note}" if args.verified_by else args.verification_note
            artifact_note = f"artifact={args.source_artifact}; " if args.source_artifact else ""
            source.notes = f"{artifact_note}{verification_note}" or source.notes
        model = {"score_band": GaokaoScoreBand, "cutoff": GaokaoCutoff, "admission": GaokaoAdmission}[args.dataset_type]
        for item in rows:
            item["source_id"] = source.id
            if args.dataset_type == "admission":
                item["verification_status"] = args.validation_status
                item["verification_note"] = args.verification_note or None
                item["verified_at"] = datetime.now() if args.validation_status == "verified" else None
            item.pop("source_sha256", None)
            item.pop("source_artifact", None)
        stmt = insert(model).values(rows)
        if args.dataset_type == "score_band":
            stmt = stmt.on_duplicate_key_update(**{k: getattr(stmt.inserted, k) for k in rows[0] if k not in {"province", "year", "track", "score"}})
        elif args.dataset_type == "cutoff":
            stmt = stmt.on_duplicate_key_update(**{k: getattr(stmt.inserted, k) for k in rows[0] if k not in {"province", "year", "track", "batch", "cutoff_type"}})
        else:
            stmt = stmt.on_duplicate_key_update(**{k: getattr(stmt.inserted, k) for k in rows[0] if k not in {"province", "year", "track", "batch", "record_kind", "admission_round", "university_code", "group_code", "major_code"}})
        await db.execute(stmt)
        await db.commit()
        print(f"✅ 已导入 {len(rows)} 行，source_id={source.id}")


if __name__ == "__main__":
    asyncio.run(_run(_args()))
