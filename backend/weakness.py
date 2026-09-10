# -*- coding: utf-8 -*-
"""薄弱项 AI 讲解（设计方案 §4）。

只做一件事：当**同一个词的同一个错误标签反复出现**时，让 AI 用 ≤2 句中文
讲清楚「你为啥总在这栽跟头」，在批改结果区原地（inline）显示。

刻意不做的事（§4.4 边界）：
  - 不碰错题本 / 周测（那套已经专门练错题，AI 不抢）
  - 不弹 modal、不跳页、不做长篇讲解
  - 同一个「词 + 标签」**只讲一次**：讲过就落库，下次直接命中，不再烧 token

触发阈值：同一 (word, tag) 累计出现 ≥ HIT_THRESHOLD 次才讲。频率极低，成本可忽略。
"""

from db import get_conn, ts
import ai_correct

# 同一个错出现几次才值得讲一次
HIT_THRESHOLD = 3

_SYS = """你是一名英语老师，要帮一个中国成年自学者改掉一个反复犯的错。

要求：
1. 只输出一个 JSON 对象：{"explain":"…"}，不要 markdown 代码块，不要任何前后缀。
2. explain 是最多 2 句简体中文，口语化，像老师当面点破：
   第 1 句说「你为什么总在这栽跟头」（根因，中文思维/母语习惯层面），
   第 2 句给一个具体的纠正办法或一个最短的正确例子。
3. 不要重复"你错了"这种废话，不要堆语法术语，不要长篇大论，不要说"建议多加练习"。
4. 控制在 60 个汉字以内。"""


# ------------------------------------------------------------------
# 计数
# ------------------------------------------------------------------
def record(word, tags):
    """每次批改后调用：给这批错误标签各记一次命中。"""
    w = (word or "").strip().lower()
    if not w or not tags:
        return
    try:
        conn = get_conn()
        for t in tags:
            t = str(t or "").strip()[:12]
            if not t:
                continue
            row = conn.execute(
                "SELECT id, times FROM weak_hits WHERE word=? AND tag=?", (w, t)).fetchone()
            if row:
                conn.execute("UPDATE weak_hits SET times=?, last_at=? WHERE id=?",
                             (int(row["times"] or 0) + 1, ts(), row["id"]))
            else:
                conn.execute(
                    "INSERT INTO weak_hits (word, tag, times, last_at) VALUES (?,?,1,?)",
                    (w, t, ts()))
        conn.commit()
        conn.close()
    except Exception as e:
        print("[weakness] 记录命中失败: %s" % e)


def pending(word):
    """这个词有没有「该讲但还没讲」的反复错误？返回 {tag, hits} 或 None。

    只看累计次数 ≥ 阈值、且还没讲过的标签；讲过的由 weak_explanations 命中，
    不再重复调 AI（只触发一次，§4.2）。
    """
    w = (word or "").strip().lower()
    if not w:
        return None
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT h.tag, h.times FROM weak_hits h"
            " LEFT JOIN weak_explanations e ON e.word=h.word AND e.tag=h.tag"
            " WHERE h.word=? AND h.times>=? AND e.id IS NULL"
            " ORDER BY h.times DESC, h.last_at DESC LIMIT 1",
            (w, HIT_THRESHOLD)).fetchone()
        conn.close()
        if row:
            return {"tag": row["tag"], "hits": int(row["times"] or 0)}
    except Exception as e:
        print("[weakness] 查询待讲失败: %s" % e)
    return None


# ------------------------------------------------------------------
# 讲解
# ------------------------------------------------------------------
def cached(word, tag):
    """已经讲过的直接取，不再调 AI。"""
    w, t = (word or "").strip().lower(), (tag or "").strip()
    if not w or not t:
        return ""
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT explain FROM weak_explanations WHERE word=? AND tag=?", (w, t)).fetchone()
        conn.close()
        return (row["explain"] or "") if row else ""
    except Exception:
        return ""


def explain(word, tag, hits=None):
    """取讲解：有缓存直接回；没有就调一次 mini 生成并落库（只讲一次）。

    返回 (text, err)。text 为 '' 且 err 为 None 表示「还没到该讲的时候」。
    """
    w, t = (word or "").strip().lower(), (tag or "").strip()
    if not w or not t:
        return "", "缺少参数"
    old = cached(w, t)
    if old:
        return old, None

    # 没到阈值就不讲 —— 单次偶发错误不值得打扰，也不该烧 token
    if not _reached(w, t):
        return "", None

    if not ai_correct.ai_enabled():
        return "", "服务端还没有配置 ARK_API_KEY，薄弱项讲解暂不可用。"

    user = "目标词：%s\n反复犯的错误类型：%s" % (w, t)
    if hits:
        user += "\n这个错他/她已经犯了 %d 次。" % int(hits)
    data, err = ai_correct.ark_json(_SYS, user, max_tokens=300,
                                    temperature=0.5, tag="weakness")
    if err:
        return "", err
    text = str(data.get("explain") or "").strip()[:200]
    if not text:
        return "", "AI 没给出讲解内容，请稍后再试。"
    _save(w, t, text, hits)
    return text, None


def _reached(word, tag):
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT times FROM weak_hits WHERE word=? AND tag=?", (word, tag)).fetchone()
        conn.close()
        return bool(row and int(row["times"] or 0) >= HIT_THRESHOLD)
    except Exception:
        return False


def _save(word, tag, text, hits=None):
    try:
        conn = get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO weak_explanations (word, tag, explain, hits, created_at)"
            " VALUES (?,?,?,?,?)",
            (word, tag, text, int(hits or 0), ts()))
        conn.commit()
        conn.close()
    except Exception as e:
        print("[weakness] 保存讲解失败: %s" % e)
