# -*- coding: utf-8 -*-
"""AI 情景生成与轮换（设计方案 §2）。

一句话定位：**AI 只负责"写情景文字"**，流程、触发、库存、轮换全由代码管。

AI 的边界（2024 修订，写死在 _SYS 里，改动时别把旧规则加回来）：
  - AI 只产出「发生了什么」的中文情景内容；
  - **不产出语法要求 / 时态指令 / "昨天或上周做过的事"这类任务模板**
    —— 语法要求由页面统一显示，AI 再写一遍只会重复甚至和页面打架；
  - 中/大情景按「场景容量」分，不按句子数；大情景允许多个学习词（含复习词）自然共存，
    判断标准是「整体自然连续 + 核心词有主线 + 复习词自然嵌入」，
    **不是**要求每个词之间有强因果，更不许为了塞词新开一堆独立事件。

触发点（§2.1，已推翻旧的"写完第一句才懒生成"）：
  ① 导入时（主）     —— weekimport 导入成功 → 后台给这批词每个生成首批 3-5 条
  ② 点「记住了」（兜底）—— /api/word/master 里若该词还没有情景 → 后台补生成
  两个触发点调同一个 generate_for_word()，已生成的词自动跳过，不重复烧 token。

练习时：按 used_count 升序取「最少用过的」一条；剩余未用 ≤1 条时后台补 3 条（§2.4）。

安全约定（与 ai_correct.py 一致）：
  - API Key 只从环境变量读（走 ai_correct.ark_json），不落库、不回前端、不进日志。
  - 只写 word_scenarios 一张表，**绝不碰** day_items / weeks / progress
    （不自动编造单词、不改用户的导入列表与周次）。
"""

import json
import re
import sys
import threading
import time
import zlib

from db import get_conn, ts
import ai_correct

# 每个词的初始库存目标：低于它就补生成
MIN_POOL = 3
# 一次生成多少条（模型被要求产出 3-5 条）
GEN_COUNT = 4
# 剩余「从没用过」的条数低于这个值就补池
LOW_WATER = 1

# 正在生成中的词（防重复触发：导入 + 记住了可能同时打过来）
_busy = set()
_busy_lock = threading.Lock()


def ai_enabled():
    """是否配了 Key（没配就整个模块空转，不报错、不花钱）。"""
    return ai_correct.ai_enabled()


# ------------------------------------------------------------------
# 生成
# ------------------------------------------------------------------
_SYS = """你是英语情景设计师，专门给中国成年英语自学者设计"可以拿来造句"的生活情景。

职责边界（最重要）：你只负责写**情景内容本身**。
语法要求、时态指令、任务模板**一律不归你写**——本周语法重点由页面统一显示，
你再写一遍就会重复，还会和页面显示的语法打架。

硬性禁止（任何一条情景里出现下面这些，都算不合格）：
1. 不写语法要求：不要在情景文字里夹「语法：xxx」这类提示，也不要写"请注意 xxx"之类的提醒。
2. 不写时态指令：不指定该用什么时态、什么句型、什么固定搭配，让学习者自己决定怎么写。
3. 不写任务模板：不要用「写一件你……做过的事」这类把句式和时间定死的套话，
   也不要用"请……"的出题口吻下达要求。你给的是"发生了什么"，不是"请做什么"。
4. 情景写成**一段连贯的叙述**，不是清单。
   （大情景尤其注意：从「事情怎么开始」讲到「后来怎么样了」，一个自然段讲完，
   不要编号、不要分行罗列。）

情景内容要求：
1. 学习者自己身上真会发生的具体小事（工作、通勤、手机、购物、看病、带娃、社交、家务…），
   具体到「谁、在哪、发生了什么」，不写抽象话题。
2. 必须明确要用到目标词（用英文原词写进 prompt 里）。
3. 简体中文，口语化，像朋友当面跟你聊起的一件真事，不书面、不套模板。
4. 同一个词的几条情景之间场景必须完全不同，禁止只把主语换一下。

三个层级（按**场景容量**分，不按句子数；写几句只是结果，不是指标）：
- small 小情景：一个微场景，一两句话说清楚就够了。服务 1 个词。
- medium 中情景：围绕 **1 个核心学习词**的、具体、真实、完整的小情境。
  可以有几个自然连续的动作，但不要为了多塞词而制造一堆事件。
  例（account）：周末坐在餐桌前核对这个月的水电账，顺手又对了一遍银行卡的自动扣款。
- large 大情景：**写一件连续发生的事，从头到尾讲完**——一个更完整、更丰富的
  真实情境，能**自然容纳多个学习词**，有因果、有时间线、是一个说得通的整体。
  一段话讲完，像在讲一件真事，不要写成"第一件事…第二件事…"的清单。
  例：刚入职公司的第一周——周一收拾工位、认识同事，周三参加入职培训，
  周五跟经理过一遍在跟的项目，里面 account / prepare / project / colleague /
  training / manager 都能自然出现。
  （大情景可能还要带上几个复习词，规则见下。）

大情景里"复习词"的处理（重要）：
- 判断标准是「整体场景自然连续 + 核心词有明确主线 + 复习词自然嵌入」，
  **不是**要求每个词之间都有强因果关系。
- 核心学习词承担场景主线；复习词只要能在场景里自然成为一个动作、一句描述或一句对话即可。
- 可以借生活转场、时间推进、人物对话把复习词带进来
  （例：核心是 account，复习词 choose 就是"从两张卡里选一张来扣"这个自然动作）。
- 复习词是**用来串成同一条故事线的**，不是一张要挨个打勾的清单：
  把它们分散到时间推进的不同节点上，让故事往前走的时候自然带出来。
- 绝对不要为了让某个复习词出现，就新开一个独立事件
  （"办公 → 突然去买东西 → 又突然去吃饭 → 又发生另一件事……"这种流水账是错的）。
- 某个复习词实在嵌不进去，就不要硬编事件，优先保证场景自然度。

只输出一个 JSON 对象，不要 markdown 代码块，不要任何前后缀：
{"scenarios":[{"tier":"small","prompt":"…"}, …]}"""


