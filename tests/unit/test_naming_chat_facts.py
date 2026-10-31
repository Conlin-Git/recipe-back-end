from app.naming.chat_facts import extract_explicit_facts


def test_explicit_boy_and_birth_fields_are_not_missing():
    facts = extract_explicit_facts("2026年10月31日19:01分出生的男孩取什么名，爸爸姓陈")
    assert facts == {"gender": "男孩", "birth_date": "2026-10-31", "birth_time": "19:01", "surname": "陈"}
