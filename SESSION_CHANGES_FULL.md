# 本次会话全部改动汇总（从头到尾 · 完整版）

> **用途**：给其他 AI 看的**完整交接文档**。涵盖**本次聊天从第一条到最后一条的全部内容**，
> 不是几个问题、不是几件事，而是**本次会话里发生过的每一处改动与每一个决定**。
> **仓库**：`cand79234-source/engilsh-study`（main 分支）
> **会话起点提交**：`8da9c38`（会话开始时 HEAD 就在这）
> **会话产生的全部提交**：`1a53240` → `97f5590` → `6e08f49` → `933b2ba`（4 个）
> **改动总量**：14 个文件，+2281 / -201 行
> **⚠️ 推送状态**：**4 个提交全部只在本机，从未推送到 GitHub**（沙箱无凭据，见第 12 节）

---

## 目录

- 第 1 节：本次会话的完整时间线（你每一条要求 + 我的每一步动作）
- 第 2 节：本次会话全部改动总览（按文件）
- 第 3 节：改动明细 A —— 用户最初提的 5 个问题
- 第 4 节：改动明细 B —— 用户后续补充的要求
- 第 5 节：改动明细 C —— 上一轮遗留、本次被一起提交的
- 第 6 节：哪些经过用户同意、哪些没有（全表）
- 第 7 节：数据库影响
- 第 8 节：会波及什么
- 第 9 节：测试与验证
- 第 10 节：做过但**回退/未采用**的方案（诚实记录）
- 第 11 节：已知问题与限制
- 第 12 节：GitHub 推送状态与操作方法
- 第 13 节：给接手 AI 的提醒

---

## 1. 本次会话的完整时间线

按用户发言顺序，逐条记录「用户说了什么 → 我做了什么」。

| # | 用户要求（原话要点） | 我的动作 |
|---|---|---|
| 1 | 「入解析器必须新增这两块的识别……以「你这份提示词的格式」为新基准……场景123……③ 只在导入新词时生效……此后导入的新的不要再用AI了20个单词的造句全部走本地，只有组合用AI」 | 先误读为"两个块"，读代码后**发现并修复了"AI 40→刷新变100"的持久化 bug**（3 个根因：INSERT-then-UPDATE 双写、响应体带本地分而 source=ai、stealth 从不落库）。改 `ai_service.py` / `main.py` / `index.html` |
| 2 | 「怎么修改的大白话给我解释下」 | 用大白话解释了那三个"洞"，并反问"两块"是什么意思 |
| 3 | 「新增这两块的识别？我没说啊。就是下面加一个场景啊场景123」 | 纠正理解：只加**一个场景**。重新测试解析器，发现 `Day 1｜...` 整行进 name、`场景1｜...` 被丢 skipped |
| 4 | 贴出**准确格式**（word/音标/—/中文/例句×3/固定搭配/Mini Scenario 1-3）「你只改输出格式那边别的提示词不改的」 | 发现三个英文 Mini Scenario 被当例句 #4/5/6。改 `importer.py`（新增 `_SCENE_LINE`+`_parse_scene_line`）、`services.py`（`normalize_scenes`）、`weekimport.py`、`main.py`、`index.html`；新建 `tests/test_import_scenes.py` |
| 5 | 「场景造句的提示词目前后端啥样的，也要改一改按照这份改场景你懂吗……你先分析需求在做我感觉你没理解」 | 先分析需求：读 `scenario.py` 的 `_SYS`、组合的 scene 字段，发现组合场景是代码构造的、large 层是 AI 生成的。提 3 个澄清问题 |
| 6 | 给出**组合句三大要求**：加 Phase 0–1/2–3/4/5 难度硬性规则；每组 5 词（3 复习+2 新）；场景保持中文；🔁 有自带就切、**不行不退回AI那套**；「20个单词造句的场景给我彻底删干净AI」 | 确认四项。追问"复习词不够时怎么办" |
| 7 | 「对的但是AI批改全部保留的AI批改AI扩写，扩写你看看我怀疑还用的本地没接入AI」 | 用 `/tmp/t4.py` **证实了用户的怀疑**：AI 扩写生成并入库了，但前端只渲染本地模板。提方案 A（只改前端）/ 方案 B（后端合并） |
| 8 | 「我服了我之前就要你改结果完全没改 你改吧，改完和我本报本次全部更改是什么，大白话说……」 | 授权执行。完成任务 #7-#12：组合 5 词配比、Phase 规则、基础句去 AI、前端 🔁 去回退、AI 扩写上台面、修 `_parse_line` 场景识别。提交 `1a53240` |
| 9 | 「我说的全部的修改本次全部修改之前造句不保留在页面上修改了嘛」 | 查证：本轮没动，"删除"发生在更早的 `2167cab`（总结页删造句记录、学习页保留）。如实回报并请其确认 |
| 10 | 「唉我是要你告诉我本次所有的全部的全部的全部的所有的修改……我要给别的AI看看」 | 写第一版交接文档 `CHANGES_THIS_ROUND.md`（提交 `97f5590`） |
| 11 | 「全部的意思是包含这个库你看下有以下问题：1 刷新40→100、2 造句记录只有当天、3 周统计不对（当天+前七天）、5 场景难度超纲……这才是全部，你从未推送过GITHUB」 | **范围修正**：原来"全部"= 最初 5 个问题。逐条核实代码，**发现并补修了问题 2、3**（之前其实没修好）。提交 `6e08f49`；重写文档 `933b2ba` |
| 12 | 「妈呀大傻春……本次聊天框的全部！！！本次会话的全部！！！从上到下从头到尾的全部！！！」 | **本文档**：把本次会话从头到尾的全部改动、全部决定、全部文件、全部提交，一次性汇总 |

