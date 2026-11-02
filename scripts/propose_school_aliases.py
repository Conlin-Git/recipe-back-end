"""让项目已配置的 LLM 识别未命中目录的简称。只发送校名，不发送成绩或用户数据。"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from openai import OpenAI

from app.config import settings
from app.gaokao.school_directory import Directory, DIR, school_label
from scripts.import_sichuan_image_batches import connect

CACHE = DIR / "llm-school-aliases.json"


def run():
    directory = Directory()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT university_name FROM gaokao_admissions WHERE NULLIF(TRIM(school_province),'') IS NULL")
        names = sorted({school_label(r['university_name']) for r in cur.fetchall()})
    todo = [n for n in names if not directory.exact(n) and n not in cache and not n.startswith('院校代码')]
    client = OpenAI(api_key=settings.LLM_API_KEY,base_url=settings.LLM_BASE_URL,timeout=35,max_retries=1)
    def propose(names):
        prompt = "将下面招生表中的高校简称识别为对应的中国高校官方全称。保留异地校区。不要根据生源地猜测。无法唯一识别写 null。只输出JSON对象，每个输入为键，全称字符串或null为值，不要解释。\n"+json.dumps(names,ensure_ascii=False)
        resp=client.chat.completions.create(model=settings.LLM_MODEL,messages=[{'role':'user','content':prompt}],temperature=0,max_tokens=1600,extra_body={'thinking':{'type':settings.LLM_THINKING}} if settings.LLM_THINKING else {})
        s=resp.choices[0].message.content or ''
        data=json.loads(s[s.index('{'):s.rindex('}')+1])
        return {k:{'proposed':v,'model':settings.LLM_MODEL} for k,v in data.items() if k in names and (v is None or isinstance(v,str) and len(v)<=100)}
    print('未缓存校名',len(todo),flush=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs=[pool.submit(propose,todo[i:i+18]) for i in range(0,len(todo),18)]
        for job in as_completed(jobs):
            try: cache.update(job.result())
            except Exception as e:
                print('可重试批次:',type(e).__name__, flush=True)
                continue
            tmp=CACHE.with_suffix('.tmp')
            tmp.write_text(json.dumps(cache,sort_keys=True,indent=1,ensure_ascii=False)+'\n')
            tmp.replace(CACHE)
    print('缓存别名:',len(cache), '未获取:',sum(n not in cache for n in todo))

if __name__=='__main__': run()
