# -*- coding: utf-8 -*-
"""文件 → 文本 提取器（多格式，尽量零依赖）。

支持格式与策略（优先级从高到低，全部失败才报错）：

| 格式           | 策略                                                     |
|----------------|----------------------------------------------------------|
| .docx          | 标准库 zipfile+XML 解析（兼容 w:cr/w:br 换行的"聊天粘贴型"文档）|
| .txt/.md/.csv  | 直接读（UTF-8 → GBK → latin1 依次尝试）                    |
| .json          | 直读；若解析成功则递归抽取所有字符串字段拼成行              |
| .html/.htm     | 宵准库去标签（<br>/<p>/<tr>/<li> → 换行）                   |
| .rtf           | 去控制字，保留文本                                        |
| .xlsx/.xlsm    | openpyxl 优先；标准库 zipfile 读 sharedStrings+sheet 兜底   |
| .pdf           | pdfplumber → pypdf → pdftotext 命令，三级兜底              |
| .doc           | OLE 文本流 best-effort 提取（建议另存为 .docx）             |
| 其他/无扩展名   | 按 UTF-8/GBK 文本尝试读                                   |

设计原则：
- 纯标准库即可处理 docx/txt/md/csv/json/html/rtf（部署环境无需额外安装）。
- 提取后统一清洗零宽字符（\ufeff/\u200b/\u200c/\u200d）与不间断空格（\xa0），
  这类字符来自网页/聊天工具粘贴，是"解析失败"的常见元凶。
"""
import io
import json
import os
import re
import shutil
import subprocess
import zipfile

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB

SUPPORTED_EXTS = [
    ".docx", ".doc", ".pdf", ".txt", ".md", ".markdown", ".csv", ".tsv",
    ".json", ".html", ".htm", ".rtf", ".xlsx", ".xlsm", ".xls", ".log", "",
]

# 零宽/不可见字符（网页与聊天工具粘贴的典型产物）
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060\u00ad]")
# HTML 块级标签 → 换行
_HTML_BLOCK_RE = re.compile(
    r"</?(?:p|div|br|tr|li|h[1-6]|table|section|article|blockquote|pre|hr)[^>]*>",
    re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&ldquo;": "\u201c", "&rdquo;": "\u201d",
    "&middot;": "·", "&hellip;": "…", "&mdash;": "—",
}


def clean_text(text):
    """统一清洗：去零宽字符、\xa0→空格、\r\n→\n、去行首尾空白、压缩连续空行。"""
    if not text:
        return ""
    text = _INVISIBLE_RE.sub("", text)
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    out, blank = [], 0
    for ln in lines:
        if ln:
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1 and out:
                out.append("")
    return "\n".join(out).strip()


def _read_bytes(raw):
    """按 UTF-8 → GBK → latin1 尝试解码 bytes。"""
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin1", errors="replace")


# ---------------- DOCX ----------------
def _docx_xml_to_text(xml_bytes):
    """把 word/document.xml 转成纯文本。

    关键：行分隔不仅来自 <w:p>（段落），还可能来自段内的 <w:cr/> 和 <w:br/>
    （"聊天粘贴型" docx 全文挤在一个 <w:p> 里、用 <w:cr/> 换行——python-docx
    读这种文件会把整篇挤成一行）。这里做 XML 级解析，两种换行都识别。
    """
    import xml.etree.ElementTree as ET

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ""
    out_lines = []
    buf = []          # 当前行累积
    pending_break = 0  # 待处理的换行数（段内 cr/br）

    def flush():
        line = "".join(buf).strip()
        if line:
            out_lines.append(line)
        buf.clear()

    def walk(elem):
        nonlocal pending_break
        tag = elem.tag
        if tag in (W + "cr", W + "br"):
            pending_break += 1
            return
        if tag == W + "t":
            if pending_break:
                flush()
                pending_break = 0
            buf.append(elem.text or "")
            return
        if tag == W + "tab":
            buf.append(" ")
            return
        for child in elem:
            walk(child)
        if tag == W + "p":  # 段落结束 = 换行
            flush()
            pending_break = 0

    body = root.find(W + "body")
    walk(body if body is not None else root)
    flush()
    return "\n".join(out_lines)


