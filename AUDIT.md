# English OS — 代码审计报告

> 审计时间：2026-09-04 · 仓库：`/workspace/engilsh-study`（本地无 `.git`，GitHub 有 `cand79234-source/engilsh-study`）
> 方法：逐文件通读后端全部模块 + 前端主页面 `index.html`，并实际运行了 feature 1–5 的既有测试与针对性现场脚本。
> 结论速览：**①—⑤ 均已实现且端到端可用，⑤ 确已提交并接入 UI（非"做一半未提交"）。PWA 为"可安装、无离线缓存"。** 但发现若干中/高风险项，最严重的是：**公网设 `EOS_TOKEN` 后前端从不携带口令 → 全站 401 无法使用**；以及 **listening 第三维只有结构占位、无任何功能喂数据**。

---

## 1. 结构概览

### 1.1 目录 / 文件用途

| 文件 | 用途 | 备注 |
|---|---|---|
| `backend/main.py` | FastAPI 入口：路由表、EOS_TOKEN 鉴权中间件、静态前端挂载 | |
| `backend/db.py` | SQLite↔Postgres(Neon) 双引擎适配层；全部建表/迁移/索引/词库种子触发；30s busy-timeout | |
| `backend/srs.py` | SM-2 + 三态隔离查询层：`submit_review`、`due_reviews(kind=…)`、`word_output` 五星读写、`weak_output_words`、`flashcard_items` | |
| `backend/services.py` | 进度/周内容/三段式造句计划/错误聚合/周测/词库查询 | |
| `backend/ai_service.py` | **本地规则批改引擎**（无 AI）：`analyze` / `correct_sentence`，多规则判错 + 三态判定 + 落库 | |
| `backend/importer.py` | 富文本解析器（块状/逐行、周/组/阶段识别、例句/搭配配对） | |
| `backend/weekimport.py` | 导入落库：词库回填 + 写入目标周 + 进度跳转（含 merge 版） | |
| `backend/fileimport.py` | 文件→文本提取器（docx/pdf/xlsx/html/rtf/json…） | |
| `backend/seed_builtin.py` | 内置基础词库（146 词/例句/搭配），幂等灌入 | |
| `backend/seed_ecdict.py` | ECDICT 全量词典合并 + 音标/词性回填（后台线程） | |
| `frontend/index.html` | 唯一被挂载的移动端单页前端（学习/复习/薄弱项 3 入口） | 见 1.3 |
| `frontend/manifest.webmanifest` / `icon-192.png` / `icon-512.png` | PWA 安装清单 + 图标 | |
| `frontend/index_desktop.html`、`index_old_3nav…`、`index_old_6nav.html` | 桌面版 / 旧版前端（**未被主流程引用**，疑似残留） | |
| `tests/*` | 各 feature 单测 + Playwright 端到端 | |
| `研究与设计报告.md` | 设计文档 | |

### 1.2 API 路由表（全部定义于 `backend/main.py`）

