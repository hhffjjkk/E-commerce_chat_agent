"""pytest 全局初始化。

确保只导入 `agent_system.harness`（不经过 `agent_system.app`）的测试也能拿到
内置工具；`register_all()` 幂等，与 app.py 的注册互不冲突。
"""

from agent_system.harness.builtin_tools import register_all

register_all()
