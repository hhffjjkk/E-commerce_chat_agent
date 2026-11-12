#!/usr/bin/env bash
# 本地启动服务：uvicorn agent_system.app:app
#
# 用法：
#   bash scripts/dev_run.sh                 # 默认 127.0.0.1:8010，JSON 日志
#   LOG_FORMAT=text bash scripts/dev_run.sh # 本地调试用可读日志
#   PORT=8020 bash scripts/dev_run.sh       # 换端口
#   额外参数会原样透传给 uvicorn，例如：
#   bash scripts/dev_run.sh --no-access-log
set -euo pipefail

# 切到项目根目录，保证 agent_system 包与 data/、static/ 相对路径都能正确解析
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Python 解释器：默认使用项目根目录的虚拟环境
PYTHON_BIN="${PYTHON_BIN:-.venv/Scripts/python.exe}"

# 运行期配置：可用同名环境变量覆盖
export SERVICE_NAME="${SERVICE_NAME:-ecommerce-agent}"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export LOG_FORMAT="${LOG_FORMAT:-json}"

# --no-access-log 建议与 JSON 日志一起用，避免 uvicorn 的文本访问日志混进结构化输出
exec "$PYTHON_BIN" -m uvicorn agent_system.app:app \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8010}" \
  "$@"