# ------------------------------------------------------------------
# 入库清洗：AI 偶尔还是会把语法要求写进情景（旧数据里就有），
# 这里在**入库时**一次性剥掉，页面照常统一显示语法，不需要前端去猜 AI 写没写。
# 只删两类很确定的东西：① 括号式「（语法：…）」② 句尾的时态指令短句。
# 其余内容一概不动，避免误伤正常情景文字。
# ------------------------------------------------------------------
_GRAMMAR_BRACKET = re.compile(r"[（(]\s*(?:语法|语法要求|时态|语法点)\s*[:：][^）)]*[）)]")
_TENSE_TAG = re.compile(
    r"[（(]\s*(?:一般过去时|一般现在时|一般将来时|现在完成时|过去完成时|"
    r"过去进行时|现在进行时|被动语态)\s*[）)]")
_TENSE_CMD = re.compile(
    r"(?:请|注意|记得|记得要)?\s*(?:使用|用|采用|请用)?\s*"
    r"(?:一般过去时|一般现在时|一般将来时|现在完成时|过去完成时|"
    r"过去进行时|现在进行时|被动语态)\s*"
    r"(?:来写|写|造句|表达|描述)?\s*[。；，、]?\s*$")


def sanitize_prompt(text):
    """剥掉情景文字里自带的语法要求 / 时态指令（语法由页面统一显示）。"""
    s = str(text or "").strip()
    if not s:
        return s
    s = _GRAMMAR_BRACKET.sub("", s)
    s = _TENSE_TAG.sub("", s)
    # 时态短句只删句尾的（"……请用一般过去时。"），句中提到的正常保留
    s = _TENSE_CMD.sub("", s)
    s = re.sub(r"\s{2,}", " ", s).strip().strip("，、；") 
    return s


def resanitize_all():
    """把库里已有情景全部重新清洗一遍（旧 prompt 带「（语法：…）」的会被修掉）。

    返回清洗了多少条。用法：python3 backend/scenario.py --clean
    """
    changed = 0
    try:
        conn = get_conn()
        rows = conn.execute("SELECT id, prompt FROM word_scenarios").fetchall()
        for r in rows:
            old = r["prompt"] or ""
            new = sanitize_prompt(old)
            if new and new != old:
                conn.execute("UPDATE word_scenarios SET prompt=? WHERE id=?", (new, r["id"]))
                changed += 1
        conn.commit()
        conn.close()
    except Exception as e:
        print("[scenario] 历史情景清洗失败: %s" % e)
    return changed


def angle_order_for_word(word, n=None):
    """给一个词生成情景时，n 条情景各自该往哪个角度取材。

    **为什么不是"整个词今天一个角度"**：情景写完是要长期留在库里反复用的
    （轮换 + 🔁 换场景），而「今天该从哪个角度出题」每天都会变。给整批情景
    打上今天的角度，明天这条情景就和用户实际拿到的题目对不上了。

    **现在的做法**：一次生成的 n 条情景各自覆盖一个不同角度 —— 用户换一次
    情景就换一个取材方向，情景库天然是"多角度"的。起点按词旋转，
    免得所有词的第 1 条情景都撞在同一个角度上。
    """
    try:
        import services as _svc
        names = [c[0] for c in _svc.FUNC_CATEGORIES]
    except Exception:
        return []
    k = len(names)
    if n is None:
        n = GEN_COUNT
    n = max(0, min(int(n), k))
    if not n:
        return []
    start = zlib.crc32(str(word or "").strip().lower().encode("utf-8")) % k
    return [names[(start + j) % k] for j in range(n)]


