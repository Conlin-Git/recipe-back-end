"""把已下载的官方来源包解析到 staging，不直接写正式数据库。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.gaokao.collector import atomic_write_json, load_manifest, utc_now
from app.gaokao.staging import normalize_rows, read_rows


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, default=ROOT / "data/gaokao/collection_runs")
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    sources = {s["source_id"]: s for s in load_manifest(manifest_path)}
    run_dir = args.root.resolve() / args.run_id
    state_path = run_dir / "state.json"
    if not state_path.exists():
        raise SystemExit(f"找不到采集状态：{state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    stage_dir = run_dir / "staging"
    stage_dir.mkdir(parents=True, exist_ok=True)
    staged_count = rejected_count = 0
    for source_id, source in sources.items():
        item = state["sources"].get(source_id)
        if not item or item.get("status") != "downloaded" or source.get("dataset_type") != "admission":
            continue
        artifact = item.get("artifact")
        if not artifact:
            continue
        artifact_path = run_dir / artifact if str(artifact).startswith("raw/") else ROOT / artifact
        try:
            rows = read_rows(artifact_path)
            staged, rejected = normalize_rows(rows, source=source, source_locator=f"artifact:{artifact}")
            for row in staged:
                row["source_sha256"] = item.get("sha256")
                row["source_artifact"] = artifact
            output = stage_dir / f"{source_id}.jsonl"
            with output.open("w", encoding="utf-8") as handle:
                for row in staged:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            report = {
                "source_id": source_id,
                "artifact": artifact,
                "created_at": utc_now(),
                "status": "pending_review" if staged else "rejected",
                "raw_rows": len(rows),
                "staged_rows": len(staged),
                "rejected_rows": len(rejected),
                "rejected": rejected[:100],
                "mapping": "保守字段映射；正式入库前必须人工核验",
            }
            (stage_dir / f"{source_id}.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            item.update({
                "stage_status": report["status"],
                "stage_output": str(output.relative_to(run_dir)),
                "stage_report": str((stage_dir / f"{source_id}.report.json").relative_to(run_dir)),
                "stage_error": None,
                "updated_at": utc_now(),
            })
            staged_count += len(staged)
            rejected_count += len(rejected)
        except Exception as exc:
            item.update({"stage_status": "stage_failed", "stage_error": str(exc), "updated_at": utc_now()})
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    print(json.dumps({"run_id": args.run_id, "staged_rows": staged_count, "rejected_rows": rejected_count, "state": str(state_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

