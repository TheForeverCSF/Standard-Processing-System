# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""纯文本类文件处理管道：.txt / .csv / .tsv / .md / .log / .rtf。

这些格式没有"页"的概念，统一按**行**处理（CSV 按行 = 记录），
页码字段写成「第N行」，与 Excel 的「工作表 行N」保持一致的表达习惯。
"""
import csv
import io
import os
from typing import Any, Callable, Dict, List, Tuple

from utils.helpers import search_standard_names, resolve_standard_display

TEXT_EXTS = (".txt", ".text", ".csv", ".tsv", ".md", ".markdown", ".log", ".rtf")

# 编码探测顺序：中文 Windows 下的文本文件常见 GBK/GB18030
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16", "cp936", "big5", "latin-1")

# 体积与行数上限：避免用户误选一个几百 MB 的日志把界面拖死
MAX_BYTES = 50 * 1024 * 1024
MAX_LINES = 200000
MAX_PREVIEW_CHARS = 400000


class TextHandler:
    """纯文本管道：编码自适应 + CSV/TSV 分列 + RTF 去控制字。"""

    def __init__(self, on_log: Callable[[str], None] = None,
                 on_progress: Callable[[int], None] = None):
        self.on_log = on_log or (lambda x: None)
        self.on_progress = on_progress or (lambda x: None)
        self.cancel_check = lambda: None

    # ── 读取与解码 ────────────────────────────────────────
    @staticmethod
    def read_text(file_path: str) -> Tuple[str, str]:
        """读取文本并自动判编码，返回 (文本, 使用的编码)"""
        ext = os.path.splitext(file_path)[1].lower()
        size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            raw = f.read(MAX_BYTES) if size > MAX_BYTES else f.read()

        if ext == ".rtf":
            return TextHandler.rtf_to_text(raw), "rtf"

        # 有 BOM 直接按 BOM 判定
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw.decode("utf-8-sig", "replace"), "utf-8-sig"
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16", "replace"), "utf-16"

        for enc in _ENCODINGS:
            try:
                return raw.decode(enc), enc
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("latin-1", "replace"), "latin-1"

    # ── RTF 去控制字 ──────────────────────────────────────
    @staticmethod
    def rtf_to_text(raw: bytes) -> str:
        """把 RTF 转成纯文本。

        只做"够用"的处理：跳过字体表/颜色表/图片等目标组、还原 \\'hh 十六进制
        与 \\uN 转义、把 \\par/\\line/\\row 变成换行、\\tab/\\cell 变成制表符。
        """
        try:
            s = raw.decode("latin-1")
        except Exception:
            return ""
        skip_dests = {
            "fonttbl", "colortbl", "stylesheet", "info", "pict", "object",
            "themedata", "datastore", "latentstyles", "listtable",
            "listoverridetable", "rsidtbl", "generator", "xmlnstbl",
            "filetbl", "revtbl", "pgptbl", "w:", "\\",
        }
        out: List[str] = []
        hexbuf: List[int] = []
        i, n = 0, len(s)
        depth = 0
        skip_until = None          # 需要整体跳过的组深度
        stack: List[bool] = []

        def flush_hex():
            if not hexbuf:
                return
            b = bytes(hexbuf)
            hexbuf.clear()
            for enc in ("gb18030", "cp1252", "latin-1"):
                try:
                    out.append(b.decode(enc))
                    return
                except UnicodeDecodeError:
                    continue
            out.append(b.decode("latin-1", "replace"))

        while i < n:
            ch = s[i]
            if ch == "\\":
                j = i + 1
                if j >= n:
                    break
                nxt = s[j]
                if nxt == "'":                     # \'hh 十六进制
                    flush_hex()
                    try:
                        hexbuf.append(int(s[j + 1:j + 3], 16))
                    except ValueError:
                        pass
                    i = j + 3
                    continue
                if nxt == "*":                     # \* 可忽略目标组
                    skip_until = depth + 1
                    i = j + 1
                    continue
                if not (nxt.isalpha()):
                    # \{ \} \\ 之类的转义字符
                    flush_hex()
                    if nxt in ("{", "}", "\\"):
                        out.append(nxt)
                    i = j + 1
                    continue
                # 控制字：字母 + 可选数字 + 一个空格
                k = j
                while k < n and s[k].isalpha():
                    k += 1
                word = s[j:k]
                num_start = k
                if k < n and (s[k] == "-" or s[k].isdigit()):
                    k += 1
                    while k < n and s[k].isdigit():
                        k += 1
                num = s[num_start:k]
                if k < n and s[k] == " ":
                    k += 1
                if word == "u" and num:            # \uN 则 N 是 Unicode 码点
                    flush_hex()
                    try:
                        cp = int(num)
                        if cp < 0:
                            cp += 65536
                        out.append(chr(cp))
                    except ValueError:
                        pass
                    i = k
                    continue
                if word in ("par", "line", "row", "page", "sect"):
                    flush_hex()
                    out.append("\n")
                elif word in ("tab", "cell"):
                    flush_hex()
                    out.append("\t")
                elif word in skip_dests:
                    skip_until = depth + 1
                i = k
                continue
            if ch == "{":
                depth += 1
                stack.append(skip_until is not None and depth >= skip_until)
                i += 1
                continue
            if ch == "}":
                if stack:
                    stack.pop()
                if skip_until is not None and depth <= skip_until:
                    skip_until = None
                depth = max(0, depth - 1)
                i += 1
                continue
            if ch not in ("\r", "\n"):
                if skip_until is None:
                    flush_hex()
                    out.append(ch)
            i += 1

        flush_hex()
        text = "".join(out)
        text = text.replace("\r", "\n")
        # 收敛多余空行
        lines = [ln.rstrip() for ln in text.split("\n")]
        return "\n".join(lines)

    # ── 行结构 ────────────────────────────────────────────
    @staticmethod
    def _split_rows(text: str, ext: str) -> List[List[str]]:
        if ext in (".csv", ".tsv"):
            sample = text[:65536]
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except Exception:
                dialect = csv.excel
            try:
                reader = csv.reader(io.StringIO(text), dialect)
                return [list(r) for r in reader]
            except Exception:
                pass
        return [[ln] for ln in text.split("\n")]

    @staticmethod
    def line_count(file_path: str) -> int:
        try:
            size = os.path.getsize(file_path)
            # 估算：按平均 60 字节一行，最多不超 MAX_LINES（避免为统计行数全读一遍）
            return max(1, min(MAX_LINES, size // 60 + 1))
        except Exception:
            return 1

    # ── 主处理入口 ────────────────────────────────────────
    def process(self, file_path: str, options: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"tables": [], "text": "", "logs": [], "toc": []}
        name = os.path.basename(file_path)
        self.on_log(f"  [处理文本] {name}")
        try:
            text, enc = self.read_text(file_path)
        except Exception as e:
            msg = f"读取失败：{e}"
            self.on_log(f"  [错误] {msg}")
            result["logs"].append(msg)
            return result

        self.on_log(f"  编码判定: {enc}，{len(text)} 字符")
        if os.path.getsize(file_path) > MAX_BYTES:
            result["logs"].append(f"文件过大，仅处理前 {MAX_BYTES // 1024 // 1024} MB")

        ext = os.path.splitext(file_path)[1].lower()
        rows = self._split_rows(text, ext)[:MAX_LINES]
        start_line = int(options.get("start_page") or 1)
        end_line = int(options.get("end_page") or 9999)

        for idx, row in enumerate(rows, start=1):
            if idx % 200 == 0:
                self.cancel_check()
                try:
                    self.on_progress(int(idx / max(1, len(rows)) * 100 + 0.5))
                except Exception:
                    pass
            if idx < start_line or idx > end_line:
                continue
            content = " | ".join(str(c) for c in row) if len(row) > 1 else (row[0] if row else "")
            if not content.strip():
                continue
            for std in search_standard_names(content):
                result["tables"].append({
                    "name": resolve_standard_display(content, std),
                    "content": content,
                    "page": f"第{idx}行",
                })
        result["text"] = text[:MAX_PREVIEW_CHARS]
        self.on_log(f"  提取完成：{len(result['tables'])} 条标准记录")
        return result

    # ── 原始内容预览 ──────────────────────────────────────
    def preview(self, file_path: str) -> Dict[str, Any]:
        try:
            text, enc = self.read_text(file_path)
        except Exception as e:
            return {"error": f"读取失败：{e}", "text": f"读取失败：{e}"}
        snippet = text[:MAX_PREVIEW_CHARS]
        esc = (snippet.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        html = (f'<div style="font-size:12px;color:#64748B;margin-bottom:6px;">'
                f'编码：{enc}</div>'
                f'<pre style="white-space:pre-wrap;font-family:inherit;font-size:12px;">'
                f'{esc}</pre>')
        return {"html": html, "text": snippet}