def partner_words(word, limit=4):
    """给 large 大情景配的"其他词"——**就是当日新词 + 当日到期复习词**。

    这两份数据页面上本来就在用（combo 组合那一组就是它们配出来的）：
      ① 当日到期复习词：srs.due_vocab_words()（按错误率 + 久未复习排序）
      ② 当日新词      ：services.get_week(progress) 的 vocab 按当天 day 过滤
    AI 写大场景时拿到的因此是真实组合，而不是随便抓几个邻居凑数。
    查不到就返回 None（行为与没配一样，不影响生成）。
    """
    w0 = (word or "").strip().lower()
    out = []

    def _push(k):
        k = (k or "").strip().lower()
        if k and k != w0 and k not in out:
            out.append(k)

    # ① 到期复习词优先（这正是大场景里要自然嵌入的复习词）
    try:
        import srs
        for r in (srs.due_vocab_words(limit=30) or []):
            _push(r.get("word"))
            if len(out) >= limit:
                return out
    except Exception:
        pass
    # ② 当日新词补位
    try:
        import services as _svc
        p = _svc.get_progress() or {}
        wk = _svc.get_week(p.get("stage"), p.get("week")) or {}
        day = int(p.get("day") or 1)
        for v in (wk.get("vocab") or []):
            try:
                vday = int(v.get("day") or day)
            except (TypeError, ValueError):
                vday = day
            if vday != day:
                continue
            _push(v.get("word"))
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out or None


def _lookup_word(word):
    """顺手查一下词库的中文/词性，让情景更贴词。查不到就返回空串，不影响生成。"""
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT meaning, pos FROM dictionary WHERE word=? LIMIT 1",
            ((word or "").strip().lower(),)).fetchone()
        conn.close()
        if row:
            return (row["meaning"] or ""), (row["pos"] or "")
    except Exception:
        pass
    return "", ""


