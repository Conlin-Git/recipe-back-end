import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from app.gaokao.collector import download_source, initial_state
from app.gaokao import staging
from app.gaokao.staging import normalize_rows


def test_normalize_preserves_record_kind_and_rejects_ambiguous_rows():
    source = {"source_id": "x", "province": "广西", "year": 2025, "record_kind": "group_admit"}
    rows, rejected = normalize_rows(
        [
            {"院校代码": "10001", "院校名称": "测试大学", "专业组": "01", "投档最低分": "612", "投档最低位次": "1000", "批次": "本科批", "科类": "物理类"},
            {"院校代码": "10002", "院校名称": "", "专业组": "02", "投档最低分": "600", "批次": "本科批", "科类": "物理类"},
        ],
        source=source,
    )
    assert len(rows) == 1
    assert rows[0]["record_kind"] == "group_admit"
    assert rows[0]["min_rank"] == 1000
    assert rejected[0]["reason"] == "缺少院校名称"


def test_download_writes_sha256_and_supports_range_resume(tmp_path: Path):
    payload = b"official-data" * 100
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.headers.get("range"))
        range_header = request.headers.get("range")
        if range_header:
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            return httpx.Response(206, headers={"content-type": "text/csv"}, content=payload[start:])
        return httpx.Response(200, headers={"content-type": "text/csv"}, content=payload)

    source = {"source_id": "resume", "url": "https://example.test/data.csv", "extension": "csv"}
    item = initial_state("manifest", [source])["sources"]["resume"]
    raw = tmp_path / "raw"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        result = download_source(source, item, raw, client)
        artifact = raw.parent / result["artifact"]
        assert artifact.read_bytes() == payload
        assert result["sha256"] == hashlib.sha256(payload).hexdigest()

        # 新的下载如果中途留下 part，会自动带 Range 继续。
        part = raw / "resume-2" / "payload.part"
        part.parent.mkdir(parents=True)
        part.write_bytes(payload[:20])
        source2 = {**source, "source_id": "resume-2"}
        item2 = initial_state("manifest", [source2])["sources"]["resume-2"]
        result2 = download_source(source2, item2, raw, client)
        assert (raw.parent / result2["artifact"]).read_bytes() == payload
        assert calls[-1] == "bytes=20-"
    finally:
        client.close()


def test_download_rejects_sha_mismatch(tmp_path: Path):
    source = {"source_id": "bad", "url": "https://example.test/data.csv", "sha256": "0" * 64}
    item = initial_state("manifest", [source])["sources"]["bad"]
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"wrong")))
    try:
        with pytest.raises(RuntimeError, match="SHA-256 不匹配"):
            download_source(source, item, tmp_path / "raw", client)
        assert item["status"] == "failed"
    finally:
        client.close()


def test_pdf_rows_require_complete_seven_columns(monkeypatch, tmp_path: Path):
    class Result:
        stdout = "10001 北京大学 205 24 24 669 28\n页脚\n"

    monkeypatch.setattr(staging.shutil, "which", lambda _: "/usr/bin/pdftotext")
    monkeypatch.setattr(staging.subprocess, "run", lambda *args, **kwargs: Result())
    rows = staging.read_rows(tmp_path / "official.pdf")
    assert rows == [{
        "院校代码": "10001",
        "院校名称": "北京大学",
        "专业组代码": "205",
        "计划数": "24",
        "投档人数": "24",
        "投档最低分": "669",
        "投档最低排位": "28",
    }]


def test_strict_ooxml_zip_rows_are_read_without_openpyxl(tmp_path: Path):
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<worksheet xmlns='http://purl.oclc.org/ooxml/spreadsheetml/main'><sheetData>
              <row r='1'><c r='A1' t='inlineStr'><is><t>科类</t></is></c><c r='B1' t='inlineStr'><is><t>院校名称</t></is></c><c r='C1' t='inlineStr'><is><t>专业组编号</t></is></c><c r='D1' t='inlineStr'><is><t>投档线</t></is></c></row>
              <row r='2'><c r='A2' t='inlineStr'><is><t>普通类(首选物理)</t></is></c><c r='B2' t='inlineStr'><is><t>测试大学</t></is></c><c r='C2' t='inlineStr'><is><t>999</t></is></c><c r='D2'><v>612</v></c></row>
            </sheetData></worksheet>""",
        )
    path = tmp_path / "official.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("湖南2026.xlsx", workbook.getvalue())
    rows = staging.read_rows(path)
    assert rows == [{"科类": "普通类(首选物理)", "院校名称": "测试大学", "专业组编号": "999", "投档线": "612"}]

