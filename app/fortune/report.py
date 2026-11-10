"""Readable serialization of engine output. No LLM or authored fortune readings."""
BRANCHES = "子丑寅卯辰巳午未申酉戌亥"
TYPE_NAMES = {"major": "主星", "minor": "辅曜", "lucky": "吉曜", "sha": "煞曜"}


def star_text(star: dict) -> str:
    fields = [TYPE_NAMES.get(star["type"], star["type"])]
    if star.get("brightnessRaw"):
        fields.append(star["brightnessRaw"])
    if star.get("siHua"):
        fields.append("生年化" + star["siHua"])
    return star["name"] + "（" + " · ".join(fields) + "）"


def marks_text(items: list) -> str:
    return "；".join(f"{m['star']}化{m['type']}·{m['palace']}" for m in items) or "无"


def palace_lines(result: dict, palace: dict) -> list[str]:
    detail = next(p for p in result["details"]["palace_details"] if p["branch"] == palace["branch"])
    badges = [label for key, label in [("isMingGong", "命宫"), ("isShenGong", "身宫")] if palace.get(key)]
    lines = [f"### {palace['name']} · {detail['stem_branch']}" + (f"（{'、'.join(badges)}）" if badges else "")]
    lines.append("星曜：" + "、".join(star_text(s) for s in palace["stars"]) + "。")
    if palace.get("borrowedStars"):
        lines.append(f"本宫无主星，借对宫{palace.get('borrowedFromName', '')}：{'、'.join(palace['borrowedStars'])}。")
    if palace.get("daXianAge"):
        lines.append(f"大限年龄：{palace['daXianAge'][0]}–{palace['daXianAge'][1]} 虚岁。")
    related = {(palace["branch"] + n) % 12 for n in (0, 4, 6, 8)}
    lines.append("三方四正：" + "、".join(p["name"] for p in result["chart"]["palaces"] if p["branch"] in related) + "。")
    lines.append("宫干自化：" + ("、".join(f"{m['starName']}自化{m['siHua']}" for m in detail["self_sihua"]) or "无") + "。")
    for mark in detail["incoming"]:
        lines.append(f"{mark['star']}化{mark['type']}的宫干来源：{'、'.join(mark['from']) or '无'}。")
    lines.append("十二神：" + "、".join(detail["spirits"]) + "。")
    return lines


