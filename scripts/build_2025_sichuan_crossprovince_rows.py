"""把各校官网公布的 2025 四川本科最低分整理成可审计 staging JSONL。
最低位次若官网未公布，严格按四川省教育考试院 2025 一分一段表把最低分换算，
并在 verification_note/evidence_note 中明确标注为分数段换算值。
"""
from __future__ import annotations
import json, hashlib, re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
manifest=json.loads((ROOT/'data/gaokao/2025_sichuan_crossprovince_sources.json').read_text())
byid={x['source_id']:x for x in manifest['sources']}
rank={}
for track in ['physical','history']:
 for line in (ROOT/f'data/gaokao/scoreband_seeds/2025-sichuan-{track}.jsonl').read_text().splitlines():
  x=json.loads(line); rank[(x['track'],x['score'])]=x['cumulative_rank']

def row(sid, university, code, track, score, *, group='普通本科', official_rank=None, note='', kind='group_admit'):
 src=byid[sid]; mr=official_rank or rank.get((track,score))
 if mr is None: raise ValueError((sid,track,score))
 derived=official_rank is None
 return {
  'province':'四川','school_province':src['school_province'],'year':2025,'track':track,
  'batch':'本科普通批','admission_round':'official_2025_sichuan_b','record_kind':kind,
  'university_code':code,'university_name':university,'group_code':group,'group_name':group,
  'major_code':'','major_name':'','subject_requirements':None,'min_score':score,'min_rank':mr,
  'avg_score':None,'max_score':None,'plan_count':None,'admission_count':None,'submission_count':None,
  'tie_breaker':None,'plan_type':None,'province_code':None,
  'source_locator':f"official:{sid}:四川本科普通批最低分",
  'source_sha256':src['sha256'],'source_artifact':src['source_artifact'],
  'raw_record_hash':hashlib.sha256(f"{sid}|{code}|{track}|{group}|{score}".encode()).hexdigest(),
  'verification_status':'verified','verified_at':None,
  'verification_note':('高校官网公布最低位次。' if not derived else '高校官网公布最低分；最低位次按四川省教育考试院2025一分一段表由最低分换算，非高校单独公布位次。') + (note or ''),
  'admission_status':'admitted','evidence_note': 'derived_rank_from_sichuan_2025_score_band' if derived else 'official_university_rank'
 }
rows={sid:[] for sid in byid}
# Guangdong: official ordinary/coop aggregate records.
rows['szu-2025-sichuan'] += [row('szu-2025-sichuan','深圳大学','SZU','物理类',611,group='普通本科',official_rank=16681),row('szu-2025-sichuan','深圳大学','SZU','历史类',607,group='普通本科',official_rank=3336),row('szu-2025-sichuan','深圳大学','SZU','物理类',592,group='中外合作办学',official_rank=27997)]
rows['jnu-2025-sichuan'] += [row('jnu-2025-sichuan','暨南大学','JNU','物理类',599,group='普通本科'),row('jnu-2025-sichuan','暨南大学','JNU','历史类',609,group='普通本科'),row('jnu-2025-sichuan','暨南大学','JNU','物理类',592,group='中外合作办学')]
rows['gdufe-2025-sichuan'] += [row('gdufe-2025-sichuan','广东财经大学','GDUFE','物理类',557,group='普通本科'),row('gdufe-2025-sichuan','广东财经大学','GDUFE','历史类',543,group='普通本科')]
# Shanghai: official ordinary/medical/coop aggregate records.
rows['fudan-2025-sichuan'] += [row('fudan-2025-sichuan','复旦大学','FDU','历史类',653,group='普通本科'),row('fudan-2025-sichuan','复旦大学','FDU','物理类',676,group='普通本科'),row('fudan-2025-sichuan','复旦大学','FDU','物理类',666,group='上海医学院')]
rows['tongji-2025-sichuan'] += [row('tongji-2025-sichuan','同济大学','TJU','历史类',645,group='普通本科'),row('tongji-2025-sichuan','同济大学','TJU','物理类',662,group='普通本科'),row('tongji-2025-sichuan','同济大学','TJU','物理类',653,group='医学类'),row('tongji-2025-sichuan','同济大学','TJU','物理类',653,group='中外合作办学'),row('tongji-2025-sichuan','同济大学','TJU','物理类',654,group='102组')]
rows['donghua-2025-sichuan'] += [row('donghua-2025-sichuan','东华大学','DHU','历史类',605,group='普通本科'),row('donghua-2025-sichuan','东华大学','DHU','物理类',610,group='普通本科')]
rows['usst-2025-sichuan'] += [row('usst-2025-sichuan','上海理工大学','USST','物理类',597,group='普通本科'),row('usst-2025-sichuan','上海理工大学','USST','历史类',573,group='普通本科'),row('usst-2025-sichuan','上海理工大学','USST','物理类',599,group='中外合作办学')]
# 上海大学官网表格按“普通、一本”列公布四川最低分；上海海事大学官网 PDF
# 按“陆上专业（不含航海、艺术、征集）”列公布四川最低分。两者均保留
# 官网的招生类型语义，不把专项计划混入普通批推荐。
rows['shu-2025-sichuan'] += [
 row('shu-2025-sichuan','上海大学','SHU','历史类',610,group='普通本科'),
 row('shu-2025-sichuan','上海大学','SHU','物理类',618,group='普通本科'),
 row('shu-2025-sichuan','上海大学','SHU','历史类',599,group='中外合作办学'),
 row('shu-2025-sichuan','上海大学','SHU','物理类',585,group='中外合作办学'),
]
rows['shmtu-2025-sichuan'] += [
 row('shmtu-2025-sichuan','上海海事大学','SHMTU','物理类',517,group='陆上专业（不含航海、艺术、征集）'),
 row('shmtu-2025-sichuan','上海海事大学','SHMTU','历史类',570,group='陆上专业（不含航海、艺术、征集）'),
]
out=ROOT/'data/gaokao/collection_runs/2025-sichuan-crossprovince-official-v1/staging'; out.mkdir(parents=True,exist_ok=True)
for sid,vals in rows.items():
 p=out/f'{sid}.jsonl'; p.write_text('\n'.join(json.dumps(v,ensure_ascii=False) for v in vals)+'\n')
 (out/f'{sid}.report.json').write_text(json.dumps({'source_id':sid,'status':'reviewed_verified','rows':len(vals),'rank_policy':'official rank when published; otherwise 2025 Sichuan official score-band conversion','universities':sorted({v['university_name'] for v in vals})},ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:len(v) for k,v in rows.items()},ensure_ascii=False))
