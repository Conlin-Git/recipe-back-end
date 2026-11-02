"""Read and reconcile physical-track pages of this 2026 Sichuan screenshot release.

Compile scripts/vision_ocr.swift, then run with --source-dir, --ocr-bin, --digits
(the output of read_sichuan_image_digits), --cache-dir and --output-dir. The only
OCR pass is checkpointed per image SHA and binary SHA. Rows are bounded by actual
grid lines. A second numeric recognizer is calibrated with score/rank cells that
exactly match the independent official score band file; it never looks up a value
to impute a missing field. Page/row name fixes are from direct visual review of
this specific release, bound to the image hash. No database writes.
"""
from __future__ import annotations
import argparse
import bisect
import collections
import concurrent.futures
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import numpy as np
from PIL import Image
from scripts.read_sichuan_image_digits import page_grid
BASE = pathlib.Path(__file__).resolve().parents[1]


def load_cells(path, cache_dir, binary, binary_sha, xs, ys, img_shape):
    """A failed page is retried on the next run; never cache partial OCR."""
    image_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    dest = cache_dir / f'{image_sha}-{binary_sha}.tsv'
    if not dest.exists():
        result = subprocess.run([str(binary), str(path)], check=True, capture_output=True, timeout=90)
        if not result.stdout.strip():
            raise ValueError(f'Vision returned no observations: {path.name}')
        with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as tmp:
            tmp.write(result.stdout)
        os.replace(tmp.name, dest)
    cells = [[[] for _ in range(6)] for _ in range(len(ys) - 1)]
    h, w = img_shape
    for ln in dest.read_text().splitlines():
        x, y, width, height, text = ln.split('\t', 4)
        px = (float(x) + float(width) / 2) * w
        py = (1 - float(y) - float(height) / 2) * h
        col, row = (bisect.bisect(xs, px) - 1, bisect.bisect(ys, py) - 1)
        if 0 <= col < 6 and 0 <= row < len(cells):
            cells[row][col].append((py, px, text))
    return (image_sha, [[''.join((text for _, _, text in sorted(cell))) for cell in row] for row in cells])


def cutdigits(gray, x1, x2, y1, y2, n):
    """根据固定宽度等距数字切单字，再以笔画和宽高生成比较特征。"""
    a = gray[y1 + 4:y2 - 3, x1 + 3:x2 - 3]
    w = x2 - x1
    pitch = 9.12
    center = w / 2 - 3 + 0.65
    cuts = [int(np.floor(center + (i - n / 2) * pitch)) + 1 for i in range(n + 1)]
    res = []
    for l, r in zip(cuts, cuts[1:]):
        c = a[:, l:r]
        ii = np.argwhere(c < 160)
        if not len(ii):
            return []
        yy, xx = ii.min(0)
        yhi, xhi = ii.max(0)
        c = c[yy:yhi + 1, xx:xhi + 1].astype('float32')
        h, w = c.shape
        c = np.asarray(Image.fromarray(np.clip(210 - c, 0, 170) / 170).resize((10, 14), Image.Resampling.BILINEAR)).ravel()
        res.append(np.concatenate([c, np.full(20, min(1.0, w / 9), 'float32'), np.full(5, min(1.0, h / 14), 'float32')]).astype('float32'))
    return res


def fingerprint(gray, x1, x2, y1, y2):
    a = gray[y1 + 4:y2 - 3, x1 + 3:x2 - 3]
    ii = np.argwhere(a < 160)
    y, z = ii.min(0)
    b, e = ii.max(0)
    c = np.clip((210 - a[y:b + 1, z:e + 1].astype('float32')) / 170, 0, 1)
    return np.concatenate([np.asarray(Image.fromarray(c).resize((60, 14), Image.Resampling.BILINEAR)).ravel(), np.full(30, (e - z) / 100)])


