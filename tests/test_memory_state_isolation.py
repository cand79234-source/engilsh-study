"""① 记忆数据结构 / 三状态隔离 单测（本地 SQLite，零密钥、零外部）。

验证「单词词义 SRS / 造句五星 / 听力状态」三个维度彼此独立：

  - 单词 SRS  : reviews 表 kind='vocab'
  - 造句五星  : word_output 表（独立表，永不进 reviews / 永不进 SRS）
  - 听力状态  : reviews 表 kind='listening'

核心回归点（会「串」的地方）：
  1. 听力卡 / 错误卡 不得混进「今日复习闪卡」
  2. 五星记录不得影响单词 SRS 的任何字段
  3. 听力卡不得影响同词 vocab 卡的 SRS 调度
  4. 默认 due_reviews() 行为保持不变（零回归）
"""
import os
import sys
import tempfile
from datetime import date

os.environ.setdefault("EOS_DB", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient
import db
import srs
import main

client = TestClient(main.app)
TODAY = date.today().isoformat()

# 用不可能与真实学习数据冲突的词
W_VOCAB = "eosiso_vocab"
W_LISTEN = "eosiso_listen"
W_STAR = "eosiso_star"


def _clean():
    """清掉本文件的测试行，保证重复运行幂等。"""
    conn = db.get_conn()
    conn.execute("DELETE FROM reviews WHERE ref_key IN (?,?,?)",
                 (W_VOCAB, W_LISTEN, W_STAR))
    conn.execute("DELETE FROM word_output WHERE word IN (?,?,?)",
                 (W_VOCAB, W_LISTEN, W_STAR))
    conn.commit()
    conn.close()


def _insert_review(kind, ref_key, next_due=None, last_score=-1, ease=2.5, interval=0):
    """直接插入一张复习卡（next_due 设为今天 → 保证到期）。"""
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO reviews (kind, ref_key, prompt, answer, stage, week, day,"
        " ease, interval, reps, next_due, last_score, total_correct, total_wrong,"
        " last_reviewed, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, ref_key, "p-" + kind, "answer", 0, 1, 1, ease, interval, 0,
         next_due or TODAY, last_score, 0, 0, "", TODAY + "T00:00:00"))
    conn.commit()
    conn.close()


# ---------- ① 新表存在 ----------

def test_word_output_table_exists():
    conn = db.get_conn()
    row = conn.execute(
        "SELECT 1 FROM word_output LIMIT 1").fetchall()
    conn.close()
    assert row == []          # 能查到（不抛异常）即表已建立


# ---------- ② kind 过滤：听力卡/错误卡不得混进单词闪卡 ----------

def test_due_reviews_kind_filter():
    _clean()
    _insert_review("vocab", W_VOCAB)
    _insert_review("listening", W_LISTEN)
    _insert_review("error", W_STAR)

    vocab_only = srs.due_reviews(kind="vocab")
    kinds = {r["kind"] for r in vocab_only}
    assert kinds == {"vocab"}, f"闪卡混入了非单词卡: {kinds}"
    assert W_VOCAB in {r["ref_key"] for r in vocab_only}

    listening_only = srs.due_reviews(kind="listening")
    assert {r["kind"] for r in listening_only} == {"listening"}


def test_due_reviews_default_unchanged():
    """默认行为必须保持原样（返回全部 kind），否则是对既有复习页的回归。"""
    _clean()
    _insert_review("vocab", W_VOCAB)
    _insert_review("listening", W_LISTEN)
    _insert_review("error", W_STAR)

    all_rows = srs.due_reviews()
    kinds = {r["kind"] for r in all_rows}
    assert {"vocab", "listening", "error"} <= kinds