---

## 2. 本次会话全部改动总览（按文件）

累计 diff = `git diff --stat 8da9c38 HEAD`：

| 文件 | 增/删 | 改动性质 |
|---|---|---|
| `backend/ai_service.py` | +339 / -43 | 批改持久化、AI 扩写合并、跨天回填、`_row_to_attempt`、`save_ai_result` |
| `backend/main.py` | +189 / -? | 批改顺序、`sentence_history`、scenes 到 day_items、`errors_trend` 周桶 |
| `backend/scenario.py` | +247 / -? | 去 AI（`AI_TIERS`）、`STAGE_CEFR`+`PHASE_RULES`、两道闸 |
| `backend/services.py` | +111 / -? | `normalize_scenes`、`_build_basic` 带 scenes、组合 5 词配比 |
| `backend/importer.py` | +93 / -4 | `_SCENE_LINE`、`_parse_scene_line`、`_parse_block`/`_parse_line` 认场景 |
| `backend/db.py` | +89 / -4 | 时区统一 Shanghai、自然周工具、`ai_*` 新增列 |
| `backend/report.py` | +56 / -15 | 「本周」改自然周、`period` 元数据 |
| `backend/weekimport.py` | +33 / -1 | `_norm_scenes`、scenes 入库、只补 large |
| `frontend/index.html` | +102 / -9 | 🔁 不回退 AI、scenes 入 tkMeta、提交后刷新、`_sumPeriod` |
| `tests/test_import_scenes.py` | +211（新） | Mini Scenario 解析/入库 8 例 |
| `tests/test_sentence_persistence_and_week.py` | +320 | 持久化/跨天/周口径 14 例 |
| `tests/test_trend_week_bucket.py` | +95（新） | 周桶口径 3 例 |
| `CHANGES_THIS_ROUND.md` | +477 | 交接文档 |
| `FIX_REPORT.md` | +120 | 更早一轮的修复报告 |

---

## 3. 改动明细 A —— 用户最初提的 5 个问题

### 问题 1：AI 批改刷新后 40 → 100

**现象**：批改时显示 AI 的 40 分，一刷新变成本地规则的 100 分。

**根因**（3 个，一起修）：
1. `sentence_check` 先 INSERT 本地分行、再 UPDATE 补 AI 字段 → 读写分离库（Neon）上刷新偶尔读到旧行。
2. `correct_sentence` 的返回体里 `source='ai'` 但 score 是本地分 → 前端只能自己覆盖，出现不一致。
3. `stealth_submit` 的 AI 结果**从来没有落库**。

**修法**：
- `ai_service.py`：
  - `correct_sentence(sentence, ..., ai=None)` 新增 `ai` 参数；
  - 新增 `_norm_insert_ai(ai)`（归一 AI 字段）、`_AI_INSERT_COLS`；
  - 重写 `_insert_sentence_with_attempt(..., ai=)`，带 3 层降级（丢 ai 列 → 丢 category → 裸插入）；
  - `return out` 前新增 `if insert_ai:` 合并块，把 AI 的 score/corrected/errors/natural/expand 并进响应体。
