"""Independent numeric check for the fixed six-column Sichuan 2026 image tables.

Uses glyphs from the visually transcribed first physics page as exemplars; it does
not infer scores or ranks from other rows. Outputs original page/grid coordinates,
match separation and SHA-256. Source images and evidence are never modified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

COLS = [0, 2, 4, 5]
# Physics 640.webp, first 12 *data* rows, read directly from the source page.
REFERENCE = [
    ("0001","101","683","90"), ("0002","101","664","718"), ("0002","102","662","853"),
    ("0003","101","679","136"), ("0003","104","677","180"), ("0004","101","636","5118"),
    ("0004","102","636","5118"), ("0004","103","634","5669"), ("0004","104","623","9659"),
    ("0004","109","609","16522"), ("0004","127","609","16522"), ("0004","107","607","17770"),
]


def runs(b):
    return [z for z in np.split(b, np.flatnonzero(np.diff(b)>1)+1) if len(z)]


def page_grid(path):
    with Image.open(path) as im:
        g = np.asarray(im.convert("RGB")).max(2)
    mask = g < 148
    ys = [round(float(z.mean())) for z in runs(np.where(mask.sum(1)>g.shape[1]*.60)[0])]
    if len(ys)<2:
        raise ValueError(f"missing horizontal grid: {path}")
    xs = [round(float(z.mean())) for z in runs(np.where(mask[ys[0]:ys[-1]].sum(0)>(ys[-1]-ys[0])*.75)[0])]
    if len(xs)!=7 or min(np.diff(ys))<22 or max(np.diff(ys))>53:
        raise ValueError(f"unexpected table grid: {path}, {xs}, {ys}")
    return g, xs, ys


def glyphs(img,l,r,t,b,length=None):
    # Text is charcoal; the pale colored watermark disappears in the brightest channel.
    v = np.clip((210-img[t+3:b-3,l+3:r-3].astype('float32'))/165,0,1)
    ink=np.where((v>.35).any(0))[0]
    if not len(ink): return []
    pitch=9.12
    # Font uses tabular (equal advance) digits. A faint numeral such as
    # '3' can split into two disconnected islands after WEBP compression.
    # Cut on the grid between glyphs, never inside a single numeral.
    if length is None:
        length=1+int(np.floor((ink[-1]-ink[0]+2)/pitch))
    if length<1 or length>7: return []
    center=(r-l)/2-3+.65
    cuts=[int(np.floor(center+(i-length/2)*pitch))+1 for i in range(length+1)]
    if ink[0]<cuts[0]-1 or ink[-1]>=cuts[-1]+1: return []
    chars=[]
    for lo,hi in zip(cuts,cuts[1:]):
        cc=ink[(ink>=lo)&(ink<hi)]
        if not len(cc): return []
        chars.append(np.arange(cc[0],cc[-1]+1))
    out = []
    for cols in chars:
        a = v[:,cols[0]:cols[-1]+1]
        rows = np.where((a>.35).any(1))[0]
        a = np.clip(a[rows[0]:rows[-1]+1],0,1)
        if a.shape[0]<7 or a.shape[0]>22 or a.shape[1]>12:
            return []
        # Compare a common-sized ink sample; use width/aspect alongside it.
        z=np.asarray(Image.fromarray(a).resize((8,14), Image.Resampling.BILINEAR))
        out.append(np.concatenate([z.ravel(), np.full(12,min(1.,a.shape[1]/10))]))
    return out


def templates(ref):
    g,x,y = page_grid(ref); examples=[]
    for i,labels in enumerate(REFERENCE,2):
        for col,label in zip(COLS,labels):
            parts=glyphs(g,x[col],x[col+1],y[i],y[i+1],len(label))
            if len(parts)!=len(label): raise ValueError(f"template segmentation: row {i}, {label}")
            examples.extend(zip(label,parts))
    chars=np.array([s for s,_ in examples]); assert len(set(chars))==10
    return [(str(n),np.stack([v for c,v in examples if c==str(n)])) for n in range(10)]


def read_number(g,x,y,c,i,models):
    parts=glyphs(g,x[c],x[c+1],y[i],y[i+1],{0:4,2:3,4:3}.get(c))
    if not parts: return "", 0.
    result=[]; confidence=[]
    for v in parts:
        scores=[(float(np.min(np.mean((a-v)**2,axis=1))),s) for s,a in models]
        (best,d),(other,_) = sorted(scores)[:2]
        result.append(d)
        confidence.append((other-best)/(other+0.00001))
    return "".join(result),round(min(confidence),3)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--physical-dir",required=True,type=Path)
    p.add_argument("--history-dir",required=True,type=Path)
    p.add_argument("--out",required=True,type=Path)
    args=p.parse_args()
    models=templates(args.physical_dir/"640.webp")
    args.out.mkdir(parents=True,exist_ok=True)
    for track,directory in [("history",args.history_dir),("physical",args.physical_dir)]:
        records=[]; digests={}
        for path in sorted(directory.glob("*.webp"),key=lambda p: int(p.stem.partition("-")[2] or 0)):
            sha=hashlib.sha256(path.read_bytes()).hexdigest()
            if sha in digests:
                continue
            digests[sha]=path.name
            g,x,y=page_grid(path)
            start=2 if path.name=="640.webp" else 0
            for i in range(start,len(y)-1):
                vals=[read_number(g,x,y,c,i,models) for c in COLS]
                records.append(dict(image=path.name,sha256=sha,row=i-start+1,
                    y=[y[i],y[i+1]], values=[t for t,_ in vals],margin=[m for _,m in vals]))
        dest=args.out/f"{track}-digits.jsonl"
        temp=dest.with_suffix(".tmp")
        temp.write_text("".join(json.dumps(row,ensure_ascii=False)+"\n" for row in records),encoding="utf-8")
        temp.replace(dest)
        print(f"{track}: {len(digests)} distinct pages, {len(records)} grid rows => {dest}",flush=True)


if __name__=="__main__":
    main()
