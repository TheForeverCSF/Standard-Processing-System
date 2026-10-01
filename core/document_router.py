# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import os

from core.text_handler import TEXT_EXTS


class DocumentRouter:
    """按扩展名判定文档类型。

    支持范围（与 web/server.py 的扩展名白名单、core/processor.py 的分发保持一致）：
        pdf   : .pdf
        docx  : .docx / .docm      （.doc 需先用 Word COM 转换）
        excel : .xlsx / .xlsm      （.xls 需先用 Excel COM 转换）
        pptx  : .pptx / .pptm      （旧版二进制 .ppt 不支持，会给出明确提示）
        text  : .txt/.csv/.tsv/.md/.markdown/.log/.rtf …
    """

    SUPPORTED = {
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "docx",
        ".docm": "docx",
        ".xls": "excel",
        ".xlsx": "excel",
        ".xlsm": "excel",
        ".ppt": "ppt",       # 旧版二进制，无法直接解析，仅用于给出明确提示
        ".pptx": "pptx",
        ".pptm": "pptx",
        **{ext: "text" for ext in TEXT_EXTS},
    }

    def detect_type(self, file_path: str) -> str:
        ext = os.path.splitext(file_path)[1].lower()
        return self.SUPPORTED.get(ext, "unknown")
