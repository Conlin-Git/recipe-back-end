"""图片代理：步骤图是豆果 CDN 的 http 外链且不支持 https，
部署到 https 站点后浏览器按混合内容拦截，经后端中转走 https 出图。

URL 约定：/api/v1/image-proxy/{host}/{原始路径}，如
/api/v1/image-proxy/cp1.douguo.net/upload/caiku/a/6/a/xxx.jpg
（路径式而非 query 参数，前端 200_ 缩略图回退靠替换最后一段文件名实现）
"""
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

router = APIRouter(tags=["image-proxy"])

# 只代理数据集里出现过的图床，防 SSRF
_ALLOWED_HOST_SUFFIXES = (".douguo.net", ".douguo.com")


def _host_allowed(host: str) -> bool:
    host = host.lower()
    return any(host == s.lstrip(".") or host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES)


@router.get("/image-proxy/{rest:path}")
async def image_proxy(rest: str):
    host, sep, path = rest.partition("/")
    if not sep or not _host_allowed(host):
        raise HTTPException(status_code=400, detail="不允许的图片源")
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            # 带豆果 referer 防防盗链拦截
            resp = await client.get(
                f"http://{host}/{path}",
                headers={"Referer": "https://www.douguo.com/"},
            )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="图片拉取失败")
    if resp.status_code != 200:
        # 404 让前端 onerror 回退 200_ 缩略图
        raise HTTPException(status_code=404, detail="图片不存在")
    # URL 按内容哈希命名，可长缓存
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg").split(";")[0],
        headers={"Cache-Control": "public, max-age=2592000, immutable"},
    )