def generate_for_word(word, grammar="", n=GEN_COUNT, extra=None, need_tier=None):
    """给一个词生成情景并入库。返回新增条数（0 = 库里已够 / AI 不可用 / 失败）。

    grammar 只作为**背景参考**交给模型（让它知道这周在学什么，选场景时更贴），
    但明确要求它**不要把语法写进情景文字**——语法要求由页面统一显示。

    extra：同批的其他学习词（含复习词），只在 large 大情景里"能自然嵌入就带上"，
    嵌不进去不许硬造事件。没有就传 None，行为与以前一致。

    need_tier（2026-09-10 新增）：只补某一层（"small" / "large"）。
        页面是按 tier 精确取景的（基础句只取 small、组合句只取 large），
        以前"够不够"只看总数，于是常出现「有 3 条 small 就以为够了、
        large 永远 0 条」——组合句一直没情景就是这么来的。
        传了 need_tier 就**只补这一层**，并跳过其它层的库存判断。
        传 None 则退回旧行为（small/large 都生成，用于导入时的首批）。
    """
    word = (word or "").strip().lower()
    if not word or not ai_correct.ai_enabled():
        return 0
    nt = (str(need_tier) if need_tier else "").strip().lower() or None
    if nt not in ("small", "large"):
        nt = None
    with _busy_lock:
        # 同一词 + 同一层正在生成中就跳过；不同层互不阻塞
        busy_key = "%s|%s" % (word, nt or "all")
        if busy_key in _busy:
            return 0
        _busy.add(busy_key)
    try:
        if nt:
            # 只补指定层：该层够了就退（其它层缺不缺不归这次管）
            if count_of(word, nt) >= TIER_TARGET.get(nt, MIN_POOL):
                return 0
        else:
            # 不指定层（导入时的首批）：**两层都够**才算够。
            # 这里以前是 `count_of(word) >= MIN_POOL`（只看总数），
            # 某词若只有 3 条 large 就会被误判"够了"、永远不补 small。
            if all(count_of(word, x) >= TIER_TARGET.get(x, MIN_POOL)
                   for x in ("small", "large")):
                return 0
        meaning, pos = _lookup_word(word)
        user = "目标词：%s" % word
        if meaning or pos:
            user += "（%s%s）" % (pos or "", ("　" + meaning) if meaning else "")
        if grammar:
            # 语法只作背景参考：页面会统一显示语法要求，AI 不该再写一遍。
            # 措辞上不能把模板字样原样贴出来 —— 「语法：…」「请用…时」抄进提示里，
            # 模型反而会把它们当成示范、照着写进情景正文。所以只描述意思。
            user += ("\n本周语法重点：%s（只是让你知道该选什么样的场景。"
                     "它是页面统一显示的背景，**不要写进情景文字**，"
                     "也别出现任何要求使用者必须用某种时态的说法）" % grammar)
        extra = [e for e in (extra or []) if (e or "").strip().lower() != word]
        if extra:
            # 「其他词」的用途必须说清 —— 只写"可以带上"的话，模型会把它当成
            # 一张要挨个打勾的清单，塞不进去就干脆一个不写（实测就是这样：
            # 传了 headache / medicine，结果 AI 一个没用，只围着 account 转）。
            # 这里点明：它们是**串进同一条故事线**用的，不是任务清单。
            user += ("\n大情景（large）里可以自然带上的其他词：%s"
                     "——它们的作用是**串进同一条故事线**，让故事往下走的时候顺带带出来"
                     "（比如换成「选哪个」「什么时候做」这类自然的动作或对话），"
                     "不是一张要挨个打勾的清单。嵌不进去的就不写，不要为它新开一件事。"
                     % "、".join(extra[:6]))
        # 取材方向：**按条给**，每条各占一个角度。
        # 情景写完是长期留在库里轮换用的，而"今天该从哪个角度出题"每天会变 ——
        # 给整批打一个当天角度，明天这条情景就跟用户手上的题目对不上。
        # 让一次生成的几条各覆盖一个角度，换一次情景就换一个取材方向。
        #
        # ⚠️ 大情景（large）不参与这个"按顺序分角度"的分配（2026-09-10 改）。
        # 原因：大情景的本质是**一条连续的故事线**，要求它同时承担 4 个不同
        # 生活角度，模型只能靠"硬编十几二十件事"来满足 —— 这就是流水账的来源。
        # 所以角度只分给 small，large 专心讲一件事讲完整。
        _angs = angle_order_for_word(word, n)
        if _angs and len(_angs) > 1:
            if nt == "large":
                # 只补大情景：不需要角度（它们本来就是同一条故事线）
                pass
            elif nt == "small":
                user += ("\n这些情景请分别取材于：%s。"
                         "只用来决定每条往哪个生活方向取料：不要把角度名写进情景文字，"
                         "也不要因此规定时态或句式（上面那几条禁止项在这里一样生效）。"
                         % " / ".join(_angs[:n]))
            else:
                _small_only = max(1, len(_angs) - 1)     # 留一条给 large，不参与分配
                user += ("\n其中 small 这 %d 条情景，请分别取材于：%s。"
                         "只用来决定每条往哪个生活方向取料：不要把角度名写进情景文字，"
                         "也不要因此规定时态或句式（上面那几条禁止项在这里一样生效）。"
                         "**large 大情景不参与这个分配** —— 它只要写一件连续发生的事，"
                         "从头到尾讲完就行，不要为了凑角度硬加情节。"
                         % (_small_only, " / ".join(_angs[:_small_only])))

        # 生成配额：只产 small / large（medium 是删掉升级句之后的死层，
        # 页面没有任何地方取它，继续生成纯属白烧额度）。
        if nt == "large":
            user += "\n请生成 %d 条情景，tier 全部填 \"large\"。" % n
        elif nt == "small":
            user += "\n请生成 %d 条情景，tier 全部填 \"small\"。" % n
        else:
            _half = max(1, n // 2)
            user += ("\n请生成 %d 条情景：其中 %d 条 tier 填 \"small\"、"
                     "%d 条 tier 填 \"large\"（**不要 medium**）。"
                     % (n, _half, max(1, n - _half)))

        data, err = ai_correct.ark_json(_SYS, user, max_tokens=1400,
                                        temperature=0.8, tag="scenario")
        if err:
            print("[scenario] %s 生成失败: %s" % (word, err))
            return 0
        items = data.get("scenarios") or []
        if not isinstance(items, list):
            return 0
        saved = 0
        conn = get_conn()
        try:
            for it in items[:6]:
                if not isinstance(it, dict):
                    continue
                # 入库前清洗：AI 万一还是写了语法要求，这里剥掉，不留到页面上
                prompt = sanitize_prompt(str(it.get("prompt") or ""))[:600]
                if len(prompt) < 4:
                    continue
                tier = str(it.get("tier") or "small").strip().lower()[:10]
                if tier not in ("small", "medium", "large"):
                    tier = "small"
                # 认准要补的那层：AI 偶尔会无视配额，这里兜一道，别把货入错层
                if nt and tier != nt:
                    continue
                conn.execute(
                    "INSERT INTO word_scenarios (word, tier, prompt, grammar, used_count, created_at)"
                    " VALUES (?,?,?,?,0,?)",
                    (word, tier, prompt, (grammar or "")[:200], ts()))
                saved += 1
            conn.commit()
        except Exception as e:
            print("[scenario] %s 入库失败: %s" % (word, e))
            try:
                conn.rollback()
            except Exception:
                pass
            return 0
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if saved:
            print("[scenario] %s 生成 %d 条情景%s"
                  % (word, saved, ("（只补 %s）" % nt) if nt else ""))
        return saved
    finally:
        with _busy_lock:
            _busy.discard(busy_key)


# ------------------------------------------------------------------
# 查询 / 轮换
# ------------------------------------------------------------------
def count_of(word, tier=None):
    """该词库里已有多少条情景（可按 tier 过滤）。

    ⚠️ 2026-09-10 加 tier 参数：以前只数总数，导致「有 3 条 small 就以为够了」，
    于是 large 永远是 0 条，组合句一直没情景。页面是**按 tier 精确取景**的
    （基础句只取 small、组合句只取 large），所以"够不够"必须按层判断。
    """
    try:
        conn = get_conn()
        w = (word or "").strip().lower()
        t = (str(tier) if tier else "").strip().lower() or None
        if t:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM word_scenarios WHERE word=? AND tier=?",
                (w, t)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM word_scenarios WHERE word=?",
                (w,)).fetchone()
        conn.close()
        return int(row["n"] if row and row["n"] is not None else 0)
    except Exception:
        return 0


