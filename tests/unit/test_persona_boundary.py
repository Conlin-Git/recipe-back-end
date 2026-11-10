import re
import pytest
from app.graph.prompts import (
    build_qa_system_prompt, PERSONA_CHAT, PERSONA_RECIPE, PERSONA_DEV,
    PERSONA_NAMING, CHAT_RULES, RECIPE_RULES, DEV_RULES, NAMING_RULES,
)


@pytest.mark.parametrize('persona,rules', [
    (PERSONA_CHAT, CHAT_RULES), (PERSONA_RECIPE, RECIPE_RULES),
    (PERSONA_DEV, DEV_RULES), (PERSONA_NAMING, NAMING_RULES),
])
def test_normal_questions_do_not_receive_private_persona_details(persona, rules):
    prompt = build_qa_system_prompt(persona, rules, None, question='今天有什么建议？')
    assert not re.search(r'\bchar\b', prompt, re.I)
    assert '0416' not in prompt and '白羊座' not in prompt


@pytest.mark.parametrize('question', ['Char是谁？', 'char喜欢吃什么？', '你心里最重要的人是谁？'])
def test_user_requested_persona_is_available(question):
    prompt = build_qa_system_prompt(PERSONA_CHAT, CHAT_RULES, None, question=question)
    assert 'Char 是你心里排第一位' in prompt
    assert '完全不吃苦瓜' not in prompt


def test_cooking_preferences_only_apply_when_user_asks_about_that_person():
    requested = build_qa_system_prompt(PERSONA_RECIPE, RECIPE_RULES, None, question='Char不吃哪些食材？')
    ordinary = build_qa_system_prompt(PERSONA_RECIPE, RECIPE_RULES, None, question='苦瓜怎么做？')
    assert 'Char 不吃苦瓜' in requested
    assert 'Char 不吃苦瓜' not in ordinary


def test_technical_char_is_not_treated_as_a_private_person():
    prompt = build_qa_system_prompt(PERSONA_DEV, DEV_RULES, None, question='C语言char类型占几个字节？')
    assert '0416' not in prompt and 'Char 是你心里' not in prompt


def test_a_request_to_stop_mentioning_the_person_does_not_enable_the_persona():
    prompt = build_qa_system_prompt(PERSONA_CHAT, CHAT_RULES, None, question='不要主动提Char')
    assert 'Char 是你心里' not in prompt
