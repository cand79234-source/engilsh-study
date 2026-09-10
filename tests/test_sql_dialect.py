"""SQL 方言回归：翻译成 PostgreSQL 后必须合法。

背景：db._tr() 负责把 SQLite 风格 SQL 翻成 PostgreSQL 风格。
它原先只翻译 `INSERT OR IGNORE`，漏了 `INSERT OR REPLACE` ——
而 PostgreSQL 根本没有 INSERT OR REPLACE 这种语法（SQLite 专有），
官方解析器直接报 `syntax error at or near "OR"`。

后果：凡是用了 INSERT OR REPLACE 的接口，在 PostgreSQL（Render/Neon）上
必定 500，但本地 SQLite 一切正常 —— 这类问题在本机怎么测都测不出来。
当时中招的是听力的两个接口（导入 / 提交作答）。

本测试用 pglast（真正的 PostgreSQL 官方解析器绑定）做静态校验，
不依赖本地有没有 PG 实例。若环境没装 pglast 则跳过，不阻塞其他测试。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
import db

pglast = pytest.importorskip("pglast", reason="需要 pglast（PostgreSQL 官方解析器）")

BACKEND = os.path.join(os.path.dirname(__file__), "..", "backend")
MAIN = os.path.join(BACKEND, "main.py")


_STR_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _extract_sql_at(src, pos):
    """pos 落在某个 SQL 关键字上。回退到它所属字符串字面量的起始引号，
    再把紧邻其后的字符串字面量一路拼起来，得到源码里那条完整 SQL。

    源码里 SQL 常常被拆成多行写：
        "INSERT OR REPLACE INTO t "
        "(a, b, c) "
        "VALUES (?,?,?)",
        (params...)

    只在「前一个字面量结束后、中间只有空白/换行、下一个字符又是引号」时
    才继续拼接；一旦遇到逗号（参数元组）或右括号就停，
    避免把后面函数体里的字符串也一起吞进来。
    """
    quote = src.rfind('"', 0, pos)
    while quote >= 0:
        m = _STR_RE.match(src, quote)
        if m and m.end() > pos:      # 确认 pos 确实落在这个字面量内部
            break
        quote = src.rfind('"', 0, quote)
    else:
        return None                  # pos 不在任何字符串里（注释或文档）

    parts, i = [], quote
    while True:
        m = _STR_RE.match(src, i)
        if not m:
            break
        parts.append(m.group(1))
        i = m.end()
        j = i
        while j < len(src) and src[j] in " \t\r\n":
            j += 1
        if j < len(src) and src[j] == '"':
            i = j                    # 还是字符串 → 同一条 SQL 的下一截
            continue
        break                        # 逗号 / 右括号 / 其它代码 → 结束
    return "".join(parts) if parts else None


def _or_statements():
    """扫 backend 下所有 INSERT OR xxx 语句，返回 (文件, 表名, 原文)。"""
    found = []
    for fn in sorted(os.listdir(BACKEND)):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(BACKEND, fn)
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r"INSERT OR (IGNORE|REPLACE)\s+INTO\s+(\w+)", src, re.IGNORECASE):
            # 跳过注释里提到的示例（说明文字不是真要执行的 SQL）
            line_start = src.rfind("\n", 0, m.start()) + 1
            if src[line_start:m.start()].lstrip().startswith("#"):
                continue
            sql = _extract_sql_at(src, m.start())
            if not sql:
                continue
            found.append((fn, m.group(2), sql))
    return found


def test_found_or_statements():
    """确保扫描器真的扫到了语句（防止正则失效后测试变成空转）。"""
    stmts = _or_statements()
    assert stmts, "没扫到任何 INSERT OR ... 语句，扫描正则可能失效了"
    # 已知听力两处用的是 REPLACE
    replaces = [s for s in stmts if "OR REPLACE" in s[2].upper()]
    assert {"listening_materials", "listening_progress"} <= {s[1] for s in replaces}


@pytest.mark.parametrize("fn,tbl,sql", _or_statements())
def test_translated_sql_is_valid_postgres(fn, tbl, sql):
    """翻译后的 SQL 必须能被 PostgreSQL 官方解析器接受。"""
    filled = re.sub(r"\?", "'x'", sql)          # 占位符填空值，只为校验语法
    translated = db._tr(filled)
    try:
        pglast.parse_sql(translated)
    except Exception as e:
        pytest.fail(f"{fn} 表 {tbl} 翻译后 PG 仍不合法：{e}\n  SQL: {translated[:200]}")


def test_replace_becomes_on_conflict_do_update():
    """INSERT OR REPLACE 必须变成 ON CONFLICT ... DO UPDATE（而不是原样透传）。"""
    sql = ("INSERT OR REPLACE INTO listening_progress "
           "(stage, week, day, listening_done, listening_total, parts_json, created_at) "
           "VALUES (?,?,?,?,?,?,?)")
    out = db._tr(sql)
    assert "OR REPLACE" not in out, f"没被翻译，PG 上会直接语法错误：{out}"
    assert "ON CONFLICT (stage, week, day) DO UPDATE SET" in out
    # 冲突列自己不该出现在 SET 里
    assert "stage=excluded.stage" not in out
    # 其余列都要更新
    assert "listening_done=excluded.listening_done" in out
    assert "parts_json=excluded.parts_json" in out


def test_ignore_still_becomes_do_nothing():
    """原有 INSERT OR IGNORE 的翻译不能被这次改动破坏。"""
    sql = "INSERT OR IGNORE INTO dictionary (word, meaning) VALUES (?,?)"
    out = db._tr(sql)
    assert "ON CONFLICT (word) DO NOTHING" in out
    assert "?" not in out          # 占位符也要换成 %s
    assert "%s" in out
