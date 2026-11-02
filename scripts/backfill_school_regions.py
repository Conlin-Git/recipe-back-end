"""向本地库导入全国院校目录，仅回填缺失所在地；默认预演，--apply 事务写入。"""
import argparse
from collections import Counter, defaultdict
import json
import re
from functools import lru_cache
from pathlib import Path

from app.gaokao.image_policy import SOURCE_KEY
from app.gaokao.school_directory import DIR, MOE_HASH, Directory, norm, school_label
from scripts.import_sichuan_image_batches import connect

DDL = (
    """CREATE TABLE IF NOT EXISTS gaokao_school_directory (
        school_id CHAR(10) PRIMARY KEY, name VARCHAR(160) NOT NULL, province VARCHAR(30) NOT NULL,
        source_url VARCHAR(512) NOT NULL, locator VARCHAR(120) NOT NULL, snapshot_sha CHAR(64) NOT NULL,
        KEY ix_school_directory_region(province,name)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS gaokao_school_region_audit (
        admission_id BIGINT PRIMARY KEY, original_value VARCHAR(30), resolved_province VARCHAR(30) NOT NULL,
        raw_name VARCHAR(160) NOT NULL, university_code VARCHAR(40) NOT NULL, admission_province VARCHAR(30) NOT NULL,
        source_id BIGINT NOT NULL, raw_hash VARCHAR(64), evidence JSON NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


class Resolver:
    def __init__(self):
        self.directory = Directory()
        for r in json.loads((DIR/'sh-reviewed-aliases.json').read_text()):
            self.directory.alias[(r['prefix'],r['label'])] = (norm(r['name']), r['source'])
        self.hints = defaultdict(list)
        hints = DIR / 'shanghai-hints.json'
        if hints.exists():
            for h in json.loads(hints.read_text()):
                self.hints[(h['code'][:3],school_label(h['label']))].append(h)
        cache = DIR / 'llm-school-aliases.json'
        self.model = json.loads(cache.read_text()) if cache.exists() else {}

    @lru_cache(maxsize=None)
    def lexical_regions(self, label):
        # 对 3 字以上简称找出所有可能的官方校名。仅地区一致可使用；
        # 分校、医学院亦参与，比全国普通高校名单覆盖面更广。
        if len(label) < 3 or not re.fullmatch(r'[\u4e00-\u9fff]+',label): return set()
        pattern = re.compile('.*'.join(map(re.escape,label)))
        return {s.province for k,values in self.directory.index.items() if pattern.search(k)
                for s in values}

    def resolve(self, row):
        n = row['university_name']
        label = school_label(n)
        evidence = {}
        school = self.directory.exact(n) or self.directory.exact(label)
        method = 'directory_exact'
        if not school and row['province']=='上海' and row['year']==2026:
            identity = (row['university_code'][:3],label)
            binding = self.directory.alias.get(identity)
            if binding:
                school = self.directory.exact(binding[0])
                evidence['alias_source'] = binding[1]
                method = 'reviewed_alias'
            else:
                hints = self.hints.get(identity, [])
                suggestion = self.model.get(label,{}).get('proposed')
                # 目录转录有时把异地校区合并到本部。只允许已审核的
                # 校区全名映射，绝不因为转录索引显示本部而回填本部省份。
                campuses = {
                    '华电保定':'华北电力大学(保定)', '北师珠海':'北京师范大学(珠海校区)',
                    '哈工威海':'哈尔滨工业大学(威海)', '山大威海':'山东大学威海分校',
                    '北交威海':'北京交通大学(威海校区)', '人大苏州':'中国人民大学(苏州校区)',
                    '理工盘锦':'大连理工大学(盘锦校区)', '中油北克':'中国石油大学(北京)克拉玛依校区',
                    '电科沙河':'电子科技大学(沙河校区)','东大秦岛':'东北大学秦皇岛分校',
                    '复旦医学':'复旦大学医学院','交大医学':'上海交通大学医学院',
                }
                if label in campuses:
                    # 仅在同一院校简称与公布组代码存在于原表+目录时使用。
                    target=self.directory.exact(campuses[label])
                    if target and hints:
                        method='campus_alias_crosschecked'
                        school=target
                        evidence.update(alias_source=hints[0]['url'],matched_group=hints[0]['code'],
                                        official_label=hints[0]['label'])
                elif suggestion and hints:
                    target = self.directory.exact(suggestion)
                    # 转录目录与考试院原表的对应关系已经逐组核对。仍需 LLM 独立
                    # 识别出同一官方全名；模型和第三方均不能提供或覆盖地区数值。
                    names = {norm(h['school']) for h in hints}
                    if target and names=={norm(suggestion)}:
                        school = target
                        method = 'llm_alias_crosschecked'
                        h = hints[0]
                        evidence.update(model=self.model[label]['model'],proposal=suggestion,
                                        alias_source=h['url'],alias_sha=h['sha256'],matched_group=h['code'],
                                        official_label=h['label'])
                if not school and label not in campuses:
                    # 名单中的全称与简称有逐字对应，且所有可匹配的学校
                    # 只落在同一个省份。模糊结果只登记省份，不冒认学校身份。
                    regions=self.lexical_regions(label)
                    target=self.directory.exact(suggestion) if isinstance(suggestion,str) else None
                    hint_target=self.directory.exact(hints[0]['school']) if hints and len({h['school'] for h in hints})==1 else None
                    witness=hint_target or target
                    if witness and regions=={witness.province} and all(ch in witness.name for ch in label) and re.search('.*'.join(map(re.escape,label)),norm(witness.name)):
                        school=witness
                        method='region_consensus'
                        evidence.update(official_label=label,alias_source=hints[0]['url'] if hint_target else None,
                                        model=self.model.get(label,{}).get('model'),proposal=suggestion)
        if not school: return None
        return dict(province=school.province, school=school.name, method=method, source=school.source,
                    locator=school.locator, **evidence)


def national_entry(original,directory):
    # 全国名单的序号、校名和标识码来自教育部；个别主管省与校址不一致。
    from dataclasses import replace
    local=directory.manual.get(norm(original.name))
    if local and local.province!=original.province:
        return replace(original,province=local.province,source=local.source,locator=local.locator)
    return original


def run(apply=False, report_path=None):
    resolver=Resolver()
    with connect() as db, db.cursor() as cur:
        if apply:
            cur.execute("SELECT GET_LOCK('recipe_school_location_backfill',10) AS held")
            if cur.fetchone()['held'] != 1: raise RuntimeError('另一任务正在回填')
            for sql in DDL: cur.execute(sql)
        cur.execute("""SELECT id,province,year,university_name,university_code,source_id,raw_record_hash,school_province
                    FROM gaokao_admissions WHERE NULLIF(TRIM(school_province),'') IS NULL ORDER BY id"""+(" FOR UPDATE" if apply else ""))
        missing = cur.fetchall()
        peers = defaultdict(list)
        if any(r['university_name'].startswith('院校代码 ') for r in missing):
            # 同一归档、招生省、年份和地方院校代码下的完整校名才可以作证。
            cur.execute("""SELECT a.source_id,a.province,a.year,a.university_code,a.university_name,a.source_locator
                FROM gaokao_admissions a JOIN gaokao_sources s ON a.source_id=s.id
                WHERE s.source_key=%s AND a.verification_status='user_confirmed' AND a.university_name NOT LIKE '院校代码 %%'""",
                (SOURCE_KEY,))
            for other in cur.fetchall():
                peers[(other['source_id'],other['province'],other['year'],other['university_code'])].append(other)
        matched,unresolved = [],Counter()
        methods=Counter()
        totals=Counter()
        for r in missing:
            ev=resolver.resolve(r)
            if not ev and r['university_name'].startswith('院校代码 '):
                candidates=peers[(r['source_id'],r['province'],r['year'],r['university_code'])]
                if len({n['university_name'] for n in candidates}) == 1:
                    ev=resolver.resolve(candidates[0])
                    if ev:
                        ev.update(method='same_release_code',peer_locator=candidates[0]['source_locator'])
            if not ev:
                unresolved[(r['province'],r['university_name'])]+=1
                continue
            methods[ev['method']]+=1
            totals[r['province']]+=1
            matched.append((r,ev))
        if apply:
            cur.executemany("""INSERT INTO gaokao_school_directory(school_id,name,province,source_url,locator,snapshot_sha)
                VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE name=VALUES(name), province=VALUES(province),
                source_url=VALUES(source_url),locator=VALUES(locator),snapshot_sha=VALUES(snapshot_sha)""",
                [(s.code,s.name,s.province,s.source,s.locator,MOE_HASH) for original in resolver.directory.master for s in [national_entry(original,resolver.directory)]])
            if matched:
                cur.executemany("""INSERT INTO gaokao_school_region_audit
                    (admission_id,original_value,resolved_province,raw_name,university_code,admission_province,source_id,raw_hash,evidence)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [(r['id'],r['school_province'],ev['province'],r['university_name'],r['university_code'],r['province'],r['source_id'],r['raw_record_hash'],json.dumps(ev,ensure_ascii=False)) for r,ev in matched])
                cur.execute("""UPDATE gaokao_admissions a JOIN gaokao_school_region_audit u ON u.admission_id=a.id
                    SET a.school_province=u.resolved_province WHERE NULLIF(TRIM(a.school_province),'') IS NULL
                    AND BINARY a.university_name=BINARY u.raw_name AND a.source_id=u.source_id AND BINARY a.raw_record_hash <=> BINARY u.raw_hash""")
                if cur.rowcount!=len(matched): raise ValueError('回填条数不符，取消事务')
            db.commit()
        summary={'applied':apply,'national_directory':len(resolver.directory.master),'exam_regions_changed':dict(totals),'methods':dict(methods),
                 'missing_before':len(missing),'matched':len(matched),'remaining':sum(unresolved.values())}
        if report_path:
            Path(report_path).write_text(json.dumps({**summary,'unresolved':[dict(province=p,name=n,count=v) for (p,n),v in sorted(unresolved.items())]},indent=1,ensure_ascii=False)+'\n')
        return summary

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--report',help='另存待核清单 JSON')
    args=parser.parse_args()
    print(json.dumps(run(args.apply,args.report),indent=2,ensure_ascii=False))
