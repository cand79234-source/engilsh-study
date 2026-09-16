# 🚀 Oracle Cloud Always Free 部署指南（迁移，不重新开发）

> 本文件只讲**部署迁移**。业务逻辑、前端、学习规则、数据库结构一律不动。
> 配套新增的部署文件：`Dockerfile`、`docker-compose.yml`、`Caddyfile`、`.dockerignore`、
> `.env.example`、`oracle-bootstrap.sh`、`.github/workflows/deploy-oracle.yml`。

---

## 0. 数据源确认（最重要，先读）

本项目生产环境**真实数据源是 Neon Postgres**，通过环境变量 `DATABASE_URL` 连接：

- `render.yaml` 注释明确写「配合 Neon Postgres 使用」；
- `backend/db.py` 检测到 `DATABASE_URL` 即连 Postgres（Neon），否则回落本地 `data/english_os.db`（仅本地开发）；
- `backend/db.py` 有硬保护：检测到 `RENDER` 环境变量但缺 `DATABASE_URL` 会**直接拒绝启动**。

**迁移结论（已据此设计，未新建/迁移任何数据库）：**

> Oracle 实例只**复用同一条 `DATABASE_URL`** 指向你现有的 Neon 实例。
> 数据库本身完全不碰：不新建、不迁移、不清空、不重置。
> 验证期间 Render 与 Oracle 会短暂**共享同一个 Neon**（Neon 支持多连接），这是数据零丢失的关键。

⚠️ 请勿在 Oracle 上自建 Postgres 或另开数据库——那会背离「不迁移数据」的要求。

---

## 1. 架构

```
           公网 (80/443)
               │
        ┌──────▼──────┐
        │   Caddy     │  反向代理 + 自动 HTTPS（无域名则仅 HTTP）
        └──────┬──────┘
               │ 内部网络 app:8000
        ┌──────▼──────┐
        │  app 容器    │  FastAPI + 前端静态（原样运行，不改代码）
        │  (DATABASE_URL → Neon)
        └─────────────┘
               │
        ┌──────▼──────┐
        │  Neon (外部) │  既有数据库，数据原地不动
        └─────────────┘
```

- 应用固定监听容器内部 `8000`，只暴露给 Caddy；**不**直接发布到宿主机公网。
- Caddy 暴露 `80/443`；设了 `DOMAIN` 自动签发 Let's Encrypt 证书走 HTTPS，否则仅 HTTP。
- 全程只用 `python:3.11-slim` 官方镜像（多架构，ARM64/AMD 通用），契合 Always Free 的 Ampere A1。

---

## 2. 第一步（一次性）：提交部署文件到仓库

本指南的部署文件需先进入 `main` 分支，Oracle VM 才能 `git clone` / `git pull` 拿到它们。
把以下文件加入仓库并提交推送（它们都已被 `.gitignore` 正确处理：`.env` 被忽略，部署文件不忽略）：

```
Dockerfile
docker-compose.yml
Caddyfile
.dockerignore
.env.example
oracle-bootstrap.sh
.github/workflows/deploy-oracle.yml
```

> 注意：**不要提交真实的 `.env`**（含密钥）。只提交 `.env.example`。

---

## 3. 第二步：Oracle Cloud 建实例 + 开放防火墙

1. OCI 控制台 → 计算 → 实例 → 创建 **VM.Standard.E2.1.Micro**（Always Free，选 **Ubuntu 22.04/24.04**）。
   - 区域建议选离你近的（如首尔、东京、新加坡）；镜像架构 Ampere(ARM64) 或 AMD 均可。
2. **开放入站端口**（OCI 防火墙是真正生效的那道）：
   - VCN → 安全列表 / 网络安全组 → 添加入站规则：
     - `22/TCP` 来源限你自己的 IP（SSH）
     - `80/TCP` 来源 `0.0.0.0/0`
     - `443/TCP` 来源 `0.0.0.0/0`
3. 记下实例的 **Public IP**（OCI 控制台 → 实例详情）。

---

## 4. 第三步：一键部署到 Oracle

在 Oracle VM 上以 `root` 或 `sudo` 执行 **任一** 方式：

```bash
# 方式 A：远程拉取脚本直接跑（需先把第 2 步的文件提交到仓库）
curl -fsSL https://raw.githubusercontent.com/cand79234-source/engilsh-study/main/oracle-bootstrap.sh | bash

# 方式 B：先 clone 再跑
git clone https://github.com/cand79234-source/engilsh-study.git /opt/english-os
cd /opt/english-os && bash oracle-bootstrap.sh
```

脚本会：装 Docker → clone 代码 → 生成 `.env` →（首次会退出让你填 `.env`）→ 你填好后重跑 → 构建并启动容器。

**首次退出后，编辑 `.env` 填写（务必与 Render 同源同值）：**

```bash
nano /opt/english-os/.env
# DATABASE_URL=postgresql://...?sslmode=require   # 复制 Render 上那条 Neon 连接串
# EOS_TOKEN=你现有的口令               # 建议与 Render 的 EOS_TOKEN 一致
# ARK_API_KEY=                         # 可选，同 Render
# DOMAIN=                             # 有域名才填（A 记录指向本机 IP）
```

