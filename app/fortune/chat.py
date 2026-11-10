"""Structured birth facts come only from the user's current turn / active fortune topic."""
import re

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.fortune.service import analyze, FortuneUnavailable, render_report, render_cycle_report
from app.fortune.interpretation import interpret, requested_years
from app.schemas.fortune import DISCLAIMER, FortuneInput

FORTUNE_TERMS = re.compile(r"(算命|紫微|斗数|命盘|排盘|运势|财运|姻缘|命宫|身宫|夫妻宫|官禄宫|财帛宫|福德宫|迁移宫|四化|五行局|空宫|大限|流年)")
NEW_PERSON = re.compile(r"(换个?人|另一个人|重新开始|重新算|帮(?:朋友|家人|妈妈|爸爸|他|她)|我(?:朋友|妈妈|爸爸|对象|老公|老婆))")
PALACE_QUERIES = {
    "命宫": r"命宫|性格|自我", "官禄": r"事业|工作|官禄|职业", "夫妻": r"感情|婚姻|恋爱|夫妻|姻缘|伴侣",
    "财帛": r"财帛|财运|收入|财富|赚钱", "福德": r"福德|兴趣|休息", "迁移": r"迁移|出行|环境",
    "兄弟": r"兄弟|同辈", "子女": r"子女|孩子|生育", "疾厄": r"疾厄|健康|疾病|寿命", "仆役": r"仆役|交友|朋友", "田宅": r"田宅|房产|居住", "父母": r"父母|长辈",
}
GLOSSARY = {
    "紫微": "紫微斗数是一种传统排盘文化：按出生年月日、时辰和排盘性别安排十二宫及星曜，再用传统意象做解读。本功能可以生成本命盘、查看大限与逐年流年、说明十二宫与四化；它不是科学预测工具。想体验排盘，可以提供公历或农历生日、准确时间和排盘性别。",
    "四化": "四化是传统紫微斗数中的化禄、化权、化科、化忌四种标记，用来描述不同的星曜意象。它们不是现实中的好运、权力、学业成绩或灾祸保证；需要结合宫位理解，不能单独判断吉凶。",
    "命宫": "命宫是紫微斗数十二宫的起点，传统解读常用它讨论自我与行为风格；它不能证明一个人的真实人格。",
    "身宫": "身宫是紫微斗数中的另一个定位标记，传统上用来讨论行动与生活重心，不能用来预测身体状况。",
    "五行局": "五行局是排盘算法中的分类（水二局、木三局、金四局、土五局、火六局），用于安排紫微星与大限起始年龄，不等同于个人缺哪种五行。",
    "空宫": "空宫指该宫没有十四主星，不代表这一方面缺失。传统解读常参考对宫主星，因此页面会明确标出「参考对宫」。",
}


def extract_facts(question: str) -> dict:
    facts = {}
    match = re.search(r"(?<!\d)(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})(?:日|号)?(?!\d)", question)
    if match:
        facts["birth_date"] = f"{int(match[1]):04}-{int(match[2]):02}-{int(match[3]):02}"
    time_text = re.sub(r"UTC\s*[+-]\d{1,2}(?::\d{2})?", "", question, flags=re.I)
    time = re.search(r"(?<!\d)(\d{1,2})[:：点时](?:(\d{1,2})(?:分)?)?(?!\d)", time_text)
    if time:
        hour = int(time[1])
        minute = int(time[2] or (30 if time_text[time.end():].startswith("半") else 0))
        if re.search(r"下午|晚上|傍晚", question) and 1 <= hour <= 11:
            hour += 12
        if "凌晨12" in question or "午夜12" in question:
            hour = 0
        facts["birth_time"] = f"{hour:02}:{minute:02}"
    gender = re.search(r"(?:^|[\s,，。;；]|性别[是为：:]?|我是)(男|女)(?:性|生|士|孩|的)?(?=$|[\s,，。;；]|出生|生于|公历|阳历|农历|阴历|\d{4})", question)
    if gender:
        facts["gender"] = "male" if gender[1] == "男" else "female"
    if re.search(r"公历|阳历", question):
        facts.update(calendar_type="solar", lunar_leap_month=False)
    elif re.search(r"农历|阴历", question):
        facts.update(calendar_type="lunar", lunar_leap_month=bool(re.search(r"(?<!不)(?<!非)闰", question)))
    if re.search(r"不是闰月|非闰月|不闰", question):
        facts["lunar_leap_month"] = False
    elif "闰月" in question:
        facts["lunar_leap_month"] = True
    zone = re.search(r"UTC\s*([+-])(\d{1,2})(?::(\d{2}))?", question, re.I)
    if zone:
        facts["utc_offset_minutes"] = (int(zone[2]) * 60 + int(zone[3] or 0)) * (1 if zone[1] == "+" else -1)
    elif "北京时间" in question or "东八区" in question:
        facts["utc_offset_minutes"] = 480
    longitude = re.search(r"(?:经度|东经|西经)\s*([+-]?\d+(?:\.\d+)?)", question)
    if longitude:
        facts["longitude"] = float(longitude[1]) * (-1 if longitude[0].startswith("西经") else 1)
    if "不校正" in question or "不修正" in question:
        facts["longitude"] = None
    if "子时当日" in question:
        facts["late_zishi"] = "current-day"
    elif "子时次日" in question:
        facts["late_zishi"] = "next-day"
    return facts


