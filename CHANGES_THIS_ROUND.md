# 交接文档：engilsh-study 本轮全部改动（给其他 AI 看）

> **仓库**：`cand79234-source/engilsh-study`（main 分支）
> **本轮全部提交**：`1a53240` → `97f5590` → `6e08f49`（父提交 `8da9c38`）
> **提交时间**：2026-09-15
> **说明**：这份文档是**给接手的 AI 看的**，所以写得尽量直白、不省略、能落到文件与行为。
> **⚠️ 重要**：用户明确纠正过——「全部」指的是**用户最初提的 5 个问题**（见第 0 节），
> 不是只有后半段那 4 件事。这份文档已按 5 个问题逐条整理。
> **⚠️ 另一件重要的事**：本轮所有代码**从未推送过 GitHub**（沙箱无凭据），
> 详见第 10 节。

---

## 0. 用户最初提的 5 个问题（这才是「全部」）

用户原话（经整理）：

| # | 问题 | 用户原话（节选） |
|---|---|---|
| 1 | **AI 批改刷新后 40→100** | 「学习页造句那块AI批改有的，但是批改后我一刷新页面就变成了本地从40分 变成100分」 |
| 2 | **造句记录只有当天** | 「造句完批改完的记录只有当天留存，我返回前一天任何造句记录都没有」 |
| 3 | **周统计口径不对** | 「我怀疑内部不是一周就是中国时间1-7，是当天加上前七天，所以数据薄弱项和总结那块的数据不对」 |
| 4/5 | **场景难度超纲** | 「学习页面场景这个场景我觉得完全不是我这个水平有的，所以我希望再提示词那边告诉AI阶段0-1给出A2初期的场景，2-3A2后期，4是B1，5B1后期」 |
| — | **补充要求**（后几轮提出） | Mini Scenario 解析、基础句场景彻底去 AI、组合句 5 词配比、组合句加 Phase 规则、AI 扩写接上台面 |

**各问题的最终状态（截至 `6e08f49`）**：

| # | 问题 | 状态 | 修在哪 |
|---|---|---|---|
| 1 | 刷新 40→100 | ✅ **已修** | `main.py` `sentence_check`/`stealth_submit`，`ai_service.correct_sentence` |
| 2 | 造句记录只有当天 | ✅ **已修**（本轮补修） | `ai_service.today_attempts` 去掉当天过滤 |
| 3 | 周统计口径 | ✅ **已修**（本轮补修） | `main.py:errors_trend` 分桶改 Python 侧自然周；`report.py` 本周=自然周 |
| 4/5 | 场景难度超纲 | ✅ **已修** | `scenario.py` `STAGE_CEFR` + `PHASE_RULES` + `build_sys_prompt` |
| — | Mini Scenario / 去AI / 5词配比 / AI扩写 | ✅ 已修 | 见第 3 节 |

---

## 0.1 一句话总览

用户提了 **5 个问题 + 若干补充要求**，我全部改完，从「导入材料解析 → 入库 → 造句计划 →
前端 🔁 换场景 → 批改落库 → 刷新/跨天读取 → 周统计」整条链路打通。
**38 个单元测试全绿**（本轮新增/改动的测试文件），端到端复验通过。

---

## 1. 用户的原话需求（按时间顺序，尽量保留原话）

**(a) 导入解析器要认 Mini Scenario**
> 「入解析器必须新增这两块的识别……以「你这份提示词的格式」为新基准……
> 场景123，英文场景123还是中文场景123随你，因为只是方便系统识别不会出现在台前，
> 只是我点击循环按钮可以改场景
> ③ 只在导入新词时生效 因为目前才使用没几天，不用管老的，但是此后导入的新的
> 不要再用AI了20个单词的造句全部走本地，只有组合用AI」

用户后来贴了**准确的格式**：
```
word /音标/ — 中文
English sentence.
中文翻译。
(×3 组)
固定搭配：「xxxxx」中文；「xxxxx」中文
Mini Scenario 1：
English mini scenario.
Mini Scenario 2：
English mini scenario.
Mini Scenario 3：
English mini scenario.
```
> 「你只改输出格式那边别的提示词不改的」

**(b) 基础句场景去 AI**
> 「不行不退回AI那套，20个单词造句的场景给我彻底删干净AI」
> 「此后导入的新的不要再用AI了20个单词的造句全部走本地，只有组合用AI」
> 「③ 只在导入新词时生效……不用管老的」

