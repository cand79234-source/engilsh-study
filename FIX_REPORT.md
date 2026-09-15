# engilsh-study 修复报告（问题 1–5）

> 仓库：`cand79234-source/engilsh-study`（main 分支）
> 排查方式：完整追踪 `前端提交造句 → AI 批改/API → 后端保存 → 数据库 → 页面刷新 → 历史记录读取 → 薄弱项统计 → 周总结` 每一环，再做代码级修复。
> 约束遵守：未用 localStorage/sessionStorage 掩盖数据库问题；未删除任何历史数据；未改页面结构；未改学习进度 / SRS / 词库导入 / 现有评分规则 / 现有 API。

---

## 1. 根本原因

| # | 现象 | 根本原因 | 位置 |
|---|---|---|---|
| 1 | AI 批改分数刷新后消失/变高 | **AI 批改结果从不落库**。`sentence_check` 只把「本地规则」分数写进 `sentences.score`；AI 的 `score/errors/corrected` 仅经响应体回前端、存在内存 `state.hist`。刷新时 `loadAttempts()` 重新拉 `/api/sentence/attempts`，读回的是 DB 里的本地规则分（规则漏判的句子会给出 90+，用户即看成「变 100」）。 | `backend/main.py:483`、`backend/ai_service.py:2002`、`frontend/index.html:1160` |
| 2 | 造句记录只有当天 | `sentences` 表其实完整保存了每次作答，`/api/sentence/history` 也已存在；但**前端没有任何入口调用它**。学习页刷新走的是 `/api/sentence/attempts`，该接口按 `stage/week/day + 当天日期` 过滤，跨天自然查不到。**数据没丢，是查询口径 + 缺读入口。** | `backend/main.py:567`、`backend/ai_service.py:2030` |
| 3/4 | 「本周」= 近 7 天 | `report.py` 里标「本周」的造句 / 周测 / 高频错误统计用了 `_days_ago(7)`（滚动 7×24 小时），而其它指标用课程周；两套口径混用。**没有任何地方按「中国时区自然周（周一~周日）」统计。** | `backend/report.py:190,201,213,226` |
| 5/6 | 场景难度超纲 | `scenario.generate_for_word()` **完全没有 stage 参数**，`_SYS` prompt **只字未提 CEFR/阶段/难度**，AI 自由发挥 → 生成 B1/B2 场景。 | `backend/scenario.py:57,248`，调用点 `main.py / weekimport.py / scenario.py` |

---

## 2. 修改了哪些文件 & 每个文件改了什么

### `backend/db.py`
- 默认时区 `Asia/Seoul` → **`Asia/Shanghai`**（不再依赖服务器本地时区，Render 为 UTC 也没问题）。
- 新增全项目统一日期工具：`get_china_now()`、`get_china_date()`、`china_week_start()`、`china_week_end()`、`china_week_range()`。`app_today()` 改为委托 `get_china_date()`。
- `sentences` 表通过既有 `_ensure_columns` 幂等迁移**新增列**：`ai_score`(可空)、`ai_corrected`、`ai_errors_json`、`ai_natural_json`、`ai_expand_json`、`ai_verdict`、`ai_summary`、`ai_model`、`ai_at`、`final_source`。

### `backend/ai_service.py`
- 新增 `save_ai_result(sentence_id, ai)`：把 AI 结果按 id 精确写回刚才那条 `sentences` 行（含 `final_source='ai'`）。
- 新增 `_row_to_attempt(row)`：读取时**AI 结果优先**——有 AI 结果就用 AI 的分数/错误/正确写法；否则回落本地规则并标 `ai_pending=True`。`attempts_of` / `today_attempts` 改用该序列化器。

### `backend/main.py`
- `sentence_check`：AI 批改成功后调用 `_ai_svc.save_ai_result()` 落库（失败不影响本次显示）。
- `/api/sentence/history`：支持 `?date_=YYYY-MM-DD`（中国时区自然日）与 `?days=N`，默认返回全部；返回体带 `total`。
- 情景调用点带上真实 `stage`（`next_word` / `scenario_next` / `word_master`）。

