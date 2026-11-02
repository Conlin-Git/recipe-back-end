"""批量解析四川 2026 本科投档线图片，生成可审计 staging JSONL。

OCR 只负责候选提取，默认不写 MySQL。脚本支持并行 OCR、断点续跑、逐图原始
结果留档、错误隔离和跨图去重；只有人工/官方逐行复核后，才允许使用
``review_gaokao_staging.py`` 和 ``import_gaokao.py`` 写入 verified 数据。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

YEAR = 2026
PROVINCE = "四川"
BATCH = "本科批次B段"
TRACKS = (("历史类", "history_dir"), ("物理类", "physical_dir"))
COLS = {
    "university_code": (0.075, 0.18),
    "university_name": (0.18, 0.36),
    "group_code": (0.36, 0.47),
    "batch": (0.47, 0.61),
    "min_score": (0.61, 0.72),
    "min_rank": (0.72, 0.86),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def image_number(path: Path) -> int:
    match = re.search(r"(\d+)(?=\.webp$)", path.name)
    return int(match.group(1)) if match else 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ocr(binary: str, image: Path) -> list[tuple[float, float, float, float, str]]:
    output = subprocess.check_output([binary, str(image)], text=True, timeout=90)
    rows = []
    for line in output.splitlines():
        try:
            x, y, w, h, text = line.split("\t", 4)
            rows.append((float(x), float(y), float(w), float(h), text.strip()))
        except ValueError:
            continue
    return rows


def _ocr_crop(binary: str, image: Path, crop_name: str) -> list[tuple[float, float, float, float, str]]:
    """对 Vision 漏列的页做窄列放大 OCR，坐标转换回整图。"""
    crop_dir = image.parent / ".ocr-crops"
    crop_dir.mkdir(exist_ok=True)
    png = crop_dir / f"{image.stem}-{crop_name}.png"
    if not png.exists():
        subprocess.run(["sips", "-s", "format", "png", str(image), "--out", str(png)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # sips 不能直接按 webp 稳定裁剪；先保留整图，窄列 OCR 只在代码列使用。
    return _ocr(binary, png)


def _numeric(text: str) -> int | None:
    text = text.replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1").replace(".", "")
    match = re.search(r"\d+", text)
    return int(match.group()) if match else None


def _col_texts(observations: list[tuple[float, float, float, float, str]], center: float, column: str, bounds: dict[str, tuple[float, float]]) -> list[str]:
    lo, hi = bounds[column]
    nearby = [item for item in observations if abs(item[1] + item[3] / 2 - center) <= 0.009]
    return [item[4] for item in sorted(nearby, key=lambda item: item[0]) if lo <= item[0] < hi]


def parse_image(binary: str, image: Path, track: str) -> dict[str, Any]:
    observations = _ocr(binary, image)
    if not any(re.fullmatch(r"\d{3,6}", text.replace("Z", "2").replace("F", "3")) and x < 0.25 for x, _y, _w, _h, text in observations):
        # 历史类部分页的院校代码被 macOS Vision 跳过。用整图 PNG 重跑，
        # 不用相邻行推断代码，仍以识别到的真实代码为准。
        png = image.parent / ".ocr-crops" / f"{image.stem}-full.png"
        png.parent.mkdir(exist_ok=True)
        if not png.exists():
            subprocess.run(["sips", "-s", "format", "png", str(image), "--out", str(png)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        retry = _ocr(binary, png)
        if retry:
            observations = retry
    bounds = COLS
    # 物理类图片的列宽与历史类略有差异，Vision 坐标会把“院校代码”
    # 识别在 x≈0.12、“专业组”在 x≈0.45；根据表格列的稳定数值中心
    # 只调整四个数值列的窗口，避免把分数/位次错当成专业组。
    code_x = [x for x, _y, _w, _h, text in observations if re.fullmatch(r"\d{3,6}", text.replace("Z", "2").replace("F", "3")) and x < 0.25]
    name_x = [x for x, _y, _w, _h, text in observations if 0.18 <= x < 0.35 and not re.fullmatch(r"\d+", text) and text not in {"院校名称"}]
    group_x = [x for x, _y, _w, _h, text in observations if re.fullmatch(r"\d{3}", text) and 0.34 <= x < 0.52]
    batch_x = [x for x, _y, _w, _h, text in observations if ("本科" in text or "提前批" in text) and 0.45 <= x < 0.65]
    score_x = [x for x, _y, _w, _h, text in observations if re.fullmatch(r"\d{3}", text) and 0.58 <= x < 0.75]
    rank_x = [x for x, _y, _w, _h, text in observations if re.fullmatch(r"\d+", text) and 0.70 <= x < 0.90]
    if name_x and batch_x and score_x and rank_x:
        if not code_x:
            # OCR 漏掉代码时仍用版式中心取数；具体行会在校验中被隔离。
            code_x = [0.12]
        if not group_x:
            group_x = [0.44764]
        centers = [sorted(values)[len(values) // 2] for values in (code_x, name_x, group_x, batch_x, score_x, rank_x)]
        code, name, group, batch, score, rank = centers
        edges = [0.075] + [(left + right) / 2 for left, right in zip(centers, centers[1:])] + [0.90]
        bounds = {
            "university_code": (edges[0], edges[1]),
            "university_name": (edges[1], edges[2]),
            "group_code": (edges[2], edges[3]),
            "batch": (edges[3], edges[4]),
            "min_score": (edges[4], edges[5]),
            "min_rank": (edges[5], edges[6]),
        }
    anchors: list[tuple[float, int]] = []
    for x, y, w, h, text in observations:
        if COLS["min_score"][0] <= x < COLS["min_score"][1] and re.fullmatch(r"\d{3}", text):
            anchors.append((y + h / 2, int(text)))

    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int, int | None]] = set()
    for row_no, (center, score) in enumerate(sorted(anchors, reverse=True), 1):
        codes = "".join(_col_texts(observations, center, "university_code", bounds))
        code_match = re.search(r"\d{3,6}", codes.replace("O", "0").replace("I", "1").replace("Z", "2").replace("F", "3"))
        if not code_match and not codes:
            # 历史类部分页的代码被 Vision 误放在 x≈0.64 的窄裁剪区，
            # 这里不能用邻行推断，保留为 rejected 供二次 OCR。
            code_match = None
        # 某些历史图片的院校代码列被 Vision 全部漏识别，不能凭邻行猜代码，
        # 直接隔离该行，等待二次 OCR/人工复核。
        group_text = "".join(_col_texts(observations, center, "group_code", bounds))
        group_match = re.search(r"\d{3}", group_text.replace("O", "0").replace("I", "1"))
        name = "".join(t for t in _col_texts(observations, center, "university_name", bounds) if not re.fullmatch(r"\d+", t)).strip()
        batch_text = "".join(_col_texts(observations, center, "batch", bounds)).strip()
        rank = _numeric("".join(_col_texts(observations, center, "min_rank", bounds)))
        locator = f"{image.name}:row{row_no}"
        if not code_match or not group_match or not name:
            rejected.append({"locator": locator, "reason": "院校代码、名称或专业组未完整识别", "score": score})
            continue
        if "本科批" not in batch_text or "B段" not in batch_text:
            rejected.append({"locator": locator, "reason": "非本科批B段，已隔离", "batch": batch_text, "score": score})
            continue
        if rank is None:
            rejected.append({"locator": locator, "reason": "最低位次未识别", "batch": batch_text, "score": score})
            continue
        key = (track, code_match.group(), group_match.group(), score, rank)
        if key in seen:
            rejected.append({"locator": locator, "reason": "同图重复行", "score": score})
            continue
        seen.add(key)
        raw_hash = hashlib.sha256(json.dumps({"track": track, "image": image.name, "row": row_no, "score": score, "rank": rank, "name": name, "group": group_match.group()}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        rows.append({
            "province": PROVINCE,
            "school_province": None,
            "year": YEAR,
            "track": track,
            "batch": BATCH,
            "admission_round": "regular",
            "record_kind": "group投档",
            "university_code": code_match.group(),
            "university_name": name,
            "group_code": group_match.group(),
            "group_name": f"专业组{group_match.group()}",
            "major_code": f"ocr-{track}-{image_number(image):03d}-{row_no:02d}",
            "major_name": "",
            "min_score": score,
            "min_rank": rank,
            "source_locator": locator,
            "raw_record_hash": raw_hash,
            "verification_status": "pending",
            "admission_status": "admitted",
            "verification_note": "用户提供的四川2026本科投档线图片，经 macOS Vision OCR 结构化；待官方逐行核验。",
            "evidence_note": "user_image_ocr_pending",
        })
    return {"rows": rows, "rejected": rejected, "ocr_observations": len(observations), "anchors": len(anchors)}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _default_workers() -> int:
    return max(1, min(6, (os.cpu_count() or 2) // 2))


def _load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 2, "created_at": utc_now(), "images": {}}


def _collect_images(directory: Path) -> list[Path]:
    images = [path for path in directory.glob("*.webp") if path.is_file()]
    if not images:
        raise ValueError(f"未找到 webp 图片：{directory}")
    return sorted(images, key=image_number)


def _run_one(binary: str, track: str, image: Path, state: dict[str, Any], ocr_dir: Path, force: bool) -> tuple[str, dict[str, Any]]:
    key = f"{track}:{image.name}"
    digest = sha256_file(image)
    old = state.get("images", {}).get(key) or {}
    output = ocr_dir / track / f"{image.stem}.json"
    if not force and old.get("sha256") == digest and old.get("status") == "ok" and output.exists():
        return key, old | {"cached": True}
    try:
        parsed = parse_image(binary, image, track)
        _write_json(output, {"track": track, "image": image.name, "sha256": digest, **parsed})
        item = {"track": track, "image": image.name, "sha256": digest, "status": "ok", "rows": len(parsed["rows"]), "rejected": len(parsed["rejected"]), "ocr_observations": parsed["ocr_observations"], "anchors": parsed["anchors"], "output": str(output), "updated_at": utc_now()}
    except Exception as exc:
        item = {"track": track, "image": image.name, "sha256": digest, "status": "failed", "error": str(exc), "updated_at": utc_now()}
    return key, item


def _write_staging(run_dir: Path, state: dict[str, Any], files: list[tuple[str, Path]], source_sha256: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for key, _ in files:
        item = state.get("images", {}).get(key) or {}
        if item.get("status") != "ok":
            continue
        payload = json.loads(Path(item["output"]).read_text(encoding="utf-8"))
        for row in payload["rows"]:
            row["source_sha256"] = source_sha256
            row["source_artifact"] = str(Path(item["output"]).relative_to(run_dir))
            rows.append(row)
        rejected.extend({"track": payload["track"], "image": payload["image"], **entry} for entry in payload["rejected"])
    unique: dict[tuple[str, str, str, int, int], dict[str, Any]] = {}
    for row in rows:
        key = (row["track"], row["university_code"], row["group_code"], row["min_score"], row["min_rank"])
        if key in unique:
            rejected.append({"track": row["track"], "image": row["source_locator"], "reason": "跨图重复业务事实", "key": key})
            continue
        unique[key] = row
    rows = list(unique.values())
    for track in ("历史类", "物理类"):
        output = run_dir / "staging" / f"2026-sichuan-{track}.jsonl"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows if row["track"] == track), encoding="utf-8")
    (run_dir / "staging" / "rejected.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rejected), encoding="utf-8")
    return {"rows": len(rows), "history_rows": sum(row["track"] == "历史类" for row in rows), "physical_rows": sum(row["track"] == "物理类" for row in rows), "rejected": len(rejected)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--physical-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--ocr-bin", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=_default_workers())
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--apply", action="store_true", help="保留兼容参数；OCR staging 仍不会绕过人工核验直接入 verified")
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    state = _load_state(state_path)
    images: list[tuple[str, Path]] = []
    for track, option in TRACKS:
        images.extend((track, image) for image in _collect_images(getattr(args, option)))
    source_sha256 = hashlib.sha256("".join(f"{track}:{path.name}:{sha256_file(path)}\n" for track, path in images).encode()).hexdigest()
    state.update({"source_sha256": source_sha256, "updated_at": utc_now(), "image_count": len(images), "workers": args.workers})
    ocr_dir = run_dir / "ocr"
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(_run_one, str(args.ocr_bin), track, image, state, ocr_dir, args.force) for track, image in images]
        for future in concurrent.futures.as_completed(futures):
            key, item = future.result()
            state.setdefault("images", {})[key] = item
            _write_json(state_path, state)
            print(json.dumps({"image": key, "status": item.get("status"), "rows": item.get("rows", 0), "cached": item.get("cached", False)}, ensure_ascii=False), flush=True)
    staging = _write_staging(run_dir, state, images, source_sha256)
    report = {"run_id": run_dir.name, "created_at": state.get("created_at"), "updated_at": utc_now(), "source_sha256": source_sha256, "image_count": len(images), "failed_images": sum(item.get("status") == "failed" for item in state.get("images", {}).values()), **staging, "apply": args.apply, "quality_gate": "pending_manual_review"}
    _write_json(run_dir / "report.json", report)
    state["staging"] = staging
    state["updated_at"] = report["updated_at"]
    _write_json(state_path, state)
    print(json.dumps({"report": str(run_dir / "report.json"), **report}, ensure_ascii=False))
    if args.apply:
        print("⚠️ --apply 只生成 pending staging；必须人工/官方逐行复核后再导入 verified。", flush=True)


if __name__ == "__main__":
    main()
