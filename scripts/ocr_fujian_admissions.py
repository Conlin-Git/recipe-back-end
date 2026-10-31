"""从福建省教育考试院扫描投档 PDF 生成双分辨率 OCR 交叉审计结果。

该工具只生成候选 JSONL 和审计报告，不直接写正式库。仅保留两种分辨率同时识别到的
院校代码、专业组代码、最低分完全一致的记录；其余行必须人工核验后另行处理。
采集机需要 macOS Vision、Swift 和 poppler；服务器部署使用已导出的 verified seed，
不需要这些工具。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


def _ocr_binary(explicit: str | None) -> str:
    if explicit:
        return explicit
    configured = os.environ.get("VISION_OCR_BIN")
    if configured:
        return configured
    root = Path(__file__).resolve().parent
    target = Path(tempfile.gettempdir()) / "conlin-vision-ocr"
    if not target.exists():
        subprocess.run(["swiftc", str(root / "vision_ocr.swift"), "-o", str(target)], check=True)
    return str(target)


def _pages(pdf: Path) -> int:
    text = subprocess.check_output(["pdfinfo", str(pdf)], text=True)
    match = re.search(r"^Pages:\s+(\d+)", text, re.MULTILINE)
    if not match:
        raise ValueError("PDF 没有可识别的页数")
    return int(match.group(1))


def _page_rows(pdf: Path, page: int, dpi: int, work: Path, ocr: str) -> list[dict]:
    stem = work / f"p{page:03d}_{dpi}"
    subprocess.run(
        ["pdftoppm", "-f", str(page), "-l", str(page), "-png", "-r", str(dpi), "-singlefile", str(pdf), str(stem)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    observations = []
    for line in subprocess.check_output([ocr, str(stem) + ".png"], text=True).splitlines():
        try:
            x, y, width, height, text = line.split("\t", 4)
            observations.append((float(x), float(y), float(width), float(height), text.strip()))
        except ValueError:
            continue
    rows = []
    for score_x, score_y, _, score_height, score_text in observations:
        score_center = score_y + score_height / 2
        if not 0.78 <= score_x <= 0.86 or not re.fullmatch(r"\d{3}", score_text):
            continue
        nearby = [item for item in observations if abs(item[1] + item[3] / 2 - score_center) <= 0.008]
        left = sorted((item for item in nearby if 0.18 <= item[0] < 0.58), key=lambda item: item[0])
        codes, names = [], []
        for _, _, _, _, text in left:
            match = re.search(r"(?<!\d)(\d{4,6})(?!\d)", text)
            if match:
                codes.append(match.group(1))
                text = text[: match.start()] + text[match.end() :]
            text = text.strip(" /|—-")
            if text and not text.isdigit():
                names.append(text)
        groups = []
        for _, _, _, _, text in sorted((item for item in nearby if 0.56 <= item[0] < 0.80), key=lambda item: item[0]):
            match = re.search(r"(?<!\d)(\d{3})(?!\d)", text)
            if match:
                groups.append(match.group(1))
        if not codes or not groups:
            continue
        rows.append({"code": codes[0], "name": "".join(names).strip(), "group": groups[0], "score": int(score_text), "page": page, "dpi": dpi})
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--track", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dpis", default="150,200")
    parser.add_argument("--ocr-bin")
    args = parser.parse_args()
    dpis = [int(item) for item in args.dpis.split(",")]
    if len(dpis) != 2:
        raise SystemExit("--dpis 必须提供两个分辨率，例如 150,200")
    ocr = _ocr_binary(args.ocr_bin)
    all_rows = {dpi: [] for dpi in dpis}
    with tempfile.TemporaryDirectory(prefix="fj-ocr-") as directory:
        work = Path(directory)
        for page in range(1, _pages(args.pdf) + 1):
            for dpi in dpis:
                all_rows[dpi].extend(_page_rows(args.pdf, page, dpi, work, ocr))
    primary, secondary = ({(row["page"], row["code"], row["group"]): row for row in all_rows[dpi]} for dpi in dpis)
    common = sorted(set(primary) & set(secondary))
    accepted = [primary[key] | {"track": args.track} for key in common if primary[key]["score"] == secondary[key]["score"]]
    mismatches = [
        {"key": key, "primary": primary[key], "secondary": secondary[key]}
        for key in common
        if primary[key]["score"] != secondary[key]["score"] or primary[key]["name"] != secondary[key]["name"]
    ]
    report = {
        "pdf": str(args.pdf),
        "track": args.track,
        "dpis": dpis,
        "counts": {str(dpi): len(rows) for dpi, rows in all_rows.items()},
        "missing_or_extra": sorted(set(primary) ^ set(secondary)),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "accepted": len(accepted),
    }
    args.output.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in accepted), encoding="utf-8")
    print(json.dumps({"accepted": len(accepted), "report": str(args.output.with_suffix('.report.json'))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