def is_fortune_turn(state: dict) -> bool:
    question = state.get("question", "")
    if FORTUNE_TERMS.search(question):
        return True
    if (state.get("topic_state") or {}).get("domain") != "fortune":
        return False
    return bool(extract_facts(question) or NEW_PERSON.search(question) or re.search(
        r"事业|工作|性格|感情|婚姻|财运|财富|收入|健康|寿命|流年|大限|今年|明年|\d{1,3}\s*岁|五行局|空宫|继续|详细|解释|解读|白话|看不懂|讲人话|联网|再说|不知道|不详|不确定|北京时间|闰月|经度", question
    ))


async def fortune_agent(state: dict) -> dict:
    question = state["question"]
    topic = dict(state.get("topic_state") or {})
    facts = dict(topic.get("fortune_facts") or {})
    if NEW_PERSON.search(question):
        facts = {}
    updates = extract_facts(question)
    if updates.get("calendar_type") and facts.get("calendar_type") not in {None, updates["calendar_type"]} and not updates.get("birth_date"):
        facts.pop("birth_date", None)
    # A new date without an explicit correction is a new chart, never silently
    # borrow someone else's time or gender from the preceding chart.
    if updates.get("birth_date") and facts.get("birth_date") not in {None, updates["birth_date"]} and not re.search(r"改成|改为|更正|纠正|写错", question):
        facts = {}
    facts.update(updates)
    if re.search(r"(?:时间|时辰).{0,4}(?:不知道|不详|不确定)|不知道.{0,4}(?:时间|时辰)", question):
        facts.pop("birth_time", None)
    topic.update(domain="fortune", fortune_facts=facts)

    def reply(text):
        return {"messages": [AIMessage(content=text)], "topic_state": topic, "local_answer": text}

    if len(set(re.findall(r"\d{4}[年/.-]\d{1,2}[月/.-]\d{1,2}", question))) > 1:
        topic["fortune_facts"] = {}
        return reply("一次只能分析一个人的命盘。请分别提供一个人的历法、出生日期、时间和排盘性别，避免混用信息。仅供娱乐。")

    if re.search(r"什么|怎么理解|什么意思|解释", question) and not re.search(r"我的|这张|这个盘|本盘", question):
        for word, text in GLOSSARY.items():
            if word in question:
                return reply(text + "\n\n" + DISCLAIMER)
    missing = [label for key, label in [("calendar_type", "公历或农历"), ("birth_date", "出生日期（YYYY-MM-DD）"), ("birth_time", "出生时间（HH:mm）"), ("gender", "排盘性别（男 / 女）")] if key not in facts]
    if missing:
        confirmed = []
        for key, label in [("birth_date", "日期"), ("birth_time", "时间")]:
            if key in facts:
                confirmed.append(f"{label}：{facts[key]}")
        text = "可以帮你生成紫微斗数排盘与文化解读，**仅供娱乐**。\n\n"
        if confirmed:
            text += "已收到「" + "，".join(confirmed) + "」。\n\n"
        text += "还需要：" + "、".join(missing) + "。出生时间不确定时，我不会替你假定时辰。\n\n时间默认北京时间 UTC+8（标准时间）；其他时区请写 UTC±HH:mm。农历请同时注明是否闰月。"
        return reply(text)
    try:
        data = FortuneInput.model_validate(facts)
        result = await analyze(data)
    except ValidationError as exc:
        return reply("出生信息还需要核对：" + str(exc.errors()[0]["msg"]).removeprefix("Value error, ") + "。请补充或更正后再试。")
    except (ValueError, FortuneUnavailable) as exc:
        return reply(str(exc))
    topic["fortune_facts"] = data.model_dump()
    async def ai_reply(*, years=None, fallback=None):
        reading = await interpret(result, question, years=years)
        if reading["status"] not in ("ready", "partial"):
            return reply(reading["message"] + "\n\n" + (fallback or render_report(result)))
        text = reading["markdown"]
        if reading["status"] == "partial":
            text += "\n\n" + reading["message"]
        if reading["search_status"] == "unavailable":
            text += "\n\n本次未能读取 GitHub 参考资料，以上为 AI 根据命盘作的文化解释。"
        if reading["sources"]:
            text += "\n\n参考资料：\n\n" + "\n".join(f"- [{s['title']}]({s['url']})" for s in reading["sources"])
        return reply(text + "\n\n" + DISCLAIMER)
    if re.search(r"流年|大限|童限|明年|今年|\d{4}年(?!\d{1,2}月)|\d{1,3}\s*(?:虚)?岁", question) and not extract_facts(question).get("birth_date"):
        age_range = re.search(r"(?<!\d)(\d{1,3})\s*(?:到|至|[-–—])\s*(\d{1,3})\s*(?:虚)?岁", question)
        age_match = re.search(r"(?<!\d)(\d{1,3})\s*(?:虚)?岁", question)
        year_match = re.search(r"(?<!\d)(\d{4})年(?!\d{1,2}月)", question)
        if age_range:
            fallback = render_cycle_report(result, age=int(age_range[1]), end_age=int(age_range[2]))
            try:
                selected = requested_years(result, question)
            except ValueError:
                return reply(fallback)
            return await ai_reply(years=selected, fallback=fallback)
        if age_match:
            age = int(age_match[1])
        elif year_match:
            age = int(year_match[1]) - result["chart"]["lunarInfo"]["lunarYear"] + 1
        else:
            age = result["chart"]["currentAge"] + (1 if "明年" in question else 0)
        selected = [y for y in result["details"]["years"] if y["age"] == age]
        fallback = render_cycle_report(result, age=age)
        return await ai_reply(years=selected, fallback=fallback) if selected else reply(fallback)
    palace = next((name for name, pattern in PALACE_QUERIES.items() if re.search(pattern, question)), None)
    return await ai_reply(fallback=render_report(result, palace))