**(c) 组合句要改三件事**
> 「不是的是组合里面的场景 【场景难度硬性规则】 Phase 0–1：…… Phase 2–3：……
> Phase 4：…… Phase 5：…… 要加入这个 组合走AI的你忘记了嘛
> 然后其实目前的组合我感觉也不对组合应该每句话是五单词，其中三复习，两当天，
> 然后这样20今天的单词就可以融入，组合走AI的……组合句场景保持中文。
> 组合没有输出结构那里来的输出结构……前端 🔁 已经优先用词自带的场景来切
> （有就用，没有就回退到 AI 那套），不行不退回AI那套」

Phase 规则原文（用户给的，我原样抄进提示词）：
```
【场景难度硬性规则】
Phase 0–1：必须使用 A2 初期水平的英语。句子短、结构简单、词汇以高频日常词为主。
主要使用一般现在时、一般过去时、一般将来时和基础情态动词。避免复杂从句、抽象表达、
低频词汇和高级连接词。场景应当让初级学习者可以直接理解。
Phase 2–3：使用 A2 后期水平。可以出现稍长的句子、更多日常表达、简单原因/结果和时间关系。
允许使用 because, so, when, if 等基础连接词。仍然避免 B1+ 的复杂表达。
Phase 4：使用 B1 水平。可以出现较自然的完整对话、较长句子、简单从句、表达观点、原因、
计划、经历和感受。
Phase 5：使用 B1 后期水平。场景可以更加完整和自然，可以包含连续对话、稍复杂的表达、
观点和解释，但不得进入 B2 难度。
```

**(d) AI 批改 + AI 扩写全保留，并怀疑扩写没接 AI**
> 「对的但是AI批改全部保留的AI批改AI扩写，扩写你看看我怀疑还用的本地没接入AI」

**(e) 授权执行 + 要报告**
> 「我服了我之前就要你改结果完全没改 你改吧，改完和我本报本次全部更改是什么，
> 大白话说，改了什么动了那里，那些经过我的同意那些没有，此次改变会波及什么」

**(f) 追问造句记录**
> 「我说的全部的修改本次全部修改之前造句不保留在页面上修改了嘛」
→ 见第 7 节，这条我**查证后如实回答**：**本轮没改**，相关的删除发生在更早的提交 `2167cab`。

---

## 2. 本轮（3 个提交）真正改了哪些文件

> 累计 diff：`git diff --stat 8da9c38 HEAD`

| 文件 | 改动量 | 属于本轮哪个提交 / 备注 |
|---|---|---|
| `backend/importer.py` | +89 / -4 | `1a53240` Mini Scenario 解析 |
| `backend/scenario.py` | +210 / -37 | `1a53240` 去AI + Phase 规则 |
| `backend/services.py` | +89 / -22 | `1a53240` scenes + 5词配比 |
| `backend/weekimport.py` | +32 / -1 | `1a53240` scenes 入库 |
| `backend/main.py` | +189 / -? | `1a53240` scenes；`6e08f49` 周桶 |
| `backend/ai_service.py` | +339 / -? | `1a53240` AI扩写；`6e08f49` 跨天 |
| `frontend/index.html` | +102 / -9 | `1a53240` 🔁 不回退 AI |
| `backend/db.py` | +89 / -4 | ⚠️ **更早一轮遗留**（时区工具），被一起提交 |
| `backend/report.py` | +56 / -15 | ⚠️ **更早一轮遗留**（自然周口径），被一起提交 |
| `tests/test_import_scenes.py` | +211（新） | `1a53240` |
| `tests/test_sentence_persistence_and_week.py` | +320 | 更早一轮创建；本轮的改断言 + 跨天用例 |
| `tests/test_trend_week_bucket.py` | +95（新） | `6e08f49` |
| `CHANGES_THIS_ROUND.md` | 本文档 | `97f5590` |
| `FIX_REPORT.md` | +120（新） | ⚠️ 更早一轮遗留文档，被一起提交 |


---

## 3. 逐项改动明细（本轮）

### 3.1 导入解析器认 Mini Scenario —— `backend/importer.py`

