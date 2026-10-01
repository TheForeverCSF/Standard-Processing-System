# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""PowerPoint(.pptx/.pptm) 处理管道。

**为什么不依赖 python-pptx**：
    1. 本机（以及打包环境）并没有安装 python-pptx，装新库会连带引入 lxml、
       Pillow 等依赖，还要改 spec、重打整包；
    2. .pptx 本身就是个 zip（OOXML），幻灯片正文就是
       `ppt/slides/slideN.xml` 里的 `<a:t>` 文本节点 —— 用标准库
       zipfile + xml.etree 足够把"文字 + 表格"完整取出来，且**零新增依赖**，
       打包后必然可用。
如需更复杂的图形/动画解析，再考虑引入 python-pptx。
"""
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Callable, Dict, List, Optional

from utils.helpers import search_standard_names, resolve_standard_display

# DrawingML 主命名空间（幻灯片正文/表格都在这里）
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_A_T = f"{{{_A_NS}}}t"
_A_P = f"{{{_A_NS}}}p"
_A_TBL = f"{{{_A_NS}}}tbl"
_A_TR = f"{{{_A_NS}}}tr"
_A_TC = f"{{{_A_NS}}}tc"

# 旧版二进制 .ppt 无法直接解析（需要 Office 组件转换）
LEGACY_EXT = ".ppt"


class PptxHandler:
    """PPTX 处理管道：幻灯片文字、表格、备注，并按行识别标准编号。"""

    def __init__(self, on_log: Callable[[str], None] = None,
                 on_progress: Callable[[int], None] = None):
        self.on_log = on_log or (lambda x: None)
        self.on_progress = on_progress or (lambda x: None)
        self.cancel_check = lambda: None

    # ── 基础工具 ──────────────────────────────────────────
    @staticmethod
    def _slide_no(name: str) -> int:
        m = re.search(r"slide(\d+)\.xml$", name.replace("\\", "/"))
        return int(m.group(1)) if m else 0

    @staticmethod
    def is_legacy(file_path: str) -> bool:
        return os.path.splitext(file_path)[1].lower() == LEGACY_EXT

    @staticmethod
    def slide_count(file_path: str) -> int:
        """幻灯片数量（= 页码）。失败返回 1。"""
        try:
            with zipfile.ZipFile(file_path) as z:
                n = len([n for n in z.namelist()
                         if re.match(r"ppt/slides/slide\d+\.xml$", n)])
            return max(1, n)
        except Exception:
            return 1

    def _read_xml(self, z: zipfile.ZipFile, name: str) -> Optional[bytes]:
        try:
            return z.read(name)
        except Exception:
            return None

    # ── 文本 / 表格提取 ────────────────────────────────────
    @staticmethod
    def _paragraph_texts(xml_bytes: bytes) -> List[str]:
        """按文档顺序取出所有段落的纯文本（每行一段）"""
        try:
            root = ET.fromstring(xml_bytes)
        except Exception:
            return []
        lines: List[str] = []
        for p in root.iter(_A_P):
            buf = "".join((t.text or "") for t in p.iter(_A_T))
            if buf.strip():
                lines.append(buf.strip())
        return lines

    @staticmethod
    def _table_rows(xml_bytes: bytes) -> List[List[str]]:
        """取出所有表格的行（每行 = 各单元格文本）"""
        try:
            root = ET.fromstring(xml_bytes)
        except Exception:
            return []
        rows: List[List[str]] = []
        for tbl in root.iter(_A_TBL):
            for tr in tbl.iter(_A_TR):
                cells = []
                for tc in tr.iter(_A_TC):
                    cells.append(" ".join(
                        (t.text or "") for t in tc.iter(_A_T)).strip())
                if any(c for c in cells):
                    rows.append(cells)
        return rows

    def _collect(self, file_path: str) -> List[Dict[str, Any]]:
        """返回 [{'no': 幻灯片号, 'lines': [...], 'rows': [[...]], 'notes': [...]}]"""
        slides: List[Dict[str, Any]] = []
        with zipfile.ZipFile(file_path) as z:
            names = z.namelist()
            slide_names = sorted(
                [n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)],
                key=self._slide_no)
            if not slide_names:
                raise ValueError("该文件不是有效的 PPTX（未找到幻灯片）")
            for name in slide_names:
                raw = self._read_xml(z, name) or b""
                no = self._slide_no(name)
                item = {"no": no,
                        "lines": self._paragraph_texts(raw),
                        "rows": self._table_rows(raw),
                        "notes": []}
                # 备注页（有时标准编号只写在备注里）
                note_name = f"ppt/notesSlides/notesSlide{no}.xml"
                note_raw = self._read_xml(z, note_name)
                if note_raw:
                    item["notes"] = self._paragraph_texts(note_raw)
                slides.append(item)
        return slides

    # ── 主处理入口 ────────────────────────────────────────
    def process(self, file_path: str, options: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"tables": [], "text": "", "logs": [], "toc": []}
        if self.is_legacy(file_path):
            msg = ("不支持的旧版 .ppt 格式，请用 PowerPoint 另存为 .pptx 后重试")
            self.on_log(f"  [跳过] {msg}")
            result["logs"].append(msg)
            return result

        self.on_log(f"  [处理PPT] {file_path}")
        slides = self._collect(file_path)
        total = len(slides)
        self.on_log(f"  共 {total} 张幻灯片")

        text_parts: List[str] = []
        start_page = int(options.get("start_page") or 1)
        end_page = int(options.get("end_page") or 9999)

        for idx, s in enumerate(slides, start=1):
            self.cancel_check()
            no = s["no"]
            if no < start_page or no > end_page:
                continue
            page_label = f"第{no}页"
            text_parts.append(f"\n[幻灯片 {no}]")
            for line in s["lines"]:
                text_parts.append(line)
                for name in search_standard_names(line):
                    result["tables"].append({
                        "name": resolve_standard_display(line, name),
                        "content": line,
                        "page": page_label,
                    })
            for row in s["rows"]:
                content = " | ".join(row)
                text_parts.append(content)
                for name in search_standard_names(content):
                    result["tables"].append({
                        "name": resolve_standard_display(content, name),
                        "content": content,
                        "page": page_label,
                    })
            if s["notes"]:
                note_text = "\n".join(s["notes"])
                text_parts.append(f"[备注 {no}] {note_text}")
                for name in search_standard_names(note_text):
                    result["tables"].append({
                        "name": resolve_standard_display(note_text, name),
                        "content": note_text,
                        "page": f"{page_label}备注",
                    })
            try:
                self.on_progress(int(idx / total * 100 + 0.5))
            except Exception:
                pass

        result["text"] = "\n".join(text_parts)
        self.on_log(f"  提取完成：{len(result['tables'])} 条标准记录，"
                    f"文本 {len(result['text'])} 字符")
        return result

    # ── 原始内容预览 ──────────────────────────────────────
    def preview(self, file_path: str) -> Dict[str, Any]:
        if self.is_legacy(file_path):
            return {"text": "不支持的旧版 .ppt 格式，请用 PowerPoint 另存为 .pptx 后重试",
                    "html": "<div class='preview-empty'>旧版 .ppt 暂不支持，请另存为 .pptx</div>"}

        def esc(s: str) -> str:
            return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;"))

        slides = self._collect(file_path)
        html: List[str] = []
        plain: List[str] = []
        for s in slides:
            html.append(f'<h3 style="margin:14px 0 6px;font-size:13px;color:#1E293B;">'
                        f'第 {s["no"]} 张幻灯片</h3>')
            plain.append(f"[第 {s['no']} 张幻灯片]")
            for line in s["lines"]:
                html.append(f'<p style="margin:2px 0;">{esc(line)}</p>')
                plain.append(line)
            if s["rows"]:
                html.append('<table style="width:100%;border-collapse:collapse;'
                            'border:1px solid #CBD5E1;font-size:12px;">')
                for row in s["rows"]:
                    html.append("<tr>")
                    for cell in row:
                        html.append('<td style="border:1px solid #CBD5E1;'
                                    f'padding:4px 6px;">{esc(cell)}</td>')
                    html.append("</tr>")
                html.append("</table>")
                for row in s["rows"]:
                    plain.append(" | ".join(row))
            if s["notes"]:
                html.append('<div style="margin:4px 0;color:#64748B;font-size:12px;">'
                            f'备注：{esc(" ".join(s["notes"]))}</div>')
                plain.append("备注：" + " ".join(s["notes"]))
        return {"html": "\n".join(html), "text": "\n".join(plain)}