# 每个 tier 的库存目标：低于它就该补。页面只消费 small（基础句）和
# large（组合句），medium 是删掉升级句之后的死层，不再补。
TIER_TARGET = {"small": 3, "large": 3}


def pick(word, exclude_id=0, tier=None):
    """取一条情景：**纯轮换，不消耗库存**（2026-09-10 改）。

    ⚠️ 这里以前是「取一条就把 used_count +1」，问题很大：
       前端每渲染一次页面、每点一次 🔁 都会调进来，于是**用户只是"看一眼"
       就被记成"用掉一条"**，紧接着 refill_if_low 数到"没用过的"变少，
       误判库存不足，后台反复重新生成 —— 用户看到的现象就是"我都没写，
       它怎么一直在生成新的"。语义从一开始就错了：
           used_count 本意是「防连着重复」，却被拿来当「库存消耗计数器」。
       现在：取景**只读不写**，绝不因为"看了一眼"触发补货。

    轮换怎么保证（不靠消耗）：按 id 顺序循环。
        - 传了 exclude_id（🔁 换场景，= 当前正在看的那条）：
          先找 id > exclude_id 的下一条；没有就绕回最小的那一条。
          两步都不带 id<>exclude_id 的硬排除，所以**只有一条时也能给出去**，
          不会开天窗。
        - 没传 exclude_id（首次取景）：固定取 id 最小的那条 —— 同一个词
          每次进页面看到的是同一条，稳定、可预期（要换用户自己点 🔁）。

    tier 用于按题型分层取景：basic=small / combo=large；
    该层级没库存时退回不过滤（保证有情景显示，不开天窗）。
    返回 dict(id, tier, prompt) 或 None。
    """
    w = (word or "").strip().lower()
    if not w:
        return None
    t = (str(tier) if tier else "").strip().lower() or None
    if t not in ("small", "medium", "large"):
        t = None
    cur = int(exclude_id or 0)
    try:
        conn = get_conn()
        row = None

        def _one(tier_filter, after):
            """取一条。tier_filter 为 None 表示不按层过滤；
            after > 0 表示要 id 比它大的（循环轮转用）。"""
            sql = "SELECT id, tier, prompt FROM word_scenarios WHERE word=?"
            args = [w]
            if tier_filter:
                sql += " AND tier=?"
                args.append(tier_filter)
            if after:
                sql += " AND id>?"
                args.append(after)
            sql += " ORDER BY id ASC LIMIT 1"
            return conn.execute(sql, tuple(args)).fetchone()

        def _first(tier_filter):
            sql = "SELECT id, tier, prompt FROM word_scenarios WHERE word=?"
            args = [w]
            if tier_filter:
                sql += " AND tier=?"
                args.append(tier_filter)
            sql += " ORDER BY id ASC LIMIT 1"
            return conn.execute(sql, tuple(args)).fetchone()

        # 按层找；该层没有 → 退回不过滤（老数据可能全是 medium）
        for tf in ([t, None] if t else [None]):
            if cur:
                row = _one(tf, cur)          # 当前这条之后的
                if not row:
                    row = _first(tf)          # 绕回：从头开始（含当前这条）
            else:
                row = _first(tf)
            if row:
                break

        conn.close()
        if not row:
            return None
        return {"id": row["id"], "tier": row["tier"] or "small",
                "prompt": row["prompt"] or ""}
    except Exception as e:
        print("[scenario] pick(%s) 失败: %s" % (w, e))
        return None


