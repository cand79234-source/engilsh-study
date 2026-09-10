"""听力导入 / 作答的端到端回归（pytest 风格）。

背景：听力两个接口用的是 `INSERT OR REPLACE`：

    INSERT OR REPLACE INTO listening_materials (...) VALUES (...)
    INSERT OR REPLACE INTO listening_progress  (...) VALUES (...)

这是 SQLite 专有语法 —— PostgreSQL 没有它，官方解析器直接报
`syntax error at or near "OR"`。而 db._tr()（SQLite→PG 方言翻译层）
原先只翻译 `INSERT OR IGNORE`，漏了 REPLACE，于是这两个接口在
Render/Neon（PG）上必定 500，前端只看到「解析失败」；
本地 SQLite 却一切正常，这类问题在本机怎么测都测不出来。

本测试锁两层：
  1. 语义层：本地 SQLite 跑通导入→读回→覆盖→作答→累加（REPLACE 语义正确）
  2. 方言层：翻译后的 SQL 必须能被 PostgreSQL 官方解析器接受
"""
import os
import sys

import pytest

sys.path.insert(0, "backend")

DB = "/tmp/eos_listening_test.db"
if os.path.exists(DB):
    os.remove(DB)
os.environ["EOS_DB"] = DB
os.environ.pop("EOS_TOKEN", None)

import db                                    # noqa: E402
import main                                  # noqa: E402
from fastapi.testclient import TestClient    # noqa: E402

client = TestClient(main.app)

STAGE, WEEK, DAY = 1, 3, 2

TEXT_V1 = """<<<LISTENING v1>>>
TITLE: At the Airport
<<<DIALOGUE>>>
A: Excuse me, where is Gate 12?
B: It's over there, next to the duty-free shop.
<<<PASSAGE>>>
The airport was crowded this morning.
<<<Q1>>>
Question: Where is Gate 12?
A. Next to the shop
B. Behind the counter
C. On the second floor
D. Outside the terminal
ANSWER: A
<<<END>>>
"""

TEXT_V2 = TEXT_V1.replace("At the Airport", "At the Station")


def test_import_returns_ok():
    r = client.post("/api/listening/import",
                    json={"stage": STAGE, "week": WEEK, "day": DAY, "text": TEXT_V1})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("ok") is True, data
    assert data.get("replaced") is False, "首次导入不该是覆盖"


def test_readback_matches_import():
    r = client.get(f"/api/listening/{STAGE}/{WEEK}/{DAY}")
    assert r.status_code == 200, r.text
    mat = r.json()["material"]
    assert mat, "导入后应能读回材料"
    assert mat["title"] == "At the Airport", mat["title"]
    assert len(mat["dialogue"]) == 2, mat["dialogue"]
    assert len(mat["questions"]) == 1, mat["questions"]
    assert mat["questions"][0]["answer"] == "A"


def test_reimport_replaces_in_place():
    """重复导入同一 (stage,week,day) 必须覆盖旧内容，而不是报错或追加。"""
    r = client.post("/api/listening/import",
                    json={"stage": STAGE, "week": WEEK, "day": DAY, "text": TEXT_V2})
    assert r.status_code == 200, r.text
    assert r.json().get("replaced") is True, "第二次导入应识别为覆盖"

    mat = client.get(f"/api/listening/{STAGE}/{WEEK}/{DAY}").json()["material"]
    assert mat["title"] == "At the Station", f"REPLACE 语义丢失：{mat['title']}"

    # 表里仍只有一行（REPLACE 不是 INSERT 追加）
    conn = db.get_conn()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM listening_materials "
            "WHERE stage=? AND week=? AND day=?", (STAGE, WEEK, DAY)).fetchone()["c"]
    finally:
        conn.close()
    assert n == 1, f"同一天应只有 1 条材料，实际 {n}"


def test_answer_accumulates_parts():
    """Part A / Part B 分别提交，分数要累加而不是互相覆盖。"""
    for part, correct, total in (("A", 3, 5), ("B", 4, 5), ("C", 2, 4)):
        r = client.post("/api/listening/answer",
                        json={"stage": STAGE, "week": WEEK, "day": DAY,
                              "part": part, "correct": correct, "total": total})
        assert r.status_code == 200, r.text

    prog = client.get(f"/api/listening/{STAGE}/{WEEK}/{DAY}").json()["progress"]
    assert prog["listening_done"] == 3 + 4 + 2, prog
    assert prog["listening_total"] == 5 + 5 + 4, prog
    assert set(prog["parts"]) == {"A", "B", "C"}, prog["parts"]


def test_answer_same_part_twice_overwrites():
    """同一 Part 重做：覆盖该 Part 的分数，总数不重复累加。"""
    client.post("/api/listening/answer",
                json={"stage": STAGE, "week": WEEK, "day": DAY,
                      "part": "A", "correct": 5, "total": 5})
    prog = client.get(f"/api/listening/{STAGE}/{WEEK}/{DAY}").json()["progress"]
    assert prog["parts"]["A"] == {"correct": 5, "total": 5}, prog["parts"]
    assert prog["listening_done"] == 5 + 4 + 2, prog
    assert prog["listening_total"] == 5 + 5 + 4, prog


def test_bad_text_is_rejected_not_500():
    r = client.post("/api/listening/import",
                    json={"stage": STAGE, "week": WEEK, "day": DAY, "text": ""})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is False, "空文本应返回 ok=false，而不是 500"


# ------------------------- 方言层（PostgreSQL） -------------------------

def test_translated_sql_accepted_by_postgres():
    """把这两个接口真实的 SQL 过一遍方言翻译，PG 官方解析器必须接受。"""
    pglast = pytest.importorskip("pglast", reason="需要 pglast（PostgreSQL 官方解析器）")

    sqls = [
        ("INSERT OR REPLACE INTO listening_materials "
         "(stage, week, day, title, dialogue_json, passage, questions_json, created_at) "
         "VALUES (?,?,?,?,?,?,?,?)"),
        ("INSERT OR REPLACE INTO listening_progress "
         "(stage, week, day, listening_done, listening_total, parts_json, created_at) "
         "VALUES (?,?,?,?,?,?,?)"),
    ]
    for sql in sqls:
        filled = sql.replace("?", "'x'")
        out = db._tr(filled)
        assert "OR REPLACE" not in out, f"没被翻译，PG 上会语法错误：{out}"
        pglast.parse_sql(out)          # 抛异常即失败
        assert "ON CONFLICT (stage, week, day) DO UPDATE SET" in out, out
