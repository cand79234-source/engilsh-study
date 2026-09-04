"""
M2 + M3 回归测试

M2 — 5 星不是终态：
  - 刚到 5 星（冷却期内）→ 不进造句计划（避免重复练熟的）
  - 超过 FIVE_STAR_RECYCLE_DAYS 天没主动输出 → 回流计划池（防真忘）
  - 回流后写错 → 掉星（能回落，不是单向上涨）

M3 — 组合题星级只给「句子里真的用到」的词：
  - 组合 word 名单里没用到的词，星级不变（不白涨星/无辜扣星）
  - 用到的词正常 ±1
  - 屈折变化（work→works/worked/working）算命中
  - 但不是同一个词的派生（real→really、work→network）不算
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.normpath(os.path.join(HERE, "..", "backend"))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import srs                      # noqa: E402
import services as svc          # noqa: E402
import main                     # noqa: E402
from db import get_conn, ts     # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

_failed = False


def t(name, ok, detail=""):
    global _failed
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {detail}")
    if not ok:
        _failed = True


def _set_star(word, stars, days_ago=0):
    """直接把某词的五星设成指定值，并伪造 last_at 为 N 天前。"""
    conn = get_conn()
    w = word.strip().lower()
    when = (datetime.now() - timedelta(days=days_ago)).isoformat()
    conn.execute(
        "INSERT INTO word_output (word, stars, total_attempts, last_result,"
        " last_score, first_at, last_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(word) DO UPDATE SET stars=excluded.stars,"
        " last_at=excluded.last_at, updated_at=excluded.updated_at",
        (w, stars, 1, "PASS", 100, when, when, when))
    conn.commit()
    conn.close()


def _del(word):
    conn = get_conn()
    conn.execute("DELETE FROM word_output WHERE word=?", (word.strip().lower(),))
    conn.commit()
    conn.close()


print("=== M2: 5 星复练冷却 ===")

# 刚到 5 星（0 天前）→ 冷却中，不该到期复练
_set_star("freshword", 5, days_ago=0)
due = srs.star_recycle_due({"freshword"}, days=svc.FIVE_STAR_RECYCLE_DAYS)
t("M2-1 刚到5星不回流（冷却中）", "freshword" not in due, f"due={due}")

# 超过冷却期（比如 20 天前）→ 应该回流
_set_star("staleword", 5, days_ago=svc.FIVE_STAR_RECYCLE_DAYS + 6)
due = srs.star_recycle_due({"staleword"}, days=svc.FIVE_STAR_RECYCLE_DAYS)
t("M2-2 超过冷却期的5星回流", "staleword" in due, f"due={due}")

# 未记录的词不算到期复练（不编造数据）
_del("neverword")
due = srs.star_recycle_due({"neverword"}, days=svc.FIVE_STAR_RECYCLE_DAYS)
t("M2-3 未记录的词不算复练", "neverword" not in due, f"due={due}")

# 端到端：build_sentence_plan 里，冷却中的 5 星词被剔除、到期的保留
today_new = [{"word": "freshword", "meaning": "x"}, {"word": "staleword", "meaning": "y"}]
plan = svc.build_sentence_plan(today_new, [], "", 0, 1, 1)
basic_words = " ".join(b.get("word", "") for b in plan["basic"]).lower()
t("M2-4 计划里不含冷却中的5星词", "freshword" not in basic_words, f"basic={basic_words}")
t("M2-5 计划里含到期回流的5星词", "staleword" in basic_words, f"basic={basic_words}")

# 关键：5 星能回落（写错掉星）
before = srs.word_stars("staleword")["stars"]
srs.update_output_star("staleword", "NEEDS_REVIEW", 40)
after = srs.word_stars("staleword")["stars"]
t("M2-6 5星写错能掉到4星", before == 5 and after == 4, f"{before} -> {after}")

# 0 星下限、5 星上限仍然有效（钳制没被破坏）
_set_star("clampword", 0, days_ago=0)
srs.update_output_star("clampword", "NEEDS_REVIEW", 0)
t("M2-7 0星不会变负", srs.word_stars("clampword")["stars"] == 0)
_set_star("clampword2", 5, days_ago=0)
srs.update_output_star("clampword2", "PASS", 100)
t("M2-8 5星不会超过5", srs.word_stars("clampword2")["stars"] == 5)

print()
print("=== M3: 只给用到的词加星 ===")

# 造一个干净的测试函数：直接测 _word_used_in
cases = [
    ("I love reading books every day", "reading", True),
    ("I love reading books every day", "apple", False),
    ("She works at a company", "work", True),
    ("He worked hard", "work", True),
    ("They are working now", "work", True),
    ("The network is down", "work", False),
    ("I really like it", "real", False),
]
for sent, w, exp in cases:
    got = main._word_used_in(sent, w)
    t(f"M3 匹配 {w!r}", got == exp, f"{sent!r} -> {got} (expect {exp})")

# 组合题端到端：word 名单 3 个词，句子只用了 1 个
for w in ["joypure", "griefpure", "hopepure"]:
    _del(w)
srs.update_output_star("joypure", "PASS", 100)   # 先给 joypure 1 星
base_joy = srs.word_stars("joypure")["stars"]

# 模拟组合题批改：名单 3 词，句子只用了 joypure
from ai_service import analyze  # noqa: E402
sentence = "I feel joypure when I see my friends."
word_field = "joypure griefpure hopepure"
used = [w for w in word_field.split() if main._word_used_in(sentence, w)]
t("M3-E1 只识别出用到的1个词", used == ["joypure"], f"used={used}")

for w in used:
    srs.update_output_star(w, "PASS", 100)
t("M3-E2 用到的词正常加星",
  srs.word_stars("joypure")["stars"] == base_joy + 1,
  f"{base_joy} -> {srs.word_stars('joypure')['stars']}")
t("M3-E3 没用到的词星级不受影响（无记录）",
  srs.word_stars("griefpure") is None and srs.word_stars("hopepure") is None,
  f"grief={srs.word_stars('griefpure')} hope={srs.word_stars('hopepure')}")

print()
if _failed:
    print("FAILED")
    sys.exit(1)
print("ALL PASSED")
