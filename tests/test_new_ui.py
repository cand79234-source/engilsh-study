"""手机视口测试 - 新3入口简洁版 UI + 纯本地纠错 + 贴词匹配 + 周自动填充"""
import time
from playwright.sync_api import sync_playwright

BASE="http://localhost:8000"
passed=failed=0
def check(name,cond,extra=""):
    global passed,failed
    if cond: passed+=1;print(f"  ✅ {name}")
    else: failed+=1;print(f"  ❌ {name} {extra}")

with sync_playwright() as p:
    b=p.chromium.launch()
    ctx=b.new_context(viewport={"width":390,"height":844},is_mobile=True,has_touch=True)
    pg=ctx.new_page()
    js=[];pg.on("pageerror",lambda e:js.append(str(e)))

    print("=== 1. 学习页加载 ===")
    pg.goto(BASE); time.sleep(2)
    check("无JS错误", len(js)==0, js)
    check("底部3个导航", pg.locator(".bottomnav a").count()==3)
    check("显示学习内容", "本周词汇" in pg.content())
    check("今日词汇卡", "今日 · Week" in pg.content())
    # 词数量
    wc=pg.locator(".word").count()
    check(f"本周词汇>0 ({wc})", wc>0)
    check("顶部进度显示W3", "W3" in pg.locator("#topProg").text_content())

    print("=== 2. 点击单词展开详情 ===")
    pg.locator(".word").first.click(); time.sleep(0.8)
    check("展开含例句", "例句" in pg.content() or "搭配" in pg.content() or "记住了" in pg.content())
    check("无JS错误", len(js)==0, js)

    print("=== 3. 贴词换本周（词库匹配）===")
    pg.click("button:has-text('换本周词')"); time.sleep(0.8)
    check("出现粘贴框", pg.locator("#imp_words").count()==1)
    pg.fill("#imp_words","hobby travel movie cooking sport")
    pg.click("button:has-text('设为本周词汇')"); time.sleep(1.5)
    check("提示匹配成功", "已匹配" in pg.content(), pg.content()[:300])
    check("无JS错误", len(js)==0, js)
    time.sleep(1)
    # 重新加载看词变
    pg.reload(); time.sleep(2)
    body=pg.content()
    check("新词 hobby 出现", "hobby" in body or "movie" in body)

    print("=== 4. 造句 + 本地批改 ===")
    # 找到第一个造句输入框
    inp=pg.locator(".sinput").first
    inp.fill("I usually go work at 9.")
    # 点击该题批改按钮
    pg.locator("button:has-text('批改')").first.click(); time.sleep(1.5)
    c=pg.content()
    check("批改提示搭配/建议", "go to work" in c or "固定搭配" in c, c[:400])
    check("无JS错误", len(js)==0, js)

    print("=== 5. 复习页 ===")
    pg.locator('.bottomnav a[data-page="review"]').click(); time.sleep(1.5)
    check("复习页标题", "复习" in pg.content())

    print("=== 6. 设置页：调进度自动匹配不同周 ===")
    pg.locator('.bottomnav a[data-page="settings"]').click(); time.sleep(1.2)
    check("设置页含进度", "当前进度" in pg.content())
    # 周+到4 触发预览
    pg.locator("#set_week").fill("5"); time.sleep(0.8)
    # 保存
    pg.click("button:has-text('保存位置')"); time.sleep(1.5)
    check("保存成功提示", "已保存位置" in pg.content() or "W5" in pg.locator("#topProg").text_content())

    print("=== 7. 回学习页看 Week5 自动填充内容 ===")
    pg.locator('.bottomnav a[data-page="learn"]').click(); time.sleep(1.8)
    c=pg.content()
    check("顶部W5", "W5" in pg.locator("#topProg").text_content())
    check("Week5交通主题有词", "本周词汇" in c)
    wc2=pg.locator(".word").count()
    check(f"Week5词汇>0 ({wc2})", wc2>0)
    check("无JS错误", len(js)==0, js)

    print("\n========== 结果: %d 通过, %d 失败 =========="%(passed,failed))
    b.close()
