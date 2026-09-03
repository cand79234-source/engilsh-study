"""手机视口端到端测试 v2 - 分场景验证，避免相互污染。
场景A：当前周填词→主页/主线立即可见
场景B：调进度到新周→新周空状态引导录入

⚠️ 本文件已过时（针对两代之前的界面）：它依赖的设置页 #we_vocab、
   「解析并预览」「保存本周内容」在当前前端已全部不存在（词汇录入改为导入弹窗）。
   当前界面的等价覆盖见 tests/test_new_ui_v2.py，
   造句相关覆盖见 tests/test_sentence_e2e.py。
   保留此文件仅作历史参考，后续要么按新 UI 重写，要么删除。
"""
import time
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
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    errors_js = []
    page.on("pageerror", lambda e: errors_js.append(str(e)))

    print("########## 场景A：当前周填词 → 主页/主线立即可见 ##########")
    page.goto(BASE)
    time.sleep(1.5)
    check("A1 无JS错误", len(errors_js)==0, errors_js)

    # 主页点击"录入/修改本周词汇"按钮
    page.click("button:has-text('修改本周词汇')")
    time.sleep(1.5)
    check("A2 跳转到设置并聚焦词汇编辑", page.locator("#we_vocab").count()==1)
    check("A3 无JS错误", len(errors_js)==0, errors_js)

    # 批量粘贴并保存（此时是 Week3 当前周）
    page.fill("#we_vocab", """swimming|名词|游泳|go swimming|I go swimming on Sundays.
cooking|名词|烹饪|love cooking|She loves cooking.
travel|动词|旅行|travel abroad|I want to travel to Japan.
photography|名词|摄影|do photography|Photography is my hobby.""")
    page.click("button:has-text('解析并预览')")
    time.sleep(0.6)
    check("A4 解析出4个词", "4</b> 个词" in page.content())
    page.click("button:has-text('保存本周内容')")
    time.sleep(1.2)
    check("A5 保存成功", "已保存" in page.content() or "本周要学的词汇" in page.content())

    # 回主页看本周词汇数
    page.locator('.bottomnav a[data-page="home"]').click()
    time.sleep(1.2)
    check("A6 主页显示本周4词", "4 词" in page.content(), page.content()[page.content().find('本周要学'):page.content().find('本周要学')+80])
    check("A7 无JS错误", len(errors_js)==0, errors_js)

    # 进主线看新词
    page.locator('.bottomnav a[data-page="main"]').click()
    time.sleep(1.2)
    check("A8 主线显示4词", page.locator(".word").count()==4, page.locator(".word").count())
    check("A9 主线显示游泳", "游泳" in page.content())

    print("########## 场景B：调进度到新周 → 新周空状态引导录入 ##########")
    page.locator('.bottomnav a[data-page="settings"]').click()
    time.sleep(1.2)
    # 把周调到 5（新周，没填词）
    page.fill("#set_week", "5")
    page.click("button:has-text('保存进度调整')")
    time.sleep(1.0)
    check("B1 保存进度成功", "已调整" in page.content())

    page.locator('.bottomnav a[data-page="home"]').click()
    time.sleep(1.2)
    check("B2 主页显示 Week5", "Week 5" in page.content())
    check("B3 主页新周显示0词空状态", "0 词" in page.content())
    check("B4 主页显示录入按钮", "录入本周词汇" in page.content())
    check("B5 无JS错误", len(errors_js)==0, errors_js)

    # 主线页空状态引导
    page.locator('.bottomnav a[data-page="main"]').click()
    time.sleep(1.2)
    check("B6 主线空状态提示录入", "录入本周词汇" in page.content())
    check("B7 无JS错误", len(errors_js)==0, errors_js)

    print("\n========== 结果: %d 通过, %d 失败 ==========" % (passed, failed))
    browser.close()
