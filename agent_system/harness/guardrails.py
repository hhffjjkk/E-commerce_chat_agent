# agent_system/harness/guardrails.py
# 写操作 Guardrails：服务端两阶段确认状态机。
# 第一次调用只创建 pending 记录并返回 needs_confirmation；确认后由 confirm 执行真正写操作。
# 状态机放在服务端而不是只写进提示词：可审计、不可被模型绕过、跨模型版本稳定。

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from contextlib import nullcontext
from typing import Callable, ContextManager

from agent_system.cache.errors import RedisUnavailable
from agent_system.errors.codes import ErrorCode
from agent_system.errors.exceptions import AgentError
from agent_system.harness.pending_store import (
    PendingAction,
    PendingActionStore,
    _is_expired,
    get_pending_store,
    new_action_id,
)
from agent_system.obs.events import log_event

# 只有这两个写工具需要两阶段确认
WRITE_TOOLS = {"apply_refund", "cancel_order"}


def _env_int(name: str, default: int) -> int:
    """读取正整数环境变量；缺失/非法/非正数回退默认值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量；非法值回退默认值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _fingerprint(args: dict) -> str:
    """计算参数指纹，用于同一会话内 pending 去重（不记录参数值到日志）。"""
    raw = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def default_summary(tool_name: str, args: dict) -> str:
    """服务端生成写操作摘要（不依赖 LLM，避免被提示注入伪造）。

    参数:
        tool_name: apply_refund / cancel_order。
        args: 原始工具参数。

    返回:
        只含安全字段的中文摘要。
    """
    order_id = str(args.get("order_id", "")).strip() or "（未知订单）"
    if tool_name == "apply_refund":
        reason = str(args.get("reason", "")).strip() or "未说明"
        return f"为订单 {order_id} 申请退款，原因：{reason}"
    if tool_name == "cancel_order":
        return f"取消订单 {order_id}"
    return f"执行 {tool_name}（订单 {order_id}）"


class Guardrails:
    """写操作两阶段确认核心：拦截、确认、拒绝与过期清理。

    生命周期:
        默认由 `get_guardrails()` 单例持有；测试可注入独立 store/ttl。
    线程安全:
        依赖 PendingActionStore 的锁；本类自身无共享可变状态。
    """

    def __init__(
        self,
        ttl_seconds: int = 300,
        summary_builder: Callable[[str, dict], str] | None = None,
        *,
        enabled: bool = True,
        max_pending_per_session: int = 5,
        store: PendingActionStore | None = None,
        lock_factory: Callable[[str], ContextManager[bool]] | None = None,
    ):
        """初始化 Guardrails。

        参数:
            ttl_seconds: pending 有效期（秒），0 表示立即过期（便于测试）。
            summary_builder: 摘要构造函数，缺省使用 default_summary。
            enabled: 总开关；False 时 intercept_write 直接执行写操作。
            max_pending_per_session: 单会话 pending 上限，超出时最旧的作废。
            store: 记录存储，缺省使用全局单例。
            lock_factory: confirm 的锁工厂；None（默认）表示不加锁。
                内存后端保持零行为变化；Redis 后端由 get_guardrails() 注入
                acquire_pending_lock，用于跨实例串行化同一 action_id 的确认。

        异常:
            ValueError: ttl 为负或上限小于 1。
        """
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds 不能为负数")
        if max_pending_per_session < 1:
            raise ValueError("max_pending_per_session 必须 >= 1")
        self.ttl_seconds = ttl_seconds
        self.summary_builder = summary_builder or default_summary
        self.enabled = enabled
        self.max_pending_per_session = max_pending_per_session
        self.store = store if store is not None else get_pending_store()
        # 默认 None = 不加锁，保证内存后端与阶段三步骤 4 的行为逐字一致
        self.lock_factory = lock_factory

    def _execute_write(self, tool_name: str, args: dict) -> dict:
        """真正执行写操作实现（绕过对外工具的拦截）。

        参数:
            tool_name: apply_refund / cancel_order。
            args: 工具参数。

        返回:
            工具实现返回的 dict。

        异常:
            AgentError: 业务失败；非领域异常会被映射为 ToolExecutionError。
        """
        # 延迟导入避免 tools <-> guardrails 的模块循环
        from agent_system import tools

        impl = getattr(tools, f"_{tool_name}_impl")
        try:
            return impl(**args)
        except AgentError:
            # 已是领域异常，保持原始错误码（例如订单状态不允许退款）
            raise
        except Exception as exc:  # noqa: BLE001 - 统一映射为领域异常
            mapped = tools.map_tool_exception(tool_name, exc, sorted(args))
            raise mapped from exc

    def intercept_write(self, tool_name: str, args: dict, session_id: str) -> dict:
        """拦截写操作：创建或复用 pending 记录，不执行真正写操作。

        参数:
            tool_name: 必须是 apply_refund / cancel_order。
            args: 原始工具参数。
            session_id: 当前会话。

        返回:
            {"needs_confirmation": True, "action_id", "summary", "expires_in"}；
            总开关关闭时直接返回工具执行结果。

        异常:
            ValueError: 非写工具或缺少 session_id。
        """
        if tool_name not in WRITE_TOOLS:
            raise ValueError(f"不是写操作工具: {tool_name}")
        if not session_id:
            raise ValueError("session_id 不能为空")

        # 应急开关：关闭两阶段时直接执行，行为回到重构前
        if not self.enabled:
            return self._execute_write(tool_name, args)

        try:
            return self._intercept_write(tool_name, args, session_id)
        except RedisUnavailable as exc:
            # pending 存储不可用：无法保证去重与单会话上限，绝不冒险直接执行写操作。
            # 用户收到 409 后可重新发起，避免“以为只是重试、实际执行两次”的风险。
            raise AgentError(
                ErrorCode.CONFIRMATION_REQUIRED,
                details={"reason": "pending_store_unavailable"},
            ) from exc

    def _intercept_write(self, tool_name: str, args: dict, session_id: str) -> dict:
        """intercept_write 的存储操作部分；Redis 异常由调用方统一映射。"""
        self.store.purge_expired()

        fingerprint = _fingerprint(args)
        # 同一会话、同一工具、同一参数已有 pending 时复用，保证重复调用幂等
        for pending in self.store.get_by_session(session_id):
            if (
                pending.status == "pending"
                and pending.tool_name == tool_name
                and _fingerprint(pending.args) == fingerprint
            ):
                return self._confirmation_payload(pending)

        # 单会话 pending 上限：超出时把最旧的 pending 作废，防止滥用刷单
        pendings = [
            p for p in self.store.get_by_session(session_id) if p.status == "pending"
        ]
        while len(pendings) >= self.max_pending_per_session:
            oldest = pendings.pop(0)
            self.store.mark(oldest.action_id, "expired")
            log_event(
                "confirmation_expired",
                action_id=oldest.action_id,
                tool=oldest.tool_name,
                session_id=session_id,
                # created_at 是墙钟，age 也必须用墙钟计算
                age_ms=round((time.time() - oldest.created_at) * 1000, 2),
            )

        # pending 时间戳使用墙钟：created_at/expires_at 会被其他实例读取并比较，
        # monotonic 的起点随进程启动时刻变化，跨实例不可比较。
        now = time.time()
        pending = PendingAction(
            action_id=new_action_id(),
            session_id=session_id,
            tool_name=tool_name,
            args=dict(args),
            summary=self.summary_builder(tool_name, args),
            created_at=now,
            expires_at=now + self.ttl_seconds,
            status="pending",
        )
        self.store.create(pending)
        payload = self._confirmation_payload(pending)
        log_event(
            "confirmation_requested",
            action_id=pending.action_id,
            tool=tool_name,
            session_id=session_id,
            ttl_seconds=self.ttl_seconds,
            expires_in=payload["expires_in"],
        )
        return payload

    def _confirmation_payload(self, pending: PendingAction) -> dict:
        """构造返回给 LLM/用户的待确认结构（字段名固定）。"""
        # expires_at 是墙钟，剩余时间也必须用墙钟计算
        remaining = max(0.0, pending.expires_at - time.time())
        return {
            "needs_confirmation": True,
            "action_id": pending.action_id,
            "summary": pending.summary,
            "expires_in": int(math.ceil(remaining)),
        }

    def _get_for_session(self, action_id: str, session_id: str) -> PendingAction:
        """取记录并校验归属；不存在/不匹配按统一错误码抛出。"""
        pending = self.store.get(action_id)
        if pending is None:
            log_event(
                "confirmation_expired",
                action_id=action_id,
                tool="unknown",
                session_id=session_id,
                age_ms=0,
            )
            raise AgentError(
                ErrorCode.CONFIRMATION_EXPIRED,
                details={"action_id": action_id},
            )
        if pending.session_id != session_id:
            # 跨会话确认属于越权，直接拒绝
            raise AgentError(
                ErrorCode.CONFIRMATION_MISMATCH,
                details={"action_id": action_id},
            )
        return pending

    def confirm(self, action_id: str, session_id: str) -> dict:
        """确认并执行 pending 写操作。

        参数:
            action_id: 拦截阶段返回的 ID。
            session_id: 当前会话，必须与创建时一致。

        返回:
            {"success": True, "action_id", "result"}；已执行时返回缓存结果。

        异常:
            AgentError: 不存在/过期（410）、跨会话/并发冲突（409）、执行失败（原错误码）。
        """
        # 锁只包住“读 pending → 执行写工具 → mark executed”整段：
        # 内存后端 lock_factory=None 时为 nullcontext(True)，行为与阶段三步骤 4 逐字一致；
        # Redis 后端由 get_guardrails() 注入 acquire_pending_lock，跨实例串行化同一 action。
        lock_context = (
            self.lock_factory(action_id)
            if self.lock_factory is not None
            else nullcontext(True)
        )
        try:
            with lock_context as acquired:
                if not acquired:
                    # 另一个实例正在执行同一 action：按并发冲突处理，避免双写
                    raise AgentError(
                        ErrorCode.CONFIRMATION_MISMATCH,
                        details={"action_id": action_id},
                    )
                return self._confirm_locked(action_id, session_id)
        except RedisUnavailable as exc:
            # Redis 故障下无法保证幂等：宁可让用户重试，也不能冒险执行两次
            log_event(
                "confirmation_failed",
                action_id=action_id,
                tool="unknown",
                session_id=session_id,
                error_code="redis_unavailable",
            )
            raise AgentError(
                ErrorCode.CONFIRMATION_MISMATCH,
                details={"action_id": action_id},
            ) from exc

    def _confirm_locked(self, action_id: str, session_id: str) -> dict:
        """confirm 的存储与写操作部分；分布式锁由调用方持有。"""
        self.store.purge_expired()
        pending = self._get_for_session(action_id, session_id)

        # 已执行：直接返回缓存结果，保证网络重试/模型重复调用幂等
        if pending.status == "executed":
            return {
                "success": True,
                "action_id": pending.action_id,
                "result": pending.result,
            }
        # confirmed 表示另一个请求正在执行：按并发冲突处理，避免双写
        if pending.status == "confirmed":
            raise AgentError(
                ErrorCode.CONFIRMATION_MISMATCH,
                details={"action_id": action_id},
            )
        # 防御性墙钟判断：purge_expired 通常已把过期记录转为 expired，
        # 但跨实例并发/索引缺失时仍可能读到 status=pending 的过期记录；
        # 确认必须拒绝，绝不能执行一个业务上已过期的写操作。
        if pending.status == "pending" and _is_expired(pending):
            self.store.mark(action_id, "expired")
            log_event(
                "confirmation_expired",
                action_id=pending.action_id,
                tool=pending.tool_name,
                session_id=session_id,
                age_ms=round((time.time() - pending.created_at) * 1000, 2),
            )
            raise AgentError(
                ErrorCode.CONFIRMATION_EXPIRED,
                details={"action_id": action_id},
            )

        if pending.status in {"rejected", "expired"}:
            log_event(
                "confirmation_expired",
                action_id=pending.action_id,
                tool=pending.tool_name,
                session_id=session_id,
                age_ms=round((time.time() - pending.created_at) * 1000, 2),
            )
            raise AgentError(
                ErrorCode.CONFIRMATION_EXPIRED,
                details={"action_id": action_id},
            )

        # 先落 confirmed 再执行：并发 confirm 会看到 confirmed 并得到 409
        self.store.mark(action_id, "confirmed")
        started = time.monotonic()
        try:
            result = self._execute_write(pending.tool_name, pending.args)
        except AgentError as exc:
            # 执行失败禁止重试同一 pending，避免订单状态反复变更卡死
            self.store.mark(action_id, "rejected")
            log_event(
                "confirmation_failed",
                action_id=pending.action_id,
                tool=pending.tool_name,
                session_id=session_id,
                error_code=exc.code,
            )
            raise

        self.store.mark(action_id, "executed", result=result)
        log_event(
            "confirmation_granted",
            action_id=pending.action_id,
            tool=pending.tool_name,
            session_id=session_id,
            execute_latency_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return {"success": True, "action_id": pending.action_id, "result": result}

    def reject(self, action_id: str, session_id: str) -> dict:
        """拒绝 pending 写操作（幂等）。

        参数:
            action_id: 拦截阶段返回的 ID。
            session_id: 当前会话。

        返回:
            {"success": True, "action_id", "status"}。

        异常:
            AgentError: 不存在/过期（410）、跨会话（409）。
        """
        self.store.purge_expired()
        pending = self._get_for_session(action_id, session_id)

        # 与 confirm 共用同一墙钟判断：过期记录不能再被“拒绝”掩盖成成功
        if pending.status == "pending" and _is_expired(pending):
            self.store.mark(action_id, "expired")
            log_event(
                "confirmation_expired",
                action_id=pending.action_id,
                tool=pending.tool_name,
                session_id=session_id,
                age_ms=round((time.time() - pending.created_at) * 1000, 2),
            )
            raise AgentError(
                ErrorCode.CONFIRMATION_EXPIRED,
                details={"action_id": action_id},
            )

        if pending.status == "pending":
            # 使用 mark 返回的对象：内存实现会原地修改，Redis 实现会返回新对象；
            # 若继续读旧的 pending.status，Redis 后端会错误地返回 pending。
            updated = self.store.mark(action_id, "rejected")
            log_event(
                "confirmation_rejected",
                action_id=pending.action_id,
                tool=pending.tool_name,
                session_id=session_id,
            )
            return {
                "success": True,
                "action_id": updated.action_id,
                "status": updated.status,
            }
        # 非 pending 状态幂等返回当前状态，不重复记日志
        return {
            "success": True,
            "action_id": pending.action_id,
            "status": pending.status,
        }

    def list_pending(self, session_id: str) -> list[dict]:
        """列出会话内仍为 pending 的记录（仅安全字段）。

        参数:
            session_id: 会话 ID。

        返回:
            [{"action_id", "tool", "summary", "expires_in", "status"}]。
        """
        self.store.purge_expired()
        result = []
        for pending in self.store.get_by_session(session_id):
            if pending.status != "pending":
                continue
            result.append(
                {
                    "action_id": pending.action_id,
                    "tool": pending.tool_name,
                    "summary": pending.summary,
                    "expires_in": int(
                        # expires_at 是墙钟，剩余时间也必须用墙钟
                        math.ceil(max(0.0, pending.expires_at - time.time()))
                    ),
                    "status": pending.status,
                }
            )
        return result


def _dynamic_lock_factory(action_id: str):
    """按当前 pending 后端动态返回锁上下文。

    参数:
        action_id: 待确认动作 ID。

    返回:
        内存后端为 nullcontext(True)（零行为变化）；
        Redis 后端为 acquire_pending_lock(action_id) 的上下文管理器。

    说明:
        在 confirm 调用时（而不是 Guardrails 构造时）判断后端，
        这样后端选择与锁注入解耦，也不会在内存模式下触碰 Redis。
    """
    from agent_system.harness.pending_store import pending_backend_name

    if pending_backend_name() != "redis":
        return nullcontext(True)

    from agent_system.harness.pending_locks import acquire_pending_lock

    return acquire_pending_lock(action_id)


_guardrails: Guardrails | None = None


def get_guardrails() -> Guardrails:
    """返回全局 Guardrails 单例（首次调用读取 CONFIRMATION_* 环境变量）。

    说明:
        注入 _dynamic_lock_factory：内存后端仍是无锁路径；
        Redis 后端在 confirm 时自动使用分布式锁。
    """
    global _guardrails
    if _guardrails is None:
        _guardrails = Guardrails(
            ttl_seconds=_env_int("CONFIRMATION_TTL_SECONDS", 300),
            enabled=_env_bool("CONFIRMATION_ENABLED", True),
            max_pending_per_session=_env_int(
                "CONFIRMATION_MAX_PENDING_PER_SESSION", 5
            ),
            lock_factory=_dynamic_lock_factory,
        )
    return _guardrails


def reset_guardrails() -> None:
    """重置全局 Guardrails（仅供测试隔离；不会清理已有 pending 记录）。"""
    global _guardrails
    _guardrails = None