| 方法 | 路径 | 作用 | 鉴权豁免 |
|---|---|---|---|
| GET | `/api/health` | 健康检查（render/uptime） | ✅ 永远开放 |
| GET | `/api/home` | 主页聚合（进度/今日完成/复习/薄弱项） | |
| GET | `/api/stages` | 阶段定义 | |
| GET/POST | `/api/progress` | 读/写当前进度 | |
| GET | `/api/week/{stage}/{week}` | 读某周内容 | |
| POST | `/api/week` | 编辑某周内容 | |
| GET | `/api/today` | 今日主线：当日词汇+三段造句计划+复习池 | |
| POST | `/api/word/master` | 标记词已掌握并排 vocab SRS 卡 | |
| POST | `/api/sentence/check` | 本地批改并落库+错题本+五星更新 | |
| GET | `/api/sentence/attempts` | 当天按题分组的作答历史 | |
| GET | `/api/sentence/attempts/{task_key}` | 单题全部作答历史 | |
| GET | `/api/sentence/history` | 跨天造句历史 | |
| POST | `/api/output/stars` | 批量读五星熟练度（只含已记录词） | |
| POST | `/api/sentence/preview` | 仅批改不入库 | |
| GET | `/api/error-bank` | 错题本 | |
| GET | `/api/review/due` | **全部 kind** 到期复习（含 error/listening） | |
| POST | `/api/review/submit` | 提交复习（3 档 quality：0/3/5） | |
| GET | `/api/review/flashcards` | 仅 vocab 到期闪卡（含词典释义） | |
| GET | `/api/memory/state/{word}` | ① 三状态隔离读取（srs/output/listening） | |
| GET | `/api/errors` | 错误类型聚合（近30天/累计/级别/规律/建议） | |
| GET | `/api/errors/{error_type}` | 单类型错误详情 | |
| GET | `/api/weakness` | ④ 薄弱项聚合（错误类型+低星词+规则建议） | |
| GET | `/api/error-types` | 错误类型常量 | |
| GET/POST | `/api/quiz/{stage}/{week}` / `/api/quiz/grade` | 周测生成 / 批改 | |
| GET/POST | `/api/dict/count` / `/api/dict/lookup` | 本地词典（ECDICT）计数 / 欧陆式点词查义 | |
| GET | `/api/vocab/all` | ⑤ 全部词汇浏览（只读） | |
| GET | `/api/dbinfo` | 诊断当前连 SQLite 还是 PG | |
| GET | `/api/lookup/{word}` | 查词全量信息 | |
| GET | `/api/week/{stage}/{week}/ensure` | 确保某周有内容并返回 | |
| POST | `/api/words/import` | 富文本整周导入 | |
| POST | `/api/file/extract` | 上传文件→纯文本（预览） | |
| POST | `/api/words/import/file` | 上传文件直接导入 | |
| GET | `/api/file/formats` | 支持的文件格式 | |
| POST | `/api/words/set` | 用户贴一批词设为当前周 | |
| GET | `/api/history` | 学习历史 | |
| GET `/` / 静态 | — | 前端 | ✅ 静态豁免 |

### 1.3 前端入口与导航
- 底部导航仅 **3 项**：`learn`/`review`/`errors`（`index.html:144-148`），对应"📖学习 / 🔁复习 / 🎯薄弱项"。
- "全部词汇浏览"不是第 4 个底部 tab，而是藏在**复习页**内一张卡片（`index.html:1017-1021` → `revAll` → `renderAllWords`）。`main.py` 注释里写着"全部词汇入口将在词库浏览阶段加入"暗示原设计是把浏览放到复习页内部，是刻意的，不是漏接。**结论：⑤ 的 UI 入口真实存在且可用。**

---

## 2. 阶段核验表（1–5 + PWA）

> ✅ = 已实现且跑通 · ⚠️ = 已实现但有限制/隐患 · ❌ = 缺失/失效

