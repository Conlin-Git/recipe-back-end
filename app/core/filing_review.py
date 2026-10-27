"""公开审核登录态的有效期与接口白名单。"""
import re

FILING_REVIEW_SCOPE = "filing-review"
FILING_REVIEW_DAYS = 30

# 匹配完整请求路径，不依赖多层 APIRouter 中不含父级 prefix 的 route.path。
# fullmatch 防止仅按前缀判断时误放行其他接口。
_ALLOWED_PATHS = {
    ("GET", "/api/v1/users/me"),
    ("GET", "/api/v1/conversations"),
    ("GET", r"/api/v1/conversations/[0-9]+/messages"),
    ("POST", r"/api/v1/conversations/[0-9]+/read"),
    ("POST", "/api/v1/chat"),
    ("GET", r"/api/v1/chat/[0-9]+/stream"),
    ("POST", r"/api/v1/chat/[0-9]+/stop"),
}


def filing_review_allows(method: str, path: str) -> bool:
    return any(
        method == allowed_method and re.fullmatch(pattern, path) is not None
        for allowed_method, pattern in _ALLOWED_PATHS
    )
