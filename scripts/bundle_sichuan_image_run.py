"""Package/validate or import both Sichuan screenshot tracks offline, including originals.

Pack accepts fully reconciled per-page JSON and original WebPs. Import validates every
member and the complete manifest before calling the transaction-based MySQL writer.
The default import is read-only; --apply is required to perform the batch insert.
"""
import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from scripts.import_sichuan_image_batches import load_pages, run, sha

TRACKS = {"history":"历史类", "physical":"物理类"}
PATTERN = re.compile(r"^(history|physical)/(images/640(?:-[0-9]+)?\.webp|pages/640(?:-[0-9]+)?\.json)$")
FMT = "sichuan-user-images-2026-v1"


def member(z, name, data):
    # Fixed timestamps and ordering make rebuilding a bundle reproducible.
    info = ZipInfo(name)
    info.compress_type = ZIP_DEFLATED
    z.writestr(info, data)


def pack(args):
    members = {}
    originals, page_dirs = {}, []
    for slug, track in TRACKS.items():
        src, pages = (getattr(args,slug+k) for k in ("_dir","_pages"))
        originals[track] = src
        page_dirs.append(pages)
        for p in src.glob("*.webp"):
            members[f"{slug}/images/{p.name}"] = p
        for p in pages.glob("*.json"):
            data = json.loads(p.read_text())
            if not isinstance(data,dict) or not isinstance(data.get("rows"),list):
                continue
            if data.get("track")!=track: raise ValueError(f"页面科类不符：{p}")
            fname=Path(data["image"]).with_suffix(".json").name
            k=f"{slug}/pages/{fname}"
            if k in members: raise ValueError(f"同一图片出现两份结果：{k}")
            members[k]=p
    validated = load_pages(page_dirs,originals)
    if not all(PATTERN.fullmatch(name) for name in members):
        raise ValueError("来源文件名不符合本批次约定")
    sums={name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in sorted(members.items())}
    manifest=dict(format=FMT,members=sums,content_id=sha(sums),pages=len(validated),
                  rows=sum(len(rows) for _,rows in validated.values()))
    args.bundle.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=args.bundle.parent,delete=False) as temp:
        tmp=Path(temp.name)
    try:
        with ZipFile(tmp,"w") as z:
            for name,path in sorted(members.items()): member(z,name,path.read_bytes())
            member(z,"manifest.json",json.dumps(manifest,ensure_ascii=False,sort_keys=True).encode())
        with tempfile.TemporaryDirectory() as out:
            unpack(tmp,Path(out))
        os.replace(tmp,args.bundle)
    finally:
        tmp.unlink(missing_ok=True)
    print(json.dumps({k:v for k,v in manifest.items() if k!="members"},ensure_ascii=False))


def unpack(path,out):
    with ZipFile(path) as z:
        infos=z.infolist()
        if len(infos)>1000 or any(i.file_size>16*1024*1024 for i in infos) or sum(i.file_size for i in infos)>512*1024*1024:
            raise ValueError("归档超出本批次限额")
        names=[i.filename for i in infos]
        if names.count("manifest.json")!=1 or len(names)!=len(set(names)):
            raise ValueError("清单缺失或归档中存在重复文件名")
        m=json.loads(z.read("manifest.json"))
        if m.get("format")!=FMT or set(names)!={"manifest.json",*m["members"]} or sha(m["members"])!=m["content_id"]:
            raise ValueError("归档清单不匹配")
        for name,expected in m["members"].items():
            if not PATTERN.fullmatch(name): raise ValueError(f"归档路径不允许: {name}")
            data=z.read(name)
            if hashlib.sha256(data).hexdigest()!=expected: raise ValueError(f"SHA-256 不匹配: {name}")
            dest=out/name
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(data)
    pages=load_pages([out/s/"pages" for s in TRACKS], {t:out/s/"images" for s,t in TRACKS.items()})
    if m["pages"]!=len(pages) or m["rows"]!=sum(len(rec) for _,rec in pages.values()):
        raise ValueError("归档页数/行数不匹配")
    return m,pages


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest="cmd",required=True)
    b=sub.add_parser("pack")
    for slug in TRACKS:
        for k in ("dir","pages"):
            b.add_argument(f"--{slug}-{k}",required=True,type=Path)
    b.add_argument("--bundle",required=True,type=Path)
    for cmd in ("verify","import"):
        a=sub.add_parser(cmd)
        a.add_argument("--bundle",required=True,type=Path)
        if cmd=="import": a.add_argument("--apply",action="store_true")
    args=p.parse_args()
    if args.cmd=="pack": return pack(args)
    with tempfile.TemporaryDirectory() as out:
        m,pages=unpack(args.bundle,Path(out))
        if args.cmd=="verify":
            result={k:v for k,v in m.items() if k!="members"}
        else:
            result=run(pages,apply=args.apply)
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
