# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import os
import traceback
from typing import Dict, Any, Callable

from core.ai_handler import LocalAIHandler
from core.document_router import DocumentRouter
from core.docx_handler import DocxHandler
from core.excel_handler import ExcelHandler
from core.pdf_handler import PdfHandler
from core.pptx_handler import PptxHandler
from core.ocr_handler import OcrHandler
from core.text_handler import TextHandler


class PreviewService:
    def __init__(self, on_progress: Callable[[int], None] = None,
                 on_status: Callable[[str], None] = None,
                 on_log: Callable[[str], None] = None):
        self.on_progress = on_progress or (lambda x: None)
        self.on_status = on_status or (lambda x: None)
        self.on_log = on_log or (lambda x: None)
        self.router = DocumentRouter()
        self.ocr = OcrHandler(self.on_log)

    def load_raw_preview(self, file_path: str) -> Dict[str, Any]:
        doc_type = self.router.detect_type(file_path)
        try:
            if doc_type == "doc":
                file_path = DocxHandler.convert_doc_to_docx(file_path, self.on_status)
                doc_type = "docx"
            elif doc_type == "excel" and ExcelHandler.is_legacy_xls(file_path):
                # 旧版 .xls 必须先转成 .xlsx（openpyxl 读不了 xls）
                file_path = ExcelHandler.convert_xls_to_xlsx(file_path, self.on_log)
            elif doc_type == "ppt":
                return {"text": "不支持的旧版 .ppt 格式，"
                                "请用 PowerPoint 另存为 .pptx 后重试"}

            if doc_type == "docx":
                return DocxHandler(self.on_log, self.ocr).preview(file_path)
            elif doc_type == "excel":
                return ExcelHandler(self.on_log).preview(file_path)
            elif doc_type == "pptx":
                return PptxHandler(self.on_log).preview(file_path)
            elif doc_type == "text":
                return TextHandler(self.on_log).preview(file_path)
            elif doc_type == "pdf":
                return PdfHandler(self.on_log, self.ocr).preview(file_path)
            else:
                return {"text": f"不支持的文件类型: {doc_type}"}
        except Exception as e:
            # traceback 只写日志，不下发给界面（前端会把返回的 text 当正常内容渲染，
            # 用户看到一屏堆栈既困惑又暴露本地路径）
            print(f"[ERROR] 预览加载失败: {e}\n{traceback.format_exc()}")
            return {"error": f"预览加载失败：{e}", "text": f"预览加载失败：{e}"}

    def process_preview(self, file_path: str, options: Dict[str, Any]) -> Dict[str, Any]:
        from core.processor import DocumentProcessor
        processor = DocumentProcessor(
            on_progress=self.on_progress,
            on_status=self.on_status,
            on_log=self.on_status
        )
        result = processor.process({
            "files": [file_path],
            "start_page": options.get("start_page", 1),
            "end_page": options.get("end_page", 9999),
            "extract_table": options.get("extract_table", True),
            "extract_body": options.get("extract_body", True),
            "extract_image": options.get("extract_image", True),
            "extract_textbox": options.get("extract_textbox", True),
            "ai_enhance": options.get("ai_enhance", True),
            "ocr_images": options.get("ocr_images", True),
        })

        # Excel 特殊处理：将合并单元格填充后的表格结构附加到第一个结果，便于预览
        doc_type = self.router.detect_type(file_path)
        if doc_type == "excel" and result.get("tables"):
            excel_preview = ExcelHandler(self.on_log).preview(file_path)
            result["tables"][0]["columns"] = excel_preview.get("columns", [])
            result["tables"][0]["rows"] = excel_preview.get("rows", [])

        return result
