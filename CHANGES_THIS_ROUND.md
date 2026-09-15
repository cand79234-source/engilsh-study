# 交接文档：engilsh-study 本次全部改动（给其他 AI 看）

> **仓库**：`cand79234-source/engilsh-study`（main 分支）
> **本次提交**：`1a53240`（父提交 `8da9c38`）
> **提交时间**：2026-09-15 14:02 +0800
> **说明**：这份文档是**给接手的 AI 看的**，所以写得尽量直白、不省略、能落到文件与行为。
> **重要前提**：这次提交里**混进了上一轮未提交的改动**（时区/自然周相关，见第 5 节），
> 下面会明确区分「本轮我按用户要求改的」和「上一轮遗留、被这次提交一起带上的」。

---

## 0. 一句话总览

用户提了 **7 件事**，我全部改完，从「导入材料解析 → 入库 → 造句计划 → 前端 🔁 换场景」
整条链路打通，6 项端到端复验 + 21 个单元测试全绿。

主要在做四件事：
1. 让导入解析器**认识 `Mini Scenario 1/2/3`**（基础句自带的场景）；
2. 把**基础句场景彻底去 AI**（只认词自带的场景，永不调 AI 补）；
3. 改**组合句**：每组 5 词（3 复习 + 2 新）、提示词加 Phase 难度硬性规则；
4. 修 **AI 扩写**：以前生成完就扔，现在能真正显示出来。

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

## 2. 本轮（1a53240）真正改了哪些文件

| 文件 | 改动量 | 属于本轮吗 |
|---|---|---|
| `backend/importer.py` | +89 / -4 | ✅ 本轮 |
| `backend/scenario.py` | +210 / -37 | ✅ 本轮 |
| `backend/services.py` | +89 / -22 | ✅ 本轮 |
| `backend/weekimport.py` | +32 / -1 | ✅ 本轮 |
| `backend/main.py` | +88 / -40 | ✅ 本轮 |
| `backend/ai_service.py` | +278 / -43 | ✅ 本轮（含上一轮持久化的收尾） |
| `frontend/index.html` | +93 / -9 | ✅ 本轮 |
| `backend/db.py` | +85 / -4 | ⚠️ **上一轮遗留**（时区工具），本次被一起提交 |
| `backend/report.py` | +41 / -15 | ⚠️ **上一轮遗留**（自然周口径），本次被一起提交 |
| `tests/test_import_scenes.py` | +211（新文件） | ✅ 本轮 |
| `tests/test_sentence_persistence_and_week.py` | +296（新文件） | ⚠️ 上一轮创建，本轮改了 2 个断言 |
| `FIX_REPORT.md` | +120（新文件） | ⚠️ 上一轮遗留文档，本次被一起提交 |

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

### 3.8 测试

- **新增** `tests/test_import_scenes.py`（8 个用例）：解析、入库、到造句计划、归一化容错、老材料不变。
- **修改** `tests/test_sentence_persistence_and_week.py` 的 2 个断言：
  `test_generate_for_word_passes_stage_into_sys_prompt`、`test_generate_defaults_to_a2_not_b1`
  原来点名 `need_tier="small"` 验证 stage 透传 —— 现在 small 不调 AI 了，改成用 `large` 验证。
  并**新增** `test_small_tier_never_calls_ai` 锁住「small 永不调 AI」。

---

## 4. 哪些经过用户同意、哪些没有

| 改动 | 用户是否明确同意 |
|---|---|
| 解析 Mini Scenario（块状 + 逐行） | ✅ 同意 |
| 场景入库并带到前端 | ✅ 同意（🔁 要能切场景的前提） |
| 基础句场景彻底去 AI + `AI_TIERS` | ✅ 同意 |
| 组合句每组 5 词（3 复习 + 2 新） | ✅ 同意 |
| 组合句提示词加 Phase 规则 | ✅ 同意 |
| AI 扩写合并进 optimizations | ✅ 同意（授权"你改吧"） |
| 前端 🔁 基础句不回退 AI | ✅ 同意 |
| **改 2 个老测试的断言 + 新增 1 个测试** | ❌ **未经单独同意**（是 3.3 的必然结果，顺手改的） |
| **`normalize_scenes` 加"过滤后按顺序重编号"** | ❌ **未经单独同意**（为让 3 条场景永远是 1/2/3，不出现断号） |
| **`generate_for_word` 加两道闸（点名 small 直接返回 0）** | ✅ 属于 3.3 的一部分，用户说"彻底删干净"，视为同意 |

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

## 7. 关于用户追问的"造句不保留在页面上"

用户最后问：「之前造句不保留在页面上修改了嘛」。

**查证结论**：
- **本轮（1a53240）没动这件事。**
- 相关的「删除」发生在**更早的提交 `2167cab`**（"薄弱项+总结页改动"），其说明写着：
  > 「总结页: 删除本周学习里的造句记录(句子记录保留在学习页)」
- 具体：
  - **总结页**（`pages.sum`）：删掉了「📝 本周造句记录（N 条，点开看批改）」整块卡片。
  - **学习页**：**没动** —— 每题的历史记录、折叠、复制都在（`state.hist` / `histOf(tk)` / `attemptCard`）。
  - **后端 `report.py`**：`sentence_list` **还在算**，只是前端不显示了。
- 我已把上述事实回报给用户，并**请其确认**到底是「确认已修」（→ 是）还是「抱怨不该删 / 现在仍看不到」（→ 新需求，需用户明确要在哪看、看什么）。

> ⚠️ 接手时请注意：**这一条用户尚未给出最终答复**，可能需要后续处理。

---

## 8. 验证与提交状态

- **21 个单元测试全绿**：`test_import_scenes.py`（8）+ `test_sentence_persistence_and_week.py`（13）。
- **6 项端到端复验全过**：解析 → 入库 → 造句计划 → 基础句去 AI → 组合配比 → Phase 提示词 → AI 扩写。
- 全量测试套件里的 **7 个 collection error** 经 `git stash` 对照确认**改动前就存在**
  （playwright 浏览器测试 + 需真实 AI Key 的脚本），**非本轮引入**。
- **提交**：`1a53240`，**仅在本机**。
- ⚠️ **未推送到 GitHub**：沙箱没有用户 GitHub 凭据（`gh` 未登录，`ghfast.top` 镜像推送也拿不到用户名/token）。
  **需要用户自行 push 或提供凭据。**

---

## 9. 给接手 AI 的提醒

1. `tests/` 下有不少**脚本式测试**（直接 `sys.exit`），pytest 收集会报错；跑测试请
   `--ignore` 掉 `test_api_flow.py`、`test_browser_flow.py`、`test_file_upload_import.py`、
   `test_mobile_fe.py`、`test_new_ui.py`、`test_new_ui_v2.py`、`test_scenario_weakness.py`、`test_stealth_ai_flow.py`。
2. 若要让**老数据**也补上自带场景，需要重新解析老材料 —— 这是独立任务，用户尚未要求。
3. `AI_TIERS` 是控制「哪一层走 AI」的总开关，想恢复旧行为就把 `"small"` 加回去并去掉 3.3 的两道闸。
4. 用户风格：**要求先分析需求再动手，并且要大白话报告**；对反复提问会不耐烦。
