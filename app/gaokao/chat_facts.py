import re

from app.gaokao.regions import SCHOOL_REGIONS

_REGION = re.compile(r"(" + "|".join(SCHOOL_REGIONS) + r")(?:壮族自治区|回族自治区|维吾尔自治区|自治区|特别行政区|省|市)?")
_DEST_BEFORE = re.compile(r"(?:报考|想考|想上|想去|考虑|目标(?:院校)?(?:地区|所在地)?|院校所在地|[去选])[:：为是 ]*$")
_DEST_AFTER = re.compile(r"^(?:的)?(?:有(?:哪些|什么)|哪些|有什么|地区的|当地的|院校|高校|大学|学校)")
_SOURCE_BEFORE = re.compile(r"(?:生源(?:地)?|考生省份|在|来自|参加)[:：为是 ]*$")
_SOURCE_AFTER = re.compile(r"^(?:的)?(?:考生|高考|生源|物理|历史|综合|位次|排名)")


def extract_gaokao_facts(text: str) -> dict[str, object]:
    value = str(text or "")
    facts: dict[str, object] = {}
    sources, targets, undecided = [], [], []
    for m in _REGION.finditer(value):
        before, after = value[:m.start()], value[m.end():]
        if _SOURCE_AFTER.search(after):
            sources.append(m[1])
        elif _DEST_BEFORE.search(before) or _DEST_AFTER.search(after):
            targets.append(m[1])
        elif _SOURCE_BEFORE.search(before):
            sources.append(m[1])
        else:
            undecided.append(m[1])
    if not sources and len(set(undecided)) == 1:
        sources = undecided
        undecided = []
    if len(set(sources)) == 1:
        facts["province"] = sources[0]
        targets.extend(r for r in undecided if r != sources[0])
    if targets:
        facts["school_provinces"] = list(dict.fromkeys(targets))
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
    batch = re.search(r"(本科提前批|提前批|本科批次B段|本科批B段|本科批|专科批|本科一批|本科二批|本科)", value)
    if batch:
        facts["batch"] = batch.group(1)
    subjects = re.findall(r"物理|历史|化学|生物|地理|政治", value)
    if subjects:
        facts["subjects"] = list(dict.fromkeys(subjects))
    if not facts.get("track") and facts.get("province") == "上海":
        facts["track"] = "综合"
    return facts
