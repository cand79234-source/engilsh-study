# -*- coding: utf-8 -*-
"""专项训练落库 + 板块数据打通 的端到端测试。

跑法：
    cd backend && python3 ../tests/test_link_and_training.py

用真实的 SQLite 文件库（临时文件），不走 mock —— 目的是验证真实的 SQL、
真实的 upsert / ON CONFLICT 行为，而不是验证测试替身。
"""
import os
import sys
import tempfile

# 必须在 import db 之前指定，db 在导入期就读取 EOS_DB
_tmpdb = tempfile.mktemp(suffix=".db")
os.environ["EOS_DB"] = _tmpdb
os.environ.pop("DATABASE_URL", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/backend")

import db  # noqa: E402
import training as tr  # noqa: E402
import link  # noqa: E402

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + str(extra)) if extra else ""))


def main():
    db.init_db()
    conn = db.get_conn()

    # ---------- 造数据 ----------
    conn.execute(
        "INSERT INTO sentences (stage,week,day,word,task_key,original,good,score,verdict,created_at) "
        "VALUES (2,3,1,'continue','basic:0','I will continue.',1,90,'正确','2026-08-01T10:00:00')")
    conn.execute(
        "INSERT INTO sentences (stage,week,day,word,task_key,original,good,score,verdict,created_at) "
        "VALUES (2,3,1,'continue','basic:1','I continue to learn.',0,55,'有错误','2026-08-01T11:00:00')")
    conn.execute(
        "INSERT INTO sentences (stage,week,day,word,task_key,original,good,score,verdict,created_at) "
        "VALUES (2,3,1,'abandon','basic:0','I abandon it.',0,60,'有错误','2026-08-02T10:00:00')")
    conn.execute(
        "INSERT INTO errors (error_type,original,corrected,word,source,times,fixed,created_at) "
        "VALUES ('时态','I continue','I continued','continue','造句',2,0,'2026-08-01T11:00:00')")
    conn.execute(
        "INSERT INTO reviews (kind,ref_key,prompt,stage,week,day,total_correct,total_wrong,last_score) "
        "VALUES ('vocab','continue','继续',2,3,1,1,4,0)")
    conn.execute(
        "INSERT INTO word_output (word,stars,total_attempts,last_result) "
        "VALUES ('continue',2,4,'needs_review')")
    conn.execute(
        "INSERT INTO listening_progress (stage,week,day,listening_done,listening_total,created_at) "
        "VALUES (2,3,1,3,10,'2026-08-03T10:00:00')")
    conn.commit()
    conn.close()

    print("\n【一】专项训练四层落库")
    state = {
        "projects": [{
            "project_id": "P1", "ability": "GRAMMAR", "problem": "现在完成时总用错",
            "priority": "P1", "intervention_level": "MUST",
            "training_goal": "分清现在完成时与一般过去时",
            "training_boundary": "只练这两种时态的区分", "forbidden": ["虚拟语气"],
            "exit_standard": "连续 2 次独立答对",
            "exit_rule": {"independent_correct": 2, "max_rounds": 6, "per_round": 4},
            "status": "CONTINUE",
            "items": [{"id": "P1-Q1", "type": "CHOICE",
                       "prompt": "I ___ finished my homework yet.",
                       "options": ["have", "did"], "answer": "A"}],
            "created_at": "2026-08-01T09:00:00", "updated_at": "2026-08-01T09:00:00",
        }],
        "sessions": [{
            "session_id": "sess_1", "project_id": "P1",
            "started_at": "2026-08-01T09:00:00", "ended_at": None,
            "round_count": 1, "valid_attempts": 3, "correct_count": 2,
            "incorrect_count": 1, "hint_count": 0,
            "independent_correct_count": 1, "consecutive_independent_correct": 1,
            "final_status": "NOT_YET", "next_step": "CONTINUE",
        }],
        "rounds": [{
            "round_id": "r_1", "session_id": "sess_1", "project_id": "P1",
            "index": 1, "started_at": "2026-08-01T09:00:00", "ended_at": None,
        }],
        "attempts": [{
            "attempt_id": "a_1", "project_id": "P1", "session_id": "sess_1",
            "round_id": "r_1", "question_id": "P1-Q1", "user_answer": "A",
            "is_correct": True, "manual": False, "used_hint": False,
            "hint_level": 0, "is_independent": True,
            "created_at": "2026-08-01T09:05:00",
        }],
    }
    r = tr.save_state(state)
    ck("save_state 返回 ok", r.get("ok") is True, r.get("counts"))
    ck("四层都写入了", r["counts"] == {"projects": 1, "sessions": 1, "rounds": 1, "attempts": 1},
       r["counts"])

    got = tr.load_state()
    ck("读回 1 个项目", len(got["projects"]) == 1)
    p0 = got["projects"][0]
    ck("project_id 往返一致", p0["project_id"] == "P1", p0["project_id"])
    ck("items 结构完整保留", len(p0["items"]) == 1 and p0["items"][0]["id"] == "P1-Q1")
    ck("exit_rule 对象完整保留", p0["exit_rule"].get("max_rounds") == 6, p0["exit_rule"])
    ck("forbidden 数组完整保留", p0["forbidden"] == ["虚拟语气"], p0["forbidden"])
    ck("session 统计往返一致",
       got["sessions"][0]["valid_attempts"] == 3 and got["sessions"][0]["correct_count"] == 2)
    ck("session final_status 保留", got["sessions"][0]["final_status"] == "NOT_YET")
    ck("round index 往返一致", got["rounds"][0]["index"] == 1, got["rounds"][0]["index"])
    ck("attempt 布尔字段往返一致",
       got["attempts"][0]["is_correct"] is True and got["attempts"][0]["used_hint"] is False)

    # upsert：同 ID 再存一次应更新而非重复
    state["sessions"][0]["valid_attempts"] = 9
    tr.save_state(state)
    got2 = tr.load_state()
    ck("再次保存是更新而非新增",
       len(got2["sessions"]) == 1 and got2["sessions"][0]["valid_attempts"] == 9,
       len(got2["sessions"]))

    # 删除：传空列表应清掉该层
    state2 = dict(state, attempts=[])
    tr.save_state(state2)
    ck("传空 attempts 会清掉该层", len(tr.load_state()["attempts"]) == 0)

    # 老数据保护：project_key 为 NULL 的历史行不能被误删
    conn = db.get_conn()
    conn.execute("INSERT INTO training_projects (ability, problem, prompt_md, created_at) "
                 "VALUES ('语法','历史遗留项目','xxx','2026-01-01T00:00:00')")
    conn.commit()
    tr.save_state(state)  # 全量保存，projects 里只有 P1
    left = conn.execute(
        "SELECT COUNT(*) n FROM training_projects WHERE project_key IS NULL").fetchone()[0]
    ck("无 project_key 的历史行未被误删", left == 1, f"剩余 {left} 行")
    ck("有 project_key 的仍在", conn.execute(
        "SELECT COUNT(*) FROM training_projects WHERE project_key='P1'").fetchone()[0] == 1)
    conn.close()

    print("\n【二】周测错题 → 错误本")
    before = db.get_conn().execute(
        "SELECT COUNT(*) FROM errors WHERE source='周测'").fetchone()[0]
    detail = [
        {"id": "q1", "question": "He has ___ to Paris.", "user": 1, "correct_idx": 0,
         "ok": False, "tag": "现在完成时"},
        {"id": "q2", "question": "She ___ her homework yesterday.", "user": 0,
         "correct_idx": 0, "ok": True, "tag": "一般过去时"},
        {"id": "q3", "question": "I have seen it ___.",
         "user": 2, "correct_idx": 1, "ok": False, "tag": "现在完成时"},
    ]
    n = link.sync_quiz_errors(2, 3, detail, day=7)
    ck("错题同步进了错误本（2 条）", n == 2, f"同步 {n} 条")
    conn = db.get_conn()
    tot = conn.execute("SELECT COUNT(*) FROM errors WHERE source='周测'").fetchone()[0]
    ck("错误本周测条目数正确", tot == before + 2, f"{before} → {tot}")
    row = conn.execute(
        "SELECT error_type, stage, week, times, original FROM errors "
        "WHERE source='周测' AND task_key='q1'").fetchone()
    ck("带上了课程周身份证", row and row["stage"] == 2 and row["week"] == 3,
       dict(row) if row else None)
    ck("知识点 tag 落到了 error_type", row and row["error_type"] == "现在完成时")
    ck("答对的题没有进错误本", conn.execute(
        "SELECT COUNT(*) FROM errors WHERE source='周测' AND task_key='q2'").fetchone()[0] == 0)

    # 重复同步应累加 times，而不是新增
    link.sync_quiz_errors(2, 3, detail, day=7)
    t2 = conn.execute(
        "SELECT times FROM errors WHERE source='周测' AND task_key='q1'").fetchone()["times"]
    ck("重复答错累加 times 而非新增", t2 == 2, f"times={t2}")
    ck("重复同步后总条数不变", conn.execute(
        "SELECT COUNT(*) FROM errors WHERE source='周测'").fetchone()[0] == tot)

    # 已改正后又答错 → 应重新打开
    conn.execute("UPDATE errors SET fixed=1, fixed_at='2026-08-05T00:00:00' "
                 "WHERE source='周测' AND task_key='q1'")
    conn.commit()
    link.sync_quiz_errors(2, 3, detail, day=7)
    fixed = conn.execute(
        "SELECT fixed FROM errors WHERE source='周测' AND task_key='q1'").fetchone()["fixed"]
    ck("已改正但再次答错 → 重新打开", fixed == 0, f"fixed={fixed}")

    print("\n【三】薄弱项综合判定")
    wk = link.build_weakness()
    ck("保留原有 error_types 字段", "error_types" in wk)
    ck("保留原有 low_star_words 字段", "low_star_words" in wk)
    ck("保留原有 recommendations 字段", "recommendations" in wk)
    ck("新增 sources 分板块明细", "sources" in wk)
    kinds = {r["kind"] for r in wk["recommendations"]}
    ck("纳入了造句维度", "sentence" in kinds, kinds)
    ck("纳入了复习维度", "review" in kinds, kinds)
    s = wk["sources"]
    ck("造句来源识别到低分词", any(w["word"] == "abandon" for w in s["sentences"]) or
       any(w["word"] == "continue" for w in s["sentences"]), s["sentences"])
    ck("复习来源识别到高错误率卡",
       any(x["ref_key"] == "continue" for x in s["reviews"]), s["reviews"])
    ck("听力来源算出正确率", s["listening"] and s["listening"]["rate"] == 30, s["listening"])
    ck("训练来源读到未达标项目", isinstance(s["training"], list), s["training"])

    print("\n【四】词全息档案")
    prof = link.word_profile("continue")
    ck("查得到 continue", prof is not None)
    ck("造句条数正确", prof["sentences"]["count"] == 2, prof["sentences"]["count"])
    ck("造句均分正确", prof["sentences"]["avg"] == 72.5, prof["sentences"]["avg"])
    ck("错误次数正确", prof["errors"]["times"] == 2, prof["errors"]["times"])
    ck("错误类型聚合正确", "时态" in prof["errors"]["types"], prof["errors"]["types"])
    ck("复习记录读到", prof["reviews"] and prof["reviews"]["total_wrong"] == 4)
    ck("五星输出读到", prof["output"] and prof["output"]["stars"] == 2)
    ck("查不到的词返回 None", link.word_profile("zzz_not_exist") is None)
    ck("大小写不敏感", link.word_profile("CONTINUE") is not None)

    print("\n【五】专项训练汇总")
    ts_ = link.training_summary()
    ck("汇总非空", ts_ is not None, ts_)
    ck("项目数正确", ts_ and ts_["projects"] == 1, ts_ and ts_["projects"])
    ck("会话数正确", ts_ and ts_["sessions"] == 1, ts_ and ts_["sessions"])
    ck("有效作答数正确", ts_ and ts_["valid_attempts"] == 9, ts_ and ts_["valid_attempts"])

    print("\n【六】词关联：训练作答挂到具体词上")
    # 用生造词保证断言确定：flurbish 在词典里，zzz / quxx 都不在，
    # 所以按 token 顺序第一个命中必然是 flurbish。
    # （P1 的题目词在首次保存时就已经定好，不会重猜，故这里用新项目 P2）
    conn = db.get_conn()
    conn.execute("INSERT OR IGNORE INTO dictionary (word, meaning) VALUES ('flurbish','测试词')")
    conn.commit()
    state3 = {
        "projects": [{
            "project_id": "P2", "ability": "GRAMMAR", "problem": "时态混淆",
            "items": [{"id": "P2-Q1", "type": "CHOICE",
                       "prompt": "Zzz flurbish quxx.", "answer": "A"}],
            "created_at": "2026-08-01T09:00:00", "updated_at": "2026-08-01T09:00:00",
        }],
        "sessions": [], "rounds": [],
        "attempts": [{
            "attempt_id": "a_9", "project_id": "P2", "session_id": "sess_9",
            "round_id": "r_9", "question_id": "P2-Q1", "user_answer": "A",
            "is_correct": True, "manual": False, "used_hint": False,
            "hint_level": 0, "is_independent": True,
            "created_at": "2026-08-01T09:05:00",
        }],
    }
    tr.save_state(state3)
    w = conn.execute(
        "SELECT word FROM training_attempts WHERE attempt_id='a_9'").fetchone()
    ck("attempt 自动填上了 word", w and w["word"] == "flurbish",
       f"word={w['word'] if w else None}")

    print("\n" + "=" * 52)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("   -", f)
    print("=" * 52)

    try:
        os.unlink(_tmpdb)
    except OSError:
        pass
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
