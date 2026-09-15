# -*- coding: utf-8 -*-
"""导入材料里的「Mini Scenario」解析与入库。

背景（用户反馈）：
用户粘贴的整周材料，每个词后面除了 3 条例句，还跟着 3 条英文 mini scenario：

    Mini Scenario 1：
    You meet your new colleague at the office and say hello.

    Mini Scenario 2：
    ...

    1. colleague /ˈkɒliːɡ/ — 同事
    ...

改动前的问题：`Mini Scenario 1：` 这行是**纯标签**，解析器不认识，被丢进
skipped；而它下面的那句英文因为「整行英文 + 以句号结尾」被当成**第 4 条例句**，
一起塞进 examples。结果：
  - 例句区凭空多出 3 句没中译的英文；
  - 情景永远进不了库 → 造句页的 🔁「换一个情景」根本没东西可换。

这里锁住四件事：
  1. `Mini Scenario N：` 下面的英文算**情景**，不算例句；
  2. 例句只留材料里真正的 3 条（带中译）；
  3. 情景随词一起入库（weeks.vocab_json → day_items → 造句计划）；
  4. 老材料（没有 Mini Scenario）行为完全不变，不能因为改动把老导入搞崩。
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "backend"))


TXT_SCENE = """第1周｜职场第一天｜40词

Day 1｜具体场景/剧情

1. colleague /ˈkɒliːɡ/ — 同事

I started working with a new colleague this week.
我这周开始和一个新同事一起工作。
My colleague helped me a lot today.
我的同事今天帮了我很多。
I had lunch with my colleague yesterday.
我昨天和同事一起吃了午饭。

固定搭配：「work with a colleague」和同事一起工作；「a new colleague」新同事

Mini Scenario 1：
You meet your new colleague at the office and say hello.

Mini Scenario 2：
Your colleague asks you for help with a report.

Mini Scenario 3：
You have lunch with your colleague and learn about his hobbies.

2. company /ˈkʌmpəni/ — 公司

I work for a small company in Beijing.
我在北京一家小公司工作。

固定搭配：「work for a company」为公司工作

Mini Scenario 1：
You tell a friend about the company you work for.

Mini Scenario 2：
You are at a job interview and describe your last company.

Mini Scenario 3：
Your company moves to a new office and you talk about it.
"""

# 老材料：没有 Mini Scenario，改动后行为必须一模一样
TXT_OLD = """第2周｜旧格式

第1组｜职场人物

colleague — 同事

colleague的英文例句.

company — 公司
"""


@pytest.fixture()
def isolated_db():
    """每个测试一个独立 SQLite 文件，并建表。

    之前这里只设了 EOS_DB 没建表，于是 import_rich_week 一写
    dictionary 就 "no such table" —— 测试自己把自己搞挂了。
    """
    d = tempfile.mkdtemp()
    os.environ["EOS_DB"] = os.path.join(d, "t.db")
    import db
    db.init_db()
    yield


# ---------------- ① 解析 ----------------
def test_scenes_parsed_not_as_examples():
    import importer
    p = importer.parse_import(TXT_SCENE)
    assert len(p["groups"]) == 1, "场景行不能被误认成新的一天"
    w = p["groups"][0]["words"][0]
    assert w["word"] == "colleague"
    # 例句只留材料里那 3 条（带中译）
    assert len(w["examples"]) == 3, w["examples"]
    assert all(e["translation"] for e in w["examples"]), "3 条例句都该配上中译"
    # 3 条情景单独拿出来
    sc = w.get("scenes") or []
    assert len(sc) == 3, sc
    assert sc[0]["text"].startswith("You meet your new colleague")
    assert sc[2]["text"].startswith("You have lunch")
    assert p["skipped"] == [], p["skipped"]


def test_every_word_gets_its_own_scenes():
    import importer
    p = importer.parse_import(TXT_SCENE)
    ws = p["groups"][0]["words"]
    assert [w["word"] for w in ws] == ["colleague", "company"]
    for w in ws:
        assert len(w.get("scenes") or []) == 3, w
    # 第二个词的情景必须是自己的，不能串到第一个词上
    assert ws[1]["scenes"][0]["text"].startswith("You tell a friend")


def test_scene_label_variants():
    import importer
    for label in ("Mini Scenario 1：", "Mini Scenario 1:", "Scenario 1：",
                  "场景1：", "情景 1：", "Mini Scene 2："):
        txt = ("第1周｜t\n\nDay 1｜d\n\n1. test — 测试\n\n"
               "This is a test sentence.\n这是一个测试句。\n\n"
               "%s\nYou go to the shop and buy some milk.\n" % label)
        p = importer.parse_import(txt)
        w = p["groups"][0]["words"][0]
        assert len(w["examples"]) == 1, (label, w["examples"])
        assert len(w.get("scenes") or []) == 1, (label, w)


def test_normal_sentence_with_word_scenario_not_mistaken():
    """含 scenario 一词的普通句子不能被误判成情景标签行。"""
    import importer
    txt = ("第1周｜t\n\nDay 1｜d\n\n1. test — 测试\n\n"
           "In this scenario, you need to talk to your manager about it.\n"
           "在这个场景里，你需要跟经理谈一谈。\n")
    p = importer.parse_import(txt)
    w = p["groups"][0]["words"][0]
    assert len(w["examples"]) == 1
    assert not (w.get("scenes") or [])


def test_old_material_unchanged():
    """没有 Mini Scenario 的老材料：不报错、不产生场景、词照常解析。"""
    import importer
    p = importer.parse_import(TXT_OLD)
    words = [w["word"] for g in p["groups"] for w in g["words"]]
    assert "colleague" in words and "company" in words
    for g in p["groups"]:
        for w in g["words"]:
            assert not (w.get("scenes") or [])


# ---------------- ② 入库 ----------------
def test_scenes_persisted_to_week(isolated_db):
    import weekimport
    import services as svc
    r = weekimport.import_rich_week(TXT_SCENE, forced_stage=0)
    assert r["ok"], r
    wk = svc.get_week(0, 1) or {}
    vocab = {v["word"]: v for v in (wk.get("vocab") or [])}
    sc = vocab["colleague"].get("scenes") or []
    assert len(sc) == 3, sc
    assert sc[0]["text"].startswith("You meet your new colleague")
    assert all(s.get("n") for s in sc), sc


def test_scenes_reach_day_items_and_plan(isolated_db):
    """最终要能到造句计划里 —— 前端的 🔁 靠它本地轮换。"""
    import weekimport
    import services as svc
    import main
    from fastapi.testclient import TestClient
    assert weekimport.import_rich_week(TXT_SCENE, forced_stage=0)["ok"]
    c = TestClient(main.app)
    res = c.get("/api/today").json()
    words = {w["word"]: w for w in res["words"]}
    assert len(words["colleague"]["scenes"]) == 3
    basic = res["sentence_plan"]["basic"]
    for b in basic:
        assert len(b.get("scenes") or []) == 3, b


def test_normalize_scenes_tolerant():
    """归一化函数要能吃掉各种脏数据，且丢掉空壳（免得 🔁 转到空场景）。"""
    import services as svc
    assert svc.normalize_scenes({"scenes": [{"n": "1", "text": "a"},
                                            {"text": ""},
                                            "b"]}) == [{"n": "1", "text": "a"},
                                                       {"n": "2", "text": "b"}]
    assert svc.normalize_scenes({"scenario": "single"}) == [{"n": "1", "text": "single"}]
    assert svc.normalize_scenes({}) == []
    assert svc.normalize_scenes({"scenes": []}) == []
