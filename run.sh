#!/usr/bin/env bash
# English OS 启动脚本
set -e
cd "$(dirname "$0")"

# 安装依赖（如需）
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r backend/requirements.txt 2>/dev/null || true

echo "=============================================="
echo "  English OS - 个人英语学习系统"
echo "  打开: http://localhost:8000"
echo "=============================================="
cd backend
exec python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
