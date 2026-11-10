"""Local, deterministic Ziwei service. No calls to the upstream website or an LLM."""
import asyncio
import json
import os
from pathlib import Path
import shutil

from app.schemas.fortune import DISCLAIMER, FortuneInput
from app.fortune.report import build_engine_report, build_brief_report, star_text
from app.utils.markdown import render_markdown

ENGINE = Path(__file__).parent / "engine" / "dist" / "runner.cjs"
SOURCE = "https://github.com/Renhuai123/ziwei-doushu"
VERSION = "ziwei-a600484-recipe-3"
BRANCHES = "子丑寅卯辰巳午未申酉戌亥"
_slots = asyncio.Semaphore(4)

class FortuneUnavailable(RuntimeError):
    pass


def describe_palace(palace: dict) -> dict:
    major = [s["name"] for s in palace["stars"] if s["type"] == "major"]
    return {"palace": palace["name"], "title": palace["name"], "evidence": "、".join(major) or "无主星", "text": "、".join(star_text(s) for s in palace["stars"]), "borrowed": not major}


async def analyze(data: FortuneInput) -> dict:
    node = shutil.which(os.environ.get("FORTUNE_NODE_BINARY", "node"))
    if not node or not ENGINE.is_file():
        raise FortuneUnavailable("排盘服务尚未就绪，请稍后重试")
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=2)
    except TimeoutError as exc:
        raise FortuneUnavailable("排盘请求较多，请稍后重试") from exc
    proc = None
    try:
        # Do not inherit API keys or NODE_OPTIONS; use no shell and fixed arguments.
        proc = await asyncio.create_subprocess_exec(
            node, "--max-old-space-size=128", str(ENGINE),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(data.model_dump_json().encode()), timeout=10)
        result = json.loads(stdout)
        if proc.returncode or "error" in result:
            raise ValueError(result.get("error", "出生信息无法排盘，请核对日期与时间"))
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        raise FortuneUnavailable("排盘服务暂时不可用，请稍后重试") from exc
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        _slots.release()

    chart = result["chart"]
    palaces = chart["palaces"]
    interpretation = [describe_palace(p) for p in palaces]
    sihua = [{"star": s["name"], "type": s["siHua"], "palace": p["name"]} for p in palaces for s in p["stars"] if s.get("siHua")]
    response = {
        "input": data.model_dump(), "version": VERSION, "disclaimer": DISCLAIMER,
        "source": {"name": "紫微研究 · 开源排盘引擎（MIT）", "url": SOURCE},
        "timing": result["timing"],
        "chart": chart, "details": result["details"],
        "summary": {"ming_branch": BRANCHES[chart["mingGongBranch"]], "shen_branch": BRANCHES[chart["shenGongBranch"]], "wuxing": chart["wuxingJuName"], "ming_zhu": chart.get("mingZhu", ""), "shen_zhu": chart.get("shenZhu", ""), "lunar_date": result["timing"]["lunar_date"]},
        "palaces": [{"name": p["name"], "branch": BRANCHES[p["branch"]], "is_ming": bool(p.get("isMingGong")), "is_shen": bool(p.get("isShenGong")), "stars": p["stars"], "borrowed_stars": p.get("borrowedStars", [])} for p in palaces],
        "sihua": sihua, "analysis": interpretation,
        "conventions": ["出生时间按所选标准时区，不自动扣减夏令时。", "23:00–23:59 " + ("按次日子时排盘。" if data.late_zishi == "next-day" else "按当日子时排盘。"), "农历正月初一换年；闰月前十五日归本月，后半月归下月；星曜亮度采用全书口径。", "已按经度修正真太阳时。" if data.longitude is not None else "未启用真太阳时校正。"],
    }

    report = build_engine_report(response)
    html = render_markdown(build_engine_report(response, include_notes=False)).replace('<table>', '<div class="report-table"><table>').replace('</table>', '</table></div>')
    response["report"] = {"markdown": report, "html": html}
    return response


def render_report(result: dict, palace: str | None = None) -> str:
    return build_brief_report(result, palace)


def render_cycle_report(result: dict, *, age: int | None = None, year: int | None = None, end_age: int | None = None) -> str:
    """Report actual upstream year/period calculations; never invent missing readings."""
    years = result["details"]["years"]
    if year is not None:
        age = year - result["chart"]["lunarInfo"]["lunarYear"] + 1
    if age is None:
        age = result["chart"]["currentAge"]
    last = end_age if end_age is not None else age
    if age < 1 or last < age or last > len(years):
        return f"这张命盘可查看 1–{len(years)} 虚岁的排盘数据，请选择范围内的年龄。\n\n{DISCLAIMER}"

    def marks(items):
        return "；".join(f"{m['star']}化{m['type']}（{m['palace']}）" for m in items)

    selected = [y for y in years if age <= y["age"] <= last]
    if len(selected) > 1:
        rows = []
        for y in selected:
            period = next((p for p in result["details"]["periods"] if p["index"] == y["period_index"]), None)
            name = f"{period['start_age']}–{period['end_age']}岁 · {period['palace_name']}" if period else "童限"
            rows.append(f"### {y['year']}年 · {y['age']} 虚岁\n\n所处阶段：{name}。\n\n流年四化：{marks(y['sihua'])}。")
        body = f"## {age}–{last} 虚岁逐年排盘\n\n" + "\n\n".join(rows)
    else:
        y = selected[0]
        period = next((p for p in result["details"]["periods"] if p["index"] == y["period_index"]), None)
        palace = next(p for p in result["chart"]["palaces"] if p["branch"] == y["palace_branch"])
        stars = "、".join(s["name"] + (f"（{s['brightnessRaw']}）" if s.get("brightnessRaw") else "") for s in palace["stars"])
        body = f"## {y['year']} {y['ganzhi']}年 · {age} 虚岁\n\n流年命宫落在本命 **{y['palace_name']}**（{BRANCHES[y['palace_branch']]}）。本宫星曜：{stars or '无星曜'}。"
        body += "\n\n所属大限：" + (f"{period['start_age']}–{period['end_age']} 虚岁 · {period['palace_name']}。" if period else "起大限之前的童限。")
        body += f"\n\n流年四化：{marks(y['sihua'])}。\n\n{'大限' if period else '童限'}四化：{marks(y['period_sihua'])}。"
    return body + "\n\n这里的年龄按虚岁计算，农历新年时增加一岁。以上为传统排盘标记，不代表这一年一定会发生某件事。\n\n" + DISCLAIMER
