"""③ 造句五星 单测（本地 SQLite，零密钥、零外部）。

验证「主动输出熟练度」与 SRS 完全独立：
  - 合格(Pass) → +1 星；明显错误/未完成(NEEDS_REVIEW) → -1 星；不确定(UNCERTAIN) → -1 星
  - 星级钳制在 0~5（不会负、不会超 5）
  - 达到 5 星的词不再进入常规造句计划（basic/upgrade/combo 都不再编排）
  - stars_map 只返回已记录的词，不编造 0 星
"""
import os
import sys
import tempfile

os.environ.setdefault("EOS_DB", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import srs
import services
import main
from fastapi.testclient import TestClient

client = TestClient(main.app)


def test_pass_increments_star():
    srs.update_output_star("eosfs_w1", "PASS", 95)
    assert srs.word_stars("eosfs_w1")["stars"] == 1
    srs.update_output_star("eosfs_w1", "PASS", 95)
    assert srs.word_stars("eosfs_w1")["stars"] == 2


def test_fail_decrements():
    srs.update_output_star("eosfs_w2", "PASS", 95)            # → 1
    srs.update_output_star("eosfs_w2", "NEEDS_REVIEW", 60)    # → 0
    assert srs.word_stars("eosfs_w2")["stars"] == 0


def test_uncertain_decrements():
    srs.update_output_star("eosfs_w3", "PASS", 95)            # → 1
    srs.update_output_star("eosfs_w3", "UNCERTAIN", 50)       # → 0
    assert srs.word_stars("eosfs_w3")["stars"] == 0


def test_clamp_max_5():
    for _ in range(7):
        srs.update_output_star("eosfs_w4", "PASS", 95)
    assert srs.word_stars("eosfs_w4")["stars"] == 5          # 封顶 5


def test_clamp_min_0():
    srs.update_output_star("eosfs_w5", "NEEDS_REVIEW", 40)
    srs.update_output_star("eosfs_w5", "NEEDS_REVIEW", 40)
    assert srs.word_stars("eosfs_w5")["stars"] == 0          # 地板 0


def test_fivestar_excluded_from_plan():
    for _ in range(5):
        srs.update_output_star("eosfs_w6", "PASS", 95)
    assert srs.word_stars("eosfs_w6")["stars"] == 5
    plan = services.build_sentence_plan(
        [{"word": "eosfs_w6", "meaning": "测试", "focus": False}],
        [], "一般现在时", 0, 2, 1)
    words = set()
    for b in plan["basic"]:
        words.add(b["word"].lower())
    for c in plan["combo"]:
        for w in c["words"]:
            words.add(w["word"].lower())
    assert "eosfs_w6" not in words


def test_stars_map_only_recorded():
    srs.update_output_star("eosfs_w7", "PASS", 95)
    m = srs.stars_map(["eosfs_w7", "eosfs_never"])
    assert m == {"eosfs_w7": 1}            # 未记录的词不返回，不编造 0 星
