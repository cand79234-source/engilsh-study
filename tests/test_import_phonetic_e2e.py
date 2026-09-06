# -*- coding: utf-8 -*-
"""端到端验证：AI 词表导入后，音标被正确保留到 vocab_json 中。"""
import os, sys, tempfile, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import db as D
import weekimport as WI


def test_phonetic_preserved_in_import():
    """模拟 AI 生成的带音标词表，导入后音标必须出现在 vocab_json 中。"""
    tmpdb = tempfile.mktemp(suffix=".db")
    os.environ["EOS_DB"] = tmpdb
    D.init_db()
    try:
        # AI 生成的典型词表格式：单词 — 释义（音标在单词后面）
        text = """Week 1｜测试周
Day 1｜测试组
continue /kənˈtɪnjuː/ — v. 继续
improve /ɪmˈpruːv/ — v. 改善
apple — n. 苹果
"""
        res = WI.import_rich_week(text, forced_stage=1, forced_week=1)
        assert res["ok"], res.get("error", "导入失败")

        # 查 weeks 表的 vocab_json
        conn = D.get_conn()
        row = conn.execute(
            "SELECT vocab_json FROM weeks WHERE stage=1 AND week_no=1"
        ).fetchone()
        conn.close()
        assert row, "weeks 表没有写入记录"
        vocab = json.loads(row["vocab_json"] or "[]")
        assert len(vocab) == 3, f"应有 3 个词，实际 {len(vocab)}"

        # 音标必须被保留
        by_word = {v["word"]: v for v in vocab}
        assert by_word["continue"].get("phonetic") == "/kənˈtɪnjuː/", \
            f"continue 音标缺失/错误: {by_word['continue'].get('phonetic')!r}"
        assert by_word["improve"].get("phonetic") == "/ɪmˈpruːv/", \
            f"improve 音标缺失/错误: {by_word['improve'].get('phonetic')!r}"
        # 没有音标的词，phonetic 应为空字符串
        assert by_word["apple"].get("phonetic") == "", \
            f"apple 应无音标: {by_word['apple'].get('phonetic')!r}"
        print("PASS: 音标在导入后正确保留到 vocab_json")
    finally:
        try:
            os.unlink(tmpdb)
        except OSError:
            pass


def test_phonetic_preserved_in_merge():
    """合并导入路径（import_rich_week_merge）同样保留音标。"""
    tmpdb = tempfile.mktemp(suffix=".db")
    os.environ["EOS_DB"] = tmpdb
    D.init_db()
    try:
        text = """Week 1｜测试周
Day 1｜测试组
hello /həˈloʊ/ — int. 你好
"""
        res = WI.import_rich_week_merge(text, forced_stage=1, forced_week=1)
        assert res["ok"], res.get("error", "合并导入失败")

        conn = D.get_conn()
        row = conn.execute(
            "SELECT vocab_json FROM weeks WHERE stage=1 AND week_no=1"
        ).fetchone()
        conn.close()
        vocab = json.loads(row["vocab_json"] or "[]")
        hello = next((v for v in vocab if v["word"] == "hello"), None)
        assert hello is not None, "hello 不在 vocab 中"
        assert hello.get("phonetic") == "/həˈloʊ/", \
            f"merge 路径音标丢失: {hello.get('phonetic')!r}"
        print("PASS: merge 路径音标也正确保留")
    finally:
        try:
            os.unlink(tmpdb)
        except OSError:
            pass


if __name__ == "__main__":
    test_phonetic_preserved_in_import()
    test_phonetic_preserved_in_merge()