| 编号 | 特性 | 判定 | 一句话证据 |
|---|---|---|---|
| ① | 记忆数据结构 + 三状态隔离 (`word_output` 表、`due_reviews(kind)`、srs/output/listening 独立) | ⚠️ | 数据层**真实且隔离正确**：`word_output` 在 `db.py:413-424` 建表且带 `UNIQUE(word)`；`due_reviews(kind=…)` 在 `srs.py:117-145` 做 kind 闸门；`srs.update_output_star` 只写 `word_output`、**从不碰 reviews**（`srs.py:156-194`），现场测试 `test_memory_state_isolation` 通过（五星/听力卡均不污染 vocab SRS）。**但"listening"维度全项目无任何功能喂数据**——`kind='listening'` 只在隔离 API/测试里出现，无 route/无 UI 创建过听力卡。`/api/memory/state/{word}` 也是后端-only、前端 0 引用（见隐患 #6） |
| ② | 词汇 SRS + 闪卡 + 3 档评分 | ✅ | 3 档逻辑与 commit 完全一致：`srs.py:68-98` 0=interval 重排 1/reps 清零、3=折半、5=1→3→7→×ease。前端确实用 **3 档**（按钮 0/3/5，`index.html:1107-1109` → `gradeFlash` 传 `quality:q`，`index.html:1119-1134`），不是旧 2 档。`/api/review/flashcards` 严格 `kind='vocab'`（`srs.py:250`）。`test_srs_flashcard` 32 例通过 |
| ③ | 造句五星 | ✅ | `update_output_star` 星级 0–5 钳制（`srs.py:179,187`）；`/api/output/stars` 未记录词不返回（只含已记录词，`srs.py:207-222`）；5 星词被移出 `build_sentence_plan` 的 today_new+due_vocab（`services.py:849-857`）。`test_sentence_fivestar` 通过 |
| ④ | 薄弱项聚合 | ✅ | `weak_output_words(threshold=3)` 真查 `word_output`（`srs.py:225-239`）；`/api/weakness` 聚合 error_types+low_star_words+recommendations 三段（`main.py:438-462`）；建议全部来自 `REMEDY_BY_TYPE` 规则表（`services.py:159-171`），**无任何 AI**。`test_weakness` 通过 |
| ⑤ | 全部词汇浏览 | ✅ | `/api/vocab/all` 真实存在（`main.py:506-536`）；现场脚本验证 **day_items∪reviews∪sentences 去重正确**（apple 出现 1 次）、搜索大小写不敏感子串、**只读**（不产生 reviews / word_output）。前端浏览视图真只读：仅 `🔊`/返回按钮（`index.html:1032-1060`），无 SRS/五星触发。**已被 commit fedad39 提交并接入 UI**——用户在笔记里的"做了一半未提交"判断**已过时**。搜索是 Python 内存子串，**非 SQL LIKE**（见隐患 #4） |
| PWA | 安装为 App | ⚠️ | `manifest.webmanifest` 合法（`start_url:"/"` 对得上真实根路由 `GET /`）；图标 192/512 都是有效 PNG（`file` 校验通过）；已在 `index.html:8` 链接。**但没有 service worker**（全仓 grep 0 命中）→ **仅可安装、无离线缓存、无 `beforeinstallprompt` 自定义**。安装后断网打不开，属"可接受但需知晓" |

---

## 3. 隐患清单

按严重度排序。`severity 高/中/低`。

### 高

**H1 — 前端从不携带 EOS_TOKEN：公网设口令后全站 401、App 直接不可用**
- 位置：`frontend/index.html:154`（`const api=(p,opt)=>fetch('/api'+p,opt)...`）— 唯一 fetch 封装，**不加任何 `X-Auth-Token`/`Authorization` 头**；全文件 0 处 localStorage/口令提示/401 处理逻辑。
- 对照：后端 `main.py:33-55` 一旦 `EOS_TOKEN` 被设置，**所有 `/api/*`（除 `/api/health`）一律要求口令**；`DEPLOY.md:127-147` 声称"前端会自动弹一次输入框、记住在 localStorage"——**该文案与代码完全不符**。
- 现场验证：设 `EOS_TOKEN` 后 `GET /api/today` 返回 401。
- 影响：按 `DEPLOY.md` 第 3.5 步照做（设口令）→ 你的正式网站**整站点任何数据都 401**，白屏/空数据。
- 建议：二选一——(a) 前端 `api()` 统一从 `localStorage.getItem('eos_token')` 附加头，并在 401 时弹出输入框后重试；(b) 若不打算让前端带口令，则后端改为"写操作才鉴权/或改为 Cookie-session"，并删除 DEPLOY 里误导性的第 3.5 步。**上线前必修。**

