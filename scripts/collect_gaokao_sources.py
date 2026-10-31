"""按来源清单下载官方高考文件，支持断点续传和离线重试。

首次运行：
  .venv/bin/python scripts/collect_gaokao_sources.py \
    --manifest data/gaokao/admission_sources.json --run-id 2026-admission

断网后继续同一个 run：
  .venv/bin/python scripts/collect_gaokao_sources.py \
    --manifest data/gaokao/admission_sources.json --run-id 2026-admission --retry-failed

只校验本地来源包、不联网：
  .venv/bin/python scripts/collect_gaokao_sources.py \
    --manifest data/gaokao/admission_sources.json --run-id 2026-admission --offline

脚本只归档原始文件，不自动把网页/PDF 解释成录取数据。解析和人工核验完成后，
再使用 scripts/import_gaokao.py 写入正式表。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx

from app.gaokao.collector import (
    atomic_write_json,
    download_source,
    initial_state,
    load_manifest,
    retry_delay,
    sha256_file,
    utc_now,
)


ROOT = Path(__file__).resolve().parents[1]


def args():
    parser = argparse.ArgumentParser(description="下载并审计官方高考数据来源包")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, default=ROOT / "data/gaokao/collection_runs")
    parser.add_argument("--offline", action="store_true", help="只检查本地文件，不发起网络请求")
    parser.add_argument("--retry-failed", action="store_true", help="重新尝试 failed 来源")
    parser.add_argument("--retry-count", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument(
        "--insecure-tls",
        action="store_true",
        help="对官方来源使用不校验证书的 TLS；仅用于本地证书链缺失场景，仍会记录并校验 SHA-256",
    )
    return parser.parse_args()


def manifest_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    options = args()
    manifest_path = options.manifest.resolve()
    sources = load_manifest(manifest_path)
    manifest_sha = manifest_hash(manifest_path)
    run_dir = options.root.resolve() / options.run_id
    state_path = run_dir / "state.json"
    raw_dir = run_dir / "raw"
    run_dir.mkdir(parents=True, exist_ok=True)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("manifest_sha256") != manifest_sha:
            raise SystemExit("manifest 已变化，不能复用旧 run；请使用新的 --run-id，避免来源错配")
    else:
        state = initial_state(manifest_sha, sources)
        atomic_write_json(state_path, state)
    # 每个 run 固化来源清单，后续清单扩展后仍可离线重放旧 run。
    snapshot_path = run_dir / "manifest.snapshot.json"
    if not snapshot_path.exists():
        atomic_write_json(snapshot_path, {"manifest_sha256": manifest_sha, "sources": sources})
    source_map = {item["source_id"]: item for item in sources}

    def save():
        state["updated_at"] = utc_now()
        atomic_write_json(state_path, state)

    if options.offline:
        checked = 0
        for source_id, item in state["sources"].items():
            artifact = item.get("artifact")
            if not artifact:
                continue
            path = run_dir / "raw" / Path(artifact).relative_to("raw") if str(artifact).startswith("raw/") else ROOT / artifact
            # 兼容旧状态中保存仓库相对路径的情况。
            if not path.exists():
                item.update({"status": "failed", "error": f"本地文件不存在：{path}", "updated_at": utc_now()})
                continue
            digest, size = sha256_file(path)
            expected = str(source_map[source_id].get("sha256") or "").lower()
            if expected and digest != expected:
                item.update({"status": "failed", "error": f"本地 SHA-256 不匹配：{digest}", "updated_at": utc_now()})
            else:
                item.update({"status": "downloaded", "sha256": digest, "bytes": size, "updated_at": utc_now()})
                checked += 1
        save()
        print(json.dumps({"run_id": options.run_id, "mode": "offline", "checked": checked, "state": str(state_path)}, ensure_ascii=False))
        return

    with httpx.Client(
        headers={"User-Agent": "Conlin-Gaokao-Official-Collector/1.0"},
        verify=not options.insecure_tls,
    ) as client:
        for source_id, source in source_map.items():
            item = state["sources"][source_id]
            if item.get("status") == "downloaded" and item.get("artifact"):
                continue
            if item.get("status") == "failed" and not options.retry_failed:
                continue
            attempts = 0
            while attempts < max(1, options.retry_count):
                attempts += 1
                try:
                    download_source(
                        source,
                        item,
                        raw_dir,
                        client,
                        timeout=options.timeout,
                        max_bytes=options.max_bytes,
                        save_state=save,
                    )
                    break
                except Exception as exc:
                    print(f"⚠️ {source_id} 第 {attempts} 次失败：{exc}")
                    if attempts < options.retry_count:
                        time.sleep(retry_delay(attempts))
            else:
                item["status"] = "failed"
                item["error"] = item.get("error") or "重试次数耗尽"
                save()

    summary = {}
    for item in state["sources"].values():
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    print(json.dumps({"run_id": options.run_id, "summary": summary, "state": str(state_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

