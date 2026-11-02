"""把并行识图的逐页 JSON 批量导入 MySQL。原图存证与正式推荐记录分开。

每一图片行、错误和重复均进入不可变审计表。仅唯一、完整、与已核验的一分一段
表相容的本科 B 段候选可以另存 gaokao_admissions，状态始终为 pending。
使用项目虚拟环境运行 python -m scripts.import_sichuan_image_batches --help。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

TRACKS = ("历史类", "物理类")
B_BATCH = {"本科批B段", "本科批次B段", "普通本科批B段", "普通本科批次B段"}
B_TARGET = "本科批次B段"
FORMAT = "sichuan-image-evidence-v1"
LOCK = "recipe_sichuan_2026_image_import"
PUBLISHER = "用户提供的四川投档线截图（发布者未经核实）"

DDL = [
    """CREATE TABLE IF NOT EXISTS gaokao_image_imports (
       bundle_sha CHAR(64) NOT NULL PRIMARY KEY, province VARCHAR(30) NOT NULL,
       year SMALLINT NOT NULL, artifact_sha CHAR(64) NOT NULL,
       page_count INT NOT NULL, total_rows INT NOT NULL, manifest_json JSON NOT NULL,
       report_json JSON NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS gaokao_image_evidence (
       bundle_sha CHAR(64) NOT NULL, track VARCHAR(16) NOT NULL, image VARCHAR(100) NOT NULL,
       image_sha CHAR(64) NOT NULL, row_no SMALLINT UNSIGNED NOT NULL, record_sha CHAR(64) NOT NULL,
       university_code VARCHAR(40), university_name VARCHAR(190), group_code VARCHAR(80),
       batch VARCHAR(100), min_score INT, min_rank INT, decision VARCHAR(32) NOT NULL,
       reason_json JSON NOT NULL, raw_json JSON NOT NULL,
       PRIMARY KEY(bundle_sha,track,image,row_no), KEY ix_image_locator(image_sha,row_no),
       KEY ix_image_school(track,university_code,group_code),
       FOREIGN KEY (bundle_sha) REFERENCES gaokao_image_imports(bundle_sha)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
]
ADMISSION_SQL = """INSERT INTO gaokao_admissions
    (province,year,track,batch,admission_round,record_kind,university_code,university_name,
    group_code,group_name,major_code,major_name,min_score,min_rank,source_id,source_locator,
    raw_record_hash,verification_status,verification_note,admission_status,evidence_note)
    VALUES ('四川',2026,%s,%s,'regular','group投档',%s,%s,%s,%s,'','',%s,%s,%s,%s,%s,
    'pending','用户提供的图表；已校验图行和官方分段范围，公布机构和投档事实尚未核实。','submitted',%s)"""
EVIDENCE_SQL = """INSERT INTO gaokao_image_evidence
    (bundle_sha,track,image,image_sha,row_no,record_sha,university_code,university_name,
     group_code,batch,min_score,min_rank,decision,reason_json,raw_json)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def int_or_none(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,8}", str(value)):
        return None
    return int(value)


def load_pages(paths, image_dirs):
    """Require a one-to-one join with all original files, never rely on cached path strings."""
    originals = {(track, p.name): p for track, base in image_dirs.items() for p in base.glob("*.webp")}
    if any(not any(t == track for t, _ in originals) for track in image_dirs):
        raise ValueError("至少一个输入科类没有 WebP 原图")
    pages = {}
    original_hash = {key:hashlib.sha256(p.read_bytes()).hexdigest() for key,p in originals.items()}
    for directory in paths:
        for file in sorted(directory.glob("*.json")):
            page = json.loads(file.read_text())
            if not isinstance(page, dict) or not isinstance(page.get("rows"), list):
                continue
            track, image = page.get("track"), page.get("image")
            key = track, image
            if key in pages or key not in originals:
                raise ValueError(f"重复页或无法匹配原图：{file}")
            digest = original_hash[key]
            if digest != page.get("sha256"):
                raise ValueError(f"原图 SHA256 不符：{file}")
            records = []
            for group in ("rows", "rejected"):
                for row in page.get(group, []):
                    no = int_or_none(row.get("row_no", row.get("row")))
                    if not no or no > 65535:
                        raise ValueError(f"{file} 包含无原图行号的候选；必须重新物理定位")
                    records.append((no, group, row))
            records.sort(key=lambda item: item[0])
            nums = [n for n, *_ in records]
            from scripts.read_sichuan_image_digits import page_grid
            _, _, grid_y = page_grid(originals[key])
            expected = len(grid_y) - 1 - (2 if image == "640.webp" else 0)
            if nums != list(range(1,expected+1)):
                raise ValueError(f"{file} 未与原图的 {expected} 行连续对应")
            if len(set(nums)) != len(nums) or not nums:
                raise ValueError(f"{file} 行号重复或空页")
            if page.get("expected_rows") is not None and len(records) != page["expected_rows"]:
                raise ValueError(f"{file} 物理行数与提取行数不一致")
            pages[key] = (digest, records)
    # Keep all physical paths in the manifest; identical bytes have one evidence set.
    # Canonical path is deterministic (numeric file order, independent of scheduling).
    by_sha = defaultdict(list)
    for (track, image), digest in original_hash.items():
        by_sha[track, digest].append(image)
    for (track, digest), images in by_sha.items():
        if len(images) < 2: continue
        images.sort(key=lambda name: (int(re.search(r"-(\d+)\.", name)[1]) if re.search(r"-(\d+)\.", name) else 0, name))
        available = [(im, pages[(track, im)][1]) for im in images if (track, im) in pages]
        if not available: continue
        _, records = available[0]
        # Never silently throw away disagreement between two OCRs of the same bytes.
        for _, other in available[1:]:
            def facts(rr):
                return [(n,grp,tuple(r.get(k) for k in ("university_code","university_name","group_code","batch","min_score","min_rank"))) for n,grp,r in rr]
            if facts(other) != facts(records): raise ValueError(f"相同原图的两份解析不一致：{images}")
        pages[track,images[0]]=(digest,records)
        for image in images[1:]:
            pages[track,image]=(digest,[])
    if set(originals) != set(pages):
        raise ValueError(f"未识别原图：{sorted(set(originals)-set(pages))}")
    return pages


def _issues(row, group):
    reasons = []
    if group == "rejected":
        reasons.append("读取工作线程隔离")
    issues = row.get("issues") or []
    reasons.extend([str(x) for x in issues] if isinstance(issues, list) else [str(issues)])
    if row.get("status") in {"failed", "rejected", "review", "uncertain", "needs_review", "conflict"}:
        reasons.append("读取工作线程标注:" + row["status"])
    return reasons


def prepare(pages, bands):
    """Dedup on the genuine school + group key, never on fabricated major/round labels."""
    inventory, rows, groups = [], [], defaultdict(list)
    canonical_images = {(t, digest):im for (t,im),(digest,rec) in pages.items() if rec}
    for (track, image), (digest, records) in sorted(pages.items()):
        meta = {"track": track, "image": image, "sha256": digest, "rows": len(records)}
        if not records: meta["duplicate_of"] = canonical_images[track, digest]
        inventory.append(meta)
        for no, group, source in records:
            reasons = _issues(source, group)
            def field(key):
                v = source.get(key)
                return "" if v is None else str(v).strip()
            code, name, group_code, batch = [field(k) for k in ("university_code", "university_name", "group_code", "batch")]
            score, rank = int_or_none(source.get("min_score")), int_or_none(source.get("min_rank"))
            if not re.fullmatch(r"[0-9]{4}", code): reasons.append("院校代码不是四位数字")
            if not name or not re.search(r"[\u3400-\u9fff]",name): reasons.append("院校名称缺失或不可读")
            if not re.fullmatch(r"[0-9]{3}", group_code): reasons.append("专业组不是三位数字")
            if not batch: reasons.append("批次缺失")
            if score is None or not 0 < score <= 750 or not rank: reasons.append("分数或位次缺失/越界")
            elif (track, score) not in bands: reasons.append("官方分段表未覆盖该分数")
            else:
                start, end = bands[track, score]
                if not start <= rank <= end: reasons.append("位次不在官方同分段区间")
            key = (track,code,group_code)
            row = dict(track=track,image=image,image_sha=digest,row_no=no,university_code=code,university_name=name,
                       group_code=group_code,batch=batch,min_score=score,min_rank=rank,reasons=reasons,
                       raw=source,record_sha=sha([digest,no,group,source]),decision="review" if reasons else "other_batch")
            rows.append(row)
            if batch in B_BATCH and code and group_code:
                groups[key].append(row)
    for siblings in groups.values():
        facts = {(r["university_name"], r["min_score"], r["min_rank"]) for r in siblings}
        if len(facts) > 1:
            for r in siblings:
                r["reasons"].append("同科类院校专业组存在不同的截图事实")
                r["decision"] = "conflict"
            continue
        selected = False
        for r in siblings:
            if r["reasons"]: continue
            r["decision"] = "duplicate" if selected else "candidate"
            selected = True
    return inventory, rows


def connect():
    from app.config import settings
    return pymysql.connect(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER,
                           password=settings.MYSQL_PASSWORD, database=settings.MYSQL_DATABASE, charset="utf8mb4",
                           cursorclass=DictCursor, read_timeout=90, write_timeout=90, connect_timeout=8,
                           autocommit=False)


def batches(cur, statement, entries):
    for i in range(0, len(entries), 300):
        cur.executemany(statement, entries[i:i+300])


def _verify_published_rows(cur, bundle, source_id, expected):
    if source_id is None:
        if expected != 0: raise RuntimeError("批次缺少业务来源")
        return
    # Compare in memory: byte-for-byte string equality without collation ambiguity.
    # Two indexed range scans avoid a quadratic join on old databases without a row-hash index.
    cur.execute("""SELECT raw_record_hash AS h,track,university_code,group_code,university_name,min_score,min_rank
        FROM gaokao_admissions WHERE source_id=%s""", (source_id,))
    actual = Counter(tuple(row[k] for k in ("h","track","university_code","group_code","university_name","min_score","min_rank"))
                     for row in cur.fetchall())
    cur.execute("""SELECT record_sha AS h,track,university_code,group_code,university_name,min_score,min_rank
        FROM gaokao_image_evidence WHERE bundle_sha=%s AND decision='candidate'""",(bundle,))
    evidence = Counter(tuple(row[k] for k in ("h","track","university_code","group_code","university_name","min_score","min_rank"))
                       for row in cur.fetchall())
    if actual.total() != expected or actual != evidence:
        raise RuntimeError("业务记录与原图证据不符，不写入；请检查原批次")



def run(pages, *, apply):
    with connect() as con, con.cursor() as cur:
        if apply:
            for ddl in DDL: cur.execute(ddl)
            cur.execute("SELECT GET_LOCK(%s,10) AS held",(LOCK,))
            if cur.fetchone()["held"] != 1: raise RuntimeError("另一个四川截图写入任务持有锁，请稍后续跑")
        cur.execute("""SELECT b.track,b.score,b.rank_start,b.rank_end FROM gaokao_score_bands b
                      JOIN gaokao_sources s ON s.id=b.source_id AND s.validation_status='verified'
                      WHERE b.province='四川' AND b.year=2026 AND b.rank_start IS NOT NULL AND b.rank_end IS NOT NULL""")
        bands = {(r["track"],r["score"]):(r["rank_start"],r["rank_end"]) for r in cur.fetchall()}
        if not set(t for t,_ in pages).issubset({t for t,_ in bands}): raise ValueError("缺少已核验的一分一段表")
        inventory, rows = prepare(pages,bands)
        artifact = sha([[p["track"],p["image"],p["sha256"]] for p in inventory])
        bundle = sha([FORMAT,artifact,[(r["record_sha"],r["decision"],r["reasons"]) for r in rows]])
        if apply:
            cur.execute("SELECT report_json FROM gaokao_image_imports WHERE bundle_sha=%s",(bundle,))
            old = cur.fetchone()
            if old:
                cur.execute("SELECT COUNT(*) AS n FROM gaokao_image_evidence WHERE bundle_sha=%s",(bundle,))
                count = cur.fetchone()["n"]
                if count != len(rows): raise RuntimeError("已有批次的数据库审计记录数不完整")
                saved = json.loads(old["report_json"])
                from app.gaokao.image_policy import BUNDLE, CONFIRMED, SOURCE_KEY
                cur.execute("SELECT source_key,validation_status FROM gaokao_sources WHERE id=%s",(saved["source_id"],))
                published = cur.fetchone()
                if bundle == BUNDLE and published and published["source_key"] == SOURCE_KEY and published["validation_status"] == CONFIRMED:
                    from scripts.enable_sichuan_image_bundle import image_fact, verify_enabled
                    cur.execute("SELECT * FROM gaokao_image_evidence WHERE bundle_sha=%s",(bundle,))
                    signed_rows = cur.fetchall()
                    if Counter(r["record_sha"] for r in signed_rows) != Counter(r["record_sha"] for r in rows):
                        raise RuntimeError("已启用批次与原始归档不符")
                    verify_enabled(cur,saved["source_id"],[image_fact(r) for r in signed_rows])
                    return dict(saved,replay=True,enabled_count=len(rows),current_status=CONFIRMED)
                _verify_published_rows(cur, bundle, saved["source_id"], saved["pending_count"])
                return dict(saved, replay=True)
        cur.execute("""SELECT track,university_code,group_code,university_name,min_score,min_rank,
                      source_id,verification_status FROM gaokao_admissions WHERE province='四川'
                      AND year=2026 AND batch=%s AND admission_round='regular' AND record_kind='group投档'
                      AND major_code=''""",(B_TARGET,))
        existing = {(r["track"],r["university_code"],r["group_code"]):r for r in cur.fetchall()}
        selected = []
        for r in rows:
            if r["decision"] != "candidate": continue
            old = existing.get((r["track"],r["university_code"],r["group_code"]))
            if old:
                same = all(old[k] == r[k] for k in ("min_score","min_rank","university_name"))
                r["decision"] = "exists" if same else "db_conflict"
                r["reasons"].append(f"保留已有 source_id={old['source_id']} status={old['verification_status']}")
            else:
                selected.append(r)
        report = {"format":FORMAT, "bundle_sha":bundle,"artifact_sha":artifact,"pages":len(inventory),
                  "unique_pages":sum("duplicate_of" not in p for p in inventory),"rows":len(rows),
                  "tracks":{t: dict(sorted(Counter(r["decision"] for r in rows if r["track"]==t).items())) for t in TRACKS},
                  "pending_count":len(selected),"source_id":None,"replay":False}
        if not apply: return report
        if selected:
            cur.execute("""INSERT INTO gaokao_sources
                    (source_key,publisher,province,year,dataset_type,source_url,retrieved_at,sha256,row_count,validation_status,notes)
                    VALUES (%s,%s,'四川',2026,'admission',%s,NOW(),%s,%s,'pending',%s)""",
                ("sichuan-2026-user-image:"+bundle,PUBLISHER,"urn:sha256:"+artifact,artifact,len(selected),
                 f"用户提供原图的清单散列，非考试院公告；逐图逐行见 gaokao_image_evidence，bundle_sha={bundle}"))
            report["source_id"] = cur.lastrowid
            batches(cur,ADMISSION_SQL,[(r["track"],B_TARGET,r["university_code"],r["university_name"],r["group_code"],
                "专业组"+r["group_code"],r["min_score"],r["min_rank"],report["source_id"],f"{r['track']}/{r['image']}#row{r['row_no']}",
                r["record_sha"],"user_image_ocr_pending; bundle="+bundle) for r in selected])
            cur.execute("SELECT COUNT(*) AS n FROM gaokao_admissions WHERE source_id=%s AND verification_status='pending'",(report["source_id"],))
            if cur.fetchone()["n"] != len(selected): raise RuntimeError("回读的业务记录数不符")
        cur.execute("""INSERT INTO gaokao_image_imports
                 (bundle_sha,province,year,artifact_sha,page_count,total_rows,manifest_json,report_json)
                 VALUES (%s,'四川',2026,%s,%s,%s,%s,%s)""",
                 (bundle,artifact,len(inventory),len(rows),canonical(inventory),canonical(report)))
        batches(cur,EVIDENCE_SQL,[(bundle,r["track"],r["image"],r["image_sha"],r["row_no"],r["record_sha"],
                                  r["university_code"] or None,r["university_name"] or None,r["group_code"] or None,
                                  r["batch"] or None,r["min_score"],r["min_rank"],r["decision"],canonical(r["reasons"]),
                                  canonical(r["raw"])) for r in rows])
        cur.execute("SELECT COUNT(*) AS n FROM gaokao_image_evidence WHERE bundle_sha=%s",(bundle,))
        if cur.fetchone()["n"] != len(rows): raise RuntimeError("回读的逐行审计数不符")
        _verify_published_rows(cur, bundle, report["source_id"], len(selected))
        con.commit()
        return report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pages", type=Path, nargs="+", required=True, help="一个或多个存放逐页 JSON 的目录")
    p.add_argument("--history-dir", type=Path, help="历史类源 WebP 目录")
    p.add_argument("--physical-dir", type=Path, help="物理类源 WebP 目录")
    p.add_argument("--apply",action="store_true",help="以事务批量写本项目配置的 MySQL；默认只读")
    p.add_argument("--report",type=Path)
    args=p.parse_args()
    dirs={t:d for t,d in zip(TRACKS,(args.history_dir,args.physical_dir)) if d is not None}
    if not dirs: p.error("至少指定一个科类原图目录")
    report = run(load_pages(args.pages, dirs), apply=args.apply)
    value = json.dumps(report,ensure_ascii=False,indent=2)+"\n"
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        tmp=args.report.with_name(args.report.name+f".{os.getpid()}.tmp")
        tmp.write_text(value); tmp.replace(args.report)
    print(value,end="")


if __name__=="__main__":
    main()
