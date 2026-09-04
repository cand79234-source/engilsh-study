"""④ 薄弱项 单测（本地 SQLite，零密钥、零外部）。

验证 /api/weakness 把「错误类型 + 主动输出偏弱词」合并诊断：
  - 低星词出现在 low_star_words；高星词不出现
  - 有活动的错误类型出现在 error_types
  - 存在薄弱项时给出针对性训练建议（规则生成，无 AI）
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


def _seed():
    conn = db.get_conn()
    # 幂等：先清掉本文件测试行（多个测试共用同一库）
    conn.execute("DELETE FROM word_output WHERE word IN ('eoswk_low','eoswk_high')")
    conn.execute("DELETE FROM errors WHERE word='school' OR error_text='go school'")
    conn.commit()
    # 低星词：主动输出偏弱
    conn.execute(
        "INSERT INTO word_output (word, stars, total_attempts, last_result, last_score,"
        " first_at, last_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("eoswk_low", 1, 2, "needs_review", 60, TODAY, TODAY, TODAY))
    # 高星词：不该进薄弱项
    conn.execute(
        "INSERT INTO word_output (word, stars, total_attempts, last_result, last_score,"
        " first_at, last_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("eoswk_high", 5, 6, "pass", 95, TODAY, TODAY, TODAY))
    # 一个近30天有活动的错误
    conn.execute(
        "INSERT INTO errors (error_type, original, corrected, explanation, created_at,"
        " word, error_text, sentence_text, times, first_at, last_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("介词", "go school", "go to school", "缺少 to", TODAY + "T10:00:00",
         "school", "go school", "I go school", 3, TODAY, TODAY))
    conn.commit()
    conn.close()


def test_weakness_low_star_shown_high_excluded():
    _seed()
    d = client.get("/api/weakness").json()
    low = [w["word"] for w in d["low_star_words"]]
    assert "eoswk_low" in low
    assert "eoswk_high" not in low        # 5 星不进薄弱项


def test_weakness_error_type_present():
    _seed()
    d = client.get("/api/weakness").json()
    types = [e["type"] for e in d["error_types"]]
    assert "介词" in types


def test_weakness_recommendations_generated():
    _seed()
    d = client.get("/api/weakness").json()
    # 至少有一个针对性训练建议（低星词 或 近期错误）
    assert len(d["recommendations"]) >= 1
    labels = [r["label"] for r in d["recommendations"]]
    assert any("eoswk_low" in l or "介词" in l for l in labels)
