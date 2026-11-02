"""院校所在地的证据库。地方招生代码和国标码不通用，分校不能归回本部。"""
from dataclasses import dataclass
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import unicodedata

import xlrd
import pymupdf

from app.gaokao.regions import SCHOOL_REGIONS, short_region

DIR = Path(__file__).parent / "data/locations"
MOE = "https://www.moe.gov.cn/jyb_xxgk/s5743/s5744/202606/W020260618416094865984.xls"
ZJ = "https://www.zjzs.net/col/xk2024/allcollege.html"
SH = "https://cdn.shmeea.edu.cn/resource/upload/www/20260723904/150.pdf"
MOE_HASH = "29c40f083b639888e429cf40b68f9a75782d1ce81131c99aabca65b65b36eaea"
ZJ_HASH = "89cf28574937c831796ee79a0b6394941a21f6518f7d8444e87068e6edad09c4"
SH_HASH = "1bb84d79c188d53817a824ae1e7d7bdae6caec525ea92b276a2721e8900c9656"


def checked(file, digest):
    data = (DIR / file).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest: raise ValueError(f"来源指纹不匹配: {file}")
    return data


def norm(name):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', name))


def school_label(name):
    """只去除末尾的招生标记；不随意截掉括号内的校区、地点或分校。"""
    s = norm(name)
    while True:
        m = re.search(r'\(([^()]*)\)?$', s)
        if not m: return s
        suffix = m[1]
        if not re.fullmatch(r'(?:[A-Z]?\d+|[组組]\d+|医学护理类|预防医学|农学|艺术类|护理|临床医学|公卫|中外双学士|内地澳门联合培养|闽台联合培养|中美人才培养|联合办学|内地香港办学|桂台合作办学|豫台联合培养|国际本科学术互认|-联合培养|(?:中外合作|中外高水平|地方专项|高校专项|联合培养|校企联合培养|协同培养|本科预科班|少数民族班|国际班|国际课程|学分互认|闽台合作|面向|粤台联合|中美121|中美计划|内地香港合作办学|内地澳门合作办学|粤澳合作|粤澳联合|原)(?:[^()]*)?)', suffix):
            return s
        s = s[:m.start()]


@dataclass(frozen=True)
class School:
    name: str
    province: str
    code: str
    source: str
    locator: str


class Directory:
    def __init__(self):
        self.master = []
        self.index = defaultdict(list)
        self.alias = {}
        sheet = xlrd.open_workbook(file_contents=checked('moe-2026.xls', MOE_HASH)).sheet_by_index(0)
        declared = {}
        province = None
        for i in range(3, sheet.nrows):
            seq, name, code, _, _, _, _ = sheet.row_values(i)
            if isinstance(seq, str):
                header = re.fullmatch(r'(.+)（(\d+)所）', seq)
                if not header: raise ValueError(f"未知省份标题: {seq}")
                province = short_region(header[1])
                if province not in SCHOOL_REGIONS: raise ValueError(province)
                declared[province] = int(header[2])
            else:
                row = School(name.strip(), province, str(int(code)), MOE, f"全国普通高等学校名单!B{i+1}")
                self.master.append(row)
                self.index[norm(row.name)].append(row)
                # 教育部名单名称不含招生分校后缀；建立审计人工校区别名时再覆盖。
        from collections import Counter
        if Counter(r.province for r in self.master) != declared or len(self.master)!=2952 or len({r.code for r in self.master}) != 2952:
            raise ValueError("教育部目录不完整")
        html = checked('zjzs-2024.html', ZJ_HASH).decode('utf8')
        for region, code, name in re.findall(r'<td>([^<>]+)</td>\s*<td>(\d{5})</td>\s*<td>([^<>]+)</td>', html):
            if region in SCHOOL_REGIONS:
                self.index[norm(name)].append(School(name, region, code, ZJ, f"院校国标码 {code}"))
        with pymupdf.open(stream=checked('shmeea-2026-aliases.pdf', SH_HASH),filetype='pdf') as pdf:
            for p in pdf:
                for name,label,code in re.findall(r'([^\n]+)\n\d+\n[^\n]{1,12}\n院校简称[:：]([^\n]+)\n([A-Z0-9]{5})', p.get_text()):
                    self.alias[(code[:3],norm(label))] = (norm(name),f"{SH}#page={p.number+1}")
        # 目录外的校区、军事院校可按官方资料补录；禁用模糊匹配。
        self.manual = {}
        manual = DIR/'reviewed-locations.json'
        if manual.is_file():
            for row in json.loads(manual.read_text()):
                if row['region'] not in SCHOOL_REGIONS or not row['source'].startswith('https://'): raise ValueError(row)
                self.manual[norm(row['name'])] = School(row['name'],row['region'],'',row['source'],row['locator'])

    def exact(self, name):
        key = norm(name)
        if key in self.manual: return self.manual[key]
        values = self.index.get(key, [])
        if len({s.province for s in values}) == 1: return values[0]
        if key.count('(')==1 and ')' not in key:
            # 福建转录偶尔丢失末尾右括号，只在补全后精确匹配时使用。
            return self.exact(key+')')
        return None