TIER_LABEL = {"small": "小情景", "medium": "中情景", "large": "大情景"}


def with_label(sc):
    """给前端补一个中文层级标签。"""
    if not sc:
        return None
    sc = dict(sc)
    sc["tier_label"] = TIER_LABEL.get(sc.get("tier"), "情景")
    return sc


def refill_if_low(word, grammar=""):
    """按 tier 检查库存，缺哪层补哪层（2026-09-10 重写）。

    ⚠️ 旧版有三个错，一起修掉：
     ① 数的是「used_count=0 的条数」，而 used_count 会被"看一眼"动作递增
        （详见 pick 的注释），于是用户没写也判定"快用完了"，反复重新生成；
     ② 完全不看 tier —— 有 3 条 small 就以为够了，large 永远补不上，
        组合句一直没情景；
     ③ 只补 3 条、层数随机，补了也可能全补到 small 上（约 30% 白补）。
    现在：**按 small / large 分别检查**，缺哪层就专门补哪层。
    仍然只在 AI 可用时补，且由调用方决定何时调（不在这里发太多请求）。
    """
    w = (word or "").strip().lower()
    if not w or not ai_correct.ai_enabled():
        return
    for t in ("small", "large"):
        need = TIER_TARGET.get(t, MIN_POOL)
        if count_of(w, t) < need:
            # 只补缺的那一层；同一词不同层可并行，同层由 _busy 去重
            spawn(generate_for_word, w, grammar, max(1, need - count_of(w, t)), None, t)


# ------------------------------------------------------------------
# 后台触发（导入 / 记住了 都走这里，不阻塞用户操作）
# ------------------------------------------------------------------
def spawn(fn, *args, **kwargs):
    """丢到后台线程跑：导入 120 个词的批量生成不该让用户在页面前干等。"""
    try:
        t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
        t.start()
        return t
    except Exception as e:
        print("[scenario] 后台线程启动失败: %s" % e)
        return None


def ensure_for_words(words, grammar="", workers=3, only_words=None):
    """导入时主触发：给这批词补齐首批情景。**已生成过的自动跳过**。

    同步执行（调用方自己放在后台线程里），返回生成了多少个词。

    ⚠️ 2026-09-10 重要调整：**导入时不再一次补全整周**。
    导入 120 个词若每词补 small+large 两层 = 240 次 AI 调用，既慢又烧额度；
    而用户当天只学 20 个词，其余的天数是往后慢慢走的。
    所以这里只保「当天要学的那批」（调用方用 only_words 指定，一般是 Day1），
    剩下的交给 backfill_step 按天慢慢补（由页面访问 / 外部定时器驱动）。

    旧版只看总数（count_of(w) < MIN_POOL）判断"缺不缺"，会出现
    「有 3 条 small 就跳过、large 永远 0 条」——组合句没情景的根因之一。
    现在按 tier 判断，缺哪层补哪层。
    """
    ws = []
    seen = set()
    pool = only_words if only_words else words
    for w in (pool or []):
        wl = (w or "").strip().lower()
        if not wl or wl in seen:
            continue
        seen.add(wl)
        ws.append(wl)
    if not ws or not ai_correct.ai_enabled():
        return 0
    todo = [w for w in ws
            if any(count_of(w, t) < TIER_TARGET.get(t, MIN_POOL)
                   for t in ("small", "large"))]
    if not todo:
        return 0
    print("[scenario] 导入触发：%d 个词待生成情景（只补当天这批）" % len(todo))

    done = 0
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as ex:
            # small / large 分别补，缺哪层补哪层（generate_for_word 传 need_tier）
            futs = []
            for w in todo:
                for t in ("small", "large"):
                    if count_of(w, t) < TIER_TARGET.get(t, MIN_POOL):
                        futs.append(ex.submit(generate_for_word, w, grammar,
                                              max(1, TIER_TARGET.get(t, MIN_POOL)
                                                  - count_of(w, t)), None, t))
            for f in as_completed(futs):
                try:
                    if f.result():
                        done += 1
                except Exception:
                    pass
    except Exception as e:
        print("[scenario] 批量生成异常: %s" % e)
    return done


