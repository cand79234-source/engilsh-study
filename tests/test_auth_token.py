"""
H1 + H2 回归测试：访问口令 + 401 + OPTIONS 预检

- 不设 EOS_TOKEN：全开放（本地模式）
- 设 EOS_TOKEN=xxx：
  - /api/health 永远 200
  - 其他 /api/* 没带口令 401
  - 带 X-Auth-Token: xxx 200
  - 带 Authorization: Bearer xxx 200
  - OPTIONS 预检 2xx + ACAO 头（不 401）
  - 错口令 401
"""
import os
import sys
import importlib

# 切到 backend 目录
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.normpath(os.path.join(HERE, "..", "backend"))
os.chdir(BACKEND)
sys.path.insert(0, BACKEND)

# 强制设口令
os.environ["EOS_TOKEN"] = "test-tok-123"

# 重新加载 main（确保新中间件生效）
import main
importlib.reload(main)

# 复用 fastapi TestClient
from fastapi.testclient import TestClient

client = TestClient(main.app)


def t(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name} {detail}")
    if not ok:
        global _failed
        _failed = True


_failed = False

# 1) /api/health 永远 200
r = client.get("/api/health")
t("health no-token 200", r.status_code == 200, f"got {r.status_code}")

# 2) 其他 /api/* 无口令 401
r = client.get("/api/today")
t("protected no-token 401", r.status_code == 401, f"got {r.status_code}")

# 3) 错口令 401
r = client.get("/api/today", headers={"X-Auth-Token": "WRONG"})
t("wrong-token 401", r.status_code == 401, f"got {r.status_code}")

# 4) X-Auth-Token 正确 200
r = client.get("/api/today", headers={"X-Auth-Token": "test-tok-123"})
t("X-Auth-Token correct 200", r.status_code == 200, f"got {r.status_code}")

# 5) Authorization: Bearer 正确 200
r = client.get("/api/today", headers={"Authorization": "Bearer test-tok-123"})
t("Bearer correct 200", r.status_code == 200, f"got {r.status_code}")

# 6) OPTIONS 预检：不能 401，且要有 ACAO 头
r = client.options(
    "/api/today",
    headers={
        "Origin": "https://example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-auth-token",
    },
)
ok = (r.status_code in (200, 204)) and ("access-control-allow-origin" in {k.lower() for k in r.headers.keys()})
t("OPTIONS preflight not 401 + has ACAO", ok, f"got {r.status_code} headers={[k for k in r.headers.keys()]}")

# 7) 同上：OPTIONS 即便带了口令也不该 401（H2）
r = client.options(
    "/api/today",
    headers={"X-Auth-Token": "WRONG", "Origin": "https://example.com",
             "Access-Control-Request-Method": "POST"},
)
t("OPTIONS with wrong token still not 401", r.status_code != 401, f"got {r.status_code}")

# 8) 健康检查带错口令不影响
r = client.get("/api/health", headers={"X-Auth-Token": "WRONG"})
t("health ignores token", r.status_code == 200, f"got {r.status_code}")

# ---- 本地模式（不设 EOS_TOKEN）----
del os.environ["EOS_TOKEN"]
importlib.reload(main)
from fastapi.testclient import TestClient as TC2
client2 = TC2(main.app)
r = client2.get("/api/today")
t("local mode no-token 200", r.status_code == 200, f"got {r.status_code}")

print()
if _failed:
    print("FAILED")
    sys.exit(1)
else:
    print("ALL PASSED")