**改了什么**：
- 新增正则 `_SCENE_LINE`，匹配 `Mini Scenario 1：` / `Scenario 1:` / `场景1：` / `情景 1：` / `Mini Scene 2：` 等。
- 新增函数 `_parse_scene_line(s)` → `(场景序号或"", 同行内容或"")`，不是场景行返回 `None`。
  判定从严：整行长度 ≤ 60，且必须是标签形状，**避免误伤** `In this scenario, you need to...` 这类正常句子。
- `_parse_block()`（块状格式）：
  - 新增状态变量 `expect_scene = None`；
  - 场景分支放在「固定搭配」之后、「英文例句」判断**之前**；
  - 在英文例句分支里，若 `expect_scene is not None`，把这句英文**归到 `scenes`** 而不是 `examples`；
  - 单词头初始化时 `w.setdefault("scenes", [])`。
- `_parse_line()`（逐行旧格式）：**同样加了场景识别**（原来完全不认，这是本轮补的）。
  这是后来修 `test_scene_label_variants` 时才补的，**块状式和逐行式现在都认场景**。

**为什么这么改**：不改的话，`Mini Scenario 1：` 标签行被丢进 `skipped`，它下面的三句英文被当成第 4/5/6 条例句塞进 `examples` —— 例句区凭空多三句没中译的英文，情景也永远进不了库。

---

### 3.2 场景随词入库、一路带到前端 —— `weekimport.py` / `services.py` / `main.py`

- `backend/services.py`：新增 `normalize_scenes(word_obj)` →
  `[{"n": "1", "text": "..."}, ...]`，兼容 list / dict / str，读 `scenes` 或老字段 `scenario`，
  **丢掉空壳**（只有标签没内容的），并在过滤后**按顺序重编号**（显式写了 `n` 就尊重，没写就按过滤后位置补 1/2/3）。
  `_build_basic()` 返回值新增 `"scenes": normalize_scenes(w)`。
- `backend/weekimport.py`：新增 `_norm_scenes()`；在 `import_rich_week` 与 `_build_word_entry`
  两处词表组装里都写入 `scenes`。
- `backend/main.py`：`day_items[key]` 新增 `"scenes": svc.normalize_scenes(w)`。

**结果链路**：`importer 解析 → weekimport 入库 weeks.vocab_json → day_items → /api/today 的 sentence_plan → 前端 tkMeta.scenes`。

---

### 3.3 基础句场景彻底去 AI —— `backend/scenario.py`（核心改动）

**改了什么**：
- 新增模块级常量：
  ```python
  AI_TIERS = ("large",)     # small（基础句）彻底不再调 AI
  ```
- `TIER_TARGET = {"small": 3, "large": 3}` **保留不变**（历史数据判断仍用），
  但所有「补货」逻辑改成**只遍历 `AI_TIERS`**：
  - `refill_if_low()`：`for t in ("small","large")` → `for t in AI_TIERS`
  - `ensure_for_words()`：todo 判断 + ThreadPool 提交，都只管 `AI_TIERS`
  - `ensure_for_word_bg()`：同改
  - `backfill_step()` 内的 `_gaps_of()`：只算 `large`
  - `backfill_status()`：只统计 `large`
- `generate_for_word()` 加**两道硬闸**（防止有人直接点名 small）：
  ```python
  # 闸①：明确要 small → 直接返回 0，一次 AI 都不调
  if nt and nt not in AI_TIERS:
      return 0
  # 闸②：没指定层 → 只生成 AI 层
  if not nt:
      nt = AI_TIERS[0]
  ```
  原来的「不指定层就把 small/large 都生成」分支被删掉。

**效果**：基础句的 `small` 层**永远不会再产生新的 AI 情景**。历史库里已有的 small 不删（用户说"不用管老的"），但也**不会再新增**。

---

### 3.4 组合句每组 5 词（3 复习 + 2 新）—— `backend/services.py`

**改了什么**：`_build_combos()` 签名从 `per=3` 改成 `per=5`，配比逻辑重写：
```python
new_slots = 2 if review_words else per      # 每组留给新词的位数
n_by_new = (len(new_words) + new_slots - 1) // new_slots if new_words else 0
_total_slots = len(new_words) + len(review_words)
n_by_pool = _total_slots // per if per else 0
n_eff = max(1, min(n, n_by_new, n_by_pool)) if new_words else min(n, n_by_pool or n)
n_eff = max(1, n_eff)
_rev_base = (len(review_words) // n_eff) if n_eff else 0
_rev_extra = (len(review_words) % n_eff) if n_eff else 0
# ...
if len(group) < per:
    break
```
配合「复习词均分（base + extra）」让复习词不重复、分布均匀。

