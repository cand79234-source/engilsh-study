# 📖 部署指南（Neon + Render + UptimeRobot）

> **当前进度**
> - ✅ GitHub：代码已推送完成（https://github.com/cand79234-source/engilsh-study）
> - ⬜ 第1步 Neon：建数据库，拿连接串（3 分钟）
> - ⬜ 第2步 Render：连仓库，上线网站（5-8 分钟）
> - ⬜ 第3步 UptimeRobot：监控 + 保活（2-3 分钟）
>
> 全程手机浏览器即可。**任何一步卡住，截图发我。**

---

## 第 1 步：Neon —— 云端数据库

### 1.1 注册
1. 打开 **https://neon.tech**
2. 点右上角 **Sign Up**
3. 选 **Continue with GitHub** → **Authorize Neon** 授权

### 1.2 创建项目
| 项目 | 怎么填 |
|---|---|
| Project name | `english-os` |
| Postgres version | 默认（不用动） |
| Region | **Singapore (ap-southeast-1)** ← 离国内最近 |

点 **Create project**。

### 1.3 复制连接串（⚠️ 最关键）
项目建好后会直接弹出 **Connection string** 框：

```
postgresql://neondb_owner:xxxx@ep-xxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
```

- 点旁边的 **复制按钮** 📋，立刻存到手机备忘录
- 确认从 `postgresql://` 开头、到 `?sslmode=require` 结尾**一整串完整**

### 1.4 以后想再找连接串
**https://console.neon.tech** → 点项目 → **Connect** 按钮 → 选 **Pooled connection**（带 `-pooler` 的那串）→ 复制

> 💡 Neon 免费额度：0.5GB 存储（这个应用用不到 1%）、闲置自动暂停（访问时毫秒级唤醒，无感知）。

---

## 第 2 步：Render —— 上线正式网站

### 2.1 注册 + 连 GitHub
1. 打开 **https://render.com** → 点 **Get Started for Free**
2. 选 **Sign in with GitHub** → **Authorize Render**
3. 如果提示安装 **Render GitHub App**：
   - 选你的账号 `cand79234-source`
   - 选 **Only select repositories** → 勾选 **engilsh-study**
   - 点 **Install**

### 2.2 创建服务
1. 控制台点 **New +** → **Web Service**
2. 找到 **engilsh-study** → 点 **Connect**
   - 列表里没有？点 **Configure account** 完成上面的 GitHub App 安装 → 回来刷新

### 2.3 填写配置（⚠️ 逐项核对，最容易错的就是这里）

| 配置项 | 填什么 |
|---|---|
| **Name** | `english-os` |
| **Language** | Python 3（会自动识别，没识别就手动选） |
| **Branch** | `main` |
| **Region** | **Singapore** |
| **Root Directory** | **留空，不填** |
| **Build Command** | `pip install -r backend/requirements.txt` |
| **Start Command** | `cd backend && uvicorn main:app --host 0.0.0.0 --port $PORT` |
| **Instance Type** | **Free** |

> 两条命令必须一字不差；Start Command 里的端口是 `$PORT` 不是 8000。

### 2.4 设置数据库连接（必须！）
在创建按钮附近点 **Add Environment Variable**：

| Key | Value |
|---|---|
| `DATABASE_URL` | 粘贴第 1 步的 Neon **整串**连接串 |

### 2.5 部署
1. 点 **Create Web Service**
2. 日志依次出现：
   ```
   Build successful               ← 依赖装好了
   Application startup complete   ← 启动成功 ✅
   ```
3. 状态变 **Live**（绿点）= 上线成功
4. 你的**正式网址**：**https://english-os-xxxx.onrender.com** → 打开能看到学习页 = 部署成功 🎉

### 2.6 以后怎么找网址 / 改配置
- 网址：**https://dashboard.render.com** → 点 `english-os` → 顶部就是
- 改环境变量：服务页 → **Environment** → 编辑 → Save（自动重新部署）
- 看日志排错：服务页 → **Logs** 标签

---

## 第 3 步：UptimeRobot —— 监控 + 保活

**作用**：Render 免费版闲置 15 分钟会休眠（下次打开要等 1 分钟）。UptimeRobot 每 5 分钟 ping 一次 → 网站随时秒开，挂了还会邮件通知你。

### 3.1 注册
1. 打开 **https://uptimerobot.com** → **Register**
2. 填邮箱 + 密码 → 提交
3. 去邮箱点验证链接（垃圾邮件里翻翻）→ 登录

### 3.2 添加监控
点 **+ Add New Monitor**：

| 项目 | 填什么 |
|---|---|
| **Monitor Type** | **HTTP(s)** |
| **Friendly Name** | `English OS` |
| **URL** | `https://你的render网址.onrender.com/api/health` |
| **Interval** | **5 minutes** |

点 **Create Monitor** → 列表里显示 **Up**（绿点）= 生效 ✅