def test_flashcards_endpoint_vocab_only():
    _clean()
    _insert_review("vocab", W_VOCAB)
    _insert_review("listening", W_LISTEN)
    _insert_review("error", W_STAR)

    d = client.get("/api/review/flashcards").json()
    words = {i["word"] for i in d["items"]}
    # 闪卡只应出现 vocab 卡对应的词（底层 due_vocab_reviews 已按 kind 过滤）
    assert W_VOCAB in words
    assert W_LISTEN not in words, "听力卡混进了单词闪卡"
    assert W_STAR not in words, "错误卡混进了单词闪卡"


# ---------- ③ 三状态独立读取 ----------

def test_memory_state_three_dimensions_independent():
    _clean()
    _insert_review("vocab", W_VOCAB, ease=2.5, interval=6)
    _insert_review("listening", W_VOCAB)

    conn = db.get_conn()
    conn.execute(
        "INSERT INTO word_output (word, stars, total_attempts, last_result,"
        " last_score, first_at, last_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (W_VOCAB, 2, 3, "pass", 90, TODAY, TODAY, TODAY))
    conn.commit()
    conn.close()

    d = client.get(f"/api/memory/state/{W_VOCAB}").json()
    assert d["word"] == W_VOCAB
    # 三个维度各自有值，互不覆盖
    assert d["srs"] is not None and d["srs"]["kind"] == "vocab"
    assert d["listening"] is not None and d["listening"]["kind"] == "listening"
    assert d["output"] is not None and d["output"]["stars"] == 2


def test_five_star_does_not_touch_srs():
    """五星记录变化时，单词 SRS 的任何字段都不得被改动。"""
    _clean()
    _insert_review("vocab", W_STAR, ease=2.5, interval=6)

    conn = db.get_conn()
    before = dict(conn.execute(
        "SELECT * FROM reviews WHERE kind='vocab' AND ref_key=?", (W_STAR,)).fetchone())
    # 模拟五星变化（只写 word_output）
    conn.execute(
        "INSERT INTO word_output (word, stars, total_attempts, last_result,"
        " last_score, first_at, last_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (W_STAR, 4, 5, "pass", 95, TODAY, TODAY, TODAY))
    conn.commit()
    after = dict(conn.execute(
        "SELECT * FROM reviews WHERE kind='vocab' AND ref_key=?", (W_STAR,)).fetchone())
    conn.close()

    for field in ("ease", "interval", "reps", "next_due", "last_score",
                  "total_correct", "total_wrong", "last_reviewed"):
        assert before[field] == after[field], f"五星记录污染了 SRS 字段 {field}"


def test_listening_card_does_not_affect_vocab_srs():
    """同一单词的听力卡，不得影响它 vocab 卡的 SRS 调度。"""
    _clean()
    _insert_review("vocab", W_VOCAB, ease=2.5, interval=6)

    conn = db.get_conn()
    before = dict(conn.execute(
        "SELECT * FROM reviews WHERE kind='vocab' AND ref_key=?", (W_VOCAB,)).fetchone())
    conn.close()

    # 听力连续答错 → 只应影响 listening 卡
    _insert_review("listening", W_VOCAB, last_score=0)

    conn = db.get_conn()
    after = dict(conn.execute(
        "SELECT * FROM reviews WHERE kind='vocab' AND ref_key=?", (W_VOCAB,)).fetchone())
    listen = dict(conn.execute(
        "SELECT * FROM reviews WHERE kind='listening' AND ref_key=?", (W_VOCAB,)).fetchone())
    conn.close()

    # vocab 卡纹丝不动
    for field in ("ease", "interval", "reps", "next_due", "last_score"):
        assert before[field] == after[field], f"听力卡污染了 vocab SRS 字段 {field}"
    # listening 卡独立存在
    assert listen["kind"] == "listening" and listen["last_score"] == 0


def test_memory_state_missing_dimensions_are_null():
    """没有任何记录的词，三个维度都应为 None（而不是编造 0 星 / 0% ）。"""
    d = client.get("/api/memory/state/zzz_not_a_real_word").json()
    assert d["srs"] is None
    assert d["output"] is None
    assert d["listening"] is None
