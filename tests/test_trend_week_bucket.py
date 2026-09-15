# -*- coding: utf-8 -*-
"""/api/errors/trend 的周桶口径回归（用户问题 3：周统计不对）。

要求：所有按「周」的统计统一为**中国自然周（周一 00:00:00 ~ 周日 23:59:59，Asia/Shanghai）**。
历史 bug：SQLite 用 strftime('%Y-W%W')、PG 用 to_char(IYYY-IW)，两者不是同一套周，
且 %W 不是自然周 → 本地(SQLite)与线上(PG)结果不一致。

运行: pytest tests/test_trend_week_bucket.py -q
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))


@pytest.fixture()
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("EOS_DB", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_TZ", "Asia/Shanghai")
    for m in [m for m in list(sys.modules)
              if m in ("db", "scenario", "services", "srs", "ai_correct",
                       "ai_service", "report", "main", "weakness")]:
        del sys.modules[m]
    import db
    db.init_db()
    import main
    from fastapi.testclient import TestClient
    yield db, TestClient(main.app)
    for m in [m for m in list(sys.modules)
              if m in ("db", "scenario", "services", "srs", "ai_correct",
                       "ai_service", "report", "main", "weakness")]:
        del sys.modules[m]
    try:
        os.remove(path)
    except OSError:
        pass


def _mk_errors(db, stamps):
    conn = db.get_conn()
    for ts in stamps:
        conn.execute(
            "INSERT INTO errors (word, error_type, level, original, corrected,"
            " explanation, fixed, created_at) VALUES (?,?,?,?,?,?,0,?)",
            ("work", "固定搭配", 1, "I go work.", "I go to work.", "x", ts))
    conn.commit()
    conn.close()


def test_week_bucket_is_natural_week(client):
    """周一~周日必须归同一周；周日不能跨进新周，周一开新周。

    2026-09-14 是周一，2026-09-20 是周日 → 同一自然周（ISO 周 2026-W38）。
    2026-09-13 是上周日（W37），2026-09-21 是下周一（W39）。
    """
    db, c = client
    _mk_errors(db, ["2026-09-13 10:00:00",   # 上周日
                    "2026-09-14 09:00:00",   # 本周一
                    "2026-09-16 09:00:00",   # 本周三
                    "2026-09-20 23:00:00",   # 本周日（23 点，仍属本周）
                    "2026-09-21 00:30:00"])  # 下周一（0 点半，属新周）
    r = c.get("/api/errors/trend?days=3650&bucket=week").json()
    weeks = {w["week"]: w["count"] for w in r["weeks"]}
    assert weeks.get("2026-W38") == 3, "周一~周日应同为 W38 共 3 条：%s" % weeks
    assert weeks.get("2026-W37") == 1, weeks
    assert weeks.get("2026-W39") == 1, weeks
    # 明确锁：周日 9/20 不能掉进 W39
    assert weeks.get("2026-W39") == 1, "9/20(周日) 被错误算进下一周"


def test_trend_does_not_crash_on_sqlite(client):
    """回归：旧实现里的 `created_at::timestamp` 是 PG 专属语法，
    SQLite 上直接 `unrecognized token` 500。现在必须能正常返回。"""
    db, c = client
    _mk_errors(db, ["2026-09-14 09:00:00"])
    r = c.get("/api/errors/trend?days=90&bucket=week")
    assert r.status_code == 200, r.text
    assert r.json().get("bucket") == "week", r.text
    r2 = c.get("/api/errors/trend?days=90&bucket=day")
    assert r2.status_code == 200, r2.text


def test_day_bucket(client):
    db, c = client
    _mk_errors(db, ["2026-09-14 09:00:00", "2026-09-14 20:00:00",
                    "2026-09-15 08:00:00"])
    r = c.get("/api/errors/trend?days=3650&bucket=day").json()
    days = {d["date"]: d["count"] for d in r["days"]}
    assert days == {"2026-09-14": 2, "2026-09-15": 1}, days
