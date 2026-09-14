"""SSE 格式化工具。"""
import json

SSE_DONE = "data: [DONE]\n\n"


def sse_data(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
