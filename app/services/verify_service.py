"""RAG 检索结果常识校验：用硅基流动免费小模型（Qwen2.5-7B-Instruct）审查菜谱资料。

校验点（只管常识，不管相关性——相关性已由 reranker 负责）：
- 食材、用量、大小、时间、温度等描述有明显离谱错误的算不通过
  （例如用户问茄子，资料却把茄子描述成"手指粗细大小"这类违背常识的说法）。

不通过时由 chat_service 排除这批菜谱重新检索（最多 settings.RAG_VERIFY_MAX_RETRIES 次）。
校验服务自身异常时 fail-open（视为通过），不阻塞主对话流程。
"""
import json
import re

from openai import AsyncOpenAI

from app.config import settings
from app.core.tracing import traceable, wrap_openai

verify_client = wrap_openai(AsyncOpenAI(
    api_key=settings.SILICONFLOW_API_KEY,
    base_url=settings.SILICONFLOW_BASE_URL,
    timeout=settings.RAG_VERIFY_TIMEOUT,
))

# 注意：JSON 示例的大括号已转义（{{ }}），format 后才是合法 JSON
VERIFY_PROMPT = """你是菜谱资料的常识校验员。给你「用户问题」和「检索到的菜谱资料」。

注意：这些资料已经过专业的相关性排序，默认和用户问题相关，你【不需要】判断相关性。
资料里出现和用户问的不同的菜品是完全正常的（比如问"炒鸡蛋"给到"番茄炒蛋"，问"清蒸鱼"给到"红烧鱼"），不算错误。

你只判断一件事：资料内容是否符合烹饪和生活常识。食材、用量、大小、时间、温度、步骤等描述
有明显离谱错误的才算不通过（例如把茄子描述成"手指粗细大小"、炒鸡蛋要煮两个小时这类明显违背常识的说法）。

拿不准就通过，只有明确违背常识的才不通过。

用户问题：{question}

检索到的菜谱资料：
{context}

只输出 JSON，不要任何其他内容：
通过：{{"pass": true}}
不通过：{{"pass": false, "reason": "一句话说明哪里违背常识"}}"""


def _compact_context(recipes: list[dict]) -> str:
    """拼校验用资料文本：只留文字不带图片链接（省 token）。"""
    blocks = []
    for i, r in enumerate(recipes, 1):
        parts = [f"【菜谱{i}】{r.get('title', '')}"]
        if r.get("desc"):
            parts.append(f"简介：{r['desc']}")
        if r.get("steps"):
            parts.append("步骤：" + "；".join(s["text"] for s in r["steps"]))
        if r.get("tip"):
            parts.append(f"小贴士：{r['tip']}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


@traceable(run_type="chain", name="check_recipe_context")
async def check_recipe_context(question: str, recipes: list[dict]) -> tuple[bool, str]:
    """校验通过返回 (True, "")，不通过返回 (False, 原因)。异常时 fail-open 返回 (True, "")。"""
    try:
        resp = await verify_client.chat.completions.create(
            model=settings.SILICONFLOW_LLM_MODEL,
            messages=[{"role": "user", "content": VERIFY_PROMPT.format(
                question=question, context=_compact_context(recipes))}],
            max_tokens=150,
            temperature=0,
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        result = json.loads(m.group(0)) if m else {}
        if result.get("pass") is False:
            return False, str(result.get("reason") or "校验模型未说明原因")
        return True, ""
    except Exception as e:
        # 校验服务挂了就放行，别阻塞主流程
        print(f"⚠️ RAG 常识校验异常，默认放行：{e}")
        return True, ""