- `main.py`：
  - `sentence_check`：**先** `_ai.correct()` 拿 AI，**再** `correct_sentence(..., ai=...)` 一次性写入；删掉旧的写后 UPDATE；
  - `stealth_submit`：同样顺序，响应带 `ai_saved` / `sentence_id`。
- `frontend/index.html`：提交成功后若拿到 `sentence_id`，重新拉 `/sentence/attempts` 覆盖内存，保证"批改看到的 = 刷新后看到的"。

**验证**：`/tmp/t2.py` → 提交 40 / 刷新 40 / 连点 3 次得 [40,40,40] / AI 不可用则 `source=rule, ai_pending=True`。

---

### 问题 2：造句记录只有当天，返回前一天就没有

**现象**：造句批改记录只留当天，回看前一天任何记录都没有。

**根因**：前端刷新走 `/api/sentence/attempts`，后端 `today_attempts()` 的 SQL 带
`WHERE stage=? AND week=? AND day=? AND created_at >= 今天` —— **跨天被过滤掉**。
（`/api/sentence/history` 支持跨天，但**前端从不调用**。）

**修法**（提交 `6e08f49`）：`ai_service.today_attempts()` **去掉 `created_at>=今天` 过滤**，
改为按 `(stage, week, day, task_key)` 取**全部历史**。同一道题的每次作答都回填，与"今天是不是那天"无关；
仍按 stage/week/day 限定，避免把别的位置的作答串进来。**前端不用改**。

**验证**：新增 `test_attempts_survive_next_day`（把 created_at 改成 2020 年，仍能读回）。

---

### 问题 3：周统计口径不对（当天 + 前七天）

**现象**：怀疑内部"一周"其实是"当天 + 前 7 天"，导致薄弱项 / 总结数据不对。

**根因**（两处）：
1. `/api/errors/trend` 周分桶交给数据库：SQLite 用 `strftime('%Y-W%W')`，
   PG 用 `to_char(...,'IYYY-"W"IW')` —— **两者不是同一套周**；SQLite 的 `%W`
   **既不是 ISO 周、也不是中国自然周** → 本地与线上结果不一致。
2. 该 SQL 用了 `created_at::timestamp`（**PG 专属语法**），SQLite 上直接 `unrecognized token` → 500。

**修法**（提交 `6e08f49`）：
- `errors_trend` 的**分桶完全搬到 Python 侧**，统一按**中国自然周（周一 00:00:00 ~ 周日 23:59:59，Asia/Shanghai）**：
  ```python
  monday = d - timedelta(days=d.weekday())   # 周一=0
  iso_year, iso_week, _ = monday.isocalendar()
  label = "%04d-W%02d" % (iso_year, iso_week)
  ```
- 参数化比较改纯字符串，去掉 `::timestamp`。
- `report.py` 的「本周」指标改用 `china_week_range()`（属更早一轮，随本次提交一起入库）。

**验证**：新增 `tests/test_trend_week_bucket.py`（3 例）：周一~周日同周、周日不跨周、SQLite 不崩、日桶正确。

---

### 问题 4/5：场景难度超纲

**现象**：学习页场景明显超出用户水平。

**根因**：`scenario.generate_for_word()` **完全没有 stage 参数**，`_SYS` 也不提 CEFR，AI 自由发挥。

**修法**：
- `scenario.py` 新增 `STAGE_CEFR = {0/1: A2 early, 2/3: A2 late, 4: B1, 5: B1 late}`；
- 新增 `cefr_for_stage(stage)` / `stage_difficulty_block(stage)` / `build_sys_prompt(stage)`；
- 新增 `PHASE_RULES`（用户给的 Phase 0–1 / 2–3 / 4 / 5 硬性规则，**逐字抄录**）；
- `build_sys_prompt(stage) = _SYS + stage_difficulty_block(stage) + "\n\n" + PHASE_RULES`；
- `generate_for_word(...)` 及所有补货函数透传 stage；漏传按最保守 A2 处理，**绝不默认 B1**。

---

## 4. 改动明细 B —— 用户后续补充的要求

### B1. 导入解析器认 Mini Scenario