def ensure_for_word_bg(word, grammar=""):
    """点「记住了」兜底触发：这个词的情景缺哪层就补哪层（后台，不卡界面）。

    ⚠️ 旧版是 `if count_of(w) >= MIN_POOL: return` —— 只看总数。某词若已有
    3 条 small，总数够 3 就再也不补，它的 large 永远补不上，组合句一直没情景。
    现在改成**按 small / large 分别判断**，谁缺补谁。
    """
    w = (word or "").strip().lower()
    if not w or not ai_correct.ai_enabled():
        return
    for t in ("small", "large"):
        if count_of(w, t) < TIER_TARGET.get(t, MIN_POOL):
            spawn(generate_for_word, w, grammar,
                  max(1, TIER_TARGET.get(t, MIN_POOL) - count_of(w, t)), None, t)
            return          # 一次只触发一层（另一层留着给下一次请求，避免瞬时打爆）


# ------------------------------------------------------------------
# 按天补齐（2026-09-10 新增）
# ------------------------------------------------------------------
# 为什么需要它：
#   情景是调 AI 生成的，一次给全周 120 个词 × 2 层 × 3 条 = 720 次调用，
#   不现实（烧额度、还慢）。但**用户当天只学 20 个词**，其余的天数是慢慢
#   往后走的 —— 所以只要保证"今天那批齐"，剩下的靠时间补上就行。
#   于是这里做一个**按 Day 顺序、从导入时第一天开始**的补齐任务：
#       Day1 齐 → 补 Day2 → Day2 齐 → 补 Day3 → ……
#   每天只推进一小步（限速），既不烧额度，到期那天又一定是齐的。
# 触发方式：① 服务里每次有相关请求时"借一步"（request_budget）；
#           ② 外部定时器（UptimeRobot）定期戳 /api/scenario/backfill 唤醒它。
# ------------------------------------------------------------------
_BF_LOCK = threading.Lock()
_BF_STATE = {"cursor": None, "ts": 0.0}
# 每轮最多补几个词（防止一次性打爆 AI）；可由调用方临时放大
BF_BATCH = 2
# 两次"借步"之间至少间隔多少秒（太频繁对外部定时器没意义，还费额度）
BF_MIN_INTERVAL = 20.0


def _bf_days():
    """返回按 (week, day) 升序排列的「天」，每天带该天的词。

    只取 kind='vocab' 的学习项；词来自 ref_key。
    返回 [{"stage":.., "week":.., "day":.., "words":[...]}, ...]
    """
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT stage, week, day, ref_key FROM day_items"
            " WHERE kind='vocab' AND ref_key IS NOT NULL AND ref_key<>''"
            " ORDER BY stage ASC, week ASC, day ASC, id ASC").fetchall()
        conn.close()
    except Exception:
        return []
    days, cur, key = [], None, None
    for r in rows:
        k = (int(r["stage"] or 0), int(r["week"] or 0), int(r["day"] or 0))
        if k != key:
            key = k
            cur = {"stage": k[0], "week": k[1], "day": k[2], "words": []}
            days.append(cur)
        w = (r["ref_key"] or "").strip().lower()
        if w and w not in cur["words"]:
            cur["words"].append(w)
    return days


def _bf_grammar_of(week):
    """取某周的语法（作背景交给 AI，页面不显示它）。查不到就空串。"""
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT grammar FROM weeks WHERE week_no=? LIMIT 1", (int(week or 0),)).fetchone()
        conn.close()
        return (row["grammar"] or "") if row else ""
    except Exception:
        return ""


def _bf_today():
    """当前学习位置 (stage, week, day)。取不到返回 None。"""
    try:
        import services as _svc
        p = _svc.get_progress() or {}
        return (int(p.get("stage") or 0), int(p.get("week") or 0),
                int(p.get("day") or 0))
    except Exception:
        return None


