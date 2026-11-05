#!/bin/zsh
set -eu
cd -- "$(dirname "$0")"
PYTHON="$PWD/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
fi
if [[ ! -x "$PYTHON" ]]; then PYTHON="$(command -v python3)"; fi
if ! "$PYTHON" -c 'import openpyxl, pymysql' 2>/dev/null; then
  echo "缺少 Python 依赖。请先在此目录创建虚拟环境并安装 requirements.txt。"
  read -r "?按回车退出。"
  exit 1
fi
if curl -fsS --max-time 2 http://127.0.0.1:8766/api/status >/dev/null 2>&1; then
  open http://127.0.0.1:8766/
  exit 0
fi
echo "PM × McKinsey Excel 上传后台 · 保留此窗口，按 Control+C 停止。"
exec "$PYTHON" local_admin.py --open "$@"
