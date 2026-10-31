from app.services.naming_rag_service import format_naming_context


def test_naming_context_keeps_source_and_evidence_metadata():
    rendered = format_naming_context([{
        "id": 42,
        "chunk_index": 3,
        "title": "三命通会",
        "text": "五行相生相克。",
        "page_start": 5,
        "page_end": 6,
        "source_id": "sanming-tonghui-mirror",
        "source_url": "https://example.test/book.pdf",
        "edition": "镜像节选",
        "school": "子平命理",
    }])
    assert "naming:sanming-tonghui-mirror:3" in rendered
    assert "页码 5-6" in rendered
    assert "版本/流派：镜像节选 / 子平命理" in rendered
    assert "五行相生相克" in rendered
