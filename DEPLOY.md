# 部署手册：GitHub + Render + Neon + UptimeRobot

本仓库已改造为「本地 SQLite 兜底 / 设了 `DATABASE_URL` 就自动用 Postgres」双模式。
所有 SQL 统一用 `?` 占位符，Postgres 模式下由 `backend/db.py` 的适配层自动转成 `%s`，
并把 `INSERT OR IGNORE` 转成 `ON CONFLICT ... DO NOTHING`，业务代码无需改动。

> ⚠️ 沙箱环境无法直连外网，所以下面的 `git push`、Neon / Render / UptimeRobot 创建步骤
> **需要在你自己的电脑（或任意能联网的终端）上执行**。代码和配置都已就绪，照抄即可。

---

## 0. 前置：把代码推到 GitHub

```bash
cd english-os
git init -q
git add -A
git commit -q -m "English OS: production-ready (Render + Neon)"
git remote add origin https://github.com/cand79234-source/engilsh-study.git
git branch -M main
git push -u origin main
```

（仓库名按你给的 `engilsh-study`；如果 GitHub 上还没建仓库，先在网页上新建一个空的。）

---

## 1. Neon（Postgres 数据库）

1. 打开 https://neon.tech ，用 GitHub 登录，新建一个 Project（选 Postgres 16，区域就近）。
2. 在 **Dashboard → Connection Details** 复制 **Connection string**，形如：
   ```
   postgresql://user:password@ep-xxx-pooler.region.aws.neon.tech/neondb?sslmode=require
   ```
3. 把整串复制好，下一步填进 Render。**表结构会在应用首次启动时由 `init_db()` 自动建好**，不用手动跑 SQL。

---

## 2. Render（Web 服务，正式网站）

1. 打开 https://render.com ，用 GitHub 登录，点 **New → Web Service**，选中本仓库 `engilsh-study`。
2. 配置：
   - **Runtime**: Python 3.11（仓库已带 `runtime.txt`，会自动识别）
   - **Build Command**: `pip install -r backend/requirements.txt`
   - **Start Command**: `cd backend && uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Plan**: Free（或选 Starter 避免休眠）
3. 在 **Environment → Add Environment Variable** 里加：
   - `DATABASE_URL` = 第 1 步复制的 Neon 连接串（**整串粘贴**，包含 `?sslmode=require`）
4. 点 **Create Web Service**。首次启动会连接 Neon 并自动建表、导入内置词库（约 10–30 秒）。
5. 部署完成后，Render 给你的域名形如 `https://english-os-xxxx.onrender.com` —— 这就是**正式网站地址**。
   - 想要自定义域名：Render 控制台 **Settings → Custom Domains** 里绑定即可。

> 健康检查路径已设为 `/api/today`，Render 会用它判断实例是否存活。

---

## 3. UptimeRobot（宕机监控）

1. 打开 https://uptimerobot.com ，注册/登录。
2. **Add New Monitor**：
   - **Monitor Type**: HTTP(s)
   - **Friendly Name**: `English OS`
   - **URL**: 你的 Render 地址 + `/api/today`（例如 `https://english-os-xxxx.onrender.com/api/today`）
   - **Interval**: 5 分钟（免费额度）
3. 保存。之后网站挂了会自动邮件/短信告警。

---

## 4. 本地开发 / 回退到 SQLite

不设 `DATABASE_URL` 时，应用自动用本地 `data/english_os.db`（当前你线上的数据就在那里）。

```bash
cd backend
pip install -r requirements.txt
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## 5. 数据迁移说明（从本地 SQLite → Neon）

当前你本地 `data/english_os.db` 里已有进度/词/造句。要搬到 Neon：
- 小数据量：直接在网页端重新导入词、进度会自动从 0 开始（最简单，推荐）。
- 想完整搬：用 `pgloader` 或写个小脚本把 SQLite 表导出成 SQL 再灌进 Neon。
  由于两个库 schema 已对齐（列名一致），迁移很直接。需要我可以单独给你迁移脚本。
