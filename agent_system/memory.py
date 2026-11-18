# memory.py
# 会话记忆：用 SQLite 持久化保存每个会话的对话历史，重启服务不丢失。

import json
import os
import sqlite3
from pathlib import Path

from agent_system.cache.errors import RedisUnavailable
from agent_system.cache.redis_manager import get_redis_manager
from agent_system.config import get_settings
from agent_system.obs.events import log_event

DEFAULT_DB = Path(__file__).resolve().parent.parent / "sessions.db"


class SessionStore:
    """基于 SQLite 的会话存储，支持多轮对话上下文与持久化。"""

    def __init__(self, max_history: int = 20, db_path: str | None = None):
        self.max_history = max_history
        self._conn = sqlite3.connect(
            str(db_path or DEFAULT_DB), check_same_thread=False
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                PRIMARY KEY (session_id, seq)
            )
            """
        )
        self._conn.commit()

    def get(self, session_id: str) -> list[dict]:
        """返回该会话最近的历史消息（按时间正序）。"""
        rows = self._conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? "
            "ORDER BY seq DESC LIMIT ?",
            (session_id, self.max_history),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def append(self, session_id: str, role: str, content: str) -> None:
        """追加一条消息，并做长度截断。"""
        seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        self._conn.execute(
            "INSERT INTO messages (session_id, seq, role, content) VALUES (?, ?, ?, ?)",
            (session_id, seq, role, content),
        )
        # 只保留最近 max_history 条，避免历史无限增长
        self._conn.execute(
            "DELETE FROM messages WHERE session_id = ? AND seq NOT IN "
            "(SELECT seq FROM messages WHERE session_id = ? ORDER BY seq DESC LIMIT ?)",
            (session_id, session_id, self.max_history),
        )
        self._conn.commit()

    def clear(self, session_id: str) -> None:
        """清空某个会话。"""
        self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self._conn.commit()


class RedisSessionStore:
    """基于 Redis 的会话存储（生产环境用，支持多实例并发与持久化）。"""

    def __init__(self, max_history: int = 20, session_ttl_seconds: int | None = None):
        """构造 Redis 会话存储。

        参数:
            max_history: 每个会话保留的最大消息条数。
            session_ttl_seconds: 历史消息 TTL（秒）；None 表示读取
                REDIS_SESSION_TTL_SECONDS（默认 0 = 不过期）。
                显式传 0 与不配置等价，保持与改动前行为一致。

        异常:
            RedisUnavailable: REDIS_URL 未配置，或 Redis 连接失败/超时/PING 失败。

        说明:
            - 客户端统一由 RedisManager 提供，不再自行 redis.from_url，
              连接池、超时与 decode_responses 全进程只有一份配置；
            - 本类不捕获 RedisUnavailable：是否回退 SQLite 由 create_store 统一决定，
              避免每个使用方各写一套降级逻辑；
            - 原先可选的 redis_url 参数已移除：所有 Redis 使用方共享同一个配置源，
              避免出现“会话写 A 实例、缓存读 B 实例”的隐性分裂；
            - Key 统一带 REDIS_KEY_PREFIX；前缀为空时 key 与历史版本逐字节一致。
        """
        self.max_history = max_history
        settings = get_settings()
        # 显式传入的 TTL 优先；None 回落到配置，兼容“不传参”的旧调用方式
        ttl = (
            settings.redis_session_ttl_seconds
            if session_ttl_seconds is None
            else session_ttl_seconds
        )
        self._session_ttl_seconds = max(0, int(ttl))
        self._key_prefix = settings.redis_key_prefix

        client = get_redis_manager().get_client()
        if client is None:
            # 已显式构造 Redis 存储但未配置地址：按不可用处理，给出可定位的原因
            raise RedisUnavailable("not_configured")
        self.redis = client

    def _key(self, session_id: str) -> str:
        """构造历史消息的 Redis Key。

        参数:
            session_id: 会话 ID。

        返回:
            `{prefix}:chat:{session_id}`；prefix 为空时返回 `chat:{session_id}`。

        说明:
            前缀为空时省略分隔符，保证既有部署与测试的 key 形态逐字节不变；
            历史消息与会话状态使用不同的 key 模板，生命周期互不影响。
        """
        if self._key_prefix:
            return f"{self._key_prefix}:chat:{session_id}"
        return f"chat:{session_id}"

    def get(self, session_id: str) -> list[dict]:
        """返回该会话最近的历史消息（按时间正序）。

        说明:
            读取不续期 TTL：只有写入才代表“会话活跃”，只读不应让 key 永远驻留。
        """
        key = self._key(session_id)
        items = self.redis.lrange(key, 0, -1)
        history = [json.loads(i) for i in items if i]
        return history[-self.max_history :]

    def append(self, session_id: str, role: str, content: str) -> None:
        """追加一条消息，并裁剪到最近 max_history 条。"""
        key = self._key(session_id)
        self.redis.rpush(key, json.dumps({"role": role, "content": content}, ensure_ascii=False))
        self.redis.ltrim(key, -self.max_history, -1)
        if self._session_ttl_seconds > 0:
            # TTL 只在写入时刷新（读取不续期）。这样“只读会话”不会永久驻留 Redis；
            # 若未来需要“读也续期”，应显式调用 touch 类方法，而不是隐式改变读语义。
            self.redis.expire(key, self._session_ttl_seconds)

    def clear(self, session_id: str) -> None:
        """清空某个会话。"""
        self.redis.delete(self._key(session_id))


def create_store(max_history: int = 20):
    """根据环境变量选择会话存储：有 REDIS_URL 用 Redis，否则用本地 SQLite。

    参数:
        max_history: 每个会话保留的最大消息条数。

    返回:
        SessionStore 或 RedisSessionStore 实例。

    异常:
        RedisUnavailable: 配置了 REDIS_URL 但 Redis 不可达，且
            REDIS_FALLBACK_TO_MEMORY=false（默认）。

    说明:
        - REDIS_URL 为空：直接 SQLite，与历史行为完全一致；
        - REDIS_URL 已配置：走 Redis；若不可达：
            - REDIS_FALLBACK_TO_MEMORY=true → 回退 SQLite，并记录 redis_fallback；
            - false（默认）→ 抛出 RedisUnavailable，让启动失败而不是悄悄分叉数据；
        - 两条分支都记录 store_selected，便于在启动日志里确认当前后端。
          回退必须显式开启且可见，禁止静默降级。
    """
    if os.getenv("REDIS_URL"):
        try:
            # 明确传入 TTL：让“存储上限”和“过期策略”都由 config 决定，便于运维对账
            store = RedisSessionStore(
                max_history,
                session_ttl_seconds=get_settings().redis_session_ttl_seconds,
            )
        except RedisUnavailable as exc:
            if get_settings().redis_fallback_to_memory:
                # 显式回退：记录原因与目标后端，避免“Redis 挂了却以为还在用 Redis”
                log_event(
                    "redis_fallback",
                    reason=str(exc) or "unavailable",
                    fallback_to="sqlite",
                )
                log_event("store_selected", backend="sqlite", reason="redis_fallback")
                return SessionStore(max_history)
            # 默认显式失败：启动阶段暴露问题，避免会话数据在两种存储之间分裂
            raise
        log_event("store_selected", backend="redis")
        return store

    log_event("store_selected", backend="sqlite", reason="redis_not_configured")
    return SessionStore(max_history)