组合句提示文本也从「建议 2-3 句」改成「建议 3-5 句，把这 5 个词串成一小段」。

**复验**：复习词 30 时 → 10 组，每组恰好 5 词（3+2），20 个新词全覆盖。
**已知限制（非本轮引入）**：复习词极少（如 0）时，数学上凑不出整组，组数会变少、新词覆盖不全。

---

### 3.5 组合句提示词加 Phase 难度硬性规则 —— `backend/scenario.py`

**改了什么**：
- 新增常量 `PHASE_RULES`，**逐字抄录**用户给的 Phase 0–1 / 2–3 / 4 / 5 规则。
- `build_sys_prompt(stage)` 改为：
  ```python
  return _SYS + stage_difficulty_block(stage) + "\n\n" + PHASE_RULES
  ```
  （`STAGE_CEFR` 早已存在，与新规则一致，无需改动。）

**复验**：Phase 0–1 / 2–3 / 4 / 5 四条都在系统提示末尾。

---

### 3.6 AI 扩写真正上台面 —— `backend/ai_service.py`（修真实 bug）

**发现的 bug**（用户怀疑对了）：AI 返回的扩写（`ai.expand`）**一直被生成、一直被存库**
（存在 `sentences.ai_expand_json`），但前端**只渲染 `optimizations` 里 `kind=='expand'` 的条目**，
而那些条目**全是本地模板** `_expand_samples()` 拼出来的。结果：**AI 的扩写永远看不到，
用户看到的是本地硬贴因果尾巴的句子**，等于白调 AI。

**怎么修的（方案 B：后端合并，前端一行不改）**：
- 新增 `_merge_ai_expand_into_opts(opts, ai_expand, ai_natural=None)`：
  - 保留本地模板的**润色类**建议（`where/suggestion/reason`）；
  - 用 **AI 的扩写替换**本地模板的 expand 条目；
  - AI 没给扩写 → 本地模板的 expand 原样保留（兜底）。
- 在两处调用，保证「实时提交」与「刷新读历史」口径一致：
  - 实时响应合并块 `if insert_ai:` 里：`"optimizations": _merge_ai_expand_into_opts(out.get("optimizations"), ai_expand, ai_natural)`
  - 读历史 `_row_to_attempt()` 的 by_ai 分支：`"optimizations": _merge_ai_expand_into_opts(opts, ai_expand, ai_natural)`

**保留未动**：AI 批改分数、AI 的 `natural`（更地道说法）、`errors`、`summary` —— 全部原样保留。

---

### 3.7 前端 🔁 不再回退 AI —— `frontend/index.html`

**改了什么**：
- 新增 `_scAllowAi(meta)`：只有 `tier==='large'`（组合句）才允许走后端 AI 情景库。
- `fetchScenario()`：有自带场景 → 本地轮换；没有 → 基础句**直接返回**（不回退 AI），组合句才调 `/scenario/next`。
- `nextScenario()`：同上；基础句点 🔁 且没有自带场景时，弹提示
  「这个词没有自带情景，点 🔁 不会用 AI 生成（20 个单词造句已全部走本地）」。
- 基础任务 `tkMeta` 新增 `scenes:(b.scenes||[])`。

---

### 3.8 问题 1：AI 批改刷新后 40→100（已修，属更早一轮）

**根因**：`sentences` 表先 INSERT 本地分（如 100）、再 UPDATE 补 AI 字段。
在 Neon 等读写分离库上，刷新时偶尔读到**还没 UPDATE 的旧行**；快速连点重写同一道题时
还可能 UPDATE 错行。于是"批改时看到 AI 的 40、刷新变回本地 100"。

**修法**：改成**先拿到 AI 结果，再连同本地结果一次性 INSERT**（不再 INSERT-then-UPDATE）。
并在 `correct_sentence` 的返回体里把 AI 的 score/corrected/errors 合并进 `out`，
保证"提交时拿到的"与"刷新后读回的"逐字段一致。

