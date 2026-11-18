# agent_system/cache/errors.py
# 缓存基础设施异常：与用户可见的 AgentError 解耦。


class RedisUnavailable(RuntimeError):
    """Redis 不可用（未配置、连接失败、超时、PING 失败）。

    为什么独立于 AgentError：
      - AgentError 是“面向用户可见”的领域异常，会被统一错误处理器映射成
        HTTP 状态码与错误响应体；
      - RedisUnavailable 是基础设施异常，是否降级由使用方决定：
        memory 层可回退 SQLite，pending store 可回退内存，语义缓存可直接视为未命中；
      - 若把它并入 AgentError，上述降级分支会被迫区分“这是基础设施还是业务错误”，
        而且可能把 Redis 故障直接暴露到 /chat 的响应体中。

    消息约定为简短原因码（not_configured / connect_error / timeout / ping_failed），
    便于日志与 /health/ready 的 detail 直接复用，且不包含连接串或凭据。
    """
