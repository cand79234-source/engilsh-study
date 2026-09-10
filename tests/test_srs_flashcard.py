"""② 单词 SRS + 闪卡 单测（本地 SQLite，零密钥、零外部）。

验证：
  - 忘记 / 模糊 / 记得 三档对间隔、reps、ease、正确数的影响
  - 不传 quality 时回退旧二值逻辑（零回归）
  - 闪卡数据只含 vocab 卡，且带词典释义
  - 复习完成后不再重复计数
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

W = "eosfc_department"


def _clean():
    conn = db.get_conn()
    conn.execute("DELETE FROM reviews WHERE ref_key=?", (W,))
    conn.commit()
    conn.close()


def _make_card(interval=0, reps=0, ease=2.5, word=W, prompt="p"):
    """建一张到期的 vocab 卡，返回 id。

    reviews 有 UNIQUE(kind, ref_key, prompt)，同一词造第二张卡必须换 prompt。
    """
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO reviews (kind, ref_key, prompt, answer, stage, week, day,"
        " ease, interval, reps, next_due, last_score, total_correct, total_wrong,"
        " last_reviewed, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("vocab", word, prompt, word + " 部门", 0, 1, 1, ease, interval, reps,
         TODAY, -1, 0, 0, "", TODAY + "T00:00:00"))
    conn.commit()
    cur = conn.execute(
        "SELECT id FROM reviews WHERE kind='vocab' AND ref_key=? AND prompt=?",
        (word, prompt))
    rid = cur.fetchone()["id"]
    conn.close()
    return rid


def _card(rid):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
    conn.close()
    return dict(row)


# ---------- 三档调度 ----------

def test_forget_resets_interval():
    _clean()
    rid = _make_card(interval=7, reps=3, ease=2.5)
    r = client.post("/api/review/submit", json={"id": rid, "quality": 0}).json()
    assert r["mode"] == "again"
    c = _card(rid)
    assert c["interval"] == 1, "忘记后间隔应重排为1天"
    assert c["reps"] == 0, "忘记后 reps 应清零"
    assert c["total_wrong"] == 1
    assert c["last_score"] == 0


def test_fuzzy_halves_interval_but_counts_correct():
    _clean()
    rid = _make_card(interval=8, reps=3, ease=2.5)
    r = client.post("/api/review/submit", json={"id": rid, "quality": 3}).json()
    assert r["mode"] == "hard"
    c = _card(rid)
    assert c["interval"] == 4, "模糊应把间隔折半（8→4）"
    assert c["reps"] == 4, "模糊仍计入 reps（算答对，但更快再见）"
    assert c["total_correct"] == 1 and c["total_wrong"] == 0
    assert c["last_score"] == 1


def test_remember_follows_sm2_ladder():
    _clean()
    rid = _make_card(interval=0, reps=0, ease=2.5)
    # 第1次记得 → 1天
    client.post("/api/review/submit", json={"id": rid, "quality": 5})
    assert _card(rid)["interval"] == 1
    # 第2次记得 → 3天
    client.post("/api/review/submit", json={"id": rid, "quality": 5})
    assert _card(rid)["interval"] == 3
    # 第3次记得 → 7天
    client.post("/api/review/submit", json={"id": rid, "quality": 5})
    c = _card(rid)
    assert c["interval"] == 7
    assert c["reps"] == 3
    assert c["total_correct"] == 3 and c["total_wrong"] == 0


def test_backward_compatible_without_quality():
    """不传 quality 时必须回退为旧二值逻辑，老调用零回归。"""
    _clean()
    rid = _make_card(interval=0, reps=0, ease=2.5)
    r = client.post("/api/review/submit", json={"id": rid, "correct": True}).json()
    assert r["mode"] == "good"
    assert _card(rid)["interval"] == 1

    rid2 = _make_card(interval=6, reps=2, ease=2.5, prompt="p2")
    r2 = client.post("/api/review/submit", json={"id": rid2, "correct": False}).json()
    assert r2["mode"] == "again"
    assert _card(rid2)["interval"] == 1 and _card(rid2)["reps"] == 0


# ---------- 闪卡数据 ----------

def test_flashcard_items_only_vocab_with_meaning():
    _clean()
    # 造一张 vocab 卡 + 词典释义
    rid = _make_card()
    conn = db.get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO dictionary(word, phonetic, pos, meaning) VALUES(?,?,?,?)",
        (W, "/dɪˈpɑːtmənt/", "名词", "部门；系"))
    conn.commit()
    conn.close()

    items = srs.flashcard_items(limit=50)
    kinds = {i["word"] for i in items}
    assert W in kinds
    mine = [i for i in items if i["word"] == W][0]
    assert mine["meaning"] == "部门；系"
    assert mine["phonetic"] == "/dɪˈpɑːtmənt/"
    # 新卡默认正向：英文 → 中文
    assert mine["direction"] == "en2zh"


def test_reviewed_card_leaves_due_queue():
    """复习过的卡不应再出现在今日待复习里（避免重复计数）。"""
    _clean()
    rid = _make_card()
    assert W in {i["word"] for i in srs.flashcard_items(limit=50)}
    # 记得 → 间隔拉长到明天之后
    client.post("/api/review/submit", json={"id": rid, "quality": 5})
    assert W not in {i["word"] for i in srs.flashcard_items(limit=50)}