- `main.py` `sentence_check`：先 `_ai.correct()` 拿 AI，再 `correct_sentence(..., ai=...)` 一次写入。
- `main.py` `stealth_submit`：同样顺序。
- `ai_service.correct_sentence`：新增 `ai=None` 参数 + `insert_ai` 合并块。

### 3.9 问题 2：造句记录只有当天（**本轮补修**）

**根因**：前端刷新走 `/api/sentence/attempts`，而后端 `today_attempts()` 的 SQL 是
`WHERE stage=? AND week=? AND day=? AND created_at >= 今天` —— **跨天就查不到**。
（`/api/sentence/history` 虽然支持跨天，但**前端从不调用它**。）

**修法**：`ai_service.today_attempts` **去掉 `created_at>=今天` 过滤**，
改为按 `(stage, week, day, task_key)` 取**全部历史**。同一道题的每一次作答都回填，
与"今天是不是那天"无关；仍按 stage/week/day 限定，避免把别的位置的作答串进来。
前端**不用改**（它本来就走 `attempts`）。

- 新增测试 `test_attempts_survive_next_day`（把 created_at 改成 2020 年，仍能读回）。

### 3.10 问题 3：周统计口径不对（**本轮补修**）

**根因（两处）**：
1. `/api/errors/trend` 的周分桶交给数据库：SQLite 用 `strftime('%Y-W%W')`，
   PG 用 `to_char(...,'IYYY-"W"IW')` —— **两者不是同一套周**；且 SQLite 的 `%W`
   **既不是 ISO 周、也不是中国自然周**。同一份数据在本地(SQLite)与线上(PG)落到不同周桶。
2. 该 SQL 还用了 `created_at::timestamp`（PG 专属语法），在 SQLite 上直接 500。

**修法**：`errors_trend` 的**分桶完全改到 Python 侧**，统一按
**中国自然周（周一 00:00:00 ~ 周日 23:59:59，Asia/Shanghai）**：
```python
monday = d - timedelta(days=d.weekday())     # weekday(): 周一=0
iso_year, iso_week, _ = monday.isocalendar()
label = "%04d-W%02d" % (iso_year, iso_week)
```
两引擎走同一条逻辑，结果必然一致，也和"自然周"口径对齐。参数化比较改用纯字符串
（去掉 `::timestamp`），SQLite 也能跑。

- 新增测试 `tests/test_trend_week_bucket.py`（3 例）：
  周一~周日归同一周、周日不跨进新周、SQLite 不崩、日桶正确。

> `report.py` 里「本周」改成 `china_week_range()` 自然周，属更早一轮已完成。

### 3.11 问题 4/5：场景难度超纲（已修）

**根因**：`scenario.generate_for_word()` 原本**完全没有 stage 参数**，`_SYS` 也不提 CEFR，
AI 自由发挥生成 B1/B2 场景。

**修法**：
- 新增 `STAGE_CEFR`(0/1→A2 early, 2/3→A2 late, 4→B1, 5→B1 late)；
- 新增 `PHASE_RULES`（用户给的 Phase 0–1 / 2–3 / 4 / 5 硬性规则，逐字抄录）；
- `build_sys_prompt(stage)` = `_SYS + stage_difficulty_block(stage) + "\n\n" + PHASE_RULES`；
- 全链路透传 stage；漏传按最保守 A2 处理，**绝不默认 B1**。

### 3.12 测试

- **新增** `tests/test_import_scenes.py`（8 例）：解析、入库、到造句计划、归一化容错、老材料不变。
- **新增** `tests/test_trend_week_bucket.py`（3 例）：自然周分桶。
- **新增** `test_attempts_survive_next_day`：跨天回填。
- **新增** `test_small_tier_never_calls_ai`：基础句层永不调 AI。
- **修改** `test_sentence_persistence_and_week.py` 的 2 个断言（small→large）。


---

## 4. 哪些经过用户同意、哪些没有

