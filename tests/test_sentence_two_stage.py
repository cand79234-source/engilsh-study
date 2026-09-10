# -*- coding: utf-8 -*-
"""两段式造句（①基础 ②组合）+ 组合配比 + 升级句已删除 —— 回归锁。

背景（2026-09-10 的产品改动，别退回去）：
  1. ② 升级句已删除。原因：20 个词在①基础句已各写过一遍，升级句只是
     "换个角度再写一句"，且挑词规则让复习词占满（起手 50 分 vs 新词 0-9 分），
     与"只服务当天新词"的设计意图相反。
  2. 组合句配比从「1 复习 + 2 新词」改成「2 复习 + 1 新词」（复习占多数）。
  3. 复习词不够时**不重复使用**（同一词一天写两三遍是纯抄），
     缺的位置让新词顶上；词总量不够凑满一组时宁可不生成那组。

运行: pytest tests/test_sentence_two_stage.py -q
"""
import os
import sys
import tempfile

os.environ.setdefault("EOS_DB", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from db import init_db          # noqa: E402

init_db()

import services as S            # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ✅ %s" % name)
    else:
        failed += 1
        print("  ❌ %s %s" % (name, extra))


def _new(n):
    return [{"word": "new%02d" % i, "meaning": "新%d" % i} for i in range(1, n + 1)]


def _due(n):
    return [{"word": "rev%02d" % i, "meaning": "复%d" % i, "error_rate": 0.3}
            for i in range(1, n + 1)]


def test_upgrade_removed():
    """② 升级句必须彻底消失：函数、方向库、返回字段一个都不许回来。"""
    print("\n[1] 升级句已删除")
    check("UPGRADE_DIRECTIONS 已删除", not hasattr(S, "UPGRADE_DIRECTIONS"))
    check("_pick_focus_words 已删除", not hasattr(S, "_pick_focus_words"))
    check("_build_upgrade 已删除", not hasattr(S, "_build_upgrade"))
    plan = S.build_sentence_plan(_new(20), _due(8), "", 0, 3, 1)
    check("plan 里没有 upgrade 字段", "upgrade" not in plan, list(plan.keys()))
    check("basic / combo 都在", "basic" in plan and "combo" in plan)
    check("meta 里没有 upgrade_count", "upgrade_count" not in plan["meta"])
    sig = S.build_sentence_plan.__code__.co_varnames[:S.build_sentence_plan.__code__.co_argcount]
    check("build_sentence_plan 不再收 n_upgrade", "n_upgrade" not in sig, sig)


def test_combo_ratio_review_majority():
    """组合配比：复习词占多数（每组 2 复习 + 1 新词）。"""
    print("\n[2] 组合句配比 = 2 复习 + 1 新词")
    combos = S._build_combos(_new(20), _due(20), "", 12345, n=10, per=3)
    tr = sum(1 for c in combos for w in c["words"] if w["review"])
    tn = sum(1 for c in combos for w in c["words"] if not w["review"])
    check("复习词多于新词（复习占多数）", tr > tn, "复习 %d / 新词 %d" % (tr, tn))
    check("复习占比约 2/3", abs(tr / (tr + tn) - 2 / 3) < 0.05,
          "实际 %.2f" % (tr / (tr + tn)))
    check("每组都是 3 个词（长度不变长）",
          all(len(c["words"]) == 3 for c in combos),
          sorted({len(c["words"]) for c in combos}))
    check("每组最多 2 个复习词",
          all(sum(1 for w in c["words"] if w["review"]) <= 2 for c in combos))


def test_combo_no_duplicate_review_words():
    """复习词不够时**不重复使用**（同一个词不当两次复习词）。"""
    print("\n[3] 复习词不够时不重复使用")
    combos = S._build_combos(_new(20), _due(8), "", 12345, n=10, per=3)
    used = [w["word"] for c in combos for w in c["words"] if w["review"]]
    check("复习词无重复", len(used) == len(set(used)), used)
    check("用上了全部 8 个复习词", len(set(used)) == 8, sorted(set(used)))
    check("缺的位置由新词顶上（不硬凑）",
          all(len(c["words"]) >= 2 for c in combos))


def test_combo_no_tiny_groups():
    """词总量不够时宁可不生成，也不出只有 1 个词的残缺组。"""
    print("\n[4] 不出单词组")
    combos = S._build_combos(_new(20), _due(2), "", 12345, n=10, per=3)
    check("没有只有 1 个词的组", all(len(c["words"]) >= 2 for c in combos),
          [len(c["words"]) for c in combos])


def test_combo_no_review_no_crash():
    """一个复习词都没有时不能崩，且不产生 review 词。"""
    print("\n[5] 无复习词时不崩")
    combos = S._build_combos(_new(20), [], "", 12345, n=10, per=3)
    check("正常返回组", len(combos) > 0, len(combos))
    check("没有任何词被标成复习词",
          not any(w["review"] for c in combos for w in c["words"]))


def test_combo_stable_same_day():
    """同一天同一 seed 结果必须一致（刷新页面不能变）。"""
    print("\n[6] 同日稳定")
    a = S._build_combos(_new(20), _due(10), "", 777, n=10, per=3)
    b = S._build_combos(_new(20), _due(10), "", 777, n=10, per=3)
    check("两次生成完全相同",
          [[w["word"] for w in c["words"]] for c in a] ==
          [[w["word"] for w in c["words"]] for c in b])


if __name__ == "__main__":
    print("=" * 60)
    print("两段式造句 + 组合配比 回归测试")
    print("=" * 60)
    test_upgrade_removed()
    test_combo_ratio_review_majority()
    test_combo_no_duplicate_review_words()
    test_combo_no_tiny_groups()
    test_combo_no_review_no_crash()
    test_combo_stable_same_day()
    print("\n" + "=" * 60)
    print("通过 %d / 失败 %d" % (passed, failed))
    print("=" * 60)
    sys.exit(1 if failed else 0)