**H2 — 鉴权在 CORS 外层：设口令时跨域 OPTIONS 预检 401（无 ACAO 头）**
- 位置：`main.py:21-55`（CORS 先 add、TokenAuth 后 add；Starlette 后 add 的排在最外，TokenAuth 先执行）。
- 现场验证：设 token 后，带 `Origin` + `OPTIONS` 预检 `/api/sentence/check` → **401 且无 `access-control-allow-origin`**。
- 缓解：当前 FE 与后端**同源**（都由 FastAPI 静态挂载），正常使用不发预检，故本地/当前部署不炸。但 `main.py:54` 注释"注意顺序：CORS 先注册（外层）…鉴权后注册（内层）"与实际相反，是错误注释；一旦将来把 FE 单独托管到其它源（或允许第三方集成），跨域调用将全军覆没。
- 建议：鉴权中间件对 `OPTIONS` 请求直接放行（`if request.method=="OPTIONS": return await call_next(...)`），或把 CORS 移到最后注册。

### 中

**M1 — 第③维"listening"只有结构占位，无任何数据来源**
- 位置：`db.py:391`（reviews.kind 注释含 `'listening'`）、`srs.py:117-154`（kind 过滤已就绪）、`main.py:400-424`（隔离读取）。全仓 grep：**没有任何 route / service / UI 创建 `kind='listening'` 的卡**；隔离单测是手动 INSERT 造的假数据。
- 影响：三态隔离的"框架"是对的，但实际只用了 2 维（vocab SRS + 五星 output）。"记忆/听力"从产品层面仍是**空维**，不是 bug 但极易让用户误以为听力功能存在。
- 建议：明确列为"未实现"，或补一个听力维度生产者后再宣传"三状态"。

**M2 — 五星到达 5 后是"终态"：永久移出计划池，无法回落/复练**
- 位置：`services.py:847-857`（≥5 星词从 today_new & due_vocab 一并剔除）。
- 影响：一旦某词到 5★，它再也不会出现在基础/升级/组合里 → 无法因"忘了写错"掉回 4★ → 主动输出维度永不失活。词义 SRS 仍会复习它，两个维度脱钩。
- 建议：5★ 词可保留在较低频率的"周期复练"池，或当 SRS 出现错误时允许其回流计划。

**M3 — 组合题五星按整行 `word` 名单广播 ±1，与实际用到与否脱钩**
- 位置：`main.py:261-264`（对 `word.split()` 的每个词都 `update_output_star`）。
- 影响：combo 题 `word` = 2–3 个建议词；用户句子只用了其中 1 个词，另 2 个也会跟着 +1/-1 → 星级失真（词被"白加星"或"无辜扣星"）。
- 建议：仅在 `analyze`/句子文本里检测到实际出现该词时才更新其星级（基本句本来就单同词；组合句需按词在 `original` 中的命中过滤）。

**M4 — `/api/vocab/all` 返回上限硬编码 500，超出部分不可浏览/搜索**
- 位置：`main.py:519-535`（fetch 全部去重词 → Python 排序 → `words[:limit]` 截 500；`q` 过滤也先于截断）。FE 又在客户端对 `state.allWords` 二次过滤（`index.html:1029-1048`）——FE 拿到的本来就是后端截断后的 ≤500。
- 影响：词量 >500 后，排序靠后 / 未进前 500 的词永久无法检索。个人学习到数百词即可能踩线。
- 建议：后端把搜索/分页下推到 SQL（`WHERE word LIKE ?` + `LIMIT/OFFSET`），前端做增量加载或传 `q` 给后端。

**M5 — `master()` 传给 SRS 卡的搭配恒为空（字段名不匹配）**
- 位置：`index.html:461` 发送 `collocation:w.collocation`；但 `/api/today` 词对象只提供 **`collocations`（数组）**、不含 `collocation` 单字段（`main.py:154-163`）。故 `w.collocation` 为 `undefined` → JSON 丢弃 → `main.py:228` `collocation_text(body)` 拿到空 → 该词 vocab 复习卡的 `answer` 只含词、不含搭配。
- 影响：闪卡背面（靠 answer/词典）暂无实际搭配提示，弱化复习质量；是字段名契约不一致导致的静默失败。
- 建议：`master()` 传 `collocations`（数组）或后端 `main.py:227` 统一走 `normalize_collocations(body)` 兼容数组。

