#!/usr/bin/env bash
# Unified one-command launcher for aistudio-api (Linux / macOS / Android Termux).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$PROJECT_ROOT"

PORT_ARG=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --port)
      [ "$#" -ge 2 ] || { echo "错误: --port 需要参数" >&2; exit 1; }
      PORT_ARG=(--port "$2")
      shift 2
      ;;
    -h|--help)
      echo "用法: bash start.sh [--port 8080]"
      exit 0
      ;;
    *)
      echo "未知选项: $1 (使用 --help 查看帮助)" >&2
      exit 1
      ;;
  esac
done

# Auto-heal: if virtual env or local IPC signer is missing, run install.sh first
if [ ! -x "${PROJECT_ROOT}/.venv/bin/python" ] || [ ! -x "${PROJECT_ROOT}/waa-signer/bin/waa-signer" ]; then
  echo "[start] 检测到尚未完成初始化或缺少本地签名器，正在自动执行安装..."
  bash "${PROJECT_ROOT}/install.sh"
fi

if [ -n "${PREFIX:-}" ] && [[ "${PREFIX}" == *"/com.termux"* ]]; then
  case ":$PATH:" in
    *":${PREFIX}/bin:"*) ;;
    *) export PATH="${PREFIX}/bin:$PATH" ;;
  esac
else
  for d in "${HOME}/.local/bin" "${HOME}/.cargo/bin"; do
    if [ -d "$d" ]; then
      case ":$PATH:" in
        *":$d:"*) ;;
        *) export PATH="$d:$PATH" ;;
      esac
    fi
  done
fi

echo "[start] 正在启动 aistudio-api；端口采用 --port、环境变量或 .env 配置..."
exec uv run --no-sync python main.py server "${PORT_ARG[@]}"
