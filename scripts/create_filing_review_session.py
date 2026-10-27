"""创建全新专用审核账号，将固定 30 天 JWT 写入前端构建环境文件。

在后端实际运行环境执行（使用与运行服务一致的 .env）：
.venv/bin/python -m scripts.create_filing_review_session \
    --frontend-env ../recipe-front-end/.env.production.local
"""
import argparse
import asyncio
import os
from pathlib import Path
import re
import secrets
import tempfile
from datetime import datetime, timezone


def write_frontend_env(path: Path, token: str) -> None:
    """只替换专用变量，保留其余部署配置；原子写入且不在终端打印凭证。"""
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    content = re.sub(
        r"^(?:export\s+)?VITE_FILING_REVIEW_TOKEN\s*=.*(?:\n|$)",
        "", original, flags=re.MULTILINE,
    ).rstrip("\n")
    content += ("\n" if content else "") + f"VITE_FILING_REVIEW_TOKEN={token}\n"
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".filing-review-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def create_session(frontend_env: Path) -> None:
    # 延迟导入，使 --help 和环境文件写入测试不依赖数据库配置。
    from app.core.security import create_filing_review_token, hash_password
    from app.database.mysql import AsyncSessionLocal, engine
    from app.models.user import User
    import jwt

    try:
        async with AsyncSessionLocal() as db:
            # 每次新建独立账号，不复用真实用户；随机密码不输出、不内置前端。
            user = User(
                username=f"filing_review_{secrets.token_hex(8)}",
                hashed_password=hash_password(secrets.token_urlsafe(32)),
                nickname="网站内容体验",
            )
            db.add(user)
            await db.flush()
            token = create_filing_review_token(user.id)
            # 先确认环境文件可写，失败时数据库事务回滚。
            # 若数据库提交失败，则还原之前的配置，避免留下无效账号凭证。
            previous = frontend_env.read_bytes() if frontend_env.exists() else None
            write_frontend_env(frontend_env, token)
            try:
                await db.commit()
            except Exception:
                if previous is None:
                    frontend_env.unlink(missing_ok=True)
                else:
                    frontend_env.write_bytes(previous)
                raise
        expires = jwt.decode(token, options={"verify_signature": False})["exp"]
        print(f"已创建审核账号：{user.username}")
        print(f"登录态到期时间（UTC）：{datetime.fromtimestamp(expires, timezone.utc).isoformat()}")
        print(f"已写入：{frontend_env}；请重新构建并部署前端。")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="创建独立审核账号及固定 30 天的内置登录态")
    parser.add_argument("--frontend-env", type=Path, required=True, help="前端 .env.production.local 路径")
    args = parser.parse_args()
    frontend_env = args.frontend_env.expanduser().resolve()
    if not frontend_env.parent.is_dir():
        parser.error("前端环境文件所在目录不存在")
    if not frontend_env.name.endswith(".local"):
        parser.error("请使用被 Git 忽略的 .local 环境文件")
    asyncio.run(create_session(frontend_env))


if __name__ == "__main__":
    main()
