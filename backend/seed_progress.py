# -*- coding: utf-8 -*-
"""一次性手动迁移脚本（阶段0 复习周专用）。

用法（需先有库；库由应用本身创建/初始化）：
    python3 backend/seed_progress.py --dry   # 只打印「复习周」计划，不动库
    python3 backend/seed_progress.py --yes   # 进度切到第4周 + 建阶段0 第1-3周复习卡

做了什么（--yes / --dry）：
    阶段0 第1-3周标记已学完，第4周(D1)一次性复习
    （progress→(0,4,1)，并为第1-3周全部词建 kind='vocab' 复习卡，next_due=今天）。

注意：
  - 本脚本**不接入任何启动钩子**，纯手动跑。
  - 阶段0 第1-12周要背的单词**不由本脚本灌入**（--stage0 自动灌词已移除）：
    由用户自己走「复制本周提示词 → 外部 AI 生成 → 导入」流程填进去。
  - 阶段1+ 的词同样走上述导入流程。
  - 复习卡从 weeks.vocab_json 里现存的词取；哪一周还没填词，那一词的复习卡就不会建。
"""
import sys
import json

from db import get_conn, ts, app_today

STAGE = 0
DONE_WEEKS = [1, 2, 3]
TARGET_WEEK = 4
TARGET_DAY = 1


def _word_list(conn):
    """从 weeks.vocab_json 取出阶段0 第1-3周的全部单词（去重，带释义）。"""
    out = []
    seen = set()
    for wk in DONE_WEEKS:
        row = conn.execute(
            "SELECT vocab_json FROM weeks WHERE stage=? AND week_no=?",
            (STAGE, wk)).fetchone()
        if not row or not row["vocab_json"]:
            continue
        try:
            vocab = json.loads(row["vocab_json"])
        except Exception:
            vocab = []
        for v in vocab:
            w = (v.get("word") or "").strip().lower()
            if w and w not in seen:
                seen.add(w)
                out.append((w, (v.get("meaning") or "").strip()))
    return out


def run(dry=False, yes=False):
    conn = get_conn()
    p = conn.execute("SELECT stage, week, day FROM progress WHERE id=1").fetchone()
    cur_week = p["week"] if p else None
    if p and p["week"] >= TARGET_WEEK:
        print("[seed_progress] 当前已是 W%d，无需重复执行，退出。" % cur_week)
        conn.close()
        return

    words = _word_list(conn)
    print("[seed_progress] 计划：阶段0 第1-3周共 %d 个单词建复习卡，进度切到 W%d D%d。"
          % (len(words), TARGET_WEEK, TARGET_DAY))

    if not yes and not dry:
        ans = input("确认执行？(y/N) ").strip().lower()
        if ans != "y":
            print("已取消。")
            conn.close()
            return

    if dry:
        conn.close()
        return

    today = app_today().isoformat()
    # 1) 进度切到第4周第1天
    if p:
        conn.execute(
            "UPDATE progress SET stage=?, week=?, day=?, updated_at=? WHERE id=1",
            (STAGE, TARGET_WEEK, TARGET_DAY, ts()))
    else:
        conn.execute(
            "INSERT INTO progress (id, stage, week, day, last_activity, updated_at)"
            " VALUES (1,?,?,?, 'vocab', ?)",
            (STAGE, TARGET_WEEK, TARGET_DAY, ts()))
    # 2) 建复习卡：next_due=今天 → 第4周即到期，一次性复习
    created = 0
    for w, meaning in words:
        prompt = w
        answer = ("%s %s" % (w, meaning)).strip()
        dup = conn.execute(
            "SELECT 1 FROM reviews WHERE kind='vocab' AND ref_key=? AND prompt=?",
            (w, prompt)).fetchone()
        if dup:
            continue
        conn.execute(
            "INSERT INTO reviews (kind, ref_key, prompt, answer, stage, week, day,"
            " interval, reps, next_due, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("vocab", w, prompt, answer, STAGE, TARGET_WEEK, TARGET_DAY, 1, 0,
             today, ts()))
        created += 1
    conn.commit()
    conn.close()
    print("[seed_progress] 完成：进度 → W%d D%d；新增复习卡 %d 张（共 %d 词，已存在则跳过）。"
          % (TARGET_WEEK, TARGET_DAY, created, len(words)))


if __name__ == "__main__":
    run(dry="--dry" in sys.argv, yes="--yes" in sys.argv)
