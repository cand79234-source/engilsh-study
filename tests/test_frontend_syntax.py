# -*- coding: utf-8 -*-
"""前端整份内联 JS 必须能被 JS 引擎解析。

为什么要这条测试：整站所有功能都写在 index.html 的**同一个** <script> 块里。
只要里面有一处括号没闭合，浏览器会直接拒绝执行整个脚本块 ——
表现不是「某个按钮坏了」，而是**整站白屏、所有功能一起消失**。

而且这类错误在 Python 侧的测试里完全看不见（后端跑得好好的），
只有真的用浏览器打开才会暴露。历史上真的发生过一次：
某版部署出去的 HTML 里 `_anaMetric('造句均分', mval((...))` 少一个右括号，
线上直接白屏，而本地后端测试全绿。

这里用 node --check 做静态解析，把这类事故挡在提交之前。
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "frontend", "index.html")


def _inline_js(path):
    """取出 index.html 里所有**可执行**的内联 script（跳过模板块）。"""
    src = open(path, encoding="utf-8").read()
    blocks = re.findall(
        r'<script(?![^>]*src=)([^>]*)>(.*?)</script>', src,
        re.DOTALL | re.IGNORECASE)
    return "\n;\n".join(
        body for attrs, body in blocks
        if "type=" not in attrs)


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能静态校验")
def test_index_html_inline_js_is_parseable():
    """整份内联 JS 必须能被 node 解析。"""
    assert os.path.exists(INDEX), INDEX
    js = _inline_js(INDEX)
    assert js.strip(), "没从 index.html 里提取到任何内联 JS —— 提取正则失效了？"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        tmp = f.name
    try:
        r = subprocess.run(["node", "--check", tmp],
                           capture_output=True, text=True)
        assert r.returncode == 0, (
            f"index.html 的内联 JS 有语法错误（会导致整站白屏）：\n{r.stderr[:2000]}")
    finally:
        os.unlink(tmp)


def test_found_inline_script_blocks():
    """防扫描器空转：确认确实抓到了脚本块，而不是正则悄悄失配。"""
    src = open(INDEX, encoding="utf-8").read()
    blocks = re.findall(r'<script(?![^>]*src=)([^>]*)>', src, re.IGNORECASE)
    executable = [a for a in blocks
                  if "text/plain" not in a and "text/template" not in a]
    assert executable, "index.html 里没找到可执行的内联 script 块"
