"""API 全流程自动化测试 - 覆盖核心学习闭环。"""
import json, os, sys, traceback
os.environ["EOS_DB"] = "/tmp/eos_api_test.db"
if os.path.exists("/tmp/eos_api_test.db"):
    os.remove("/tmp/eos_api_test.db")
sys.path.insert(0, "backend")

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
passed = failed = 0

def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {extra}")

print("=== 1. 主页 ===")
r = client.get("/api/home").json()
check("主页返回当前进度", r["progress"]["stage"]==0 and r["progress"]["week"]==3)
check("主页包含本周主题", r["week_title"]=="爱好与休闲")
check("今日复习为空(初始)", len(r["due_reviews"])==0)

print("=== 2. 今日主线 ===")
r = client.get("/api/today").json()
check("今日20词", len(r["words"])==20)
check("今日10句引导", len(r["sentence_prompts"])==10)
check("语法为 like/enjoy/hate", "like" in r["grammar"])

print("=== 3. 学词 + SRS 入队 ===")
# 契约：/api/today 返回 collocations=[{phrase,meaning}]，master 也按数组回传
# （此前测试读单数 w["collocation"]，字段早已不存在 → KeyError）
for w in r["words"][:3]:
    client.post("/api/word/master", json={"word": w["word"], "mastered": 2, "meaning": w["meaning"], "collocations": w.get("collocations") or []})
r = client.get("/api/home").json()
check("主页显示今日词完成3/20", r["today"]["vocab_done"]>=3)

print("=== 4. 造句 + 本地规则批改 ===")
# 错误句子：I usually go work at 9.
res = client.post("/api/sentence/check", json={"sentence": "I usually go work at 9."}).json()
print("  批改:", json.dumps(res, ensure_ascii=False)[:150])
check("识别固定搭配错误", res["error_type"]=="固定搭配", res["error_type"])
check("给出修正句", "go to work" in res["corrected"])
check("标记需复习", res["needs_review"]==True)

print("=== 5. 错误入库 + 薄弱项 ===")
# 造几个介词/固定搭配错误
for s in ["I go school by bus.", "She live in Beijing.", "He go to work yesterday."]:
    client.post("/api/sentence/check", json={"sentence": s})
r = client.get("/api/errors").json()
types = {x["type"]: x["count_30d"] for x in r}
print("  错误统计:", types)
check("固定搭配累积>=2", types.get("固定搭配",0)>=2)
check("主谓一致被记录", types.get("主谓一致",0)>=1)
check("薄弱项按次数排序", r[0]["count_30d"]>=r[-1]["count_30d"])

print("=== 6. 错误详情页 ===")
r = client.get("/api/errors/固定搭配").json()
check("错误详情含历史", r["total"]>=2 and len(r["items"])>=2)

print("=== 7. 复习自动注入当天 ===")
r = client.get("/api/home").json()
check("今日复习自动出现", len(r["due_reviews"])>0, f"实际{len(r['due_reviews'])}")
r = client.get("/api/review/due").json()
check("复习到期列表非空", len(r["items"])>0)
# 答对一次，间隔应增长
first = r["items"][0]
r2 = client.post("/api/review/submit", json={"id": first["id"], "correct": True}).json()
check("答对间隔递增", r2["reps"]>=1)

print("=== 8. 周测 ===")
q = client.get("/api/quiz/0/3").json()
check("周测有10题", len(q["questions"])==10)
answers = {str(i+1): i for i in range(10)}  # 全错
g = client.post("/api/quiz/grade", json={"stage":0, "week":3, "answers":answers}).json()
check("全错不得分", g["pct"]<75 and g["passed"]==False)
# 全对
correct_ans = {str(q["questions"][i]["id"]): q["questions"][i]["answer"] for i in range(10)}
g2 = client.post("/api/quiz/grade", json={"stage":0, "week":3, "answers":correct_ans}).json()
check("全对通过", g2["pct"]==100 and g2["passed"]==True)

print("=== 9. 手动调整进度（不删历史）===")
before = client.get("/api/history").json()
hist_before = len(before)
# 调整回 Day 3
client.post("/api/progress", json={"stage":0, "week":3, "day":3})
p = client.get("/api/progress").json()
check("进度改为Day3", p["day"]==3)
# 调整到 Week4 Day1
client.post("/api/progress", json={"stage":0, "week":4, "day":1})
p = client.get("/api/progress").json()
check("进度改为Week4 Day1", p["week"]==4 and p["day"]==1)
after = client.get("/api/history").json()
check("历史数据保留且新增调整记录", len(after) > hist_before)
# 关键验证：历史学习记录/错误/SRS全保留
e = client.get("/api/errors").json()
check("错误库历史保留", sum(x["total"] for x in e)>0)
d = client.get("/api/review/due").json()
check("SRS数据保留", len(d["items"])>0)

print("=== 10. 无 AI 配置依赖 ===")
r = client.get("/api/error-types").json()
check("错误类型列表可访问(无 AI 路由)", isinstance(r, list) and len(r) == 11)

print(f"\n结果: {passed} 通过, {failed} 失败")
sys.exit(1 if failed else 0)