### `backend/report.py`
- 「本周」的造句数/均分、周测、高频错误、造句列表，从 `_days_ago(7)` 改为 **自然周 `china_week_range()`（周一 00:00:00~周日 23:59:59, Asia/Shanghai）**。
- 返回体新增 `period` 口径元数据（时区 / 周起止 / 口径规则）。
- 「本周 vs 上周」环比**保留课程周口径**（stage+week），并显式标注区分。

### `backend/scenario.py`
- 新增 **Stage→CEFR 映射** `STAGE_CEFR` 与 `stage_difficulty_block(stage)` / `build_sys_prompt(stage)` / `cefr_for_stage(stage)`。
- `_SYSTEM` 追加「本阶段英语难度」块：明写 `Learner stage: Stage N` + `Target CEFR: ...` + 该等级允许/禁止项 + 「场景复杂度 ≠ 英语语言难度」原则 + Stage 0 正/反例。
- `generate_for_word(..., stage=None)` 新增 stage；user prompt 也明写 `Learner stage/Target CEFR`；`ensure_for_words/ensure_for_word_bg/refill_if_low/backfill_step` 全链路透传 stage。**漏传时按最保守的 A2 初期处理，绝不默认成 B1。**
- `weekimport.py`：导入触发情景时透传 stage。

### `frontend/index.html`
- 造句批改提交后，内存记录带上 `source / ai_pending`，与刷新后后端返回的字段一致。
- `attemptCard`：对「尚未 AI 批改」的记录如实标注「本地估算」（不伪装满分）——不新增区块、不改结构。
- `_sumPeriod` 优先采用后端 `/api/report` 的 `period`（自然周），使周期标签与数据同一口径。

---

## 3. 数据库有没有变更？是否需要 migration？是否影响已有数据？

- **有变更，但是纯新增列**：`sentences` 增加 10 个 `ai_*` / `final_source` 列。
- **不需要手写 migration**：走项目既有的 `_ensure_columns()`，启动时 `init_db()` 自动 `ALTER TABLE ADD COLUMN`（幂等，列存在则跳过）。SQLite 与 Postgres 双兼容。
- **不影响已有数据**：
  - 旧行 `ai_score` 为 `NULL`、`final_source` 为空 → 读取时识别为「未 AI 批改」，前端标「本地估算」，**绝不伪造 100 分**，也**不覆盖**原有 `score/errors_json` 等本地规则字段。
  - 没有删除、没有改写任何历史行；唯一索引 `ux_sentences_attempt` 等保持不变。

---

## 4. 测试了哪些场景（新增 `tests/test_sentence_persistence_and_week.py`，12 项全过）

| 用例 | 覆盖 | 结果 |
|---|---|---|
| Case 1 | 提交「I go work every day.」本地规则给 70、模拟 AI 给 **40** → 落库 → 重新读取仍为 **40**（含 corrected / errors / natural 一致） | ✅ |
| Case 1b | AI 未跑通时刷新读本地规则结果并标 `ai_pending` | ✅ |
| Case 1c | 历史缺失（`ai_score=NULL, score=0`）**不被伪造成 100** | ✅ |
| Case 2 | 9/14 建的记录，按 9/14 查询仍可读；9/15 查询为空 | ✅ |
| Case 3 | 今天=2026-09-15(周二) → 本周 = **2026-09-14 00:00:00 ~ 2026-09-20 23:59:59**（不是 09-08） | ✅ |
| Case 4 | 跨周：9/13(周日)=09-07~09-13；9/14(周一)=09-14~09-20；9/13 数据不计入新周 | ✅ |
| Case 4b | 报告「本周」只统计窗口内数据（9/15 计入，9/8 与更早不计） | ✅ |
| Case 4c | 报告返回 `period` 元数据（Asia/Shanghai + natural_week_mon_sun + course_week 环比） | ✅ |
| Case 5 | Stage 0/1→A2 early，2/3→A2 late，4→B1，5→B1 late | ✅ |
| Case 5b | 最终发给 AI 的 prompt 明写 `Learner stage`+`Target CEFR`+难度要求+正反例 | ✅ |
| Case 5c | `generate_for_word` 真把 stage 传进 sys prompt | ✅ |
| Case 5d | 漏传 stage 默认 A2（不默认 B1） | ✅ |

