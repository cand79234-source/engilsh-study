# -*- coding: utf-8 -*-
"""给浏览器回归测试库注入种子数据，让三套测试可复现。

跑法（先起好服务，DB 指向同一个库）：
    python3 tests/seed_regress_data.py

test_browser_link.py 需要 hobby 的造句/错误/复习/输出 + 一份周测；
test_browser_training.py 需要 PT1 训练项目（首题必须是 CHOICE，
因为它只设 tr.picked 而不填文本输入框）。
"""
import os
import sqlite3
DB = os.environ.get("EOS_DB", "/tmp/snap_test.db")
c = sqlite3.connect(DB)
def has(t, w="", col="word"):
    return c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col}=?", (w,)).fetchone()[0] if w else c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

if not has("sentences", "hobby"):
    c.execute("INSERT INTO sentences (stage,week,day,word,task_key,original,good,score,verdict,created_at) "
              "VALUES (2,3,1,'hobby','basic:0','My hobby is read.',0,70,'有错误','2026-08-01T10:00:00')")
    c.execute("INSERT INTO sentences (stage,week,day,word,task_key,original,good,score,verdict,created_at) "
              "VALUES (2,3,1,'hobby','basic:1','My hobby is reading.',1,80,'正确','2026-08-01T11:00:00')")
if not has("errors", ""):
    c.execute("INSERT INTO errors (error_type,original,corrected,word,source,times,fixed,stage,week,day,created_at) "
              "VALUES ('非谓语','My hobby is read.','My hobby is reading.','hobby','造句',2,0,2,3,1,'2026-08-01T10:00:00')")
if not has("reviews", "") or not c.execute("SELECT COUNT(*) FROM reviews WHERE ref_key='hobby'").fetchone()[0]:
    c.execute("INSERT INTO reviews (kind,ref_key,prompt,stage,week,day,total_correct,total_wrong,last_score) "
              "VALUES ('vocab','hobby','爱好',2,3,1,3,2,80)")
if not has("word_output", "hobby"):
    c.execute("INSERT INTO word_output (word,stars,total_attempts,last_result,stage,week) "
              "VALUES ('hobby',3,5,'good',2,3)")
if not has("quizzes", ""):
    c.execute("INSERT INTO quizzes (kind,stage,week,score,passed,detail_json,created_at) "
              "VALUES ('weekly',2,3,60,0,?,'2026-08-04T10:00:00')",
              (json.dumps([
                  {"no":1,"ok":False,"type":"时态","q":"He ___ to school yesterday.","a":"go","right":"went"},
                  {"no":2,"ok":False,"type":"三单","q":"She ___ English.","a":"like","right":"likes"},
                  {"no":3,"ok":True,"type":"时态","q":"I ___ it.","a":"did","right":"did"},
              ], ensure_ascii=False),))
c.commit()
for t in ["sentences","errors","reviews","word_output","quizzes"]:
    print(f"  {t}: {has(t)} 行")
c.close()

# 专项训练项目 PT1（test_browser_training.py 依赖）
import sqlite3 as _sq
_c = _sq.connect(DB)
if not _c.execute("SELECT COUNT(*) FROM training_projects WHERE project_key='PT1'").fetchone()[0]:
    _c.execute(
        "INSERT INTO training_projects (ability, problem, prompt_md, created_at, project_key, "
        "priority, status, items_json, stage, week) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("时态", "一般过去时用错", "PT1 训练题：把下列句子改成一般过去时。",
         "2026-08-01T09:00:00", "PT1", "P1", "NOT_STARTED",
         '[{"id":"q1","type":"CHOICE","prompt":"Which is past tense?","options":[{"key":"A","text":"He went."},{"key":"B","text":"He go."}],"answer":"A","ability":"时态"},'
         '{"id":"q2","type":"FILL","prompt":"She ___ (like) English.","accepted":["likes"],"ability":"三单"},'
         '{"id":"q3","type":"CHOICE","prompt":"They ___ happy yesterday.","options":[{"key":"A","text":"are"},{"key":"B","text":"were"}],"answer":"B","ability":"时态"}]',
         2, 3))
    _c.commit()
print("  training_projects(PT1):",
      _c.execute("SELECT COUNT(*) FROM training_projects WHERE project_key='PT1'").fetchone()[0], "行")
_c.close()