> ⚠️ **URL 一定要用 `/api/health`，不要用 `/api/today`**：UptimeRobot 不能带自定义请求头，
> 一旦你开了访问口令（下一步），`/api/today` 会返回 401，监控会误报"网站挂了"。
> `/api/health` 是专为健康检查/保活准备的，永远不鉴权，也不返回任何学习数据。

---

## 🔒 第 3.5 步（强烈建议）：设置访问口令

**为什么**：Render 是公网 Web Service，谁拿到网址谁就能调用 `/api/progress`、
`/api/words/import`、`/api/word/master` 等接口改你的学习数据。设了口令后，
没有口令的请求一律 401。

### 怎么开
1. Render 控制台 → 你的服务 → **Environment** → **Add Environment Variable**
2. Key = `EOS_TOKEN`，Value = 你自己想一个口令（建议 12 位以上，字母+数字）
3. 点 **Save Changes** → 服务会自动重新部署

### 怎么用
打开你的网站 → 首次访问任意接口都会自动弹一次输入框 → 输入刚设的口令
→ 浏览器会记在 `localStorage` 里（key = `eos_token`），**以后不用再输**。

### 改口令后怎么生效
浏览器记的是旧口令，**新口令输错 1 次后旧口令会被清掉**（前端会清 `localStorage` 再弹新框）。
如果只输错没清缓存就刷不出来了：在浏览器 DevTools → Application → Local Storage
删掉 `eos_token`，刷新页面，会重新弹输入框。

### 其它注意事项
- **UptimeRobot 的监控地址保持 `/api/health`**，它不带口令也能 ping 通
- Render 的 Health Check Path 已设为 `/api/health`（见 `render.yaml`），**不要改回 `/api/today`**
- 想临时关掉口令：把 `EOS_TOKEN` 这个环境变量删掉即可（不设 = 完全开放）
- 口令可以用 `X-Auth-Token: 你的口令` 请求头传，也可以用 `Authorization: Bearer 你的口令`
- 跨域/JSON 预检（OPTIONS）已放行，前端不会被 401 顶回去
- **GitHub PAT 永远不要进前端代码 / 也不要在对话/工单里贴**——它只能放后端环境变量（如果你将来真用 GitHub API 的话）

---

## ✅ 第 4 步：部署后验收

打开 `https://你的网址.onrender.com`，逐项检查：

| 检查项 | 预期 |
|---|---|
| 页面加载 | 顶部有进度徽章（阶段0 · W3 · D1） |
| 本周词汇 | Week3 的 20 个种子词（hobby / relax / enjoy…） |
| 点开一个词 | 有 🔊 发音、例句、固定搭配 |
| 造句区 | ① 基础 / ② 升级 / ③ 组合表达 三段都在 |
| 底部入口 | 学习 / 复习 / 薄弱项 能切换 |

---

## 📦 第 5 步：恢复你的数据（部署完找我）

新站是空数据库，你导入过的词和进度不在旧数据里。**把新站网址发我**，我立刻：
1. 远程验证站点和数据库连通
2. 生成你 **Week2 第 1 组 16 个词**（含例句、固定搭配）的导入文本 → 你贴进新站导入框
3. 进度恢复：点进度徽章 → 阶段0 / 第 2 周 / Day1 → 保存

---

## 🔧 故障排查速查表

| 现象 | 解决 |
|---|---|
| Render 报数据库连接错误 | `DATABASE_URL` 没设或不完整（结尾必须有 `?sslmode=require`） |
| `pip install` 失败 | Build Command 拼写错误，严格照抄 2.3 |
| `Errno 98 address already in use` | Start Command 端口写死了，必须是 `$PORT` |
| 一直 Deploying 卡住 | 等 5-10 分钟；超时点 Manual Deploy → Clear build cache & deploy |
| 打开网站转圈 1 分钟 | 免费实例冷启动，装好 UptimeRobot 后基本消失 |
| 打开报 500 | 服务 Logs 标签截图发我 |
| Neon 找不到连接串 | 项目页 → **Connect** → Pooled connection |
| UptimeRobot 收不到验证邮件 | 翻垃圾邮件；还不行换邮箱 |

---

## 📚 常用网址收藏

| 用途 | 网址 |
|---|---|
| **你的正式网站** | https://english-os-xxxx.onrender.com |
| Render 控制台 | https://dashboard.render.com |
| Neon 控制台 | https://console.neon.tech |
| UptimeRobot | https://uptimerobot.com/dashboard |
| GitHub 仓库 | https://github.com/cand79234-source/engilsh-study |

---

## 🔄 以后怎么更新网站

1. 我在沙箱改好代码 → 推送到 GitHub `main`
2. Render 检测到推送 → **自动重新部署**（2-3 分钟）
3. 你刷新网址就是新版，数据不丢（都在 Neon 云端）
