"""欧陆式点词查义 —— /api/dict/lookup 单测（本地 SQLite，零密钥、零外部）。

验证：
  ① 已收录词（大小写不敏感）→ found=true 且返回音标/词性/释义
  ② 未收录词 → found=false
  ③ 空词 / 超长 → found=false（不报错）
"""
import os
import sys
import tempfile

os.environ.setdefault("EOS_DB", tempfile.mktemp(suffix=".db"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient
import db
import main

# 预置一个唯一测试词进本地词典（避免与后台播种词冲突）
_CONN = db.get_conn()
_CONN.execute(
    "INSERT OR IGNORE INTO dictionary(word, phonetic, pos, meaning) VALUES(?,?,?,?)",
    ("eostestword", "/ˈtɛst/", "名词", "测试词；仅用于单测"),
)
_CONN.commit()
_CONN.close()

client = TestClient(main.app)


def test_lookup_found_case_insensitive():
    r = client.post("/api/dict/lookup", json={"word": "EosTestWord"})
    assert r.status_code == 200
    d = r.json()
    assert d["found"] is True
    assert d["word"] == "eostestword"
    assert d["phonetic"] == "/ˈtɛst/"
    assert d["pos"] == "名词"
    assert "测试词" in d["meaning"]


def test_lookup_not_found():
    r = client.post("/api/dict/lookup", json={"word": "zzzqqqnotaword"})
    assert r.status_code == 200
    assert r.json()["found"] is False


def test_lookup_empty_and_overlong():
    assert client.post("/api/dict/lookup", json={"word": ""}).json()["found"] is False
    assert client.post("/api/dict/lookup", json={"word": "x" * 80}).json()["found"] is False
