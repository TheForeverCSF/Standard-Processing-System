# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import os
import tempfile
import base64
import threading
import zipfile
from typing import Dict, Any, List, Callable, Optional

from utils.helpers import (
    search_standard_names, resolve_standard_display,
    extract_text_from_runs, fill_merged_cells_down,
    build_toc_from_docx, find_context
)
from core.ocr_handler import OcrHandler
from core.toc_extractor import build_chapter_marks_from_docx, attach_chapter_by_offset

# Word 的 COM 自动化实际上是单实例的：多线程同时 Dispatch/Open/Quit 会互相把
# 对方的实例顶掉（文档打不开 / 转换出空文件）。这里用一把模块级锁把转换串起来，
# 只锁 COM 这一段，其它文件类型仍然并行。
_WORD_COM_LOCK = threading.RLock()


def _escape_html(text: str) -> str:
    if not text:
        return ""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))


class DocxHandler:
    """DOCX 文档处理管道：转换、目录、表格、正文、图片 OCR、文本框、合并单元格填充。"""

    def __init__(self, on_log: Callable[[str], None] = None, ocr: OcrHandler = None,
                 on_progress: Callable[[int], None] = None):
        self.on_log = on_log or (lambda x: None)
        self.on_progress = on_progress or (lambda x: None)
        self.ocr = ocr
        self.cancel_check = lambda: None

    @staticmethod
    def convert_doc_to_docx(doc_path: str, on_log: Callable[[str], None] = None) -> str:
        """把 .doc 转成 .docx（依赖本机 Word 的 COM 接口）。

        这里的几个约束都是踩过坑之后加的，改动前请先读完：

        1. **串行**：Word 的 COM 自动化实际上是单实例的，多线程同时 Dispatch/Open/Quit
           会互相把对方的 Word 实例顶掉（表现为"文档打不开""转换出来是空文件"），
           所以用模块级锁把整段转换串起来。注意锁只包住 COM 这一段，
           其它类型的文件（PDF/Excel）仍然并行，不会因为 .doc 而变慢。
        2. **DispatchEx**：用 DispatchEx 强制开"新实例"。用 Dispatch 有可能**连上用户
           自己正开着的 Word**，随后 `word.Quit()` 会把用户没保存的文档一起关掉。
        3. **CoInitialize**：COM 要求调用线程先初始化，否则工作线程里会报
           "尚未调用 CoInitialize"。
        4. **关掉一切提示框**：`DisplayAlerts=False` + 只读打开，避免加密/损坏文件
           弹出的对话框把线程挂住（线程一旦挂住，用户看到的就是"卡住不动"）。
        """
        log = on_log or (lambda x: None)
        log(f"  [转换] DOC -> DOCX: {doc_path}")
        out_dir = None
        word = None
        doc = None
        com_inited = False
        with _WORD_COM_LOCK:
            try:
                try:
                    import pythoncom
                    pythoncom.CoInitialize()
                    com_inited = True
                except Exception:
                    com_inited = False
                import win32com.client as win32
                try:
                    word = win32.DispatchEx("Word.Application")
                except Exception:
                    # 个别环境不支持 DispatchEx，退回 Dispatch（仍受锁保护）
                    word = win32.Dispatch("Word.Application")
                try:
                    word.Visible = False
                    word.DisplayAlerts = 0          # wdAlertsNone，不弹任何框
                except Exception:
                    pass
                doc_abs = os.path.abspath(doc_path)
                out_dir = tempfile.mkdtemp()
                out_path = os.path.join(out_dir,
                                        os.path.splitext(os.path.basename(doc_path))[0] + ".docx")
                doc = word.Documents.Open(doc_abs, ReadOnly=True, AddToRecentFiles=False)
                doc.SaveAs2(out_path, FileFormat=16)  # 16 = wdFormatXMLDocument
                doc.Close()
                doc = None
                word.Quit()
                word = None
                # 复制到系统临时目录下的标准系统缓存（避免返回路径指向已删除的 mkdtemp）
                persistent_dir = os.path.join(tempfile.gettempdir(), "standard_system", "_doc_converted")
                os.makedirs(persistent_dir, exist_ok=True)
                final_path = os.path.join(persistent_dir, os.path.basename(out_path))
                import shutil
                shutil.copy2(out_path, final_path)
                log(f"  [转换完成] {final_path}")
                return final_path
            except Exception as e:
                log(f"  [转换失败] {e}，请尝试手动转换后重新上传 DOCX")
                raise
            finally:
                # 无论成功失败都要释放 Word：否则加密/损坏的 .doc 会残留一个
                # 看不见的 WINWORD.EXE，并一直占住该文件，后续处理全部失败
                try:
                    if doc is not None:
                        doc.Close(False)
                except Exception:
                    pass
                try:
                    if word is not None:
                        word.Quit()
                except Exception:
                    pass
                if out_dir and os.path.exists(out_dir):
                    import shutil
                    shutil.rmtree(out_dir, ignore_errors=True)
                if com_inited:
                    try:
                        import pythoncom
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass

    def process(self, file_path: str, options: Dict[str, Any]) -> Dict[str, Any]:
        from docx import Document
        self.on_log(f"  [处理DOCX] {file_path}")
        doc = Document(file_path)
        result = {"tables": [], "text": "", "logs": [], "toc": []}
        self.on_progress(5)

        # 1. 构建目录
        result["toc"] = build_toc_from_docx(doc)
        if result["toc"]:
            self.on_log(f"  构建目录: {len(result['toc'])} 项")
        self.on_progress(10)

        # 2. 提取正文
        body_marks = []
        body = ""
        if options.get("extract_body", True):
            body = self._extract_body(doc)
            result["text"] += body
            self.on_log(f"  提取正文: {len(body)} 字符")
            # 记录 Heading 在正文中的字符偏移，供后续标注章节
            try:
                body_marks = build_chapter_marks_from_docx(doc, body)
                if body_marks:
                    self.on_log(f"  正文章节标记: {len(body_marks)} 处")
            except Exception as e:
                self.on_log(f"  章节标记构建异常(忽略): {e}")
            for name in search_standard_names(body):
                display_name = resolve_standard_display(body, name)
                result["tables"].append({
                    "name": display_name,
                    "content": self._find_context(body, name),
                    "page": "正文",
                })
        self.on_progress(35)

        # 3. 提取表格（含合并单元格填充）
        self.cancel_check()   # 支持"终止处理"即时生效
        if options.get("extract_table", True):
            tables = self._extract_tables(doc)
            result["tables"].extend(tables)
            self.on_log(f"  提取表格: {len(tables)} 条记录")
        self.on_progress(60)

        # 4. 图片 OCR 识别
        self.cancel_check()
        if options.get("extract_image", True) and options.get("ocr_images", True):
            images = self._extract_images(doc, file_path)
            if images:
                result["text"] += "\n[图片识别内容]\n" + "\n".join(images)
                self.on_log(f"  图片 OCR: {len(images)} 条记录")
                for name in search_standard_names("\n".join(images)):
                    display_name = resolve_standard_display("\n".join(images), name)
                    result["tables"].append({
                        "name": display_name,
                        "content": self._find_context("\n".join(images), name),
                        "page": "图片",
                    })
        self.on_progress(80)

        # 5. 提取文本框
        self.cancel_check()
        if options.get("extract_textbox", True):
            textboxes = self._extract_textboxes(doc)
            if textboxes:
                result["text"] += "\n[文本框内容]\n" + "\n".join(textboxes)
                self.on_log(f"  提取文本框: {len(textboxes)} 个")
                for name in search_standard_names("\n".join(textboxes)):
                    display_name = resolve_standard_display("\n".join(textboxes), name)
                    result["tables"].append({
                        "name": display_name,
                        "content": self._find_context("\n".join(textboxes), name),
                        "page": "文本框",
                    })

        # 为每条记录标注"所在章节"（DOCX 无页码，改用正文字符偏移定位）
        if body_marks and body:
            try:
                attach_chapter_by_offset(result["tables"], body, body_marks, self.on_log)
            except Exception as e:
                self.on_log(f"  章节标注异常(忽略): {e}")

        self.on_progress(100)
        return result

    @staticmethod
    def _safe_font_name(name) -> str:
        """字体名安全化：只保留字母/数字/空格/-/_/.（含中文）。

        安全审计（C5，已 POC）：原实现把文档里的字体名**不转义**直接拼进 style 属性
        （正文文本有 _escape_html，字体名漏了）。构造一个 docx，把字体名写成
            x" /><img src=x onerror="alert(1)">
        预览面板用 innerHTML 插入后即执行脚本 —— 而同源页面能调本机全部接口。
        字体名根本不需要引号/括号/分号/尖括号，白名单过滤即可根除，且不会误伤正常字体名。
        """
        if not name:
            return ""
        cleaned = "".join(ch for ch in str(name)
                          if ch.isalnum() or ch in " -_.")
        return cleaned.strip()

    def preview(self, file_path: str) -> Dict[str, Any]:
        from docx import Document
        doc = Document(file_path)
        html_parts = []

        # 1. 段落（带样式）
        for para in doc.paragraphs[:300]:
            if not para.text.strip():
                html_parts.append("<p>&nbsp;</p>")
                continue

            # 提取样式
            style_parts = []
            tag = "p"

            # 判断标题
            style_name = para.style.name or ""
            if "Heading" in style_name:
                try:
                    level = int(style_name.replace("Heading", "").replace(" ", ""))
                    if 1 <= level <= 6:
                        tag = f"h{level}"
                except:
                    pass

            # 从 runs 提取格式
            if para.runs:
                run = para.runs[0]
                if run.bold or (para.style and para.style.font and para.style.font.bold):
                    style_parts.append("font-weight:bold;")
                if run.italic:
                    style_parts.append("font-style:italic;")
                if run.font.size:
                    try:
                        size_pt = run.font.size.pt
                        style_parts.append(f"font-size:{size_pt}px;")
                    except:
                        pass
                if run.font.color and run.font.color.rgb:
                    style_parts.append(f"color:#{run.font.color.rgb};")
                if run.font.name:
                    _fn = self._safe_font_name(run.font.name)
                    if _fn:
                        style_parts.append(f"font-family:'{_fn}',serif;")

            # 对齐
            align_map = {1: "center", 2: "right", 3: "justify"}
            if para.alignment in align_map:
                style_parts.append(f"text-align:{align_map[para.alignment]};")
            elif para.style and para.style.paragraph_format and para.style.paragraph_format.alignment in align_map:
                style_parts.append(f"text-align:{align_map[para.style.paragraph_format.alignment]};")

            text = _escape_html(para.text.strip())
            if style_parts:
                html_parts.append(f'<{tag} style="{"".join(style_parts)}">{text}</{tag}>')
            else:
                html_parts.append(f'<{tag}>{text}</{tag}>')

        # 2. 表格
        for t_idx, table in enumerate(doc.tables[:5], start=1):
            html_parts.append(f'<p style="margin-top:12px;margin-bottom:4px;font-weight:600;">[表格 {t_idx}]</p>')
            html_parts.append('<table style="width:100%;border-collapse:collapse;border:1px solid #CBD5E1;margin-bottom:8px;">')
            for row in table.rows[:20]:
                html_parts.append("<tr>")
                for cell in row.cells:
                    text = _escape_html(extract_text_from_runs(cell.paragraphs))
                    html_parts.append(f'<td style="border:1px solid #CBD5E1;padding:4px 6px;font-size:12px;">{text}</td>')
                html_parts.append("</tr>")
            html_parts.append("</table>")

        # 3. 图片
        image_html = self._extract_images_for_preview(file_path)
        if image_html:
            html_parts.append(image_html)

        return {"html": "\n".join(html_parts)}

    def _extract_images_for_preview(self, file_path: str) -> str:
        html_parts = []
        if not zipfile.is_zipfile(file_path):
            return ""
        with zipfile.ZipFile(file_path) as zf:
            for name in zf.namelist():
                if name.startswith("word/media/"):
                    ext = os.path.splitext(name)[1].lower()
                    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif"):
                        try:
                            img_data = zf.read(name)
                            b64 = base64.b64encode(img_data).decode("utf-8")
                            mime = "image/png" if ext == ".png" else "image/jpeg" if ext in (".jpg", ".jpeg") else "image/gif"
                            html_parts.append(f'<img src="data:{mime};base64,{b64}" style="max-width:100%;margin:8px 0;border:1px solid #E2E8F0;" />')
                        except Exception:
                            pass
        return "\n".join(html_parts)

    def _extract_body(self, doc) -> str:
        paragraphs = []
        for _pi, para in enumerate(doc.paragraphs, start=1):
            if _pi % 200 == 0:
                self.cancel_check()
            text = para.text.strip()
            if text:
                paragraphs.append(text)
        return "\n".join(paragraphs)

    def _extract_tables(self, doc) -> List[Dict[str, Any]]:
        tables = []
        for table_idx, table in enumerate(doc.tables, start=1):
            self.cancel_check()   # 表格可能很多，逐表检查取消
            rows = []
            for row in table.rows:
                cells = [extract_text_from_runs(cell.paragraphs) for cell in row.cells]
                rows.append(cells)
            rows = fill_merged_cells_down(rows)
            for r_idx, cells in enumerate(rows, start=1):
                content = " | ".join(str(c) for c in cells if c is not None)
                for name in search_standard_names(content):
                    display_name = resolve_standard_display(content, name)
                    tables.append({
                        "name": display_name,
                        "content": content,
                        "page": f"表格{table_idx} 行{r_idx}",
                    })
        return tables

    def _extract_images(self, doc, file_path: str) -> List[str]:
        """提取图片并进行 OCR 识别，返回识别到的文本列表。"""
        texts = []
        if self.ocr is None:
            self.on_log("  未提供 OCR 引擎，跳过图片识别")
            return texts
        try:
            from docx.oxml.ns import qn
            import zipfile

            # 先尝试读取 docx 包中的图片文件
            media_files = []
            if zipfile.is_zipfile(file_path):
                with zipfile.ZipFile(file_path) as zf:
                    for name in zf.namelist():
                        if name.startswith("word/media/"):
                            media_files.append(name)

            # 读取 alt 文本作为补充
            root = doc.element
            alt_texts = []
            for drawing in root.iter(qn("w:drawing")):
                descr = drawing.find(qn("wp:docPr"))
                if descr is not None:
                    alt = descr.get("descr") or descr.get("title")
                    if alt:
                        alt_texts.append(alt)

            # 对图片文件进行 OCR
            if media_files and zipfile.is_zipfile(file_path):
                with zipfile.ZipFile(file_path) as zf:
                    for idx, media in enumerate(media_files, start=1):
                        # OCR 是最慢的一环，逐张检查取消，让"终止处理"能立刻生效
                        self.cancel_check()
                        ext = os.path.splitext(media)[1].lower()
                        if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".tiff"):
                            continue
                        img_data = zf.read(media)
                        # 唯一文件名，避免并行处理多个 DOCX 时互相覆盖图片
                        img_fd, img_path = tempfile.mkstemp(prefix="docx_img_", suffix=ext)
                        os.close(img_fd)
                        with open(img_path, "wb") as f:
                            f.write(img_data)
                        try:
                            ocr_result = self.ocr.recognize(img_path)
                            if ocr_result.get("text", "").strip():
                                texts.append(ocr_result.get("text", "").strip())
                        except Exception as e:
                            self.on_log(f"  图片 OCR 失败: {e}")
                        finally:
                            try: os.remove(img_path)
                            except: pass

            # 如果没有识别到图片文字，至少返回 alt 文本
            if not texts and alt_texts:
                texts.extend(alt_texts)

        except Exception as e:
            self.on_log(f"  图片提取异常: {e}")
        return texts

    def _extract_textboxes(self, doc) -> List[str]:
        """遍历 OOXML 提取文本框文本。

        原先按 `qn("w:txbx")` 找容器，但 OOXML 里**没有**这个元素：
        VML 文本框是 `w:pict > v:shape > v:textbox > w:txbxContent`，
        现代文本框是 `mc:AlternateContent > wps:txbx > w:txbxContent`。
        所以两个分支都命中不了，文本框提取一直是空转（界面上的"文本框"
        复选框形同虚设，图纸说明/流程框里的标准号 100% 漏掉）。
        直接遍历真正承载文字的 `w:txbxContent` 即可覆盖两种结构。

        注意跳过 `mc:Fallback` 子树：Word 会把同一段内容在 Choice 和
        Fallback 里各存一份，不跳过会重复计数。
        """
        texts = []
        try:
            from docx.oxml.ns import qn
            root = doc.element
            mc_uri = "http://schemas.openxmlformats.org/markup-compatibility/2006"
            fallback_nodes = set()
            for alt in root.iter(f"{{{mc_uri}}}Fallback"):
                for node in alt.iter():
                    fallback_nodes.add(node)
            for content in root.iter(qn("w:txbxContent")):
                if content in fallback_nodes:
                    continue
                for p in content.iter(qn("w:p")):
                    para_text = " ".join(t.text for t in p.iter(qn("w:t")) if t.text)
                    if para_text.strip():
                        texts.append(para_text.strip())
        except Exception as e:
            self.on_log(f"  文本框提取异常: {e}")
        return texts

    def _find_context(self, text: str, keyword: str, window: int = 80) -> str:
        # 统一走 utils.helpers.find_context（与 PDF/OCR 路径同一实现）
        return find_context(text, keyword, window)
