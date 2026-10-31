"""从命名聊天文本提取用户已经明确说出的事实，防止 Agent 重复追问。"""
import re


def extract_explicit_facts(text: str) -> dict[str, str]:
    text = text or ""
    facts: dict[str, str] = {}
    if re.search(r"(男孩|男宝|男宝宝|儿子|男生|男性)", text):
        facts["gender"] = "男孩"
    elif re.search(r"(女孩|女宝|女宝宝|女儿|女生|女性)", text):
        facts["gender"] = "女孩"
    date_match = re.search(r"(\d{4})\s*[年\-/](\d{1,2})\s*[月\-/](\d{1,2})\s*(?:日|号)?", text)
    if date_match:
        facts["birth_date"] = "-".join(date_match.groups())
    time_match = re.search(r"(\d{1,2})\s*(?:[:：点时])\s*(\d{1,2})\s*(?:分)?", text)
    if time_match:
        facts["birth_time"] = f"{int(time_match.group(1)):02d}:{int(time_match.group(2)):02d}"
    surname_match = re.search(r"(?:爸爸姓|父亲姓|姓氏是|姓)([\u4e00-\u9fff])", text)
    if surname_match:
        facts["surname"] = surname_match.group(1)
    return facts