**M6 — 记忆三状态读取 `/api/memory/state/{word}` 前端 0 引用**
- 位置：后端 `main.py:400-424`（已实现+有单测），前端 `grep -c "memory/state" index.html` = **0**。
- 影响：三态隔离的"读取视图"无处可看；用户界面无法展示"同一词 SRS 稳定 + 输出弱 + 听力无"这类诊断，① 的产品价值打了折扣。
- 建议：在薄弱项/复习页为词条加一个"三态"小面板调该接口；否则该 API 属未接入的死代码。

**M7 — 错误复习卡（kind='error'）混入"复习到期"聚合，污染非闪卡入口**
- 位置：`ai_service.py:1205-1211`（每次硬错误建 error 卡）；`main.py:366-368` `/api/review/due` 与 `services.py:312-322` `home_overview` 都用**无 kind 过滤**的 due（返回 error/listening 混排）。闪卡已隔离（无碍），但复习页"今日复习"计数/主页 due 会把错误卡算进去，用户看到"10 张到期"实际含错误卡。
- 影响：轻度困惑；闪卡会话里不会真的出现错误卡，但入口计数虚高。
- 建议：`due_summary`/`home_overview` 统计与展示时排除 `error`（错误走薄弱项），只统计 `vocab`。

### 低

**L1 — Postgres 下 `schedule_review` 返回的 lastrowid 恒为 0**
- 位置：`srs.py:31-33`（`cur.lastrowid`），而 `db.py:278-290` `insert_get_id` 注释明确点出 psycopg2 `lastrowid` 恒 0、须走 `RETURNING id`；`schedule_review` 没用 `insert_get_id`。
- 影响：现网所有调用方都不吃这个返回 id（word_master / correct_sentence 都忽略返回值），故**当前无害**，是潜伏缺陷。Neon(PG) 下若要取新卡 id 会拿 0。
- 建议：把 `schedule_review` 的 INSERT 改用 `insert_get_id`。

**L2 — 后端双提交缺幂等防护，快速双击可致星级/间隔翻倍**
- 位置：`srs.py:36-114` `submit_review`、`srs.py:156-194` `update_output_star` 均无幂等键/乐观锁；仅前端 `gradeFlash` 有 `state.revBusy`（`index.html:1121`）。
- 影响：弱网/双击（或多端同时）会重复扣分/重复递增间隔。单用户场景概率低。
- 建议：submit_review 加同 id 同秒提交去重，或用 `last_reviewed` 时间窗判断重复。

**L3 — 部分错误返回 shape 不一致（缺 `ok` 字段）**
- 位置：`main.py:249-250` 返回 `{"error": "句子不能为空"}`；对比多数接口返回 `{"ok":False,"error":...}`。`review_submit` 对不存在 id 直接返回 `null`。
- 影响：前端依赖 `if(!r||r.error)` 尚能兜住，但对 API 消费者不够规整。
- 建议：统一错误返回 `{"ok":False,"error":...}`。

**L4 — `main.py:54` 关于中间件顺序的注释与 Starlette 实际行为相反**（见 H2），易误导后续改动。

**L5 — 测试混用两种风格：含模块级 `sys.exit` 的脚本式测试令 `pytest` 收集直接 INTERNALERROR**
- 位置：`tests/test_api_flow.py:109`（`sys.exit(...)` 模块顶层）；同类：`test_file_upload_import.py`、`test_browser_flow.py`、`test_sentence_*.py`、`test_new_ui*.py`、`test_wordlookup_e2e.py`、`test_memory_state_isolation.py` 等一批文件顶层跑副作用逻辑。
- 现场验证：`pytest tests/test_api_flow.py` → `INTERNALERROR … SystemExit`。
- 影响：想用 pytest 跑全量回归会被脚本式文件打断；只能逐个 `python tests/xxx.py` 手动跑。
- 建议：统一为 pytest 函数或加 `if __name__=="__main__"` 包裹脚本段。

