"""将四川六列表格按真实网格并行读为原始候选，不推定官方核验或写库。

首次运行前：swiftc -O scripts/vision_ocr.swift -o /tmp/sichuan-vision
.venv/bin/python -m scripts.extract_sichuan_grid_observations \
  --source-dir <一种科类原图目录> --output-dir <该科观察缓存> --ocr-bin /tmp/sichuan-vision
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image

from scripts.read_sichuan_image_digits import page_grid

FORMAT = 1

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write(path, obj):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
        json.dump(obj,tmp,ensure_ascii=False, separators=(",",":"))
        tmp.write("\n")
    os.replace(tmp.name,path)

def observations(binary, path, w, h, xs, ys, *, ox=0, oy=0, sx=1, sy=1):
    text = subprocess.run([str(binary), str(path)], capture_output=True, text=True,
                          check=True, timeout=90).stdout
    cells = [[[] for _ in range(6)] for _ in range(len(ys)-1)]
    for line in text.splitlines():
        fields = line.split("\t",4)
        if len(fields)!=5:
            raise ValueError("识别器返回了无坐标文本")
        left, bottom, width, height = map(float,fields[:4])
        x = ox+(left+width/2)*w/sx
        y = oy+(1-bottom-height/2)*h/sy
        row, col = bisect.bisect(ys,y)-1, bisect.bisect(xs,x)-1
        if 0<=row<len(cells) and 0<=col<6:
            cells[row][col].append([round(x,2),round(y,2),fields[4]])
    return cells

def page(source, out, binary, engine_sha, script_sha):
    key={"sha256":sha(source),"engine_sha256":engine_sha,"script_sha256":script_sha,"format":FORMAT}
    dest=out/(source.stem+".json")
    if dest.exists():
        try:
            prev=json.loads(dest.read_text())
            if all(prev.get(k)==v for k,v in key.items()) and prev.get("rows"):
                return "cached"
        except (ValueError, OSError):
            pass
    data,xs,grid=page_grid(source)
    ys=grid[2:] if source.name=="640.webp" else grid
    h,w=data.shape
    # Two separate Vision passes: preserve original input, then reconstruct a grid-free
    # image. Light watermarks lose contrast in the brightest RGB channel.
    raw=observations(binary,source,w,h,xs,ys)
    cleaned=data.copy()
    for x in xs:
        cleaned[:,x-2:x+3]=255
    for y in ys:
        cleaned[y-2:y+3,:]=255
    cleaned[cleaned>183]=255
    ox,oy=xs[0]-6,ys[0]-3
    excerpt=Image.fromarray(cleaned).crop((ox,oy,xs[-1]+8,ys[-1]+3))
    w2,h2=int(excerpt.width*1.65),int(excerpt.height*1.65)
    with tempfile.TemporaryDirectory(dir=out) as temp:
        img=Path(temp)/"clean.png"
        excerpt.resize((w2,h2),Image.Resampling.LANCZOS).save(img)
        clean=observations(binary,img,w2,h2,xs,ys,ox=ox,oy=oy,sx=w2/excerpt.width,sy=h2/excerpt.height)
    # All physical rows have a record, including rows with empty cells in both passes.
    if sha(source)!=key["sha256"]:
        raise RuntimeError(f"源文件在读取过程中变更：{source.name}")
    write(dest,{**key,"image":source.name,"xs":xs,"rows":[
        {"row_no":i+1,"y":[ys[i],ys[i+1]],"original":raw[i],"cleaned":clean[i]}
        for i in range(len(raw))]})
    return "read"

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-dir",required=True,type=Path)
    p.add_argument("--output-dir",required=True,type=Path)
    p.add_argument("--ocr-bin",required=True,type=Path)
    p.add_argument("--workers",type=int,default=3,choices=range(1,9))
    args=p.parse_args()
    paths=sorted(args.source_dir.glob("*.webp"))
    if not paths:
        p.error("源目录未找到 WebP")
    args.output_dir.mkdir(parents=True,exist_ok=True)
    binary=args.ocr_bin.resolve()
    from scripts import read_sichuan_image_digits
    version=hashlib.sha256((sha(Path(__file__))+sha(Path(read_sichuan_image_digits.__file__))).encode()).hexdigest()
    binary_sha=sha(binary)
    errors={}
    with ThreadPoolExecutor(args.workers) as pool:
        futures={pool.submit(page,path,args.output_dir,binary,binary_sha,version):path.name for path in paths}
        for fut in as_completed(futures):
            name=futures[fut]
            try:
                print(name,fut.result(),flush=True)
            except Exception as exc:
                errors[name]=str(exc)
                print(name,exc,flush=True)
    if errors:
        raise SystemExit(f"失败页（其他页已保留）：{errors}")

if __name__=="__main__":
    main()