def scan_pages(args, digits, bands):
    """每次读一组页，保留逐行两套观察，不以 OCR 是否识别到分数决定行数。"""
    examples = collections.defaultdict(list)
    dataset = []
    attrs = {}
    batchrefs = collections.defaultdict(list)
    paths = sorted(args.source_dir.glob('*.webp'), key=lambda p: int(p.stem.partition('-')[2] or 0))
    if not paths:
        raise ValueError('Missing source screenshots')
    engine_sha = hashlib.sha256(args.ocr_bin.read_bytes()).hexdigest()

    def page(p):
        gray, x, ys = page_grid(p)
        points = np.flatnonzero((gray < 120).sum(axis=1) > 750)
        groups = np.split(points, np.flatnonzero(np.diff(points) > 1) + 1)
        core = [round(float(g.mean())) for g in groups if len(g)]
        if len(core) != len(ys) or any((abs(a - b) > 1 for a, b in zip(core, ys))):
            raise ValueError(f'二次表格横线识别不相符: {p.name}')
        ys = core[2:] if p.name == '640.webp' else core
        dg, rows = load_cells(p, args.cache_dir, args.ocr_bin, engine_sha, x, ys, gray.shape)
        return (p, dg, gray, x, ys, rows)
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        for start in range(0, len(paths), args.workers):
            for p, dg, gray, x, ys, rows in pool.map(page, paths[start:start + args.workers]):
                for i, row in enumerate(rows, 1):
                    vals = digits[dg, i]['values']
                    top, bot = ys[i - 1:i + 1]
                    bands_ok = row[4].isdigit() and row[5].isdigit() and (bands.get(int(row[4])) == int(row[5]))
                    cells = []
                    for j, k in enumerate([0, 2, 4, 5]):
                        raw = row[k]
                        n = len(vals[j]) or (len(raw) if raw.isdigit() else 0)
                        if k == 5 and bands_ok:
                            n = len(raw)
                        if k in (0, 2, 4):
                            n = {0: 4, 2: 3, 4: 3}[k]
                        glyphs = cutdigits(gray, x[k], x[k + 1], top, bot, n) if 1 <= n <= 7 else []
                        cells.append(glyphs)
                        if bands_ok and k in (4, 5) and (len(raw) == len(glyphs)):
                            for l, g in zip(raw, glyphs):
                                examples[l].append(g)
                    bp = fingerprint(gray, x[3], x[4], top, bot)
                    dataset.append((p.name, dg, i))
                    attrs[p.name, i] = (row, vals, cells, bp, top, bot)
                    if row[3] in ['本科批B段', '提前批']:
                        batchrefs[row[3]].append(bp)
    return (dataset, attrs, examples, batchrefs)


def fit(examples):
    """保存覆盖字形差异的一组代表样本，用于抗压缩伪影的距离比较。"""
    banks = {}
    for label, v in examples.items():
        v = np.stack(v)
        d = np.ones(len(v), np.float32)
        idx = len(v) // 2
        chosen = []
        for i in range(min(96, len(v))):
            c = v[idx]
            chosen.append(c)
            ds = ((v - c) ** 2).mean(axis=1)
            d = np.minimum(d, ds)
            idx = int(np.argmax(d))
            if d[idx] < 2e-05:
                break
        banks[label] = np.stack(chosen)
    return banks


def classify(cells, banks):
    """分批计算每个样本与不同标签的最近距离，并保留第一、二选择的间距。"""
    idx = []
    vv = []
    for k, z in cells.items():
        for i, v in enumerate(z):
            idx.append((k, i))
            vv.append(v)
    if not vv:
        return {}
    vv = np.stack(vv)
    labels = sorted(banks)
    scores = []
    for k in labels:
        b = banks[k]
        bsq = (b * b).mean(1)
        res = []
        for i in range(0, len(vv), 512):
            v = vv[i:i + 512]
            res.extend(np.min((v * v).mean(1)[:, None] + bsq[None, :] - 2 * (v @ b.T) / b.shape[1], axis=1))
        scores.append(res)
    pred = {}
    for (k, i), vec in zip(idx, np.array(scores).T):
        order = np.argsort(vec)[:2]
        d = labels[order[0]]
        score = max(0, vec[order[0]])
        margin = (vec[order[1]] - score) / (vec[order[1]] + 1e-06)
        pred.setdefault(k, [[], []])
        pred[k][0].append(d)
        pred[k][1].append(float(margin))
    return {k: [''.join(d), min(m)] for k, (d, m) in pred.items()}


