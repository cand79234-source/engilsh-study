# -*- coding: utf-8 -*-
"""AI 情景生成与轮换（设计方案 §2）。

一句话定位：**AI 只负责"写情景文字"**，流程、触发、库存、轮换全由代码管。

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
import threading
import time

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

要求：
1. 每个情景必须是学习者自己身上真会发生的具体小事（工作、通勤、手机、购物、看病、带娃、社交、家务…），
   要具体到「谁、在哪、发生了什么」，不要写抽象的话题。
2. 每个情景必须明确要求用到目标词（用英文原词写进 prompt 里），并尽量顺带给出一个搭配或语法要求。
3. 情景文字用简体中文写，1-3 句，像一个朋友当面给你派的一个小任务，口语化、不书面。
4. 三个层级，本次生成里要有 mix：
   - small ：一个微场景，写 1 句就够
   - medium：一个生活主题，串 2-3 个相关词，写 2-3 句
   - large ：一个大主题（约 20 句量），比如"你刚入职的第一周"
5. 同一个词的几条情景之间场景必须完全不同，禁止套模板、禁止只是把主语换一下。
6. 不要写英文例句（那是学习者待会儿要自己写的），只给中文情景 + 必须用到的词。

只输出一个 JSON 对象，不要 markdown 代码块，不要任何前后缀：
{"scenarios":[{"tier":"small","prompt":"…"}, …]}"""


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


def generate_for_word(word, grammar="", n=GEN_COUNT):
    """给一个词生成首批情景并入库。返回新增条数（0 = 库里已够 / AI 不可用 / 失败）。"""
    word = (word or "").strip().lower()
    if not word or not ai_correct.ai_enabled():
        return 0
    with _busy_lock:
        if word in _busy:
            return 0
        _busy.add(word)
    try:
        if count_of(word) >= MIN_POOL:
            return 0            # 已生成过 —— 不重复烧 token
        meaning, pos = _lookup_word(word)
        user = "目标词：%s" % word
        if meaning or pos:
            user += "（%s%s）" % (pos or "", ("　" + meaning) if meaning else "")
        if grammar:
            user += "\n本周语法重点：%s（情景里可以顺带要求用上，但不要生硬）" % grammar
        user += "\n请生成 %d 条情景。" % n

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
                prompt = str(it.get("prompt") or "").strip()[:600]
                if len(prompt) < 4:
                    continue
                tier = str(it.get("tier") or "small").strip().lower()[:10]
                if tier not in ("small", "medium", "large"):
                    tier = "small"
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
            print("[scenario] %s 生成 %d 条情景" % (word, saved))
        return saved
    finally:
        with _busy_lock:
            _busy.discard(word)


# ------------------------------------------------------------------
# 查询 / 轮换
# ------------------------------------------------------------------
def count_of(word):
    """该词库里已有多少条情景。"""
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM word_scenarios WHERE word=?",
            ((word or "").strip().lower(),)).fetchone()
        conn.close()
        return int(row["n"] if row and row["n"] is not None else 0)
    except Exception:
        return 0


def pick(word, exclude_id=0, tier=None):
    """取一条情景：used_count 最少的优先（最少用过 → 避免连着重复）。

    取到就把 used_count +1。exclude_id 用于 🔁 换场景：跳过当前正在看的这条。
    tier 用于按题型分层取景：basic=small / upgrade=medium / combo=large；
    指定层级无库存时自动退回不过滤（保证有情景显示，不开天窗）。
    返回 dict(id, tier, prompt) 或 None。
    """
    w = (word or "").strip().lower()
    if not w:
        return None
    t = (str(tier) if tier else "").strip().lower() or None
    if t not in ("small", "medium", "large"):
        t = None
    try:
        conn = get_conn()
        row = None
        if t:
            # 先按层级取；该层级没库存 → 退回不过滤
            row = conn.execute(
                "SELECT id, tier, prompt, used_count FROM word_scenarios"
                " WHERE word=? AND tier=? AND id<>?"
                " ORDER BY used_count ASC, id ASC LIMIT 1",
                (w, t, int(exclude_id or 0))).fetchone()
        if not row:
            row = conn.execute(
                "SELECT id, tier, prompt, used_count FROM word_scenarios WHERE word=?"
                " AND id<>? ORDER BY used_count ASC, id ASC LIMIT 1",
                (w, int(exclude_id or 0))).fetchone()
        if not row and exclude_id:
            # 只有一条且正好被排除 → 还是把这条给出去，别开天窗
            row = conn.execute(
                "SELECT id, tier, prompt, used_count FROM word_scenarios WHERE word=?"
                " ORDER BY used_count ASC, id ASC LIMIT 1", (w,)).fetchone()
        if not row:
            conn.close()
            return None
        conn.execute("UPDATE word_scenarios SET used_count=used_count+1 WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()
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
    """剩余「从没用过」的条数 ≤ LOW_WATER → 后台再补 3 条（成本极低）。"""
    w = (word or "").strip().lower()
    if not w or not ai_correct.ai_enabled():
        return
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM word_scenarios WHERE word=? AND used_count=0",
            (w,)).fetchone()
        conn.close()
        if int(row["n"] if row and row["n"] is not None else 0) > LOW_WATER:
            return
    except Exception:
        return
    spawn(generate_for_word, w, grammar, 3)


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


def ensure_for_words(words, grammar="", workers=3):
    """导入时主触发：给这批词补齐首批情景。**已生成过的自动跳过**。

    同步执行（调用方自己放在后台线程里），返回生成了多少个词。
    """
    ws = []
    seen = set()
    for w in (words or []):
        wl = (w or "").strip().lower()
        if not wl or wl in seen:
            continue
        seen.add(wl)
        ws.append(wl)
    if not ws or not ai_correct.ai_enabled():
        return 0
    todo = [w for w in ws if count_of(w) < MIN_POOL]
    if not todo:
        return 0
    print("[scenario] 导入触发：%d 个词待生成情景" % len(todo))
    done = 0
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as ex:
            for r in ex.map(lambda w: generate_for_word(w, grammar), todo):
                done += 1 if r else 0
    except Exception as e:
        print("[scenario] 批量生成异常: %s" % e)
    return done


def ensure_for_word_bg(word, grammar=""):
    """点「记住了」兜底触发：这个词还没有情景才补生成（后台，不卡界面）。"""
    w = (word or "").strip().lower()
    if not w or not ai_correct.ai_enabled():
        return
    if count_of(w) >= MIN_POOL:
        return
    spawn(generate_for_word, w, grammar)
