import re

PROVINCES = (
    "北京|天津|河北|山西|内蒙古|辽宁|吉林|黑龙江|上海|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|广西|海南|重庆|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆"
)


def extract_gaokao_facts(text: str) -> dict[str, object]:
    value = str(text or "")
    facts: dict[str, object] = {}
    province = re.search(rf"({PROVINCES})(?:省|市|自治区)?", value)
    if province:
        facts["province"] = province.group(1)
    year = re.search(r"((?:19|20)\d{2})\s*年", value)
    if year:
        facts["year"] = int(year.group(1))
    rank = re.search(r"(?:位次|排名|名次)\s*(?:是|为|：|:)?\s*(\d{1,7})", value)
    if rank:
        facts["rank"] = int(rank.group(1))
    score = re.search(r"(?:考了|分数|成绩|总分)\s*(?:是|为|：|:)?\s*(\d{2,3})\s*分?", value)
    if score:
        facts["score"] = int(score.group(1))
    for track in ("物理类", "历史类", "理科", "文科", "综合"):
        if track in value:
            facts["track"] = track
            break
    batch = re.search(r"(本科提前批|本科批|专科批|本科一批|本科二批|本科)", value)
    if batch:
        facts["batch"] = batch.group(1)
    subjects = re.findall(r"物理|历史|化学|生物|地理|政治", value)
    if subjects:
        facts["subjects"] = list(dict.fromkeys(subjects))
    return facts
