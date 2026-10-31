"""导入仓库内按官方来源导出的 verified seed，供部署环境重建高考表。"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed-dir", type=Path, default=Path("data/gaokao/seeds"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = args.seed_dir.resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        command = [
            sys.executable,
            "scripts/import_gaokao.py",
            "--input", str((root.parent / item["path"]).resolve()),
            "--dataset-type", "admission",
            "--source-url", item["source_url"],
            "--publisher", item["publisher"],
            "--year", str(item["year"]),
            "--province", item["province"],
            "--validation-status", "verified",
            "--confirm-official",
            "--verified-by", "seed manifest official review",
            "--verification-note", "从仓库 verified seed 重建；原始来源 SHA-256 见 source manifest。",
            "--source-sha256", item["sha256"],
            "--source-artifact", f"seed:{item['path']}",
        ]
        if args.dry_run:
            print(" ".join(command))
        else:
            subprocess.run(command, check=True)
    print(f"✅ 已处理 {len(manifest)} 个 verified seed 来源")


if __name__ == "__main__":
    main()