另外跑了既有回归：`test_scenario_prompt_rules`(27)、`test_scenario_tier`、`test_frontend_syntax`(2)、`test_sentence_two_stage`(18/0)、`test_sentence_v2`、`test_sentence_fivestar`、`test_sentence_false_positive`、`test_sentence_star_none`、`test_sentence_rules_v3`、`test_bugfixes`、`test_stealth_ai_flow`、`test_sentence_angle_rotation`、`test_weak_snapshots`、`test_m2_m3_fix`、`test_srs_flashcard`、`test_weakness_expression` —— **全部通过**。

> 说明：`test_scenario_weakness.py`（3 项）与「混跑时 `test_weakness.py`」的失败，经 `git stash` 对照验证为**修改前就存在**的测试隔离问题（仓库无 conftest 隔离、脚本式测试共用 `data/english_os.db`，即 AUDIT.md 记录的 L5 问题），**非本次改动引入**。

真实 HTTP 链路（FastAPI TestClient）也验证过：提交返回本地 70 + AI 40（`ai_saved:true`）→ 刷新读回 **40**；`/api/sentence/history?date_=2026-09-14` 命中、`2026-09-15` 为空；`/api/report` 周窗口 = `2026-09-14 ~ 2026-09-20`。

---

## 5. 最终的日期统计规则

- **时区统一**：全项目一律 `Asia/Shanghai`（`APP_TZ` 可配，默认已改为 Shanghai），**不依赖服务器本地时区**。
- **自然周定义**：**周一 00:00:00 → 周日 23:59:59**。所有「本周」指标（造句数/均分、周测、高频错误、造句列表）都用它。
- **例外与区分**（明确保留，不强行改成一周）：
  - 「本周 vs 上周」环比 = **课程周**（`progress.stage + progress.week`），返回体标 `week_compare_rule: course_week(stage+week)`。
  - 「本月」= 自然月。
  - 薄弱项里的「近 30 天」「近 7 天」「累计」= **滚动/历史**口径，保持原意并在文案上标注，不混入「本周」。
- 提供统一工具：`get_china_now / get_china_date / china_week_start / china_week_end / china_week_range`，业务层不再各写一套。

---

## 6. 最终的 Stage → CEFR 对照

| Stage | Target CEFR | 说明 |
|---|---|---|
| 0 | **A2 early（A2 初期）** | 短句、高频词、以一般现在时为主，禁复杂从句/高级词，常见生活场景 |
| 1 | **A2 early（A2 初期）** | 同上 |
| 2 | **A2 late（A2 后期）** | 可用 because/but/so/when/if、简单过去时/将来表达，句子稍长 |
| 3 | **A2 late（A2 后期）** | 同上，仍禁明显 B1/B2 表达 |
| 4 | **B1** | 观点/原因/经历/比较/计划，because/although/however，句子可明显更长 |
| 5 | **B1 late（B1 后期）** | 更复杂日常交流；仍不生成明显 B2/C1 学术英语 |

Prompt 里明写 `Learner stage: Stage N` / `Target CEFR: ...`，并强制「场景复杂度 ≠ 英语语言难度」——场景可以真实，语言必须匹配阶段。

---

## 7. 关于历史数据（诚实说明）

- **没有任何数据丢失**。造句记录一直存在 `sentences` 表；问题 2 是**读取口径/入口**问题，不是数据被删。
- 无法「恢复」的只有一项：**历史上那些 AI 批改结果**——因为旧代码从不落库，那些 AI 分数/AI 纠错**从未被保存过**，库里只剩当时的本地规则结果。修复后**新的** AI 批改会永久保存；旧记录会如实显示为「本地估算」，我们**没有伪造**成 AI 结果或 100 分。
- 未对任何历史行做删除或数值改写。
