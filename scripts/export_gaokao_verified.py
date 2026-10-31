"""把已核验的院校专业组数据导出为可部署的按来源 JSONL seed。"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from sqlalchemy import select

from app.database.mysql import AsyncSessionLocal
from app.models.gaokao import GaokaoAdmission, GaokaoSource


def _args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/gaokao/seeds"))
    return parser.parse_args()


async def main(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    async with AsyncSessionLocal() as db:
        source_ids = (
            await db.execute(
                select(GaokaoAdmission.source_id)
                .where(GaokaoAdmission.verification_status == "verified")
                .distinct()
            )
        ).scalars().all()
        for source_id in source_ids:
            source = await db.get(GaokaoSource, source_id)
            if source is None:
                raise ValueError(f"正式记录引用了不存在的 source_id={source_id}")
            rows = (
                await db.execute(
                    select(GaokaoAdmission)
                    .where(
                        GaokaoAdmission.source_id == source_id,
                        GaokaoAdmission.verification_status == "verified",
                    )
                    .order_by(GaokaoAdmission.id)
                )
            ).scalars().all()
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source.source_url.rsplit("/", 1)[-1])[:80]
            path = output / f"source-{source_id}-{safe or 'admission'}.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    payload = {column.name: getattr(row, column.name) for column in GaokaoAdmission.__table__.columns}
                    payload["source_sha256"] = source.sha256
                    payload["source_artifact"] = (
                        (re.search(r"artifact=([^;]+)", source.notes or "") or [None, ""])[1]
                    )
                    handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            manifest.append({
                "source_id": source.id,
                "path": str(path.relative_to(output.parent)),
                "publisher": source.publisher,
                "province": source.province,
                "year": source.year,
                "source_url": source.source_url,
                "sha256": source.sha256,
                "rows": len(rows),
                "validation_status": source.validation_status,
            })
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sources": len(manifest), "rows": sum(item["rows"] for item in manifest), "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    args = _args()
    asyncio.run(main(args.output.resolve()))