| 改动 | 用户是否明确同意 |
|---|---|
| 问题1 刷新 40→100（先 AI 再一次性入库） | ✅ 同意（用户最初就要求修） |
| 问题2 造句记录跨天可见（去掉当天过滤） | ✅ 同意（用户明确要求"返回前一天也要有记录"） |
| 问题3 周统计统一自然周（Python 侧分桶） | ✅ 同意（用户明确质疑"当天+前七天"口径） |
| 问题4/5 场景难度按阶段（STAGE_CEFR + PHASE_RULES） | ✅ 同意 |
| 解析 Mini Scenario（块状 + 逐行） | ✅ 同意 |
| 场景入库并带到前端 | ✅ 同意（🔁 要能切场景的前提） |
| 基础句场景彻底去 AI + `AI_TIERS` | ✅ 同意 |
| 组合句每组 5 词（3 复习 + 2 新） | ✅ 同意 |
| 组合句提示词加 Phase 规则 | ✅ 同意 |
| AI 扩写合并进 optimizations | ✅ 同意（授权"你改吧"） |
| 前端 🔁 基础句不回退 AI | ✅ 同意 |
| **改 2 个老测试的断言 + 新增若干测试** | ❌ **未经单独同意**（是"基础句去 AI"的必然结果，顺手改的） |
| **`normalize_scenes` 加"过滤后按顺序重编号"** | ❌ **未经单独同意**（为让 3 条场景永远是 1/2/3，不出现断号） |
| **`errors_trend` 顺带删掉 PG-only 的 `::timestamp`** | ❌ **未经单独同意**（是修周桶时必须一并处理的 SQLite 兼容问题） |
| **`generate_for_word` 加两道闸（点名 small 直接返回 0）** | ✅ 属于"彻底删干净"的一部分，视为同意 |

---

## 5. 上一轮遗留、被本次提交一起带上的改动（不是本轮我做的）

这两块是**更早一轮**的工作，当时没提交，这次 `git add -A` 一起提交了。**接手时要知道它们也在里面**：

### 5.1 `backend/db.py`（时区统一）
- 默认时区 `Asia/Seoul` → `Asia/Shanghai`（`APP_TZ` 可配，不依赖服务器本地时区）。
- 新增统一日期工具：`get_china_now()` / `get_china_date()` / `china_week_start()` / `china_week_end()` / `china_week_range()`。
- `sentences` 表通过 `_ensure_columns()` 幂等新增 10 个列：
  `ai_score`、`ai_corrected`、`ai_errors_json`、`ai_natural_json`、`ai_expand_json`、
  `ai_verdict`、`ai_summary`、`ai_model`、`ai_at`、`final_source`。

### 5.2 `backend/report.py`（自然周口径）
- 「本周」的造句数/均分、周测、高频错误，从 `_days_ago(7)`（滚动 7×24 小时）改成
  **自然周 `china_week_range()`（周一 00:00:00 ~ 周日 23:59:59，Asia/Shanghai）**。
- 「本周 vs 上周」环比**保留课程周口径**（stage+week）并显式标注。

> 相关背景文档：`FIX_REPORT.md`（上一轮写的，问题 1–5 的修复报告）。

---

## 6. 数据库影响 / 波及范围

**数据库**：
- **纯新增列**（10 个 `ai_*` / `final_source`），走既有 `_ensure_columns()` 自动 `ALTER TABLE ADD COLUMN`，
  幂等，SQLite + Postgres 双兼容，**不需要手写 migration**。
- **不删、不改任何历史行**。老行 `ai_score` 为 `NULL` → 读取时识别为「未 AI 批改」，
  前端如实标「本地估算」，**绝不伪造 100 分**。

**波及**：
1. 只有**以后导入的新词**才有自带场景；老数据不受影响（你说过"不用管老的"）。
2. 基础句不再产生新的 AI 情景；历史 small 情景不删，但不再新增、前端也不再取用。
3. 组合句答案更"重"（每组 5 词写一段）。
4. AI 扩写开始显示（会看到 AI 的更地道说法替换掉本地模板句）。
5. 基础句场景这块**不再烧 AI 额度**。

**未动（守住的红线）**：数据库结构、SRS、学习进度、词汇导入主流程、
现有评分规则、AI key（仍只从环境变量读，不写进代码/前端/日志）。

---

## 7. 关于"造句不保留在页面上"与"跨天看不到记录"

用户问过两句：「之前造句不保留在页面上修改了嘛」+ 「返回前一天任何造句记录都没有」。

**查证结论**：

1. **"造句记录不显示在页面上"** —— 这件事的**删除**发生在**更早的提交 `2167cab`**
   （"薄弱项+总结页改动"），其说明原话：「总结页: 删除本周学习里的造句记录(句子记录保留在学习页)」。
   - **总结页**（`pages.sum`）：删掉了「📝 本周造句记录」整块卡片。
   - **学习页**：**没动** —— 每题的历史、折叠、复制都在。

