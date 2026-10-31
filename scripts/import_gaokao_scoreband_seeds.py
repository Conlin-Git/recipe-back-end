"""从仓库内的官方一分一段表 seed 重建 gaokao_score_bands。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-dir", type=Path, default=Path("data/gaokao/scoreband_seeds"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = args.seed_dir.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo_root), env.get("PYTHONPATH", "")]))
    for item in manifest:
        command = [
            sys.executable,
            str(repo_root / "scripts/import_gaokao.py"),
            "--input", str((root.parent / item["path"]).resolve()),
            "--dataset-type", "score_band",
            "--source-url", item["source_url"],
            "--publisher", item["publisher"],
            "--year", str(item["year"]),
            "--province", item["province"],
            "--validation-status", "verified",
            "--source-sha256", item["sha256"],
            "--source-artifact", f"seed:{item['path']}",
            "--verification-note", "从仓库官方 scoreband seed 重建；原始来源 SHA-256 见 scoreband manifest。",
        ]
        if args.dry_run:
            print(" ".join(command))
        else:
            subprocess.run(command, check=True, cwd=repo_root, env=env)
    print(f"✅ 已处理 {len(manifest)} 个官方 scoreband seed 来源")


if __name__ == "__main__":
    main()
