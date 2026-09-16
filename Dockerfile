# English OS — Oracle Cloud Always Free 部署镜像
# 仅用于部署，不修改任何业务代码。
# 数据源：由运行时的 DATABASE_URL 决定（设了 = Neon Postgres；不设 = 本地 SQLite 回退）。
# 生产部署务必在 .env 里设置 DATABASE_URL 指向**现有 Neon**（与 Render 同源）。

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# tzdata：backend/db.py 用 zoneinfo("Asia/Shanghai") 计算学习天数/自然周。
# slim 镜像默认缺 tzdata，会退化成固定 +8 偏移；显式安装以保证时区换算正确。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，利用镜像层缓存（requirements 不变时无需重装）
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# 复制整个仓库（后端 + 前端静态 + 词库种子）。业务代码原样，不做任何改写。
COPY . .

# 端口固定 8000，仅对内部（Caddy）暴露；不对外直接暴露。
# 注意：绝不设置 RENDER 环境变量，否则 db.py 会因缺少 DATABASE_URL 而拒绝启动。
EXPOSE 8000
CMD ["sh", "-c", "cd backend && exec uvicorn main:app --host 0.0.0.0 --port 8000"]
