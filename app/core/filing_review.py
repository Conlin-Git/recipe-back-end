"""公开审核登录态的有效期与接口白名单。"""

FILING_REVIEW_SCOPE = "filing-review"
FILING_REVIEW_DAYS = 30

# 使用 FastAPI 已匹配的路由模板，避免路径前缀匹配误放行其他接口。
_ALLOWED_ROUTES = {
    ("GET", "/api/v1/users/me"),
    ("GET", "/api/v1/conversations"),
    ("GET", "/api/v1/conversations/{conversation_id}/messages"),
    ("POST", "/api/v1/conversations/{conversation_id}/read"),
    ("POST", "/api/v1/chat"),
    ("GET", "/api/v1/chat/{conversation_id}/stream"),
    ("POST", "/api/v1/chat/{conversation_id}/stop"),
}


def filing_review_allows(method: str, route_path: str) -> bool:
    return (method, route_path) in _ALLOWED_ROUTES
