"""采集上海专业目录的公开转录，仅作校名消歧佐证，不作为招生事实来源。"""
import concurrent.futures
import hashlib
import json
import re
from pathlib import Path
import httpx

from scripts.import_sichuan_image_batches import connect

BASE = "https://aisou.yydl.com/tools/zsjh/shanghai-2026/"
OUT = Path(__file__).resolve().parents[1] / "app/gaokao/data/locations/shanghai-hints.json"
CACHE = Path(__file__).resolve().parents[1] / ".cache/gaokao-location-hints"


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    with connect() as db, db.cursor() as c:
        c.execute("SELECT DISTINCT university_code,university_name FROM gaokao_admissions WHERE province='上海' AND year=2026 AND batch='本科普通批'")
        official = {(r['university_code'], r['university_name']) for r in c.fetchall()}
    with httpx.Client(follow_redirects=True, timeout=15) as client:
        def fetch(name):
            path = CACHE/name
            if not path.is_file():
                r = client.get(BASE + ('' if name == 'index.html' else name))
                r.raise_for_status()
                path.write_bytes(r.content)
            return path.read_bytes()
        listing = fetch('index.html').decode()
        section = re.search(r'<h2>本科普通批次[^<]*</h2><div[^>]*>(.*?)</div>', listing, re.S).group(1)
        links = dict(re.findall(r'<a href="([a-z0-9-]+\.html)">([^<>]+)</a>', section))

        def read(item):
            path, name = item
            data = fetch(path)
            rows = re.findall(r'<td>本科普通批次</td><td>([0-9A-Z]{5})</td><td>[^<>]*</td><td></td><td>([^<>]+)</td>', data.decode())
            return [dict(code=code, label=label, school=name, url=BASE+path, sha256=hashlib.sha256(data).hexdigest()) for code,label in rows if (code,label) in official]

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            jobs = {pool.submit(read,item):item[0] for item in links.items()}
            for job in concurrent.futures.as_completed(jobs):
                try: results.extend(job.result())
                except (httpx.HTTPError, UnicodeError) as e: print(jobs[job], type(e).__name__, flush=True)
    if len(results) < 500: raise ValueError("转录佐证覆盖异常；不覆盖已有快照")
    with OUT.with_suffix('.tmp').open('w') as out:
        json.dump(sorted(results,key=lambda e:(e['code'],e['school'])), out, ensure_ascii=False, indent=1)
    OUT.with_suffix('.tmp').replace(OUT)
    print('可与上海考试院投档原表按组代码与简称同时对应的佐证条数:',len(results))

if __name__ == '__main__': main()
