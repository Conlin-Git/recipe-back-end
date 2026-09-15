"""注册 / 登录业务。登录功能开发时实现，api/v1/auth.py 调用这里。"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import passwd_crypto, rate_limit, security
from app.core.exceptions import BusinessException
from app.crud import user as user_crud
from app.models.user import User
from app.schemas.auth import LoginIn, RegisterIn


async def register(db: AsyncSession, data: RegisterIn) -> User:
    if await user_crud.get_by_username(db, data.username):
        raise BusinessException(400, "用户名已存在")
    # 密文传输 → 解密出明文；明文长度校验放在解密后（schema 只能约束密文长度）
    plain_password = passwd_crypto.decrypt_password(data.password)
    if not 6 <= len(plain_password) <= 64:
        raise BusinessException(400, "密码长度需为 6-64 位")
    user = User(
        username=data.username,
        email=data.email,
        hashed_password=security.hash_password(plain_password),
        nickname=data.username,
    )
    return await user_crud.create(db, user)


async def login(db: AsyncSession, data: LoginIn, client_ip: str) -> str:
    """校验成功返回 JWT，失败抛业务异常。IP + 用户名双维度限流防爆破。"""
    await rate_limit.check_login_allowed(data.username, client_ip)
    plain_password = passwd_crypto.decrypt_password(data.password)
    user = await user_crud.get_by_username(db, data.username)
    if not user or not security.verify_password(plain_password, user.hashed_password):
        await rate_limit.record_login_failure(data.username, client_ip)
        raise BusinessException(401, "用户名或密码错误")
    await rate_limit.clear_login_failures(data.username, client_ip)
    return security.create_access_token(user.id)
