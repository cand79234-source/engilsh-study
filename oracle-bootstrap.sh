#!/usr/bin/env bash
# English OS — Oracle Cloud Always Free 一键部署脚本
#
# 在 Oracle VM（Ubuntu 22.04 / 24.04，Ampere ARM64 或 AMD 均可）上以 root 或 sudo 运行：
#     curl -fsSL https://raw.githubusercontent.com/cand79234-source/engilsh-study/main/oracle-bootstrap.sh | bash
# 或先把仓库 clone 下来再执行：  bash oracle-bootstrap.sh
#
# 本脚本只做部署，不碰任何业务代码、不新建/迁移数据库。
# 前置：OCI 控制台已把 22/80/443 入站放行（见 ORACLE_DEPLOY.md）。

set -euo pipefail

REPO="https://github.com/cand79234-source/engilsh-study.git"
APP_DIR="/opt/english-os"

# 记录实际使用者（sudo 调用时为 SUDO_USER），后续把目录属主与 docker 组交给它，
# 这样人工操作与 GitHub Actions CI（同一非 root 用户）都能免 sudo 跑 docker。
if [ -n "${SUDO_USER:-}" ]; then
  RUN_USER="$SUDO_USER"
else
  RUN_USER="$(id -un)"
fi

echo "==> [1/5] 安装 Docker 与 compose 插件"
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
else
  echo "    Docker 已安装，跳过"
fi

echo "==> [2/5] 获取代码（clone 或 pull）"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
  chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR" 2>/dev/null || true
fi
cd "$APP_DIR"
# 把使用者加入 docker 组（重新登录后生效），便于后续免 sudo 操作
usermod -aG docker "$RUN_USER" 2>/dev/null || true

echo "==> [3/5] 准备 .env"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    已生成 .env，请先填写后再启动（本脚本不替你写密钥，避免落到日志/历史里）："
  echo "        nano $APP_DIR/.env"
  echo "    至少填写 DATABASE_URL（与 Render 相同的 Neon 连接串）和 EOS_TOKEN。"
  echo "    填好后重新执行本脚本：  bash $APP_DIR/oracle-bootstrap.sh"
  exit 0
fi

# 安全闸：没有有效的 DATABASE_URL 绝不启动，防止回落到容器内 SQLite（数据会丢）
if ! grep -q '^DATABASE_URL=postgresql' .env; then
  echo "错误：DATABASE_URL 未设置或未以 postgresql:// 开头。" >&2
  echo "请先在 .env 填写与 Render 相同的 Neon 连接串，否则会回落到容器内 SQLite（数据丢失）。" >&2
  exit 1
fi

echo "==> [4/5] 放行防火墙端口 80/443/22（OCI 控制台入站规则也需开放）"
ufw allow 22/tcp 2>/dev/null || true
ufw allow 80/tcp 2>/dev/null || true
ufw allow 443/tcp 2>/dev/null || true

echo "==> [5/5] 构建并启动（docker compose）"
docker compose up -d --build

echo ""
echo "==> 完成。容器状态："
docker compose ps
echo ""
echo "    浏览器打开："
DOMAIN_VAL="$(grep '^DOMAIN=' .env | cut -d= -f2- | tr -d '[:space:]')"
if [ -n "$DOMAIN_VAL" ]; then
  echo "      https://$DOMAIN_VAL"
else
  echo "      本机公网 IP（OCI 控制台 → 实例详情 → Public IP）"
fi
echo ""
echo "    验证健康：  curl http://<IP或域名>/api/health  应返回 {\"ok\":true,...}"
echo "    查看日志：  docker compose logs -f app"
echo ""
echo "    说明：已将用户 '$RUN_USER' 加入 docker 组，重新登录（或 newgrp docker）后"
echo "          该用户即可免 sudo 执行 docker；GitHub Actions CI 也请用同一个用户。"
