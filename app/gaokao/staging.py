"""把公开来源文件解析为可审计 staging 行。

解析器宁可拒绝一张格式不明确的表，也不猜列。每行保留原始字段和来源定位，
后续必须经过人工核验才可导入 gaokao_admissions。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import subprocess
import zipfile
from xml.etree import ElementTree
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


ALIASES: dict[str, tuple[str, ...]] = {
    "university_code": ("院校代码", "高校代码", "学校代码", "招生院校代码", "院校代号"),
    "university_name": ("院校名称", "高校名称", "学校名称", "招生院校", "院校、专业组"),
    "group_code": ("专业组代码", "专业组", "院校专业组代码", "专业组号"),
    "group_name": ("专业组名称", "院校专业组名称"),
    "major_code": ("专业代码", "专业代号", "招生专业代码"),
    "major_name": ("专业名称", "招生专业", "专业"),
    "subject_requirements": ("选科要求", "再选科目要求", "科目要求", "考试科目要求"),
    "min_score": ("专业录取最低分", "录取最低分", "投档最低分", "投档线", "最低投档分", "最低分"),
    "min_rank": ("录取最低位次", "最低位次", "投档最低位次", "最低排位", "录取最低排名", "排名"),
    "avg_score": ("录取平均分", "平均分", "平均成绩"),
    "max_score": ("录取最高分", "最高分", "最高成绩"),
    "plan_count": ("计划数", "招生计划", "计划人数"),
    "admission_count": ("录取数", "录取人数", "实际录取数"),
    "submission_count": ("投档数", "投档人数", "第一次投档数"),
    "batch": ("批次", "批次名称", "录取批次"),
    "track": ("科类", "科类名称", "首选科目", "科目组"),
    "province_code": ("生源省代码", "省份代码"),
    "school_province": ("院校所在地省份", "学校所在省份", "院校所在地", "院校省份"),
    "plan_type": ("计划性质", "招生类型", "计划类别"),
    "tie_breaker": ("同分排序项", "同分排序", "排序项"),
}


def clean_header(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").replace("\n", ""))


def clean_value(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "—", "-", "/"}:
        return ""
    return text


def normalize_track(value: Any) -> str:
    text = clean_value(value)
    if "历史" in text:
        return "历史类"
    if "物理" in text:
        return "物理类"
    return text


def to_int(value: str) -> int | None:
    value = clean_value(value).replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    number = float(match.group(0))
    return int(number) if number.is_integer() else None


def normalize_columns(columns: list[Any], record_kind: str) -> dict[str, str]:
    names = {clean_header(column): str(column) for column in columns}
    result: dict[str, str] = {}
    for field, aliases in ALIASES.items():
        choices = aliases
        if field == "min_score":
            if record_kind == "group_admit":
                choices = ("投档最低分", "最低投档分", "投档线", "最低分", "录取最低分")
            elif record_kind == "major_admit":
                choices = ("专业录取最低分", "录取最低分", "最低分", "投档最低分")
        for alias in choices:
            alias = clean_header(alias)
            exact = next((header for header in names if header == alias), None)
            if exact:
                result[field] = names[exact]
                break
        if field not in result:
            for alias in choices:
                alias = clean_header(alias)
                partial = next(
                    (
                        header for header in names
                        if alias and alias in header
                        and not (field == "major_name" and "专业组" in header)
                    ),
                    None,
                )
                if partial:
                    result[field] = names[partial]
                    break
    return result


def _raw_hash(row: dict[str, Any]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_rows(
    rows: list[dict[str, Any]],
    *,
    source: dict[str, Any],
    source_locator: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows:
        raise ValueError("原始文件没有表格行")
    record_kind = str(source.get("record_kind") or "").strip()
    if record_kind not in {"group_admit", "major_admit"}:
        raise ValueError(f"来源 {source['source_id']} 未声明可入库 record_kind")
    mapping = normalize_columns(list(rows[0]), record_kind)
    if "university_name" not in mapping:
        raise ValueError(f"来源 {source['source_id']} 未识别院校名称列：{list(rows[0])}")
    if "min_score" not in mapping and "min_rank" not in mapping:
        raise ValueError(f"来源 {source['source_id']} 未识别最低分或最低位次列：{list(rows[0])}")
    staged: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row_number, raw in enumerate(rows, 2):
        raw_clean = {clean_header(key): clean_value(value) for key, value in raw.items()}
        university_header = mapping.get("university_name", "")
        university_name = clean_value(raw.get(university_header, ""))
        combined_group = ""
        if "院校、专业组" in clean_header(university_header) and "group_code" not in mapping:
            match = re.match(r"^(.*?)(\d{1,4})专业组(?:\((.*?)\))?$", university_name)
            if not match:
                rejected.append({"row": row_number, "reason": "院校与专业组未能无歧义拆分", "raw": raw_clean})
                continue
            university_name, combined_group, combined_requirement = match.groups()
        else:
            combined_requirement = ""
        if not university_name:
            rejected.append({"row": row_number, "reason": "缺少院校名称", "raw": raw_clean})
            continue
        item: dict[str, Any] = {
            "province": str(source["province"]),
            "year": int(source["year"]),
            "track": normalize_track(raw.get(mapping.get("track", ""), "")) or normalize_track(source.get("track") or ""),
            "batch": clean_value(raw.get(mapping.get("batch", ""), "")) or str(source.get("batch") or ""),
            "admission_round": str(source.get("admission_round") or "regular").strip(),
            "record_kind": record_kind,
            "university_code": clean_value(raw.get(mapping.get("university_code", ""), "")),
            "university_name": university_name,
            "group_code": clean_value(raw.get(mapping.get("group_code", ""), "")) or combined_group,
            "group_name": clean_value(raw.get(mapping.get("group_name", ""), "")),
            "major_code": clean_value(raw.get(mapping.get("major_code", ""), "")),
            "major_name": clean_value(raw.get(mapping.get("major_name", ""), "")),
            "subject_requirements": clean_value(raw.get(mapping.get("subject_requirements", ""), "")) or combined_requirement or None,
            "min_score": to_int(raw.get(mapping.get("min_score", ""), "")),
            "min_rank": to_int(raw.get(mapping.get("min_rank", ""), "")),
            "avg_score": to_int(raw.get(mapping.get("avg_score", ""), "")),
            "max_score": to_int(raw.get(mapping.get("max_score", ""), "")),
            "plan_count": to_int(raw.get(mapping.get("plan_count", ""), "")),
            "admission_count": to_int(raw.get(mapping.get("admission_count", ""), "")),
            "submission_count": to_int(raw.get(mapping.get("submission_count", ""), "")),
            "province_code": clean_value(raw.get(mapping.get("province_code", ""), "")) or None,
            "school_province": clean_value(raw.get(mapping.get("school_province", ""), "")) or str(source.get("school_province") or "").strip() or None,
            "plan_type": clean_value(raw.get(mapping.get("plan_type", ""), "")) or None,
            "tie_breaker": clean_value(raw.get(mapping.get("tie_breaker", ""), "")) or None,
            "source_locator": source_locator or f"row:{row_number}",
            "verification_status": "pending",
            "admission_status": "admitted" if item_score_or_rank(raw, mapping) else "not_reported",
            "raw_record_hash": _raw_hash(raw_clean),
            "raw_fields": raw_clean,
        }
        if not item["track"] or not item["batch"]:
            rejected.append({"row": row_number, "reason": "缺少科类或批次口径", "raw": raw_clean})
            continue
        if item["min_score"] is None and item["min_rank"] is None:
            item["admission_status"] = "not_reported"
        if item["min_score"] is not None and not 0 <= item["min_score"] <= 900:
            rejected.append({"row": row_number, "reason": "最低分超出范围", "raw": raw_clean})
            continue
        key = (item["university_code"] or item["university_name"], item["group_code"], item["major_code"])
        if key in seen:
            rejected.append({"row": row_number, "reason": f"重复业务键：{key}", "raw": raw_clean})
            continue
        seen.add(key)
        staged.append(item)
    return staged, rejected


def item_score_or_rank(raw: dict[str, Any], mapping: dict[str, str]) -> bool:
    return bool(clean_value(raw.get(mapping.get("min_score", ""), "")) or clean_value(raw.get(mapping.get("min_rank", ""), "")))


def read_rows(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else value.get("rows", [])
    if suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if suffix in {".csv", ".tsv"}:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle, delimiter="\t" if suffix == ".tsv" else ","))
    if suffix in {".xls", ".xlsx"}:
        try:
            import pandas as pd
            workbook = pd.ExcelFile(path)
            if workbook.sheet_names:
                return pd.read_excel(path).fillna("").to_dict(orient="records")
        except Exception:
            pass
        return _read_xlsx_bytes(path.read_bytes())
    if suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith((".xlsx", ".xls"))]
            if len(names) != 1:
                raise ValueError(f"ZIP 中应恰好包含一个 XLSX/XLS，实际：{names}")
            return _read_xlsx_bytes(archive.read(names[0]))
    if suffix in {".html", ".htm"}:
        return _read_html_rows(path)
    if suffix == ".pdf":
        return _read_pdf_rows(path)
    raise ValueError(f"暂不自动解析 {suffix}；保留原始文件并进入人工处理队列")


def _xml_local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_xlsx_bytes(payload: bytes) -> list[dict[str, Any]]:
    """读取严格 OOXML 文件，兼容部分省考试院导出文件不被 openpyxl 识别的情况。"""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                if _xml_local(item.tag) == "si":
                    shared.append("".join(node.text or "" for node in item.iter() if _xml_local(node.tag) == "t"))
        sheet_names = sorted(name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
        if not sheet_names:
            raise ValueError("XLSX 中没有 worksheet")
        root = ElementTree.fromstring(archive.read(sheet_names[0]))
        rows: list[dict[str, str]] = []
        for row in root.iter():
            if _xml_local(row.tag) != "row":
                continue
            values: dict[str, str] = {}
            for cell in row:
                if _xml_local(cell.tag) != "c":
                    continue
                ref = str(cell.attrib.get("r") or "")
                col = re.sub(r"\d", "", ref)
                value_node = next((node for node in cell if _xml_local(node.tag) in {"v", "t"}), None)
                if value_node is not None and _xml_local(value_node.tag) == "t":
                    value = value_node.text or ""
                elif value_node is not None and value_node.text is not None:
                    value = value_node.text
                elif cell.attrib.get("t") == "inlineStr":
                    value = "".join(node.text or "" for node in cell.iter() if _xml_local(node.tag) == "t")
                else:
                    value = ""
                if cell.attrib.get("t") == "s" and value:
                    value = shared[int(value)]
                values[col] = value
            if values:
                rows.append(values)
        header_index = next(
            (
                index for index, row in enumerate(rows)
                if "院校名称" in row.values()
                and any("专业组" in value for value in row.values())
                and any(value in {"投档线", "投档最低分", "最低分"} for value in row.values())
            ),
            None,
        )
        if header_index is None:
            raise ValueError("XLSX 未找到院校名称、专业组和投档线表头")
        header_row = rows[header_index]
        headers = {column: value for column, value in header_row.items() if value}
        result: list[dict[str, str]] = []
        for row in rows[header_index + 1:]:
            item = {headers[column]: value for column, value in row.items() if column in headers}
            if item.get("院校名称") or item.get("院校代号") or item.get("院校代码"):
                result.append(item)
        if not result:
            raise ValueError("XLSX 表头后没有数据行")
        return result


def _read_pdf_rows(path: Path) -> list[dict[str, Any]]:
    """用 poppler 的 raw 文本顺序读取省考试院投档 PDF。

    PDF 仍作为不可变原始证据保留；这里只接受完整的七列本科投档行，任何看起来像
    数据行但无法完整匹配都会使本批失败，避免 PDF 排版异常时静默漏行。
    """
    executable = shutil.which("pdftotext")
    if executable is None:
        raise ValueError("解析 PDF 需要 pdftotext（poppler）；请安装后重试，原始文件不会被丢弃")
    result = subprocess.run(
        [executable, "-raw", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    pattern = re.compile(r"^(\d{4,6})\s+(.+?)\s+(\d{1,4})\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)$")
    rows: list[dict[str, Any]] = []
    malformed: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
        # 广东官方 PDF 的页脚是 1/30、2/30……，不是投档记录。
        if re.fullmatch(r"\d+/\d+", line):
            continue
        match = pattern.match(line)
        if not match:
            malformed.append(line)
            continue
        code, name, group, plan, submitted, score, rank = match.groups()
        rows.append({
            "院校代码": code,
            "院校名称": name.strip(),
            "专业组代码": group,
            "计划数": plan,
            "投档人数": submitted,
            "投档最低分": score,
            "投档最低排位": rank,
        })
    if malformed:
        raise ValueError(f"PDF 中有 {len(malformed)} 行疑似数据但未完整匹配，首行：{malformed[0]}")
    if not rows:
        raise ValueError("PDF 未识别到投档数据行")
    return rows


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs):
        if tag == "table" and self._table is None:
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str):
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(clean_value("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


def _read_html_rows(path: Path) -> list[dict[str, Any]]:
    raw = path.read_bytes()
    text = ""
    for encoding in ("utf-8", "gb18030", "gbk"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    parser = _TableParser()
    parser.feed(text)
    rows: list[dict[str, Any]] = []
    for table in parser.tables:
        header_index = next(
            (index for index, row in enumerate(table[:12])
             if any("院校" in cell or "学校" in cell for cell in row)
             and any("专业" in cell or "最低" in cell for cell in row)),
            None,
        )
        if header_index is None:
            continue
        headers = [clean_header(value) or f"column_{index}" for index, value in enumerate(table[header_index])]
        for row in table[header_index + 1:]:
            if len(row) < 2:
                continue
            values = row + [""] * (len(headers) - len(row))
            rows.append(dict(zip(headers, values[:len(headers)])))
    if not rows:
        raise ValueError("HTML 中没有识别到官方录取表格")
    return rows

