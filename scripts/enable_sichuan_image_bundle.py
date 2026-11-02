"""启用用户指定的不可变截图批次；不更改原图审计记录或其他来源。"""
import argparse
import json
from collections import Counter
from pathlib import Path

from app.gaokao.image_policy import ARTIFACT, BUNDLE, CONFIRMED, LABEL, SOURCE_KEY
from scripts.import_sichuan_image_batches import B_TARGET, LOCK, batches, connect, sha, _verify_published_rows
from scripts.bundle_sichuan_image_run import unpack
from tempfile import TemporaryDirectory

ARCHIVE = Path(__file__).resolve().parents[1] / "data/gaokao/user_import/sichuan_2026_image_evidence_v1.zip"
ARCHIVE_CONTENT = "3d8e5d9526be01960dad898bef13d7bc69eb8477edc9f0c328f314a3b787a631"

def image_fact(row):
    reasons=json.loads(row["reason_json"])
    clipped=any("校名" in m for m in reasons)
    bounded="官方分段表未覆盖该分数" in reasons
    code=row["university_code"]
    note="用户确认截图。"
    if clipped: note+="原图校名被截断，以院校代码为准。"
    if bounded: note+=f"截图所列位次 {row['min_rank']} 不作精确位次分档。"
    return (
        row["track"], {"本科批B段":B_TARGET,"提前批":"提前批"}[row["batch"]],
        code, f"院校代码 {code}（原图校名不全）" if clipped else row["university_name"],
        row["group_code"], "专业组"+row["group_code"],row["min_score"],row["min_rank"],
        f"{row['track']}/{row['image']}#row{row['row_no']}",row["record_sha"],note,
        "user_confirmed_image; bundle="+BUNDLE+("; rank_unbounded" if bounded else "")
    )

def verify_enabled(cur, sid, facts):
    cur.execute("""SELECT track,batch,university_code,university_name,group_code,group_name,min_score,min_rank,
        source_locator,raw_record_hash,verification_note,evidence_note FROM gaokao_admissions
        WHERE source_id=%s AND verification_status=%s""",(sid, CONFIRMED))
    if Counter(tuple(v.values()) for v in cur.fetchall()) != Counter(facts):
        raise ValueError("已启用的投档行与原图不一致")


