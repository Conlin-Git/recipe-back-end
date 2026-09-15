"""登录防爆破：基于 Redis 的固定窗口限流。

按 IP 和用户名双维度计数，任一维度在窗口内失败超限即锁定：
- 防单账号爆破（username 维度）
- 防单 IP 扫多账号撞库（IP 维度）
Redis 异常时 fail-open 放行并记日志（与项目其他旁路服务一致，不阻塞主链路）。
"""
from app.core.exceptions import BusinessException
from app.database.redis import redis_client

# 窗口内允许的最大失败次数 / 窗口时长（锁定时间即窗口剩余时间）
LOGIN_MAX_FAILURES: int = 5
LOGIN_WINDOW_SECONDS: int = 15 * 60  # 15 分钟

_IP_PREFIX = "rl:login:ip:"
_USER_PREFIX = "rl:login:user:"
_CHAT_PREFIX = "rl:chat:"


async def check_login_allowed(username: str, client_ip: str) -> None:
    """任一维度已超限则抛 429。登录校验前先调。"""
    try:
        ip_count, user_count = await redis_client.mget(
            f"{_IP_PREFIX}{client_ip}", f"{_USER_PREFIX}{username}"
        )
        for count in (ip_count, user_count):
            if count is not None and int(count) >= LOGIN_MAX_FAILURES:
                raise BusinessException(429, "尝试次数过多，请15分钟后再试")
    except BusinessException:
        raise
    except Exception as e:
        print(f"⚠️ 登录限流检查异常，放行本次请求：{e}")


async def record_login_failure(username: str, client_ip: str) -> None:
    """登录失败计数 +1（双维度），首次计数时设置窗口过期时间。"""
    try:
        async with redis_client.pipeline(transaction=True) as pipe:
            for key in (f"{_IP_PREFIX}{client_ip}", f"{_USER_PREFIX}{username}"):
                await pipe.incr(key)
                await pipe.expire(key, LOGIN_WINDOW_SECONDS, nx=True)
            await pipe.execute()
    except Exception as e:
        print(f"⚠️ 登录失败计数异常：{e}")


async def clear_login_failures(username: str, client_ip: str) -> None:
    """登录成功后清零双维度计数。"""
    try:
        await redis_client.delete(f"{_IP_PREFIX}{client_ip}", f"{_USER_PREFIX}{username}")
    except Exception as e:
        print(f"⚠️ 登录计数清零异常：{e}")


async def check_chat_rate(user_id: int, limit_per_minute: int) -> None:
    """对话请求限流（防成本攻击）：每用户每分钟超限抛 429。

    每次对话会触发编排/情感/（可能）校验多路 LLM 调用，必须在进图之前拦。
    计入即消耗：本次请求无论后续成败都占一次额度。Redis 异常 fail-open。
    """
    try:
        key = f"{_CHAT_PREFIX}{user_id}"
        async with redis_client.pipeline(transaction=True) as pipe:
            await pipe.incr(key)
            await pipe.expire(key, 60, nx=True)
            count, _ = await pipe.execute()
        if int(count) > limit_per_minute:
            raise BusinessException(429, "说话太频繁啦，歇一分钟再聊")
    except BusinessException:
        raise
    except Exception as e:
        print(f"⚠️ 对话限流检查异常，放行本次请求：{e}")
