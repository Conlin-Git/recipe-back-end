"""Report layout derived from the user's reference, using only this chart's facts."""


def reference_context(topics: list[dict], result: dict) -> list[dict]:
    by_id = {t['id']: dict(t) for t in topics}
    if 'overview' not in by_id:
        return topics
    chart = result['chart']
    palaces = chart['palaces']
    ordered = []
    def add(topic, chapter, title=None):
        topic = {**topic, 'chapter': chapter}
        if title:
            topic['title'] = title
        ordered.append(topic)
    if 'personality' in by_id:
        add(by_id['personality'], '一、命宫核心格局', '命宫星曜与性格特点')
    body = next((p for p in palaces if p.get('isShenGong') or p.get('branch') == chart.get('shenGongBranch')), None)
    if body:
        add({'id': 'body', 'palace': body['name'], 'stars': body['stars'], 'borrowed_stars': body.get('borrowedStars', []),
             'explain': '按实际身宫星曜、亮度、辅星说明行动与生活重心，并解释与命宫处事方式的联系。不把身宫当成身体诊断。'},
            '二、身宫分析', f"身宫（在{body['name']}）")
    overview = by_id['overview']
    overview.update(palaces=palaces, patterns=result['details'].get('patterns', []),
                    explain='依据实际命盘选出值得关注的组合，先列宫位、主星、亮度和辅星，再解释相互关系与生活含义。不得照抄样例的迁移宫组合，不把亮度直接当作人生评分，不保证发财或贵人救灾。')
    add(overview, '三、突出格局与组合', '本盘值得关注的组合')
    for t in topics:
        if t['id'].startswith('pattern_') or t['id'] == 'patterns':
            add(t, '三、突出格局与组合', t.get('pattern', {}).get('name', '组合说明'))
    for t in topics:
        if ('palace' in t and t['id'] != 'personality') or t['id'] in ('transforms', 'connections'):
            add(t, '四、重要宫位分析', f"{t['palace']} · {t['title']}" if 'palace' in t else t['title'])
    for t in topics:
        if t['id'] == 'childhood' or t['id'].startswith(('period_', 'year_')):
            title = t['title']
            if t.get('period') and t['id'].startswith('period_'):
                p = t['period']
                title = f"{p['start_age']}–{p['end_age']}岁 · {p['palace_name']}"
                if p['start_age'] <= chart['currentAge'] <= p['end_age']:
                    title += ' ← 当前大限'
            add(t, '五、大运走势', title)
    basics = by_id.get('basics', {'id': 'basics'})
    basics.update(summary={k: v for k, v in result['summary'].items() if k != 'lunar_date'}, explain='以summary.wuxing为唯一五行局依据，先说实际局名及起限用途，再说明传统象征。不可推断缺什么五行；没有提供星曜五行依据时不编造相生相克；不能仅凭五行局推荐行业或断定性格。不得照搬样例局名或在正文自问自答纠错。')
    wuxing = result['summary']['wuxing']
    first = result['details']['periods'][0]['start_age']
    meaning = {'水': '灵活、流动与适应', '木': '生长、学习与拓展', '金': '条理、边界与决断', '土': '稳定、承载与耐心', '火': '热情、表达与行动'}.get(wuxing[0], '一种传统分类')
    basics['fixed_paragraphs'] = [
        f"本盘为{wuxing}。五行局是紫微排盘中的分类，用于安排星曜和大限的起始年龄。本盘第一个大限从{first}虚岁开始，之后每十年进入一个阶段。",
        f"传统文化中，这一类常用来象征{meaning}。可以将它作为观察做事习惯的一个角度，但不能单凭五行局断定你的性格、职业或未来经历。",
        "五行局不等于八字中缺少哪种五行，也不能单凭局名与命主、身主推断整个命盘的好坏。工作和发展方向仍需结合相关宫位，并以现实能力、兴趣和条件为准。",
    ]
    add(basics, '六、五行局分析', result['summary']['wuxing'])
    facts = {'palaces': palaces, 'patterns': result['details'].get('patterns', []), 'periods': result['details'].get('periods', [])}
    for key, title, explanation in [
        ('strengths', '优势', '用3至5个要点总结本盘可讨论的优势，每点注明真实星曜组合并解释日常含义。'),
        ('cautions', '需要注意', '用3至5个要点说明相处和做事时可注意的方面，不能预测具体疾病、灾难、死亡或婚姻结局。'),
        ('directions', '适合方向', '用3至5个要点说明可探索的工作方式与发展环境，结合命宫、官禄与迁移的实际资料，不能断定必须从事某行业或给投资建议。'),
        ('conclusion', '总体评价与关键建议', '用两个短段落串起主要特点、当前阶段及实际建议，避免模板套话，不给命盘或人生打等级，不将远期阶段断言为人生巅峰。')]:
        add({'id': key, **facts, 'explain': explanation}, '七、人生总体评价', title)
    return ordered


def basic_information(result: dict) -> str:
    c, s, t = result['chart'], result['summary'], result['timing']
    body = next((p['name'] for p in c['palaces'] if p.get('isShenGong') or p.get('branch') == c.get('shenGongBranch')), '未标注')
    lines = ['## 基本信息', f"- 公历：{t['solar_date']} {t['clock']}", f"- 农历：{s['lunar_date']}",
             '- 四柱（节气）：' + ' '.join(c['lunarInfo'].get('fourPillars', [])),
             f"- 命宫：{s['ming_branch']}；身宫：{s['shen_branch']}（在{body}）", f"- 五行局：{s['wuxing']}",
             f"- 阴阳：{c.get('yinYangGender', '未标注')}", f"- 命主：{s['ming_zhu']}；身主：{s['shen_zhu']}", f"- 当前年龄：{c['currentAge']}虚岁"]
    branch = c['lunarInfo'].get('yearBranch')
    if isinstance(branch, int) and 0 <= branch < 12:
        lines.insert(4, '- 生肖：' + '鼠牛虎兔龙蛇马羊猴鸡狗猪'[branch])
    ziwei = next((p for p in c['palaces'] if any(star['name'] == '紫微' for star in p['stars'])), None)
    if ziwei:
        lines.append('- 紫微位置：' + '子丑寅卯辰巳午未申酉戌亥'[ziwei['branch']])
    return '\n'.join(lines)


def layout_report(text: str, result: dict) -> str:
    seen, lines = set(), []
    for line in text.splitlines():
        if line.startswith('## '):
            if line in seen:
                continue
            seen.add(line)
        lines.append(line)
    return basic_information(result) + '\n\n' + '\n'.join(lines)