**格式**（用户给定）：
```
word /音标/ — 中文
English sentence.
中文翻译。
(×3 组)
固定搭配：「xxxxx」中文；「xxxxx」中文
Mini Scenario 1：
English mini scenario.
Mini Scenario 2：
...
Mini Scenario 3：
...
```

**改动**（`importer.py`）：
- 新增 `_SCENE_LINE` 正则，匹配 `Mini Scenario 1：` / `Scenario 1:` / `场景1：` / `情景 1：` / `Mini Scene 2：`；
- 新增 `_parse_scene_line(s)` → `(序号或"", 同行内容或"")`，长度 ≤60、判定从严（不误伤 `In this scenario, ...`）；
- `_parse_block()`：新增 `expect_scene` 状态；场景分支放在"固定搭配"之后、"英文例句"之前；
  英文分支里若 `expect_scene` 有值 → 归 `scenes` 而不是 `examples`；
- `_parse_line()`（逐行旧格式）：**同样加了场景识别**（原来完全不认）。

**修的问题**：不改的话 `Mini Scenario 1：` 被丢 skipped、下面三句英文被当第 4/5/6 条例句。

### B2. 场景随词入库、一路带到前端

- `services.py` 新增 `normalize_scenes(word_obj)` → `[{"n","text"}]`，容错、丢空壳、**过滤后按顺序重编号**；`_build_basic()` 返回值加 `"scenes"`。
- `weekimport.py` 新增 `_norm_scenes()`；`import_rich_week` / `_build_word_entry` 两处写 `scenes`。
- `main.py` 的 `day_items[key]` 加 `"scenes": svc.normalize_scenes(w)`。
- 链路：`importer → weeks.vocab_json → day_items → /api/today → 前端 tkMeta.scenes`。

### B3. 基础句场景彻底去 AI

- `scenario.py` 新增 `AI_TIERS = ("large",)`；
- 所有补货逻辑（`refill_if_low` / `ensure_for_words` / `ensure_for_word_bg` / `backfill_step._gaps_of` / `backfill_status`）**只遍历 `AI_TIERS`**；
- `generate_for_word` 加两道闸：点名 `small` → 直接返回 0；不传层 → 只生成 `large`。
- 效果：基础句 `small` 层**永不再产生新 AI 情景**；历史 small 不删。

### B4. 组合句每组 5 词（3 复习 + 2 新）

- `services.py` `_build_combos(..., per=3)` → `per=5`，配比逻辑重写（`new_slots=2`、复习词均分 base+extra、`if len(group)<per: break`）；
- 提示文本「建议 2-3 句」→「建议 3-5 句，把这 5 个词串成一小段」。

### B5. 组合句提示词加 Phase 难度硬性规则

- 同"问题 4/5"，`PHASE_RULES` 注入 `build_sys_prompt`。

### B6. AI 扩写真正上台面

**发现的 bug**：AI 扩写一直生成并存入 `sentences.ai_expand_json`，但前端**只渲染 `optimizations` 里 `kind=='expand'`** 的条目，而那些条目全是**本地模板** `_expand_samples()` 做的 → AI 扩写永远看不到。

**修法**（方案 B，前端一行不改）：
- `ai_service.py` 新增 `_merge_ai_expand_into_opts(opts, ai_expand, ai_natural)`：保留本地润色类建议，用 AI 扩写**替换**本地 expand 条目，AI 没给则本地兜底；
- 两处调用保证口径一致：实时响应 `if insert_ai:` 块 + `_row_to_attempt()` 的 by_ai 分支。

### B7. 前端 🔁 不再回退 AI

- `index.html` 新增 `_scAllowAi(meta)`：只有 `tier==='large'` 才允许走后端 AI 情景库；
- `fetchScenario()` / `nextScenario()`：有自带场景 → 本地轮换；基础句没有 → **不回退 AI**（点 🔁 弹提示）；组合句才调 `/scenario/next`。

---

## 5. 改动明细 C —— 更早一轮遗留、本次被一起提交的

**不是本次会话我做的**，但在这 4 个提交里，接手时必须知道：

### C1. `backend/db.py`（时区统一）
- 默认时区 `Asia/Seoul` → `Asia/Shanghai`（`APP_TZ` 可配，不依赖服务器本地时区）。
- 新增统一日期工具：`get_china_now()` / `get_china_date()` / `china_week_start()` / `china_week_end()` / `china_week_range()`。
- `sentences` 表经 `_ensure_columns()` 幂等新增 10 列：`ai_score`、`ai_corrected`、`ai_errors_json`、`ai_natural_json`、`ai_expand_json`、`ai_verdict`、`ai_summary`、`ai_model`、`ai_at`、`final_source`。