def enable(apply=False):
    with TemporaryDirectory() as tmp:
        manifest,pages=unpack(ARCHIVE,Path(tmp))
    if manifest["content_id"]!=ARCHIVE_CONTENT or len(pages)!=134:
        raise ValueError("不是用户确认的原图归档")
    originals = {(track,image,no):(digest,sha([digest,no,group,r]),r)
                 for (track,image),(digest,page) in pages.items() for no,group,r in page}
    digests=Counter(v[1] for v in originals.values())
    with connect() as db, db.cursor() as cur:
        if apply:
            cur.execute("SELECT GET_LOCK(%s,10) AS held",(LOCK,))
            if cur.fetchone()["held"]!=1: raise RuntimeError("截图导入任务正占用写锁")
        cur.execute("SELECT id,sha256,validation_status FROM gaokao_sources WHERE source_key=%s FOR UPDATE",(SOURCE_KEY,))
        source=cur.fetchone()
        if not source or source["sha256"]!=ARTIFACT or source["validation_status"] not in ("pending",CONFIRMED):
            raise ValueError("请先导入对应离线归档")
        sid=source["id"]
        cur.execute("SELECT artifact_sha,total_rows,page_count FROM gaokao_image_imports WHERE bundle_sha=%s",(BUNDLE,))
        batch=cur.fetchone()
        if not batch or (batch["artifact_sha"],batch["total_rows"],batch["page_count"])!=(ARTIFACT,4732,134):
            raise ValueError("前置导入记录不符")
        cur.execute("SELECT * FROM gaokao_image_evidence WHERE bundle_sha=%s",(BUNDLE,))
        rows=cur.fetchall()
        if Counter(r['record_sha'] for r in rows)!=digests: raise ValueError("数据库原始行指纹与离线归档不符")
        for row in rows:
            digest,record,raw=originals[row["track"],row["image"],row["row_no"]]
            if ((digest,record) != (row["image_sha"],row["record_sha"]) or json.loads(row["raw_json"]) != raw or
                any(raw[k]!=row[k] for k in ("university_code","university_name","group_code","batch","min_score","min_rank"))):
                raise ValueError("审计字段与原图归档不一致")
        facts=[image_fact(r) for r in rows]
        counts=Counter((f[0],f[1]) for f in facts)
        if len(counts)!=4 or len({(f[0],f[1],f[2],f[4]) for f in facts})!=4732: raise ValueError("批次或业务键存在重叠")
        cur.execute("SELECT raw_record_hash,verification_status FROM gaokao_admissions WHERE source_id=%s",(sid,))
        existing=cur.fetchall()
        complete=source["validation_status"]==CONFIRMED
        required_status=CONFIRMED if complete else "pending"
        required={r['record_sha'] for r in rows if complete or r['decision']=='candidate'}
        if len(existing)!=len(required) or {r['raw_record_hash'] for r in existing}!=required or any(r['verification_status']!=required_status for r in existing):
            raise ValueError("已有投档数据与预期状态不符，不覆盖")
        report={"source_id":sid,"bundle_sha":BUNDLE,"unique_images":133,"enabled_rows":len(rows),
                "status":CONFIRMED,"by_batch":{f"{t}/{b}":n for (t,b),n in sorted(counts.items())},
                "applied":apply,"already_enabled":complete}
        if complete:
            verify_enabled(cur,sid,facts)
            return report
        _verify_published_rows(cur,BUNDLE,sid,len(required))
        if not apply: return report
        # New rows are limited to the 194 previously retained only in the evidence table.
        # A foreign fact on the same business key aborts this transaction (no upsert).
        new=[f+(sid,) for f in facts if f[9] not in required]
        batches(cur,"""INSERT INTO gaokao_admissions
            (track,batch,university_code,university_name,group_code,group_name,min_score,min_rank,
             source_locator,raw_record_hash,verification_note,evidence_note,source_id,province,year,
             record_kind,admission_round,major_code,major_name,verification_status,admission_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'四川',2026,'group投档','regular','','','user_confirmed','submitted')""",new)
        cur.execute("""UPDATE gaokao_admissions SET verification_status=%s,verification_note=%s,evidence_note=%s
            WHERE source_id=%s AND verification_status='pending'""",(CONFIRMED,"用户确认截图。","user_confirmed_image; bundle="+BUNDLE,sid))
        # Fill locations only from exact, unambiguous names in existing verified admission records.
        cur.execute("""SELECT university_name,MIN(school_province) AS region FROM gaokao_admissions
            WHERE verification_status='verified' AND school_province IS NOT NULL AND school_province!=''
            GROUP BY university_name HAVING COUNT(DISTINCT school_province)=1""")
        regions={r['university_name']:r['region'] for r in cur.fetchall()}
        school_names={f[3] for f in facts}
        locations=[(v,sid,k) for k,v in regions.items() if k in school_names]
        if locations: batches(cur,"UPDATE gaokao_admissions SET school_province=%s WHERE source_id=%s AND BINARY university_name=BINARY %s",locations)
        cur.execute("""UPDATE gaokao_sources SET validation_status=%s,publisher=%s,row_count=%s,
            notes=%s WHERE id=%s""",(CONFIRMED,LABEL,len(facts),"用户明确确认去重后的 133 张图片；原图及审计保留。bundle_sha="+BUNDLE,sid))
        verify_enabled(cur,sid,facts)
        db.commit()
        return report

if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true")
    print(json.dumps(enable(parser.parse_args().apply),ensure_ascii=False,indent=2))
