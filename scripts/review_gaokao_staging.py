"""人工核验 staging 来源并生成可导入的 verified JSONL。

程序不会替用户判断页面含义。审核人必须打开原始文件，确认省份、年份、批次、科类、
院校/专业组/专业和每个数值后，显式传 --approve；原始 staging 文件不被覆盖。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--note", required=True)
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()
    if not args.approve:
        raise SystemExit("必须显式提供 --approve；未批准不会生成 verified 文件")
    run_dir = args.run_dir.resolve()
    report_path = run_dir / "staging" / f"{args.source_id}.report.json"
    input_path = run_dir / "staging" / f"{args.source_id}.jsonl"
    if not report_path.exists() or not input_path.exists():
        raise SystemExit(f"找不到 staging 文件：{args.source_id}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "pending_review":
        raise SystemExit(f"来源当前不能审核：{report.get('status')}")
    rows = [json.loads(line) for line in input_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise SystemExit("没有可审核行")
    verified_at = datetime.now(timezone.utc).isoformat()
    for row in rows:
        row["verification_status"] = "verified"
        row["verified_by"] = args.reviewer
        row["verified_at"] = verified_at
        row["verification_note"] = args.note
    output_path = run_dir / "staging" / f"{args.source_id}.verified.jsonl"
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    report.update({
        "status": "verified",
        "verified_by": args.reviewer,
        "verified_at": verified_at,
        "verification_note": args.note,
        "verified_output": str(output_path.relative_to(run_dir)),
    })
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state_path = run_dir / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        item = state["sources"].get(args.source_id)
        if item is not None:
            item.update({"stage_status": "verified", "verified_output": str(output_path.relative_to(run_dir)), "verified_by": args.reviewer, "updated_at": verified_at})
        state["updated_at"] = verified_at
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_id": args.source_id, "verified_rows": len(rows), "output": str(output_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

