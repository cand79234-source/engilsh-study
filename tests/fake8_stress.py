#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""8 周假数据压测：周报 / 月报 / 薄弱项 / 导入 跑不跑得动。

用法：
    python3 fake8_stress.py                       # 默认跑 /tmp/fake8.db
    python3 fake8_stress.py --db /tmp/fake8.db
    python3 fake8_stress.py --scale 10            # 先生成 ×10 数据再压（极限压测）
    python3 fake8_stress.py --regen               # 强制重新生成数据

做什么：
    1. 确保目标库有 8 周假数据（没有就用 fake8_seed.py 生成）
    2. 起一个独立的 uvicorn 子进程（端口 8055）
    3. 逐个调用周报 / 月报 / 薄弱项等接口，记录：
       - HTTP 状态码
       - 响应时间
       - 该请求前后服务进程的常驻内存（RSS）变化
    4. 汇总输出，标出慢的和涨内存的
    5. 关掉服务进程

内存测量：读 /proc/<pid>/status 的 VmRSS，这是 Linux 上进程实际占用的
物理内存，比 Python 的 resource 模块更能反映 Render 看到的真实占用。
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = "/tmp/fake8.db"
PORT = 8055
BASE = f"http://127.0.0.1:{PORT}"
H = {"X-Auth-Token": "faketest123"}


def rss_mb(pid):
    """读进程实际物理内存（MB）。读不到返回 None。"""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return None


def http(method, path, body=None, timeout=120):
    """发一个请求，返回 (状态码, 响应体, 耗时秒)。"""
    data = json.dumps(body).encode() if body is not None else None
    # 路径里可能有中文（如 /api/errors/非谓语），必须转码，否则服务端收到乱码
    url = BASE + urllib.parse.quote(path, safe="/?=&")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Auth-Token", H["X-Auth-Token"])
    if data:
        req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300], time.time() - t0
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", time.time() - t0


# 要压的接口：[方法, 路径, 说明, 请求体]
IMPORT_TEXT = """第9周｜测试导入｜3词
第1组｜压测第一天
fkimport1 /ˈkɒliːɡ/ — 同事
fkimport2 /ˈkʌmpəni/ — 公司
fkimport3 /pəˈzɪʃn/ — 职位"""

CASES = [
    ("GET",  "/api/health",           "健康检查", None),
    ("GET",  "/api/home",             "首页汇总", None),
    ("GET",  "/api/report",           "【月报】总结页统计", None),
    ("GET",  "/api/activity?weeks=8", "【周报】8 周活动聚合", None),
    ("GET",  "/api/activity?weeks=4", "【周报】4 周活动聚合", None),
    ("GET",  "/api/weakness",         "【薄弱项】综合判定", None),
    ("GET",  "/api/errors",           "错误本统计", None),
    ("GET",  "/api/errors/trend",     "错误趋势", None),
    ("GET",  "/api/errors/非谓语",     "错误本明细（非谓语）", None),
    ("GET",  "/api/training/summary", "专项训练汇总", None),
    ("GET",  "/api/training/state",   "专项训练四层状态", None),
    ("GET",  "/api/test/projects",    "训练项目列表", None),
    ("GET",  "/api/word/fk11/profile", "词全息档案", None),
    ("GET",  "/api/history",          "历史记录", None),
    ("GET",  "/api/review/due",       "待复习", None),
    ("GET",  "/api/vocab/all",        "全部词汇", None),
    ("GET",  "/api/weak/snapshots",   "薄弱项快照", None),
    ("POST", "/api/words/import",     "【导入】压测 OOM",
     {"text": IMPORT_TEXT, "merge": True, "week": 9}),
]


def start_server(db_path):
    """起 uvicorn 子进程，返回 Popen。"""
    env = dict(os.environ)
    env["EOS_DB"] = db_path
    env["ACCESS_TOKEN"] = H["X-Auth-Token"]
    env.pop("DATABASE_URL", None)

    backend = None
    for cand in [HERE, os.path.join(HERE, "backend"),
                 os.path.join(HERE, "..", "..", "..", "tmp", "pushtest", "backend")]:
        if os.path.exists(os.path.join(cand, "db.py")):
            backend = cand
            break
    if not backend:
        print("❌ 找不到 backend/db.py")
        sys.exit(1)

    log = open("/tmp/fake8_stress_srv.log", "w")
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=backend, env=env, stdout=log, stderr=subprocess.STDOUT)
    return p


