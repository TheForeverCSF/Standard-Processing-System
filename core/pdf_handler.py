# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import os
import tempfile
from typing import Dict, Any, List, Callable

from utils.helpers import (search_standard_names,
                           resolve_standard_display, normalize_std_no,
                           find_context)
from core.ocr_handler import OcrHandler
from core.quiet_io import quiet_stderr_once, suppressed_stderr
from core.toc_extractor import extract_pdf_toc, attach_chapter_by_page


class PdfHandler:
    """PDF 文档处理管道：扫描页判断、OCR、文本提取、pdfplumber 表格提取。"""

    def __init__(self, on_log: Callable[[str], None] = None, ocr: 'OcrHandler' = None,
                 on_progress: Callable[[int], None] = None,
                 on_status: Callable[[str], None] = None):
        self.on_log = on_log or (lambda x: None)
        self.on_status = on_status or (lambda x: None)
        self.ocr = ocr
        self.on_progress = on_progress or (lambda x: None)
        self.cancel_check = lambda: None

    @staticmethod
    def _quiet_fitz():
        """静默导入 fitz（C 层 stderr 重定向），屏蔽 MuPDF 噪音。

        重定向只做一次（进程级），不再每个文件都 dup/dup2/close ——
        多线程并行时那些操作会互相把描述符关掉。
        """
        quiet_stderr_once()
        import fitz as _fitz
        return _fitz

    def _suppress_stderr(self):
        """包裹 fitz 操作的 stderr 抑制上下文（进程级一次性，之后为空操作）"""
        return suppressed_stderr()

    def process(self, file_path: str, options: Dict[str, Any]) -> Dict[str, Any]:
        fitz = self._quiet_fitz()
        self.on_log(f"  [处理PDF] {file_path}")
        # 把整个处理循环都放进 stderr 抑制范围，屏蔽 MuPDF 解析警告
        with self._suppress_stderr():
            doc = fitz.open(file_path)
        start = max(1, options.get("start_page", 1))
        end = min(len(doc), options.get("end_page", len(doc)))
        result = {"tables": [], "text": "", "logs": [], "toc": []}
        total_pages = max(1, end - start + 1)
        self.on_log(f"  PDF共{len(doc)}页, 处理范围{start}-{end}({total_pages}页), ocr_scanned={options.get('ocr_scanned')}")

        # ---------- 第一阶段：快速扫描 ----------
        self.on_status("PDF 快速扫描中...")
        pages_with_standard = set()
        all_text_parts = []
        page_texts = []
        text_pages_count = 0
        scanned_pages_count = 0
        for i, page_num in enumerate(range(start, end + 1)):
            self.cancel_check()
            with self._suppress_stderr():
                page = doc[page_num - 1]
                text = page.get_text()
            page_texts.append(text)
            page_header = f"\n[第 {page_num} 页]\n"
            all_text_parts.append(page_header)
            all_text_parts.append(text)
            text_len = len(text.strip())
            images = len(page.get_images())
            if search_standard_names(text):
                pages_with_standard.add(page_num)
            if text_len < 30 and images > 0:
                scanned_pages_count += 1
            else:
                text_pages_count += 1
            self.on_progress(int((i + 1) / total_pages * 40 + 0.5))

        self.on_log(f"  扫描完成: {text_pages_count}页有文本, {scanned_pages_count}页扫描, {len(pages_with_standard)}页含标准号匹配, "
                     f"全文总字符={sum(len(t) for t in page_texts)}")

        full_text = "".join(all_text_parts)
        result["text"] = full_text

        # 提取章节目录（用于结果页展示"所在章节"），失败不影响主流程
        try:
            with self._suppress_stderr():
                result["toc"] = extract_pdf_toc(doc, start, end, self.on_log)
            if result["toc"]:
                self.on_log(f"  识别章节 {len(result['toc'])} 项，示例: "
                            f"{[t['title'] for t in result['toc'][:3]]}")
            else:
                self.on_log("  未识别到章节标题（该 PDF 可能无编号标题或为扫描件）")
        except Exception as e:
            self.on_log(f"  章节提取异常(忽略): {e}")
            result["toc"] = []

        # 建立编号到显示名的缓存
        all_std_names = search_standard_names(full_text)
        self.on_log(f"  全文共匹配到 {len(all_std_names)} 个不同标准号: {all_std_names[:10]}{'...' if len(all_std_names)>10 else ''}")
        display_cache = {}
        for name in all_std_names:
            self.cancel_check()
            display_cache[name] = resolve_standard_display(full_text, name)
        if display_cache:
            self.on_log(f"  显示名缓存示例: {dict(list(display_cache.items())[:3])}")

        # ---------- 第二阶段：逐页处理 ----------
        self.on_status("PDF 表格提取中...")
        total_extracted = 0
        # 扫描页未能 OCR 的统计（最后汇总提示，避免用户在结果异常时无从判断）
        ocr_unavailable_pages: List[int] = []
        ocr_skipped_pages = 0
        for i, page_num in enumerate(range(start, end + 1)):
            self.cancel_check()
            text = page_texts[i]
            before_count = len(result["tables"])
            try:
                with self._suppress_stderr():
                    page = doc[page_num - 1]
                    is_scanned = self._is_scanned_page(page)
                self.cancel_check()
                if is_scanned and options.get("ocr_scanned", True):
                    self.on_log(f"  第 {page_num} 页为扫描页，启用 OCR")
                    pix = page.get_pixmap(dpi=200)
                    self.cancel_check()
                    # 用唯一文件名：并行处理多个 PDF 时，固定名 pdf_page_N.png 会互相覆盖，
                    # 导致 A 文件的 OCR 结果里混进 B 文件的内容
                    img_fd, img_path = tempfile.mkstemp(prefix="pdf_page_", suffix=".png")
                    os.close(img_fd)
                    pix.save(img_path)
                    ocr_result = {"text": "", "tables": []}
                    if self.ocr is not None and self.ocr.is_available():
                        self.cancel_check()
                        try:
                            ocr_result = self.ocr.recognize(img_path)
                        except Exception as e:
                            self.on_log(f"  OCR 失败: {e}")
                    else:
                        # 引擎不可用时必须说清楚。原先这里是静默跳过，
                        # 扫描件处理出来是 0 条，用户会以为"文档里没有标准"，
                        # 而不是"OCR 没装好" —— 排查成本极高。
                        ocr_unavailable_pages.append(page_num)
                        if len(ocr_unavailable_pages) <= 3:
                            self.on_log(f"  第 {page_num} 页是扫描页，但 OCR 引擎不可用，"
                                        f"该页内容未识别（装好 OCR 引擎后重试即可识别）")
                        # 扫描页判据是"文字很少 + 有图"，封面、单条标准页、
                        # 盖了章/贴了 logo 的页都可能被判成扫描页。这些页往往
                        # 仍有可读文字（图注、印章、少量正文），若整页只等 OCR，
                        # 引擎不可用时这页真实存在的标准就静默丢了。
                        # 这里补一次纯文本搜索兜底（只在 OCR 不可用时，
                        # 避免与 OCR 结果重复计数）。
                        if text.strip():
                            for _nm in search_standard_names(text):
                                result["tables"].append({
                                    "name": resolve_standard_display(text, _nm),
                                    "content": self._find_context(text, _nm),
                                    "page": f"第{page_num}页",
                                })
                    # 识别完立即删掉临时图，否则 %TEMP% 里会越堆越多（原实现从不删除）
                    try:
                        os.remove(img_path)
                    except Exception:
                        pass
                    self.cancel_check()
                    result["text"] += ocr_result.get("text", "")
                    ocr_tables = 0
                    for row in ocr_result.get("tables", []):
                        row["page"] = f"第{page_num}页"
                        result["tables"].append(row)
                        ocr_tables += 1
                    if ocr_tables > 0:
                        self.on_log(f"  第{page_num}页 OCR 识别到 {ocr_tables} 条标准: {[r.get('name','') for r in ocr_result.get('tables',[])]}")
                elif is_scanned and not options.get("ocr_scanned", True):
                    # OCR 未开启，扫描页跳过（前几页记日志，方便排查）
                    ocr_skipped_pages += 1
                    if ocr_skipped_pages <= 3:
                        self.on_log(f"  第 {page_num} 页是扫描页，未勾选「扫描件 OCR」，已跳过")
                else:
                    # ── 非扫描页：page.get_text() 已包含表格文字，
                    # Path A（文本搜索）和 Path B（表格提取）可能重复。
                    # 策略：
                    #   Path B（表格）优先，保留每行级精度，不对 B 内部去重
                    #   Path A（全文搜索）只补充表格未覆盖的标准（用 seen_from_table 记录）
                    seen_from_table = set()  # 仅用于 Path A 跳过已被表格覆盖的标准

                    # Path B 优先：从 pdfplumber 表格中提取（保留行级精度，不过滤）
                    if options.get("extract_table", True) and page_num in pages_with_standard:
                        self.cancel_check()
                        tables = self._extract_tables_with_pdfplumber(file_path, page_num)
                        for tbl_entry in tables:
                            raw_name = tbl_entry.get("name", "")
                            std_num = raw_name.split("《")[0].strip() if "《" in raw_name else raw_name
                            # 用规范化标准号作为去重 key：
                            # 表格原文可能含折行换行，字典里的旧数据也可能是无空格写法
                            # （如 TB/T46-2020），与 Path A 的归一化结果（TB/T 46-2020）
                            # 字面不同但实为同一条标准，不规范化会导致同位置重复计数。
                            seen_from_table.add((normalize_std_no(std_num), page_num))
                            result["tables"].append(tbl_entry)

                    # Path A 补充：从页面纯文本搜索（跳过已在表格中出现的标准）
                    for name in search_standard_names(text):
                        if (normalize_std_no(name), page_num) in seen_from_table:
                            continue
                        display_name = display_cache.get(name, name)
                        if display_name == name:
                            display_name = resolve_standard_display(text, name)
                        result["tables"].append({
                            "name": display_name,
                            "content": self._find_context(text, name),
                            "page": f"第{page_num}页",
                        })
                    # 如果本页没有通过 Path A/B 提取到标准，但快速扫描阶段发现了标准号，
                    # 则走兜底路径：从全文已缓存的标准中找出属于本页的标准
                    if before_count == len(result["tables"]) and page_num in pages_with_standard:
                        self.on_log(f"  第{page_num}页 Path A/B 均未提取到标准，尝试兜底提取")
                        for name in all_std_names:
                            if name in text:
                                display_name = display_cache.get(name, name)
                                if display_name == name:
                                    display_name = resolve_standard_display(text, name)
                                result["tables"].append({
                                    "name": display_name,
                                    "content": self._find_context(text, name),
                                    "page": f"第{page_num}页",
                                })

                    added = len(result["tables"]) - before_count
                    if added > 0:
                        self.on_log(f"  第{page_num}页 非扫描 文本/表格共提取 {added} 条: "
                                    f"{[r['name'] for r in result['tables'][before_count:]]}")
            except Exception as e:
                self.on_log(f"  第 {page_num} 页处理异常: {e}")
            finally:
                self.on_progress(int(40 + (i + 1) / total_pages * 60 + 0.5))

        # 为每条记录标注"所在章节"
        if result["toc"]:
            try:
                attach_chapter_by_page(result["tables"], result["toc"], self.on_log)
            except Exception as e:
                self.on_log(f"  章节标注异常(忽略): {e}")

        # 扫描页的汇总提示：让"结果比预期少"有明确原因，而不是让人怀疑程序漏提取
        if ocr_unavailable_pages:
            msg = (f"有 {len(ocr_unavailable_pages)} 页是扫描页但 OCR 引擎不可用，"
                   f"这些页未识别（页码示例：{ocr_unavailable_pages[:5]}）。"
                   f"装好 OCR 引擎后重新处理即可识别这些页。")
            self.on_log(f"  [注意] {msg}")
            result["logs"].append(msg)
        if ocr_skipped_pages:
            msg = (f"有 {ocr_skipped_pages} 页是扫描页，因未勾选「扫描件 OCR」被跳过"
                   f"（如需识别请在处理前勾选该选项）")
            self.on_log(f"  [注意] {msg}")
            result["logs"].append(msg)

        self.on_log(f"  PDF处理完成: 共提取 {len(result['tables'])} 条标准记录, 文本 {len(result['text'])} 字符")
        doc.close()
        return result

    def preview(self, file_path: str) -> Dict[str, Any]:
        fitz = PdfHandler._quiet_fitz()
        with self._suppress_stderr():
            doc = fitz.open(file_path)
            html_parts = []
            text_parts = []
            for page_num in range(1, len(doc) + 1):
                page = doc[page_num - 1]
                text = page.get_text().strip()
                # 对于扫描页（get_text 返回极少文字），回退到页面级别占位
                if not text:
                    text = f"[扫描页 - 第 {page_num} 页]"
                html_parts.append(f'<div class="page-break" id="pdf-page-{page_num}">第 {page_num} 页</div>')
                html_parts.append(f'<div style="white-space:pre-wrap;font-size:12px;line-height:1.6;">{text}</div>')
                text_parts.append(f"[第 {page_num} 页]\n{text}")
            doc.close()
        return {"html": "\n".join(html_parts), "text": "\n".join(text_parts)}

    def _is_scanned_page(self, page) -> bool:
        text = page.get_text().strip()
        images = page.get_images()
        return len(text) < 30 and len(images) > 0

    def _extract_tables_with_pdfplumber(self, file_path: str, page_num: int) -> List[Dict[str, Any]]:
        extracted = []
        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                if page_num > len(pdf.pages):
                    return extracted
                page = pdf.pages[page_num - 1]
                tables = page.extract_tables()
                if not tables:
                    return extracted
                for t_idx, table in enumerate(tables, start=1):
                    if not table:
                        continue
                    # 标准提取模式不使用 fill_merged_cells_down：目录型 PDF 中的
                    # 分组标题行（如跨列合并的"铁路国家标准"）会被填充成伪数据行，
                    # 导致同一标准被重复识别。只做 None→"" 规范化即可。
                    rows = [[("" if c is None else c) for c in row] for row in table]
                    for r_idx, row in enumerate(rows, start=1):
                        content = " | ".join(str(c) for c in row if c)
                        for name in search_standard_names(content):
                            display_name = resolve_standard_display(content, name)
                            extracted.append({
                                "name": display_name,
                                "content": content,
                                "page": f"第{page_num}页 表{t_idx} 行{r_idx}",
                            })
        except Exception as e:
            self.on_log(f"  pdfplumber 表格提取异常: {e}")
        return extracted

    def _find_context(self, text: str, keyword: str, window: int = 80) -> str:
        # 统一走 utils.helpers.find_context：OCR 路径也用同一实现，
        # 三处拷贝合并后行为（含边界）保持一致。
        return find_context(text, keyword, window)