**L6 — 两个已声明过时的 UI 测试仍未移除/未标 skip**
- 位置：`tests/test_new_ui.py:3-10` 与 `tests/test_mobile_fe.py:3-11` 的 docstring 自述"已过时/针对两代前 UI/当前不可用"，但既无 `skip` 也无 pytest 收集隔离，运行会引用已删除的 `#imp_words`、`#we_vocab` 等选择器而失败。等价覆盖在 `test_new_ui_v2.py`。
- 建议：标注 `pytestmark = pytest.mark.skip` 或删除。

**L7 — PWA 无 service worker / 无离线缓存 / 无 install 提示**
- 位置：全仓 0 个 `serviceWorker` 引用；`index.html` 仅 `<link rel="manifest">`。
- 影响：可作为 standalone App 安装，但断网即不可用；也无缓存版本号，改版后无自动更新路径。
- 建议：后续加一个简单 SW（precache index.html/css），并确认 `start_url:/` 在 Render 子路径无影响。

**L8 — 无障碍/移动体验小项**
- `index.html:5` `user-scalable=no` 禁用缩放（可访问性违例，WCAG 1.4.4）。
- `index.html:86 .spk` 与 `.colloc`（`index.html:68`）字号 13–16px 触达热区 <44px（iOS 点击偏小）。
- `body{min-height:100vh}`（`index.html:22`）在 iOS 视口高度问题上有轻微影响，但因内容可滚动、顶栏固定，影响有限。
- 非阻塞；若追求移动质量可择机处理。

---

## 4. 未实现清单（确认仍缺失）

| 项 | 现状 | 证据 |
|---|---|---|
| **Listening / 听力** | 仍缺失——`reviews.kind='listening'` 是**结构占位**，无任何创建听力卡的功能/页面/路由 | grep 仅命中隔离读取/注释；无 producer |
| **Analytics / 统计分析（学习数据看板）** | 仍缺失——无独立 analytics 路由/页；仅 `home_overview`（`services.py:297`）与 `error_breakdown` 提供基础计数 | 路由表无分析类接口 |
| **外部语料/词库"联调"（Tatoeba 等）** | 仍缺失——`README.md:78-81` 把 Tatoeba 列为"后续接入"；目前只内置 ECDICT 合并（`seed_ecdict.py`） | README / 无相关模块 |
| **五星"手写入口 / 词级记忆三态 UI"** | `/api/memory/state` 后端有、前端无接线 | 见隐患 M6 |

---

## 5. 建议下一改的 3–5 个（按 影响/成本 排序）

1. **（高，先修）前端携带 EOS_TOKEN + 401 弹窗**（H1）
   - 改 `index.html:154` 的 `api()`：从 `localStorage` 读 token 附加请求头；收到 401 弹输入框 → 存 localStorage → 重试。成本极低，直接让 `DEPLOY.md` 第 3.5 步从"整站不可用"变成"真的能保护"。同批把 `OPTIONS` 在鉴权中间件放行（H2）。

2. **补上"listening 数据生产者"或明确降级为 2 态（M1 + M6 一起）**
   - 若暂不做听力，把三态文案收敛为"词义 SRS + 输出五星 双维"，避免承诺落空；同时决定 `/api/memory/state` 去留（接 UI 或删）。

3. **修五星终态 + 组合广播失真（M2/M3）**
   - 5★ 词回池做低频复练；组合句仅对"句子中确实出现的词"更新星级。成本可控、直接提升 ③ 的语义准确性。

4. **`/api/vocab/all` 支持分页/后端搜索（M4）**
   - 加 `LIMIT/OFFSET` 与 SQL `LIKE`；前端浏览视图加"加载更多"。适合词量增长后不崩。

5. **清理测试基建（L5/L6）**
   - 把脚本式测试包进 `__main__` / 统一 pytest；给两个过时 UI 测试标 skip。让 `python -m pytest` 成为可信回归入口。

> 附带（可选）：修 L1 PG lastrowid、L2 幂等、M7 计数口径——三者皆几行改动，可在任一功能迭代时顺手带上。