def read_all(args, digits, bands):
    dataset, attrs, ex, br = scan_pages(args, digits, bands)
    banks = fit(ex)
    cells = {(f, i, j): a[2][j] for (f, i), a in attrs.items() for j in range(4)}
    nums = classify(cells, banks)
    batch = classify({k: [a[3]] for k, a in attrs.items()}, fit(br))
    out = {}
    for f, dg, i in dataset:
        raw, old, _, bp, top, bot = attrs[f, i]
        res = [nums.get((f, i, j), ['', 0]) for j in range(4)]
        out.setdefault(f, []).append(dict(image=f, sha256=dg, row=i, y=[top, bot], raw=raw, old=old, chars=[v[0] for v in res], margins=[round(v[1], 3) for v in res], batch=batch[f, i]))
    return out


def save(args, readings, bands):
    overrides = json.loads((BASE / 'data/gaokao/user_import/sichuan_physical_image_review.json').read_text())
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    counts = collections.Counter()
    for filename, candidates in readings.items():
        rows = []
        for v in candidates:
            sha, i, raw, vals, old = (v['sha256'], v['row'], v['raw'], v['chars'], v['old'])
            reviewed = overrides.get(f'{sha}:{i}', {})
            issues = []
            name = raw[1]
            if 'name' in reviewed:
                if reviewed['raw_name'] != name:
                    issues.append('复核档与识别文本不符')
                else:
                    name = reviewed['name']
            if reviewed.get('clipped'):
                issues.append('原图印刷截断校名，不能仅凭 OCR 补全')
            score = int(vals[2]) if vals[2].isdigit() else None
            rank = int(vals[3]) if vals[3].isdigit() else None
            if score not in bands or rank != bands.get(score):
                issues.append('分数/位次与官方分段累计不一致')
            batch, bm = v['batch']
            if batch != raw[3] and (raw[3] in ('本科批B段', '提前批') or bm < 0.6):
                issues.append('批次字形与 OCR 结果不一致')
            if vals[0] != raw[0] and vals[0] != old[0] and (reviewed.get('code') != vals[0]):
                issues.append('学校代码需人工核对')
            if vals[1] != raw[2] and vals[1] != old[1]:
                issues.append('专业组需人工核对')
            if min(v['margins']) < 0.5:
                issues.append('数值字形区分度偏低')
            counts[batch] += 1
            counts['review'] += bool(issues)
            rows.append(dict(row=i, y=v['y'], university_code=vals[0], university_name=name, group_code=vals[1], batch=batch, min_score=score, min_rank=rank, issues=issues, ocr_cells=raw, glyph_digits=vals, ref_digits=old, match_margins=v['margins']))
        result = dict(track='物理类', image=filename, sha256=candidates[0]['sha256'], expected_rows=len(rows), rows=rows)
        if hashlib.sha256((args.source_dir / filename).read_bytes()).hexdigest() != result['sha256']:
            raise ValueError(f'源文件在处理期间改变: {filename}')
        dest = out / (pathlib.Path(filename).stem + '.json')
        with tempfile.NamedTemporaryFile(mode='w', dir=out, encoding='utf-8', delete=False) as tmp:
            json.dump(result, tmp, ensure_ascii=False, separators=(',', ':'))
            tmp.write('\n')
        os.replace(tmp.name, dest)
    print(json.dumps(dict(pages=len(readings), **counts), ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ('source-dir', 'ocr-bin', 'digits', 'cache-dir', 'output-dir'):
        p.add_argument('--' + key, required=True, type=pathlib.Path)
    p.add_argument('--bands', type=pathlib.Path, default=BASE / 'data/gaokao/scoreband_seeds/sichuan-2026-scoreband-physical.jsonl')
    p.add_argument('--workers', type=int, default=3, choices=range(1, 9))
    args = p.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    with args.digits.open() as f:
        digits = {(v['sha256'], v['row']): v for v in map(json.loads, f)}
    with args.bands.open() as f:
        bands = {v['score']: v['cumulative_rank'] for v in map(json.loads, f) if v.get('track') == '物理类' and v.get('province') == '四川' and (v.get('year') == 2026) and (v.get('verification_status') == 'verified')}
    if not bands:
        raise ValueError('缺少四川 2026 年物理类核验分段表')
    save(args, read_all(args, digits, bands), bands)
if __name__ == '__main__':
    main()