### C2. `backend/report.py`（自然周口径）
- 「本周」的造句数/均分、周测、高频错误，从 `_days_ago(7)`（滚动 7×24 小时）改为**自然周 `china_week_range()`**。
- 「本周 vs 上周」环比**保留课程周口径**（stage+week）并标注。
- 返回体新增 `period` 元数据（时区 / 周起止 / 口径规则）。

### C3. `FIX_REPORT.md`（更早一轮的修复报告文档）

---

## 6. 哪些经过用户同意、哪些没有（全表）

| 改动 | 用户是否明确同意 |
|---|---|
| 问题1 刷新 40→100 | ✅ 同意（用户最初就要求修） |
| 问题2 造句记录跨天可见 | ✅ 同意（用户明确要求"返回前一天也要有记录"） |
| 问题3 周统计统一自然周 | ✅ 同意（用户明确质疑"当天+前七天"口径） |
| 问题4/5 场景难度按阶段 | ✅ 同意 |
| 解析 Mini Scenario（块状+逐行） | ✅ 同意 |
| 场景入库并带到前端 | ✅ 同意（🔁 要能切场景的前提） |
| 基础句场景彻底去 AI + `AI_TIERS` | ✅ 同意 |
| 组合句每组 5 词 | ✅ 同意 |
| 组合句加 Phase 规则 | ✅ 同意 |
| AI 扩写合并进 optimizations | ✅ 同意（授权"你改吧"） |
| 前端 🔁 基础句不回退 AI | ✅ 同意 |
| 改 2 个老测试断言 + 新增若干测试 | ❌ **未经单独同意**（"基础句去 AI"的必然结果） |
| `normalize_scenes` 加"过滤后重编号" | ❌ **未经单独同意**（避免场景断号 1/3） |
| `errors_trend` 删掉 PG-only 的 `::timestamp` | ❌ **未经单独同意**（修周桶时必须一并处理的 SQLite 兼容） |
| `generate_for_word` 加两道闸 | ✅ 属"彻底删干净"的一部分，视为同意 |
| **更早一轮的 `db.py` / `report.py` 改动** | ❓ **不是本会话发生的**，无法判断当时是否单独同意 |

---

## 7. 数据库影响

- **纯新增列**（10 个 `ai_*` / `final_source`），走既有 `_ensure_columns()` 自动 `ALTER TABLE ADD COLUMN`，幂等，SQLite + Postgres 双兼容，**不需手写 migration**。
- **不删、不改任何历史行**。老行 `ai_score` 为 NULL → 识别为「未 AI 批改」，前端标「本地估算」，**绝不伪造 100 分**。
- **不改表结构以外的任何东西**：不加表、不删列、不改唯一索引。

---

## 8. 会波及什么

**会变**：
1. 只有**以后导入的新词**才有自带场景；老数据不受影响。
2. 基础句不再产生新 AI 情景；历史 small 不删、但不再新增、前端不再取用。
3. 组合句答案更"重"（每组 5 词写一段）。
4. AI 扩写开始显示（AI 的更地道说法替换本地模板句）。
5. 基础句场景**不再烧 AI 额度**。
6. **造句记录跨天可见**（回看前一天能看到历史）。
7. 周统计（近四周趋势）统一自然周，SQLite/PG 结果一致。

**不会变（红线）**：数据库结构（仅加列）、SRS、学习进度、词汇导入主流程、
现有评分规则、AI key（仍只从环境变量读，不写进代码/前端/日志）。

---

## 9. 测试与验证

**本次会话新增/改动的测试文件，共 38 项全绿**：
- `tests/test_import_scenes.py`（8）
- `tests/test_sentence_persistence_and_week.py`（14）
- `tests/test_trend_week_bucket.py`（3）
- 及既有场景/持久化相关用例

**端到端复验全过**：解析 → 入库 → 造句计划 → 基础句去 AI → 组合配比 → Phase 提示词 → AI 扩写 → 跨天读取 → 周分桶。

**全量套件对比**（`git stash` 对照）：
- 无本次改动：**22 failed / 153 passed**
- 有本次改动：**18 failed / 157 passed**
- → **净修好 4 个，未引入任何新失败**。剩余失败为 playwright 浏览器测试、需真实 AI Key 的脚本、仓库既有测试隔离问题。