2. **"返回前一天任何造句记录都没有"** —— 这是**真 bug**，本轮（`6e08f49`）**已修**：
   - 根因：`today_attempts()` 的 SQL 带 `created_at >= 今天`，跨天过滤掉了历史。
   - 修法：去掉该过滤，按 `(stage, week, day, task_key)` 取全部历史。
   - 现在**返回前一天再看，以前的造句记录都在**。

> ⚠️ 接手注意：若用户仍表示"某处看不到历史"，需先确认他指的是
> **学习页**（现已修好）还是**总结页那块被删掉的卡片**（那是另一个决定，
> 是否加回要问用户）。

> ⚠️ 接手时请注意：**这一条用户尚未给出最终答复**，可能需要后续处理。

---

## 8. 验证与提交状态

- **本轮新增/改动的测试文件 38 项全绿**：
  - `test_import_scenes.py`（8）
  - `test_sentence_persistence_and_week.py`（14，含新增的跨天用例）
  - `test_trend_week_bucket.py`（3，本轮新增）
  - 以及既有的场景/持久化相关用例
- **端到端复验全过**：解析 → 入库 → 造句计划 → 基础句去 AI → 组合配比 → Phase 提示词 → AI 扩写 → 跨天读取 → 周分桶。
- 全量测试套件仍有若干失败/collection error，但经 `git stash` 对照确认：
  - 无我改动时：**22 failed / 153 passed**
  - 有我的改动时：**18 failed / 157 passed**
  → 我的改动**净修好 4 个**，**没有新增任何失败**。剩余失败是 playwright 浏览器测试、
    需真实 AI Key 的脚本、以及仓库既有的测试隔离问题（无 conftest 隔离、脚本式测试共用库）。
- **提交**：`1a53240` → `97f5590` → `6e08f49`，三个提交**均仅在本机**。

---

## 9. 给接手 AI 的提醒

1. `tests/` 下有不少**脚本式测试**（直接 `sys.exit`），pytest 收集会报错；跑测试请
   `--ignore` 掉 `test_api_flow.py`、`test_browser_flow.py`、`test_file_upload_import.py`、
   `test_mobile_fe.py`、`test_new_ui.py`、`test_new_ui_v2.py`、`test_scenario_weakness.py`、`test_stealth_ai_flow.py`。
2. 若要让**老数据**也补上自带场景，需要重新解析老材料 —— 这是独立任务，用户尚未要求。
3. `AI_TIERS` 是控制「哪一层走 AI」的总开关，想恢复旧行为就把 `"small"` 加回去并去掉 3.3 的两道闸。
4. 时间口径统一入口在 `db.py`：`get_china_now / get_china_date / china_week_range`。
   **业务层不许再各写一套日期逻辑**；`errors_trend` 的分桶也已改到 Python 侧统一自然周。
5. 用户风格：**要求先分析需求再动手，并且要大白话报告**；对反复提问会不耐烦。

---

## 10. ⚠️ GitHub 推送状态（用户特别在意）

**用户原话**：「你从未推送过GITHUB拿来的一轮一轮，这就是一轮无语」。

**实情**：本轮 3 个提交（`1a53240`、`97f5590`、`6e08f49`）**全部只在本机沙箱**，
**从未推送到 GitHub**。原因：沙箱里没有可用的 GitHub 凭据 ——
- `gh` 未登录（`gh auth status` → not logged into any hosts）；
- 直连 `https://github.com` 报 TLS 握手失败（`gnutls_handshake() failed`）；
- `ghfast.top` 镜像推送时拿不到用户名/token（`could not read Username`）；
- `git-credential-helper` 返回空。

**已为用户准备好两条路**（任一即可）：
1. **补丁包**：`/workspace/待推送补丁.patch`（含 3 个提交，`git am` 即可应用）；
2. **本地直接推**：用户在能联网的机器上 `git push origin main`。

**待推送的 3 个提交**：
```
6e08f49 fix: 造句记录跨天可见 + 错误趋势周桶统一为中国自然周
97f5590 docs: 补本次全部改动的交接文档（给其他 AI 看）
1a53240 feat: Mini Scenario 解析 + 基础句场景去AI + 组合句5词配比/Phase规则 + AI扩写上台面
```

