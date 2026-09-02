#!/usr/bin/env bash
# English OS - 发布用启动入口（适配注入的 PORT 环境变量）
set -e
cd "$(dirname "$0")"

# 安装依赖
pip install -q -r backend/requirements.txt 2>/dev/null || true

cd backend
exec python3 -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