填好保存后再次执行 `bash /opt/english-os/oracle-bootstrap.sh` 即完成启动。

> 脚本内置安全闸：若 `DATABASE_URL` 不是 `postgresql://` 开头，**拒绝启动**，避免回落到容器内 SQLite 导致数据丢失。

---

## 5. 第四步：验证（在停掉 Render 之前必须全部通过）

逐项核对，**全部通过**才算成功：

| 检查项 | 预期 / 命令 |
|---|---|
| 健康检查 | `curl http://<IP>/api/health` → 返回 `{"ok":true,...}` |
| 网站打开 | 浏览器打开 `http://<IP>`（或 `https://域名`），看到学习页、顶部进度徽章 `阶段0 · W3 · D1` |
| 登录/口令 | 输入 `EOS_TOKEN` 同款口令能进；或你没设口令则直接进 |
| 原有功能 | 学习 / 复习 / 薄弱项 三个入口切换正常；点词有发音、例句、搭配 |
| **数据没有丢失** | 你之前在 Render 上导入的词、进度、错误本在 Oracle 上一样能看到（因为同源 Neon） |
| **新数据能保存** | 在 Oracle 上做一次「记住了 / 造一句 / 改进度」，提交成功 |
| **刷新后仍在** | 刷新页面，刚产生的数据依然存在（写在 Neon，不在内存） |

> 验证时 Render 继续保持运行。由于两者指向同一 Neon，你在 Oracle 上的任何改动也会实时反映在 Render 上——这正是预期的。

---

## 6. 第五步：只有在「第 5 步全部通过」后，才停 Render

1. 确认上面 7 项全绿。
2. OCI 控制台确认站点稳定运行 1~2 天（观察 `/api/health` 与日志 `docker compose logs -f app`）。
3. 再去 Render 控制台把 `english-os` 服务 **Pause / Delete**。
4. 停 Render 后，再次验证 Oracle 站点数据仍在、新数据可保存——确认彻底切干净。

> 原则：**Render 一直保留到 Oracle 验证通过**。本指南不删除 Render、不重置数据。

---

## 7. 环境变量速查

| 变量 | 必填 | 说明 |
|---|---|---|
| `DATABASE_URL` | ✅ | 现有 Neon 连接串（与 Render 完全相同）。决定数据源，绝不新建库 |
| `EOS_TOKEN` | 建议 | 访问口令，建议与 Render 一致 |
| `ARK_API_KEY` | 可选 | 火山方舟 AI 批改；不填回落本地规则 |
| `ARK_MODEL` | 可选 | AI 模型名，可留空 |
| `APP_TZ` | 可选 | 默认 `Asia/Shanghai` |
| `DOMAIN` | 可选 | 填了自动 HTTPS；留空仅 HTTP |
| `RENDER` | ❌ **绝不设置** | 设了会触发 db.py 的 FATAL 校验要求 DATABASE_URL |

---

## 8. 以后的更新流程（GitHub → Oracle 自动部署）

1. 代码推到 `main`。
2. 在仓库 `Settings → Secrets → Actions` 配置：
   - `ORACLE_HOST`（VM 公网 IP/域名）、`ORACLE_USER`（如 `ubuntu`）、`ORACLE_SSH_KEY`（私钥）。
3. 推送后 `.github/workflows/deploy-oracle.yml` 自动 SSH 进 VM 执行
   `git pull && docker compose up -d --build`，无需手动登录。
4. `ORACLE_USER` 建议用你登录 VM 的同一用户（如 `ubuntu`）；`oracle-bootstrap.sh` 已把它
   加入 docker 组（重新登录后生效），因此 CI 里 `docker` 命令免 sudo。
5. 数据仍在 Neon，更新不影响。

---

## 9. 故障排查

| 现象 | 解决 |
|---|---|
| `curl /api/health` 无响应 | OCI 安全列表是否放了 `80`；容器是否起来 `docker compose ps`；日志 `docker compose logs app` |
| 应用启动报 `FATAL: 检测到运行环境为 Render` | 你误设了 `RENDER` 环境变量，删掉它；正常不应设置 |
| 启动后数据像空的 / 刷新丢失 | `DATABASE_URL` 没填或填错 → 应用回落容器内 SQLite；按第 4 步补填 `postgresql://...` 后重建 `docker compose up -d --build` |
| Neon 连接错误 | `DATABASE_URL` 结尾必须有 `?sslmode=require`；Neon 项目需处于活跃（未被暂停） |
| 443 不通 / 无 HTTPS | 没填 `DOMAIN` 或域名 A 记录未指向本机 IP；先确认 DNS 生效再等 Caddy 签发 |
| 内存吃紧（1GB） | 默认 1 个 uvicorn worker，足够；不要加 `--workers` |

---

## 10. 回滚

任何时候出问题：Render 仍保留且数据在 Neon，随时可切回 Render 使用。
Oracle 侧回滚：`docker compose down` 即可停掉，不影响任何数据（数据在 Neon）。