def wait_ready(p, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if p.poll() is not None:
            print("❌ 服务进程提前退出，日志：")
            print(open("/tmp/fake8_stress_srv.log").read()[-2000:])
            return False
        try:
            s, _, _ = http("GET", "/api/health")
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser(description="8 周假数据压测（周报/月报/薄弱项）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--scale", type=int, default=1, help="数据量倍数")
    ap.add_argument("--regen", action="store_true", help="强制重新生成假数据")
    ap.add_argument("--keep", action="store_true", help="跑完保留服务进程不关")
    args = ap.parse_args()

    # 1. 准备数据
    if args.regen or not os.path.exists(args.db):
        print(f"生成 8 周假数据 → {args.db}（规模 ×{args.scale}）")
        cmd = [sys.executable, os.path.join(HERE, "fake8_seed.py"),
               "--db", args.db, "--scale", str(args.scale)]
        if args.regen:
            cmd.append("--force")
        r = subprocess.run(cmd, capture_output=True, text=True)
        print(r.stdout[-1500:])
        if r.returncode != 0:
            print(r.stderr[-1500:])
            return 1
    else:
        print(f"复用已有数据: {args.db}")

    size = os.path.getsize(args.db) / 1024 / 1024
    print(f"数据库大小: {size:.1f} MB\n")

    # 2. 起服务
    print(f"启动服务 (端口 {PORT}) ...")
    srv = start_server(args.db)
    try:
        if not wait_ready(srv):
            return 1
        base_rss = rss_mb(srv.pid)
        print(f"服务就绪，基线内存 {base_rss:.0f} MB\n")

        # 3. 逐个压
        # 每个接口跑 REPEAT 次取中位数：第一次往往包含冷库 IO / 惰性初始化，
        # 只看单次会把一次性开销误判成持续问题（实测 /api/home 首次 3.2s、
        # 之后稳定在 4ms，就是这么来的）。
        REPEAT = int(os.environ.get("STRESS_REPEAT", "3"))
        print(f"{'接口':<32} {'状态':<5} {'中位耗时':>9} {'最慢':>9} {'内存':>9} {'增量':>8}")
        print("-" * 74)
        rows = []
        peak = base_rss
        for method, path, desc, body in CASES:
            before = rss_mb(srv.pid)
            times_ms, last_st, last_txt = [], 0, ""
            for _ in range(REPEAT):
                st, txt, el = http(method, path, body)
                times_ms.append(el * 1000)
                last_st, last_txt = st, txt
            after = rss_mb(srv.pid)
            peak = max(peak, after or 0)
            delta = (after - before) if (before and after) else 0
            med = statistics.median(times_ms)
            mx = max(times_ms)
            flag = "✅" if last_st == 200 else "❌"
            print(f"{flag} {desc:<30} {last_st:<5} {med:>7.0f}ms {mx:>7.0f}ms "
                  f"{after:>7.0f}MB {delta:>+7.1f}MB")
            rows.append({"desc": desc, "path": path, "status": last_st,
                         "ms": round(med), "ms_max": round(mx),
                         "rss": round(after or 0),
                         "delta": round(delta, 1),
                         "size": len(last_txt)})
            # 输出关键接口的返回内容摘要，方便肉眼核对数字算得对不对
            if desc.startswith("【") and st == 200:
                try:
                    j = json.loads(txt)
                    print(f"      └ {json.dumps(j, ensure_ascii=False)[:200]}")
                except Exception:
                    print(f"      └ {txt[:200]}")

        # 4. 汇总
        print("\n" + "=" * 70)
        print("汇总")
        print("=" * 70)
        bad = [r for r in rows if r["status"] != 200]
        slow = sorted(rows, key=lambda r: -r["ms"])[:3]
        grow = sorted(rows, key=lambda r: -r["delta"])[:3]
        print(f"  请求总数        {len(rows)}")
        print(f"  失败            {len(bad)}")
        for r in bad:
            print(f"      ❌ {r['desc']} → HTTP {r['status']}")
        print(f"  基线内存        {base_rss:.0f} MB")
        print(f"  峰值内存        {peak:.0f} MB")
        print(f"  最慢的三个      " +
              ", ".join(f"{r['desc']} {r['ms']}ms" for r in slow))
        print(f"  内存涨最多的    " +
              ", ".join(f"{r['desc']} {r['delta']:+.1f}MB" for r in grow))

        # Render 512MB 判定
        limit = 512
        print()
        if peak > limit:
            print(f"  ⚠️  峰值 {peak:.0f}MB 已超 Render 免费实例 {limit}MB 上限")
        elif peak > limit * 0.7:
            print(f"  ⚠️  峰值 {peak:.0f}MB 已达 {limit}MB 上限的 "
                  f"{peak/limit*100:.0f}%，余量不多了")
        else:
            print(f"  ✅ 峰值 {peak:.0f}MB，距 {limit}MB 上限还有 "
                  f"{limit - peak:.0f}MB 余量")

        json.dump(rows, open("/tmp/fake8_stress_result.json", "w"),
                  ensure_ascii=False, indent=2)
        print("\n明细已存 /tmp/fake8_stress_result.json")
    finally:
        if args.keep:
            print(f"\n服务保留在 {BASE}（pid {srv.pid}），测完手动 kill {srv.pid}")
        else:
            srv.terminate()
            try:
                srv.wait(timeout=10)
            except Exception:
                srv.kill()
            print("\n服务已关闭")

    print(f"\n清理数据： python3 {os.path.join(HERE, 'fake8_seed.py')} "
          f"--db {args.db} --clean")
    print(f"或直接删库： rm -f {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
