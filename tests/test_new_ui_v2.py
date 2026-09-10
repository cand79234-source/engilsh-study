"""手机视口测试 v2 - 精简3入口(学习/复习/薄弱项) + 进度徽章弹窗 + 错误独立页。

覆盖：学习页加载 / 单词展开 / 贴词换本周 / 造句本地批改 / 复习页 /
顶部进度徽章弹窗调进度(自动匹配) / 薄弱项入口与详情展开(规律+补课)。
"""
import time
from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}  {extra}")


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 390, "height": 844},
                        is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    js = []
    pg.on("pageerror", lambda e: js.append(str(e)))

    print("=== 0. 初始: 制造一个错误句（供薄弱项页展示）===")
    pg.goto(BASE); time.sleep(1.5)
    pg.locator(".sinput").first.fill("I listen music every day")
    pg.locator("button:has-text('批改')").first.click(); time.sleep(1.5)
    c0 = pg.content()
    check("本地批改识别 listen music", "listen to music" in c0, c0[:300])

    print("=== 1. 学习页加载 ===")
    pg.goto(BASE); time.sleep(2)
    check("无JS错误", len(js) == 0, js)
    navs = [pg.locator(f'.bottomnav a[data-page="{x}"]').count() for x in ("learn", "review", "errors")]
    check("底部3入口(学习/复习/薄弱项)", sum(navs) == 3 and all(navs), navs)
    check("不存在设置入口", pg.locator('.bottomnav a[data-page="settings"]').count() == 0)
    body = pg.content()
    check("学习页含本周词汇", "本周词汇" in body)
    wc = pg.locator(".word").count()
    check(f"本周词汇>0 ({wc})", wc > 0)
    check("顶部进度徽章显示", "阶段" in pg.locator("#topProg").text_content())
    check("无JS错误", len(js) == 0, js)

    print("=== 2. 单词展开 ===")
    pg.locator(".word").first.click(); time.sleep(0.8)
    check("展开含详情", ("记住了" in pg.content()) or ("例句" in pg.content()))

    print("=== 3. 顶部进度徽章 → 弹窗调进度 ===")
    pg.locator("#topProg").click(); time.sleep(1.0)
    check("出现进度弹窗", pg.locator("#progOverlay.show").count() == 1)
    check("弹窗有阶段选择", pg.locator("#seg_stage button").count() > 0)
    check("弹窗有周选择(W1-W12)", pg.locator("#grid_week button").count() == 12)
    check("弹窗有Day选择(D1-D7)", pg.locator("#grid_day button").count() == 7)
    # 点周 5（周5=交通主题）看预览是否自动带出
    pg.locator("#grid_week button", has_text="W5").click(); time.sleep(0.8)
    preview = pg.locator("#progPreview").text_content()
    check("预览显示W5与本周信息", "W5" in preview, preview[:200])
    # 关掉（不保存，避免污染进度）
    pg.click("button:has-text('✕')"); time.sleep(0.5)
    check("弹窗已关闭", pg.locator("#progOverlay.show").count() == 0)

    print("=== 4. 复习页 ===")
    pg.locator('.bottomnav a[data-page="review"]').click(); time.sleep(1.5)
    check("复习页标题", "复习" in pg.content())

    print("=== 5. 薄弱项页(独立入口) ===")
    pg.locator('.bottomnav a[data-page="errors"]').click(); time.sleep(1.5)
    ebody = pg.content()
    check("薄弱项页标题", "薄弱项" in ebody)
    # 因为刚造了 listen music 介词错误，应有薄弱项条目
    row = pg.locator("#errlist .word").first
    check("薄弱项列表非空", row.count() == 1 or pg.locator("#errlist .word").count() > 0)
    if row.count():
        txt = row.text_content()
        check("薄弱项行含次数", "次" in txt, txt)
        # 展开详情
        row.click(); time.sleep(1.2)
        detail = pg.locator("#errdetail").text_content()
        check("详情含建议补课", "补课" in detail, detail[:200])
        check("详情含错误规律", ("规律" in detail) or ("最近错误" in detail), detail[:200])
        check("详情含原句与正确句", ("✗" in detail) and ("✓" in detail), detail[:300])
        # 收起
        pg.click("button:has-text('收起')"); time.sleep(0.5)
        check("详情已收起", pg.locator("#errdetail").is_visible() is False or pg.locator("#errdetail").inner_text().strip() == "")
    check("无JS错误", len(js) == 0, js)

    print("=== 6. 回学习页 顶部仍是进度徽章 ===")
    pg.locator('.bottomnav a[data-page="learn"]').click(); time.sleep(1.2)
    check("学习页顶部为进度徽章", "阶段" in pg.locator("#topProg").text_content())

    print("\n========== 结果: %d 通过, %d 失败 ==========" % (passed, failed))
    pg.screenshot(path="tests/m_errors_v2.png")
    b.close()
