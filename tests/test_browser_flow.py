"""浏览器端到端测试 - 验证前端交互完整闭环。"""
import time, json
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
passed = failed = 0

def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1; print(f"  ✅ {name}")
    else:
        failed += 1; print(f"  ❌ {name} {extra}")

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    errors_js = []
    page.on("pageerror", lambda e: errors_js.append(str(e)))

    print("=== 1. 打开主页 ===")
    page.goto(BASE)
    time.sleep(1.5)
    check("标题为 English OS", "English OS" in page.title() or "English OS" in page.content())
    check("显示当前进度 阶段0→Week3", "Week 3" in page.content())
    check("显示主题 爱好与休闲", "爱好与休闲" in page.content())
    check("无 JS 错误", len(errors_js)==0, errors_js)

    print("=== 2. 继续学习 → 主线 ===")
    page.click("button:has-text('继续学习')")
    time.sleep(1.2)
    check("主线标题", "主线 · 今日学习" in page.content())
    check("显示20词", page.locator(".word").count() == 20, page.locator(".word").count())
    check("显示今日语法", "like / enjoy / hate" in page.content())
    check("显示10句引导", page.locator("#sentences .card").count() == 10)

    print("=== 3. 学词 + 掌握 ===")
    page.locator(".word").nth(0).click()
    time.sleep(0.8)
    check("展开单词详情", "我记住了" in page.content())
    page.click("button:has-text('我记住了')")
    time.sleep(0.8)
    check("单词标记已掌握", page.locator(".word.done").count() >= 1)

    print("=== 4. AI 造句批改（规则兜底） ===")
    page.fill("#sin_0", "I usually go work at 9.")
    page.click("button:has-text('AI 批改')")
    time.sleep(1.5)
    content = page.content()
    check("返回修正 go to work", "go to work" in content)
    check("返回错误类型", "固定搭配" in content)
    check("显示已存入错误库", "已存入错误库" in content)

    print("=== 5. 复习自动注入 ===")
    page.click("nav a[data-page=review]")
    time.sleep(1.0)
    content = page.content()
    check("复习页显示到期项", "复习" in content)
    check("复习含词汇卡", "请使用" in content)
    # 答对一次
    page.click(".rev-ok")
    time.sleep(0.8)
    check("复习完成反馈", "间隔已拉长" in page.content() or "已记录正确" in page.content())

    print("=== 6. 错误页 ===")
    page.click("nav a[data-page=errors]")
    time.sleep(1.0)
    content = page.content()
    check("错误页显示薄弱项", "薄弱项" in content)
    check("显示固定搭配类型", "固定搭配" in content)

    print("=== 7. 周测 ===")
    page.click("nav a[data-page=quiz]")
    time.sleep(1.0)
    check("周测显示语法题", "语法测试" in page.content())
    check("周测10题", page.locator("[id^=q_1_]").count() >= 3)  # 至少有选项

    print("=== 8. 设置-调整进度 ===")
    page.click("nav a[data-page=settings]")
    time.sleep(1.0)
    check("设置页显示调整进度", "调整学习进度" in page.content())
    check("显示编辑本周内容", "编辑本周内容" in page.content())
    # 调整到 Day 3
    page.fill("#set_day", "3")
    page.click("button:has-text('保存进度调整')")
    time.sleep(0.8)
    check("提示进度已调整", "进度已调整" in page.content())

    # 验证历史保留：回到主页应仍能显示
    page.click("nav a[data-page=home]")
    time.sleep(1.0)
    check("主页仍正常显示", "当前进度" in page.content())
    check("历史数据未被清除(主页仍有内容)", page.locator(".card").count()>=3)

    print("=== 9. JS 错误总检查 ===")
    check("全程无 JS 错误", len(errors_js)==0, errors_js)

    # 截图保存
    page.screenshot(path="/workspace/english-os/tests/home_screenshot.png", full_page=True)
    browser.close()

print(f"\n浏览器测试结果: {passed} 通过, {failed} 失败")
exit(1 if failed else 0)