def extract_docx(raw):
    """docx → 文本。纯标准库实现（不依赖 python-docx）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            names = set(z.namelist())
            # 主体：document.xml；WPS/兼容模式可能在 word/document.xml 之外
            doc_name = None
            for cand in ("word/document.xml",):
                if cand in names:
                    doc_name = cand
                    break
            if doc_name is None:
                # 找 word/ 下第一个 document*.xml
                for n in sorted(names):
                    if re.match(r"word/document\d*\.xml$", n):
                        doc_name = n
                        break
            if doc_name is None:
                raise ValueError("docx 内没有 word/document.xml（文件可能已损坏或不是 docx）")
            xml_bytes = z.read(doc_name)
            text = _docx_xml_to_text(xml_bytes)
            # 兜底：若 XML 解析结果为空但 python-docx 可用，再试一次
            if not text.strip():
                try:
                    import docx  # python-docx
                    d = docx.Document(io.BytesIO(raw))
                    text = "\n".join(p.text for p in d.paragraphs if p.text.strip())
                except Exception:
                    pass
            return text
    except zipfile.BadZipFile:
        raise ValueError("文件不是有效的 .docx（如果是老版 .doc 请另存为 .docx 再试）")


# ---------------- HTML ----------------
def extract_html(raw):
    text = _read_bytes(raw)
    # 去掉 script/style
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    text = _HTML_BLOCK_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub("", text)
    for k, v in _HTML_ENTITY.items():
        text = text.replace(k, v)
    text = re.sub(r"&#x?[0-9a-fA-F]+;", "", text)
    return text


# ---------------- RTF ----------------
def extract_rtf(raw):
    text = _read_bytes(raw)
    text = re.sub(r"\{\\\*[^{}]*\}", "", text)            # 忽略组
    text = re.sub(r"\\par[d]?\b", "\n", text)             # 段落 → 换行
    text = re.sub(r"\\line\b", "\n", text)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) & 0xFFFF), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)        # 其余控制字
    text = text.replace("{", "").replace("}", "")
    return text


# ---------------- JSON ----------------
def _json_strings(obj):
    """递归抽取 JSON 里的所有字符串值，一行一个（保留结构感：列表元素分行）。"""
    out = []
    if isinstance(obj, str):
        if obj.strip():
            out.append(obj.strip())
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_json_strings(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_json_strings(v))
    return out


def extract_json(raw):
    text = _read_bytes(raw)
    try:
        obj = json.loads(text)
        lines = _json_strings(obj)
        if lines:
            return "\n".join(lines)
    except Exception:
        pass
    return text  # 不是合法 JSON 就当纯文本


# ---------------- XLSX ----------------
def extract_xlsx(raw, warn):
    # 1) openpyxl
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        lines = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c).strip() for c in row]
                line = "\t".join(c for c in cells if c)
                if line.strip():
                    lines.append(line)
        return "\n".join(lines)
    except ImportError:
        pass
    except Exception as e:
        warn.append(f"openpyxl 解析失败（{e}），尝试备用方案。")
    # 2) 标准库兜底：读 sharedStrings.xml + sheet*.xml
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            import xml.etree.ElementTree as ET
            NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            shared = []
            if "xl/sharedStrings.xml" in z.namelist():
                sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in sroot.findall(NS + "si"):
                    shared.append("".join(t.text or "" for t in si.iter(NS + "t")))
            lines = []
            sheet_names = [n for n in sorted(z.namelist())
                           if re.match(r"xl/worksheets/sheet\d+\.xml", n)]
            for sn in sheet_names:
                root = ET.fromstring(z.read(sn))
                for row in root.iter(NS + "row"):
                    cells = []
                    for c in row.iter(NS + "c"):
                        v = c.find(NS + "v")
                        if v is None or v.text is None:
                            continue
                        if c.get("t") == "s":
                            try:
                                cells.append(shared[int(v.text)])
                            except Exception:
                                pass
                        else:
                            cells.append(v.text)
                    line = "\t".join(x.strip() for x in cells if x.strip())
                    if line:
                        lines.append(line)
            return "\n".join(lines)
    except Exception as e:
        raise ValueError(f"xlsx 解析失败：{e}（可安装 openpyxl 后重试）")


# ---------------- PDF ----------------
def extract_pdf(raw, warn):
    # 1) pdfplumber
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            for pg in pdf.pages:
                pages.append(pg.extract_text() or "")
        return "\n".join(p for p in pages if p)
    except ImportError:
        pass
    except Exception as e:
        warn.append(f"pdfplumber 解析失败（{e}），尝试备用方案。")
    # 2) pypdf
    try:
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        pages = [(pg.extract_text() or "") for pg in reader.pages]
        return "\n".join(p for p in pages if p)
    except ImportError:
        pass
    except Exception as e:
        warn.append(f"pypdf 解析失败（{e}），尝试备用方案。")
    # 3) pdftotext 命令（poppler-utils）
    if shutil.which("pdftotext"):
        try:
            p = subprocess.run(["pdftotext", "-layout", "-", "-"], input=raw,
                               capture_output=True, timeout=60)
            if p.returncode == 0 and p.stdout:
                return p.stdout.decode("utf-8", errors="replace")
        except Exception:
            pass
    raise ValueError("无法解析 PDF：请安装 pdfplumber（pip install pdfplumber）"
                     "或 poppler-utils（apt install poppler-utils）后重试。")


# ---------------- DOC（老二进制，best-effort） ----------------
def extract_doc(raw, warn):
    # 优先 antiword / catdoc
    for cmd in (["antiword", "-"], ["catdoc", "-"]):
        if shutil.which(cmd[0]):
            try:
                p = subprocess.run(cmd, input=raw, capture_output=True, timeout=60)
                out = p.stdout.decode("utf-8", errors="replace").strip()
                if out:
                    return out
            except Exception:
                pass
    # olefile（若装了）读 WordDocument 流做简单提取
    try:
        import olefile
        ole = olefile.OleFileIO(io.BytesIO(raw))
        if ole.exists("WordDocument"):
            data = ole.openstream("WordDocument").read()
            # 2 字节宽字符启发式：连续 UTF-16LE 可打印段
            text = data.decode("utf-16-le", errors="ignore")
            text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
            if len(text.strip()) > 20:
                warn.append("老版 .doc 为尽力提取，格式可能不完整，建议另存为 .docx 后重试。")
                return text
    except Exception:
        pass
    # 最后兜底：字节流里抽可打印 ASCII + 常见中文（GBK）
    try:
        text = raw.decode("gb18030", errors="ignore")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
        cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        if cjk > 10:
            warn.append("老版 .doc 为尽力提取，格式可能不完整，建议另存为 .docx 后重试。")
            return text
    except Exception:
        pass
    raise ValueError("无法解析老版 .doc：请用 Word/WPS 另存为 .docx 后重新上传。")


def extract_text(raw, filename):
    """主入口：bytes + 文件名 → (纯文本, format, warnings)。

    任何格式失败都会抛 ValueError（带人话原因），由 API 层转成友好错误。
    """
    if not raw:
        raise ValueError("文件内容为空。")
    if len(raw) > MAX_FILE_SIZE:
        raise ValueError("文件超过 20MB，请拆分后上传。")
    ext = os.path.splitext(filename or "")[1].lower()
    warn = []

    if ext == ".docx":
        text = extract_docx(raw)
        fmt = "docx"
    elif ext == ".doc":
        text = extract_doc(raw, warn)
        fmt = "doc"
    elif ext == ".pdf":
        text = extract_pdf(raw, warn)
        fmt = "pdf"
    elif ext in (".xlsx", ".xlsm"):
        text = extract_xlsx(raw, warn)
        fmt = "xlsx"
    elif ext in (".xls",):
        # 老 xls：无标准库方案，尝试按 xls→(可能其实是 xml/xlsx 改名) 或提示
        if raw[:2] == b"PK":
            text = extract_xlsx(raw, warn)
        else:
            raise ValueError("暂不支持老版 .xls：请用 Excel 另存为 .xlsx 后重新上传。")
        fmt = "xls"
    elif ext in (".html", ".htm"):
        text = extract_html(raw)
        fmt = "html"
    elif ext == ".rtf":
        text = extract_rtf(raw)
        fmt = "rtf"
    elif ext == ".json":
        text = extract_json(raw)
        fmt = "json"
    elif ext in ("", ".txt", ".md", ".markdown", ".csv", ".tsv", ".log",
                 ".text", ".srt", ".vtt", ".xml", ".yml", ".yaml", ".ini", ".conf"):
        text = _read_bytes(raw)
        # srt/vtt 字幕去掉时间轴行
        if ext in (".srt", ".vtt"):
            text = re.sub(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->.*$", "",
                          text, flags=re.M)
        fmt = ext.lstrip(".") or "text"
    else:
        # 未知扩展名：嗅探内容（zip? pdf? 还是纯文本?）
        if raw[:2] == b"PK":
            try:
                text = extract_docx(raw)
                fmt = "docx(zip嗅探)"
            except Exception:
                try:
                    text = extract_xlsx(raw, warn)
                    fmt = "xlsx(zip嗅探)"
                except Exception:
                    raise ValueError(f"不支持的格式「{ext or '未知'}」："
                                     f"请另存为 docx/pdf/txt/xlsx 后重试。")
        elif raw[:5] == b"%PDF-":
            text = extract_pdf(raw, warn)
            fmt = "pdf(嗅探)"
        else:
            text = _read_bytes(raw)
            fmt = f"{ext or '未知扩展名'}(按文本读取)"
            warn.append(f"按纯文本读取了「{ext or '未知'}」格式，若乱码请另存为 docx/pdf/txt。")

    text = clean_text(text)
    if not text:
        raise ValueError("文件里没有提取到任何文字内容"
                         "（可能是纯图片型 PDF/扫描件，请上传文字版或直接粘贴文本）。")
    return text, fmt, warn