def build_engine_report(result: dict, palace: str | None = None, *, include_notes: bool = True) -> str:
    chart, summary, timing, detail = (result[k] for k in ("chart", "summary", "timing", "details"))
    if palace:
        chosen = next(p for p in chart["palaces"] if p["name"] == palace)
        return "\n\n".join(palace_lines(result, chosen) + [result["disclaimer"]])
    birth = result["input"]
    lines = ["## 一、命盘概览", "本报告直接整理开源紫微引擎的计算结果与规则文字，未使用 AI 补写断语。",
        f"公历 {timing['solar_date']}；农历 {summary['lunar_date']}；输入时间 {birth['birth_time']}，UTC{birth['utc_offset_minutes'] / 60:+g}。",
        f"排盘日期 {timing['chart_date']}；采用时间 {timing['clock']}；时间校正 {timing['correction_minutes']} 分钟。",
        f"{chart.get('yinYangGender', '')} · {summary['wuxing']} · 命宫 {summary['ming_branch']} · 身宫 {summary['shen_branch']} · 命主 {summary['ming_zhu']} · 身主 {summary['shen_zhu']}。",
        f"引擎当前年龄：{chart['currentAge']} 虚岁。",
        "四柱（节气）：" + "、".join(chart["lunarInfo"].get("fourPillars", [])) + "。",
        "四柱（农历）：" + "、".join(chart["lunarInfo"].get("fourPillarsLunar", [])) + "。",
        "命宫主星：" + ("、".join(detail["ming_summary"]["stars"]) or "空宫") + "；星性：" + detail["ming_summary"]["nature"] + "。",
        "引擎关键词：" + ("、".join(detail["ming_summary"]["keywords"]) or "无") + "。",
        "## 二、十二宫完整星曜"]
    for p in chart["palaces"]:
        lines.extend(palace_lines(result, p))
    lines += ["## 三、生年四化", marks_text(result["sihua"]) + "。", "## 四、格局识别"]
    for pattern in detail["patterns"]:
        lines += ["### " + pattern["name"], pattern["description"], "涉及宫位：" + "、".join(pattern["palaces"]) + "。"]
        for key, label in [("required", "成立条件"), ("bonus", "附加条件"), ("breaking", "破格条件")]:
            values = (pattern.get("conditions") or {}).get(key)
            if values:
                lines.append(label + "：" + "；".join(values) + "。")
        if pattern.get("source"):
            lines.append("出处：" + pattern["source"])
    if not detail["patterns"]:
        lines.append("未匹配到引擎规则中的特定格局。")
    lines += ["## 五、大限全览", "年龄统一按虚岁计算；起大限前的童限逐岁数据见下一节。"]
    rows = ["| 虚岁 | 农历年份 | 所在本命宫 | 大限四化 |", "| --- | --- | --- | --- |"]
    rows.extend(f"| {p['start_age']}–{p['end_age']} | {p['start_year']}–{p['end_year']} | {p['palace_name']} | {marks_text(p['sihua'])} |" for p in detail["periods"])
    lines.append("\n".join(rows))
    first_age, last_age = detail["years"][0]["age"], detail["years"][-1]["age"]
    lines += ["## 六、逐年排盘数据", f"当前为 {chart['currentAge']} 虚岁。以下计算范围为 {first_age}–{last_age} 虚岁，由十二个十年阶段展开，末尾年龄随起始年龄变化，不代表当前年龄或寿命。农历正月初一换年；四化后的宫名均指本命宫位。"]
    rows = ["| 虚岁 | 农历流年 | 流年命宫落宫 | 所属大限 | 流年四化 | 大限 / 童限四化 |", "| --- | --- | --- | --- | --- | --- |"]
    for y in detail["years"]:
        p = next((p for p in detail["periods"] if p["index"] == y["period_index"]), None)
        period = f"{p['start_age']}–{p['end_age']}岁·{p['palace_name']}" if p else "童限"
        rows.append(f"| {y['age']} | {y['year']} {y['ganzhi']} | {y['palace_name']}（{BRANCHES[y['palace_branch']]}） | {period} | {marks_text(y['sihua'])} | {marks_text(y['period_sihua'])} |")
    lines.append("\n".join(rows))
    if include_notes:
        lines += ["## 排盘口径与来源", "\n".join('- ' + note for note in result["conventions"]),
            f"来源：[{result['source']['name']}]({result['source']['url']})。", result["disclaimer"]]
    return "\n\n".join(lines)


def build_brief_report(result: dict, palace: str | None = None) -> str:
    topics = {"命宫": "性格与处事方式", "官禄": "工作与做事方式", "夫妻": "感情与相处", "财帛": "金钱与资源", "福德": "内心感受与休息", "迁移": "外出与环境", "兄弟": "同辈相处", "子女": "与晚辈的互动", "疾厄": "传统健康主题", "仆役": "朋友与合作", "田宅": "居住与家庭环境", "父母": "长辈相处"}
    selected = [p for p in result["chart"]["palaces"] if p["name"] == (palace or "命宫")]
    lines = ["## 命盘简要说明"]
    for p in selected:
        stars = [s["name"] for s in p["stars"] if s["type"] == "major"]
        lines += [f"### {p['name']} · {topics.get(p['name'], p['name'])}",
                  f"这个宫位在传统解读中讨论{topics.get(p['name'], p['name'])}。"]
        if stars:
            lines.append("本盘的主星是" + "、".join(stars) + "。这些是传统分类，不能仅凭星名判断现实中的性格或经历。")
        else:
            lines.append("这里没有主星，解读时要参考对面的宫位" + ("，本盘参考" + "、".join(p.get("borrowedStars", [])) if p.get("borrowedStars") else "") + "；不代表生活中缺少这一方面。")
    lines += ["可以继续问工作、感情，或指定某一年的解读。", result["disclaimer"]]
    return "\n\n".join(lines)