def backfill_step(max_words=None, force=False):
    """推进一轮"按天补齐"。返回本次补齐了多少个词。

    优先级（2026-09-10 定）：
      ① **今天要学的那天**最优先 —— 用户马上就用到，必须最先齐；
      ② 其余按 (week, day) 顺序，从前往后慢慢补（时间平摊，不一次生成整周）。
    每轮只补 limit 个词，补完就停；下一次调用接着推进。
    force=True 忽略最小间隔（供外部定时器调用）。线程安全：同一时刻只跑一个。
    """
    if not ai_correct.ai_enabled():
        return 0
    now = time.monotonic()
    with _BF_LOCK:
        if not force and (now - _BF_STATE["ts"]) < BF_MIN_INTERVAL:
            return 0
        _BF_STATE["ts"] = now
        if _BF_STATE["cursor"] is None:
            _BF_STATE["cursor"] = 0
        start = _BF_STATE["cursor"]

    days = _bf_days()
    if not days:
        return 0
    limit = int(max_words or BF_BATCH)

    def _gaps_of(d):
        """这一天还缺哪些 (词, 层)。"""
        g = []
        for w in d["words"]:
            for t in ("small", "large"):
                if count_of(w, t) < TIER_TARGET.get(t, MIN_POOL):
                    g.append((w, t))
            if len(g) >= limit:
                return g
        return g

    # ① 今天那批优先：不占游标，每次都先看它
    today = _bf_today()
    if today:
        for d in days:
            if (d["stage"], d["week"], d["day"]) == today:
                g = _gaps_of(d)
                if g:
                    gr = _bf_grammar_of(d["week"])
                    n = 0
                    for w, t in g[:limit]:
                        if generate_for_word(w, gr, TIER_TARGET.get(t, MIN_POOL), None, t):
                            n += 1
                    if n:
                        print("[scenario] 按天补齐（今天）：W%s D%s 补了 %d 个词"
                              % (d["week"], d["day"], n))
                    return n
                break

    # ② 其余按顺序推进
    done = 0
    idx = start
    scanned = 0
    # 从游标往后找第一个"有缺口"的天；扫完一圈后从头再来（数据可能新增）
    while scanned < len(days):
        d = days[idx % len(days)]
        scanned += 1
        gaps = _gaps_of(d)
        if gaps:
            g = _bf_grammar_of(d["week"])
            for w, t in gaps[:limit]:
                if generate_for_word(w, g, TIER_TARGET.get(t, MIN_POOL), None, t):
                    done += 1
            # 停在这一天：下次接着补同一天，直到它齐了再往后走
            with _BF_LOCK:
                _BF_STATE["cursor"] = idx % len(days)
            if done:
                print("[scenario] 按天补齐：W%s D%s 补了 %d 个词"
                      % (d["week"], d["day"], done))
            return done
        idx += 1
    # 全部齐了：游标归零，之后有新词会从头再扫
    with _BF_LOCK:
        _BF_STATE["cursor"] = 0
    return 0


def backfill_status():
    """给外部定时器看的状态：总共几天、每天齐了没、当前游标。"""
    days = _bf_days()
    out = []
    for d in days:
        miss = 0
        for w in d["words"]:
            for t in ("small", "large"):
                if count_of(w, t) < TIER_TARGET.get(t, MIN_POOL):
                    miss += 1
        out.append({"week": d["week"], "day": d["day"],
                    "words": len(d["words"]), "missing": miss})
    with _BF_LOCK:
        cur = _BF_STATE["cursor"]
    return {"days": out, "cursor": cur, "total_days": len(out),
            "all_done": all(x["missing"] == 0 for x in out) if out else True}


# ------------------------------------------------------------------
# 命令行
#   python3 backend/scenario.py --clean      清洗历史情景里的「（语法：…）」残留
#   python3 backend/scenario.py --status     看每天情景齐了没（不调 AI）
#   python3 backend/scenario.py --backfill   按天补齐（会调 AI，逐个词跑）
# ------------------------------------------------------------------
def _cli_status():
    st = backfill_status()
    print("[scenario] 共 %d 天，全部齐了: %s（游标 %s）"
          % (st["total_days"], st["all_done"], st["cursor"]))
    for d in st["days"]:
        flag = "✅" if d["missing"] == 0 else "缺 %d" % d["missing"]
        print("   W%-2s D%-2s  %2d 个词   %s" % (d["week"], d["day"], d["words"], flag))
    return st


def _cli_backfill():
    if not ai_correct.ai_enabled():
        print("[scenario] 未配置 AI Key，无法补齐。请先设置环境变量。")
        return
    total = 0
    for i in range(2000):          # 硬上限，防死循环
        n = backfill_step(force=True, max_words=2)
        total += n
        if n == 0:
            break
        if i % 10 == 0:
            print("[scenario] 已补齐 %d 个词…" % total)
    print("[scenario] 补齐完成，共 %d 个词。" % total)
    _cli_status()


if __name__ == "__main__":
    if "--clean" in sys.argv:
        n = resanitize_all()
        print("[scenario] 历史情景清洗完成：修复 %d 条" % n)
    elif "--status" in sys.argv:
        _cli_status()
    elif "--backfill" in sys.argv:
        _cli_backfill()
    else:
        print("用法：\n"
              "  python3 backend/scenario.py --clean     清洗情景里的语法残留\n"
              "  python3 backend/scenario.py --status    查看每天情景齐了没\n"
              "  python3 backend/scenario.py --backfill  按天补齐（会调 AI）")