---

## 10. 做过但回退/未采用的方案（诚实记录）

为让接手 AI 知道"哪些路走过、为什么没走"：

1. **组合句 5 词配比**：尝试过 4 版才定稿——
   - 版1 `min(3, max(1, per-1))` → 只出 6 组、有 `[2,5]` 大小组；
   - 版2 "退回池子再继续" → 更糟（8 组、有 2 词小组）；
   - 版3 `if len(group)<per: break` → 组都是 5 词但新词覆盖下降；
   - **定稿**：`n_eff = max(1, min(n, n_by_new, n_by_pool))` + 复习词均分 + `break`。
2. **两个"死层"分支代码**：`scenario.generate_for_word` 里 `nt=="small"` 与"不指定层"的分支在去 AI 后**已走不到**，但**保留**了结构（加注释说明），以便将来恢复 small。
3. **`today_attempts` 的 `since` 参数**：保留签名但不再用于过滤（兼容老调用），未删除。

---

## 11. 已知问题与限制

1. **组合句新词覆盖的数学上限**：复习词极少（如 0）时，凑不出"3 复习+2 新"，组数变少、20 个新词无法全覆盖。**这是改动前就存在的约束**，非本次引入。
2. **老数据无自带场景**：已导入的老词不会补场景；要补需重新解析老材料（独立任务，用户未要求）。
3. **历史 AI 批改结果无法恢复**：旧代码从不落库，那些 AI 分数/纠错**从未被保存过**，库里只剩当时的本地规则结果。修复后**新的** AI 批改会永久保存。
4. **总结页的"本周造句记录"卡片**：早在 `2167cab` 就被删除（学习页保留）。**本次会话未恢复**；若用户要，是独立决定。

---

## 12. GitHub 推送状态与操作方法

**⚠️ 本次会话 4 个提交全部只在本机沙箱，从未推送到 GitHub。**

原因（已逐一尝试）：
- `gh` 未登录（`gh auth status` → not logged into any hosts）；
- 直连 `https://github.com` → TLS 握手失败（`gnutls_handshake() failed`）；
- `ghfast.top` 镜像推送 → 拿不到用户名/token（`could not read Username`）；
- `git-credential-helper` 返回空。

**已备好两条路（任一即可）**：
1. **补丁包**：`/workspace/待推送补丁.patch`（含 4 个提交）→ 本地 `git am /path/待推送补丁.patch && git push origin main`；
2. **本地直接推**：在能联网的机器 `git push origin main`。

**待推送的 4 个提交**：
```
933b2ba docs: 交接文档按用户5个问题重写（范围修正 + 问题2/3 + 推送状态）
6e08f49 fix: 造句记录跨天可见 + 错误趋势周桶统一为中国自然周
97f5590 docs: 补本次全部改动的交接文档（给其他 AI 看）
1a53240 feat: Mini Scenario 解析 + 基础句场景去AI + 组合句5词配比/Phase规则 + AI扩写上台面
```

**提交基线**：会话起点 = `8da9c38`（会话开始时 HEAD 就在这里）。

---

## 13. 给接手 AI 的提醒

1. `tests/` 下有不少**脚本式测试**（直接 `sys.exit`），pytest 收集会报错；跑测试请 `--ignore` 掉：
   `test_api_flow.py`、`test_browser_flow.py`、`test_file_upload_import.py`、`test_mobile_fe.py`、
   `test_new_ui.py`、`test_new_ui_v2.py`、`test_scenario_weakness.py`、`test_stealth_ai_flow.py`。
2. **时间口径统一入口在 `db.py`**：`get_china_now / get_china_date / china_week_range`。业务层不许再各写一套日期逻辑。
3. **`AI_TIERS = ("large",)`** 是控制"哪一层走 AI"的总开关。恢复旧行为 = 把 `"small"` 加回去 + 去掉 `generate_for_word` 的两道闸。
4. **`normalize_scenes` 会重编号**（过滤空壳后按 1/2/3）。
5. **用户风格**：要求先分析需求再动手；要**大白话报告**；对反复提问 / 挤牙膏非常不耐烦；
   **极其在意代码是否真的推送到了 GitHub**。下一次回复前先想清楚"全部"的范围。
