"""高考官方来源包采集与断点续传。

这个模块只负责把公开来源原样保存下来，不负责把网页内容直接当成可信录取数据。
下载状态采用原子 JSON 文件持久化，网络中断后可以继续同一个 run；每个来源都保留
尝试次数、错误信息、HTTP 元数据和 SHA-256，便于离线重放和审计。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import httpx


USER_AGENT = "Conlin-Gaokao-Official-Collector/1.0 (+source-audit)"
STATE_VERSION = "gaokao-collection-state-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str, fallback: str = "source") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return value[:120] or fallback


def load_manifest(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    sources = value.get("sources") if isinstance(value, dict) else value
    if not isinstance(sources, list) or not sources:
        raise ValueError("来源清单必须是非空 sources 数组")
    seen: set[str] = set()
    normalized = []
    required = {"source_id", "url", "publisher", "province", "year", "dataset_type"}
    for item in sources:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(f"来源缺少必填字段：{item}")
        source_id = str(item["source_id"]).strip()
        url = str(item["url"]).strip()
        if source_id in seen:
            raise ValueError(f"source_id 重复：{source_id}")
        if not url.startswith(("https://", "http://")):
            raise ValueError(f"来源 URL 必须是 HTTP(S)：{url}")
        seen.add(source_id)
        normalized.append({**item, "source_id": source_id, "url": url})
    return normalized


def initial_state(manifest_sha256: str, sources: Iterable[dict[str, Any]]) -> dict[str, Any]:
    now = utc_now()
    return {
        "state_version": STATE_VERSION,
        "manifest_sha256": manifest_sha256,
        "created_at": now,
        "updated_at": now,
        "sources": {
            source["source_id"]: {
                "status": "pending",
                "attempts": 0,
                "updated_at": now,
                "error": None,
                "artifact": None,
                "sha256": None,
                "bytes": 0,
                "http_status": None,
                "content_type": None,
            }
            for source in sources
        },
    }


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _extension(source: dict[str, Any], content_type: str | None) -> str:
    explicit = str(source.get("extension") or "").strip().lower()
    if explicit:
        return explicit if explicit.startswith(".") else f".{explicit}"
    suffix = Path(urlparse(source["url"]).path).suffix.lower()
    if suffix in {".pdf", ".xls", ".xlsx", ".csv", ".json", ".html", ".htm", ".zip"}:
        return suffix
    content_type = (content_type or "").split(";", 1)[0].lower()
    return {
        "application/pdf": ".pdf",
        "application/json": ".json",
        "text/csv": ".csv",
        "text/html": ".html",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }.get(content_type, ".bin")


def download_source(
    source: dict[str, Any],
    state_item: dict[str, Any],
    raw_dir: Path,
    client: httpx.Client,
    *,
    timeout: float = 30,
    max_bytes: int = 100 * 1024 * 1024,
    chunk_size: int = 1024 * 1024,
    checkpoint_bytes: int = 4 * 1024 * 1024,
    save_state: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """下载一个来源，支持 Range 续传；成功返回 downloaded 状态。"""
    source_id = source["source_id"]
    source_dir = raw_dir / safe_name(source_id)
    source_dir.mkdir(parents=True, exist_ok=True)
    part = source_dir / "payload.part"
    state_item["attempts"] = int(state_item.get("attempts") or 0) + 1
    state_item["status"] = "downloading"
    state_item["error"] = None
    state_item["updated_at"] = utc_now()
    if save_state:
        save_state()

    existing = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    try:
        with client.stream("GET", source["url"], headers=headers, follow_redirects=True, timeout=timeout) as response:
            state_item["http_status"] = response.status_code
            state_item["content_type"] = response.headers.get("content-type")
            if response.status_code not in {200, 206}:
                # 保存错误响应片段，便于判断反爬、权限、临时故障，而不把它当成来源文件。
                error_path = source_dir / f"error-{state_item['attempts']}.bin"
                error_path.write_bytes(response.read()[:1024 * 1024])
                state_item["error_artifact"] = str(error_path.relative_to(raw_dir.parent))
                raise RuntimeError(f"HTTP {response.status_code}")
            append = existing > 0 and response.status_code == 206
            if not append and existing:
                part.unlink()
                existing = 0
            mode = "ab" if append else "wb"
            size = existing
            with part.open(mode) as handle:
                for chunk in response.iter_bytes(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise RuntimeError(f"文件超过大小上限 {max_bytes} bytes")
                    handle.write(chunk)
                    if save_state and size - int(state_item.get("bytes") or 0) >= checkpoint_bytes:
                        state_item["bytes"] = size
                        state_item["updated_at"] = utc_now()
                        save_state()
                handle.flush()
                os.fsync(handle.fileno())
            digest, size = sha256_file(part)
            expected = str(source.get("sha256") or "").strip().lower()
            if expected and digest != expected:
                raise RuntimeError(f"SHA-256 不匹配，期望 {expected}，实际 {digest}")
            payload = source_dir / f"payload{_extension(source, response.headers.get('content-type'))}"
            os.replace(part, payload)
            (source_dir / "headers.json").write_text(
                json.dumps(dict(response.headers), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            state_item.update({
                "status": "downloaded",
                "artifact": str(payload.relative_to(raw_dir.parent)),
                "sha256": digest,
                "bytes": size,
                "updated_at": utc_now(),
            })
            if save_state:
                save_state()
            return state_item
    except Exception as exc:
        state_item.update({"status": "failed", "error": str(exc), "updated_at": utc_now()})
        if save_state:
            save_state()
        raise


def retry_delay(attempt: int) -> float:
    return min(60.0, 2 ** max(0, attempt - 1))

