"""把人工从官方页面下载的原始文件挂载到已有采集 run。

适用于来源站点要求验证码/登录、或断网后在另一台机器完成下载的场景。文件只复制到
run/raw，不修改原文件；挂载时立即计算 SHA-256，后续仍需 stage + 人工 review。
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from app.gaokao.collector import atomic_write_json, load_manifest, safe_name, sha256_file, utc_now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path("data/gaokao/collection_runs"))
    args = parser.parse_args()
    sources = {item["source_id"]: item for item in load_manifest(args.manifest.resolve())}
    if args.source_id not in sources:
        raise SystemExit(f"manifest 不存在 source_id：{args.source_id}")
    source = sources[args.source_id]
    source_file = args.file.resolve()
    if not source_file.is_file():
        raise SystemExit(f"文件不存在：{source_file}")
    run_dir = args.root.resolve() / args.run_id
    state_path = run_dir / "state.json"
    if not state_path.exists():
        raise SystemExit(f"找不到 run 状态：{state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    item = state["sources"].get(args.source_id)
    if item is None:
        raise SystemExit(f"state 不存在 source_id：{args.source_id}")
    digest, size = sha256_file(source_file)
    expected = str(source.get("sha256") or "").strip().lower()
    if expected and digest != expected:
        raise SystemExit(f"SHA-256 不匹配，期望 {expected}，实际 {digest}")
    suffix = source_file.suffix.lower() or ".bin"
    destination_dir = run_dir / "raw" / safe_name(args.source_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"payload{suffix}"
    if destination.exists():
        old_digest, _ = sha256_file(destination)
        if old_digest != digest:
            raise SystemExit(f"目标文件已存在且 SHA 不同：{destination}")
    else:
        shutil.copy2(source_file, destination)
    relative = str(destination.relative_to(run_dir))
    item.update({
        "status": "downloaded",
        "artifact": relative,
        "sha256": digest,
        "bytes": size,
        "manually_attached": True,
        "updated_at": utc_now(),
        "error": None,
    })
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    print(json.dumps({"source_id": args.source_id, "artifact": relative, "sha256": digest, "bytes": size}, ensure_ascii=False))


if __name__ == "__main__":
    main()

