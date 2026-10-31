"""可复现的命理命名计算核心。

这里把传统命理的历法计算、五行启发式和语言文化风险分开记录。当前规则集是
bootstrap 版本，输出明确标注“传统文化分析/启发式评分”，不把流派差异伪装成科学事实。
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from lunar_python import Lunar, Solar

RULESET_VERSION = "bazi-bootstrap-v2"
KNOWLEDGE_SNAPSHOT_ID = "naming-sources-2026-10-31"
CHARACTER_VERSION = "characters-bootstrap-v1"
KANGXI_VERSION = "kangxi-bootstrap-v1"
RISK_VERSION = "risk-bootstrap-v1"
REFERENCE_IDS = ["gf3002-1999", "unicode-unihan", "pmo-calendar", "naming-cultural-bootstrap"]
ELEMENTS = ("木", "火", "土", "金", "水")
DATA_DIR = Path(__file__).parent / "data"


def _read_json(filename: str) -> Any:
    return json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))


CHARACTERS: list[dict[str, str]] = _read_json("characters.json")
CHARACTER_MAP = {item["char"]: item for item in CHARACTERS}
KANGXI_STROKES: dict[str, int] = _read_json("kangxi_strokes.json")
RISK_DATA = _read_json("risk_terms.json")
RISK_MAP = {item["term"]: item for item in RISK_DATA["terms"]}
BAZI_RULES = _read_json("bazi_rules.json")


def canonical_input(data: dict[str, Any]) -> dict[str, Any]:
    """统一日期时间和字符串，保证同输入跨进程得到同一指纹。"""
    result = dict(data)
    birth_date = result.get("birth_date")
    birth_time = result.get("birth_time")
    if isinstance(birth_date, (date, datetime)):
        result["birth_date"] = birth_date.isoformat()
    if isinstance(birth_time, time):
        result["birth_time"] = birth_time.isoformat()
    for key, value in list(result.items()):
        if isinstance(value, str):
            result[key] = value.strip()
    result.setdefault("calendar_type", "solar")
    result.setdefault("timezone", "Asia/Shanghai")
    result.setdefault("purpose", "person_name")
    result.setdefault("variant_index", 0)
    result.setdefault("given_length", 2)
    result.setdefault("avoid_chars", "")
    result.setdefault("preferred_elements", [])
    return result


def input_fingerprint(user_id: int, data: dict[str, Any]) -> str:
    payload = {
        "user_id": int(user_id),
        "input": canonical_input(data),
        "ruleset_version": RULESET_VERSION,
        "knowledge_snapshot_id": KNOWLEDGE_SNAPSHOT_ID,
        "character_version": CHARACTER_VERSION,
        "kangxi_version": KANGXI_VERSION,
        "risk_version": RISK_VERSION,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _dt(data: dict[str, Any]) -> tuple[date, time]:
    d = data["birth_date"]
    if isinstance(d, str):
        d = date.fromisoformat(d)
    t = data.get("birth_time")
    if isinstance(t, str):
        t = time.fromisoformat(t)
    return d, t or time(12, 0)


def _bazi(data: dict[str, Any]) -> dict[str, Any]:
    d, t = _dt(data)
    if data.get("calendar_type", "solar") == "lunar":
        # lunar-python 对常规农历日期提供稳定转换；闰月由接口提前拒绝，避免默算错误。
        lunar = Lunar.fromYmdHms(d.year, d.month, d.day, t.hour, t.minute, t.second)
        solar = lunar.getSolar()
    else:
        solar = Solar.fromYmdHms(d.year, d.month, d.day, t.hour, t.minute, t.second)
    eight = solar.getLunar().getEightChar()
    pillars = [eight.getYear(), eight.getMonth(), eight.getDay(), eight.getTime()]
    pillar_elements = [eight.getYearWuXing(), eight.getMonthWuXing(), eight.getDayWuXing(), eight.getTimeWuXing()]
    counts = Counter("".join(pillar_elements))
    counts = {element: int(counts.get(element, 0)) for element in ELEMENTS}
    least = min(counts.values())
    support = [element for element in ELEMENTS if counts[element] == least]
    day_master_element = (eight.getDayWuXing() or "")[0]
    month_branch = pillars[1][-1]
    month_element = BAZI_RULES["month_order"].get(month_branch)
    generated_by = BAZI_RULES["relations"]["生"].get(day_master_element)
    controlled_by = next((element for element, target in BAZI_RULES["relations"]["克"].items() if target == day_master_element), None)
    strength_score = (2 if month_element == day_master_element else 0) + (1 if month_element == generated_by else 0) - (1 if month_element == controlled_by else 0)
    strength = "偏强" if strength_score >= 2 else ("偏弱" if strength_score <= -1 else "中和"
    )
    return {
        "calendar_type": data.get("calendar_type", "solar"),
        "input_date": d.isoformat(),
        "input_time": t.isoformat(),
        "solar_date": f"{solar.getYear():04d}-{solar.getMonth():02d}-{solar.getDay():02d}",
        "pillars": pillars,
        "pillar_elements": pillar_elements,
        "day_master": eight.getDayGan(),
        "day_master_element": day_master_element,
        "month_branch": month_branch,
        "month_element": month_element,
        "day_master_strength": {"label": strength, "score": strength_score, "rule_id": "BAZI-STRENGTH-001"},
        "useful_elements_heuristic": support,
        "pattern_judgement": {"label": "平衡启发式，未判定格局", "rule_id": "BAZI-PATTERN-001"},
        "element_counts": counts,
        "least_elements": support,
        "method": "lunar-python 1.4.8 / civil time / 节气四柱",
        "timezone": data.get("timezone", "Asia/Shanghai"),
        "caveats": [
            "当前按民用时间计算，未做真太阳时校正。",
            "五行喜用仅按元素数量平衡启发式，不等同于任何单一命理流派的格局定论。",
            "日主强弱、月令和生克关系是 bootstrap 粗粒度规则，正式版需由专家按流派审核。",
        ],
    }


def _char_detail(char: str) -> dict[str, Any]:
    item = CHARACTER_MAP.get(char)
    risk = RISK_MAP.get(char)
    return {
        "char": char,
        "element": item.get("element") if item else None,
        "meaning": item.get("meaning", "待人工补充") if item else "待人工补充",
        "kangxi_strokes": KANGXI_STROKES.get(char),
        "character_source": item.get("source") if item else "UNKNOWN",
        "stroke_source": "kangxi-bootstrap-v1" if char in KANGXI_STROKES else "UNKNOWN",
        "risk": risk,
    }


def _risk_for_name(name: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for term, item in RISK_MAP.items():
        if term in name:
            findings.append({"term": term, **item, "rule_id": "RISK-TERM-001"})
    if len(set(name)) != len(name):
        findings.append({"term": name, "risk": "duplicate_char", "severity": "medium", "note": "名字出现重复字", "rule_id": "RISK-STRUCT-001"})
    if any(ord(char) > 0xFFFF for char in name):
        findings.append({"term": name, "risk": "unsupported_char", "severity": "medium", "note": "超出当前基础数据覆盖范围", "rule_id": "RISK-DATA-001"})
    return findings


def _score_name(name: str, bazi: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    details = [_char_detail(char) for char in name]
    preferred = list(data.get("preferred_elements") or [])
    target = preferred or list(bazi["least_elements"])
    known = [d for d in details if d["element"]]
    match = sum(1 for d in known if d["element"] in target)
    element_score = min(40.0, 20.0 + (20.0 * match / max(1, len(details))))
    meaning_score = 20.0 * len(known) / max(1, len(details))
    structure_score = 15.0 if len(set(name)) == len(name) else 8.0
    purpose = data.get("purpose", "person_name")
    style = str(data.get("style") or "")
    purpose_score = 15.0
    if purpose == "screen_name" and (style or any(c in name for c in "云野星舟墨")):
        purpose_score += 0.0
    if purpose == "person_name" and len(name) not in (1, 2, 3):
        purpose_score = 8.0
    risks = _risk_for_name(name)
    risk_score = max(0.0, 10.0 - sum(6.0 if r["severity"] == "high" else 3.0 for r in risks))
    total = round(element_score + meaning_score + structure_score + purpose_score + risk_score, 2)
    return {
        "total": total,
        "element_balance": round(element_score, 2),
        "meaning": round(meaning_score, 2),
        "structure": round(structure_score, 2),
        "purpose": round(purpose_score, 2),
        "risk": round(risk_score, 2),
        "target_elements": target,
        "char_details": details,
        "risk_findings": risks,
        "evidence": [
            {"rule_id": "BAZI-ELEMENT-001", "claim": "候选字元素与八字低频元素相配时提高启发式分", "source": "naming-cultural-bootstrap-v1"},
            {"rule_id": "CHAR-MEANING-001", "claim": "字义和字符属性来自当前字符快照", "source": CHARACTER_VERSION},
            {"rule_id": "KANGXI-001", "claim": "康熙笔画单独记录，未与现代笔画混用", "source": KANGXI_VERSION},
            {"rule_id": "RISK-001", "claim": "负面词和重复字触发风险扣分", "source": RISK_VERSION},
        ],
    }


def _candidate_pool(data: dict[str, Any], bazi: dict[str, Any]) -> list[str]:
    avoid = set(str(data.get("avoid_chars") or ""))
    preferred = set(data.get("preferred_elements") or bazi["least_elements"])
    ranked = sorted(
        (item for item in CHARACTERS if item["char"] not in avoid),
        key=lambda item: (0 if item["element"] in preferred else 1, item["char"]),
    )
    # 有界候选池让三字名也能在请求内稳定完成，后续由专家扩充数据后再提高上限。
    return [item["char"] for item in ranked[:24]]


def _generate_candidates(data: dict[str, Any], bazi: dict[str, Any], user_id: int) -> list[dict[str, Any]]:
    length = max(1, min(3, int(data.get("given_length", 2))))
    pool = _candidate_pool(data, bazi)
    generation = str(data.get("generation_char") or "")
    seed_text = f"{user_id}|{input_fingerprint(user_id, data)}|{data.get('variant_index', 0)}"
    seed = int(hashlib.sha256(seed_text.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    if length == 1:
        names = pool
    else:
        names = []
        for _ in range(min(3000, max(300, len(pool) ** length))):
            chars = [rng.choice(pool) for _ in range(length)]
            if generation and generation in pool:
                chars[0] = generation
            names.append("".join(chars))
        names = list(dict.fromkeys(names))
    scored = [{"name": name, "score_breakdown": _score_name(name, bazi, data)} for name in names]
    for item in scored:
        item["score"] = item["score_breakdown"]["total"]
        item["tie_breaker"] = hashlib.sha256(f"{seed_text}|{item['name']}".encode()).hexdigest()
    scored.sort(key=lambda item: (-item["score"], item["tie_breaker"]))
    for item in scored:
        item.pop("tie_breaker", None)
    return scored[:12]


def _references() -> list[dict[str, str]]:
    return [
        {"id": "gf3002-1999", "title": "GF 3002-1999《GB13000.1字符集汉字笔顺规范》", "url": "https://www.moe.gov.cn/jyb_sjzl/ziliao/A19/201001/W020150902457900316281.pdf", "usage": "现代规范字形/笔顺参考；不替代康熙笔画"},
        {"id": "unicode-unihan", "title": "Unicode Standard Annex #38: Unicode Han Database", "url": "https://www.unicode.org/reports/tr38/", "usage": "字符属性、异体和读音参考"},
        {"id": "pmo-calendar", "title": "中国科学院紫金山天文台历书资料", "url": "https://www.pmo.cas.cn/xwdt2019/kpdt2019/202203/t20220309_6386774.html", "usage": "历法与节气校验来源"},
        {"id": "lunar-python", "title": "lunar-python 1.4.8", "url": "https://github.com/6tail/lunar-python", "usage": "运行时四柱计算实现版本"},
        {"id": "naming-cultural-bootstrap", "title": "命名文化规则 bootstrap 快照", "url": "docs/naming/REFERENCE_SOURCES.md", "usage": "当前五行映射、康熙笔画与风险库的版本边界"},
    ]


def _report(data: dict[str, Any], bazi: dict[str, Any], candidates: list[dict[str, Any]], score: dict[str, Any] | None, mode: str) -> str:
    lines = ["# 命理命名分析报告", "", f"- 分析模式：{mode}", f"- 用途：{data.get('purpose', 'person_name')}", f"- 规则版本：`{RULESET_VERSION}`", f"- 知识快照：`{KNOWLEDGE_SNAPSHOT_ID}`", "", "## 一、输入与四柱", "", f"- 出生日期：{bazi['input_date']} {bazi['input_time']}（{bazi['timezone']}）", f"- 四柱：`{' '.join(bazi['pillars'])}`", f"- 日主：`{bazi['day_master']}`", f"- 五行统计：" + "、".join(f"{e}{bazi['element_counts'][e]}" for e in ELEMENTS), f"- 本规则集低频元素：`{'、'.join(bazi['least_elements'])}`", "", "## 二、方法与边界", "", "本报告将传统命理术语转换为可复现的文化分析启发式。它不宣称科学预测，也不把不同流派的喜用神结论合并成单一事实。汉字五行映射、康熙笔画和风险库都独立标注版本。"]
    if mode == "generate":
        lines += ["", "## 三、候选结果", ""]
        for i, item in enumerate(candidates, 1):
            lines.append(f"{i}. **{data.get('surname', '')}{item['name']}**（{item['score']}分）——目标五行：{'、'.join(item['score_breakdown']['target_elements'])}；字义：{'；'.join(d['meaning'] for d in item['score_breakdown']['char_details'])}")
    else:
        lines += ["", "## 三、姓名评分", "", f"- 姓名：**{data.get('surname', '')}{data.get('name', '')}**", f"- 总分：**{score['total']} / 100**", f"- 分项：五行平衡 {score['element_balance']}、字义 {score['meaning']}、结构 {score['structure']}、用途 {score['purpose']}、风险 {score['risk']}"]
        if score["risk_findings"]:
            lines.append("- 风险提示：" + "；".join(f"{r['term']}（{r['note']}）" for r in score["risk_findings"]))
    lines += ["", "## 四、逐项证据", ""]
    evidence = score["evidence"] if score else (candidates[0]["score_breakdown"]["evidence"] if candidates else [])
    lines.extend(f"- `{e['rule_id']}`：{e['claim']}（来源：`{e['source']}`）" for e in evidence)
    lines += ["", "## 五、参考文献", ""]
    lines.extend(f"- [{ref['id']}] {ref['title']}：{ref['url']}（{ref['usage']}）" for ref in _references())
    lines += ["", "> 免责声明：本结果用于传统文化与语言文化分析，不能替代户籍、法律、医学、心理或其他专业意见。"]
    return "\n".join(lines)


class NamingEngine:
    """无数据库副作用的命理命名引擎。"""

    @staticmethod
    def normalize(data: dict[str, Any]) -> dict[str, Any]:
        result = canonical_input(data)
        result["preferred_elements"] = list(result.get("preferred_elements") or [])
        if result.get("calendar_type") not in {"solar", "lunar"}:
            raise ValueError("calendar_type 必须是 solar 或 lunar")
        if result.get("lunar_leap_month"):
            raise ValueError("当前版本暂不接受闰月输入，请先关闭 lunar_leap_month")
        return result

    def analyze(self, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
        data = self.normalize(data)
        bazi = _bazi(data)
        candidates = _generate_candidates(data, bazi, user_id)
        fingerprint = input_fingerprint(user_id, data)
        return {
            "mode": "generate", "input": data, "input_fingerprint": fingerprint,
            "ruleset_version": RULESET_VERSION, "knowledge_snapshot_id": KNOWLEDGE_SNAPSHOT_ID,
            "character_version": CHARACTER_VERSION, "kangxi_version": KANGXI_VERSION, "risk_version": RISK_VERSION,
            "bazi": bazi, "candidates": candidates, "score_breakdown": candidates[0]["score_breakdown"] if candidates else None,
            "evidence": candidates[0]["score_breakdown"]["evidence"] if candidates else [], "references": _references(),
            "report_markdown": _report(data, bazi, candidates, None, "generate"),
        }

    def score(self, user_id: int, data: dict[str, Any]) -> dict[str, Any]:
        data = self.normalize(data)
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("name 不能为空")
        bazi = _bazi(data)
        score = _score_name(name, bazi, data)
        fingerprint = input_fingerprint(user_id, data)
        return {
            "mode": "score", "input": data, "input_fingerprint": fingerprint,
            "ruleset_version": RULESET_VERSION, "knowledge_snapshot_id": KNOWLEDGE_SNAPSHOT_ID,
            "character_version": CHARACTER_VERSION, "kangxi_version": KANGXI_VERSION, "risk_version": RISK_VERSION,
            "bazi": bazi, "score_breakdown": score, "evidence": score["evidence"], "references": _references(),
            "report_markdown": _report(data, bazi, [], score, "score"),
        }
