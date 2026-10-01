# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import os
from typing import Dict, Any, List

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextEdit, QProgressBar, QPushButton
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

from gui.styles import CARD_BG, BORDER, TEXT, SECONDARY, PRIMARY, BG
from core.preview_service import PreviewService


class PreviewPanel(QWidget):
    """右侧文件预览面板，支持原始内容、处理后预览。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(360)
        self.setMaximumWidth(520)
        self.setStyleSheet(f"background: {CARD_BG};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QLabel("文件预览")
        header.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {TEXT};")
        layout.addWidget(header)

        self.subtitle = QLabel("请选择文件以查看预览")
        self.subtitle.setStyleSheet(f"color: {SECONDARY}; font-size: 12px;")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.subtitle)

        self.tabs = QTabWidget()

        self.raw_text = QTextEdit()
        self.raw_text.setReadOnly(True)
        self.tabs.addTab(self.raw_text, "原始内容")

        self.processed_table = QTableWidget()
        self.processed_table.setColumnCount(0)
        self.processed_table.setHorizontalHeaderLabels([])
        self.tabs.addTab(self.processed_table, "处理后预览")

        self.processed_text = QTextEdit()
        self.processed_text.setReadOnly(True)
        self.tabs.addTab(self.processed_text, "处理文本")

        layout.addWidget(self.tabs, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet(f"color: {SECONDARY};")
        layout.addWidget(self.status_label)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.process_btn = QPushButton("解析预览")
        self.process_btn.setEnabled(False)
        self.process_btn.clicked.connect(self.on_process_click)
        btn_layout.addWidget(self.process_btn)
        layout.addLayout(btn_layout)

        self._current_file = ""
        self._pending_keyword = None
        self._worker = None

    def set_file(self, file_path: str):
        self._current_file = file_path
        self.subtitle.setText(f"当前文件: {os.path.basename(file_path)}")
        self.process_btn.setEnabled(bool(file_path))
        self.progress.setValue(0)
        self.status_label.setText("加载预览中...")
        self.raw_text.clear()
        self.tabs.setCurrentIndex(0)
        self._load_raw_preview_async(file_path)

    def jump_to(self, file_path: str, keyword: str):
        """切换到指定文件，并在处理文本中定位关键词。"""
        self.set_file(file_path)
        self.process_file(file_path, {
            "extract_table": True,
            "extract_body": True,
            "extract_image": True,
            "extract_textbox": True,
            "ai_enhance": True,
            "ocr_images": True,
            "start_page": 1,
            "end_page": 9999,
        })
        self._pending_keyword = keyword

    def _load_raw_preview_async(self, file_path: str):
        self._safe_stop_worker()
        self._worker = RawPreviewWorker(file_path, self)
        self._worker.result.connect(self._on_raw_preview_loaded)
        self._worker.error.connect(self._on_raw_preview_error)
        self._worker.start()

    def _safe_stop_worker(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.quit()
            if not self._worker.wait(2000):
                self._worker.terminate()
                self._worker.wait(2000)
            try:
                self._worker.disconnect()
            except Exception:
                pass
        self._worker = None

    def _on_raw_preview_loaded(self, text: str):
        self.raw_text.setPlainText(text)
        self.status_label.setText("原始内容已加载")
        self.progress.setValue(100)

    def _on_raw_preview_error(self, msg: str):
        self.raw_text.setPlainText(msg)
        self.status_label.setText("预览加载失败")
        self.progress.setValue(100)

    def on_process_click(self):
        if not self._current_file:
            return
        options = {
            "extract_table": True,
            "extract_body": True,
            "extract_image": True,
            "extract_textbox": True,
            "ai_enhance": True,
            "ocr_images": True,
            "start_page": 1,
            "end_page": 9999,
        }
        self.process_file(self._current_file, options)

    def process_file(self, file_path: str, options: Dict[str, Any]):
        self._current_file = file_path
        self.process_btn.setEnabled(False)
        self.progress.setValue(0)
        self.status_label.setText("正在解析...")

        self._safe_stop_worker()

        self._worker = PreviewWorker(file_path, options, self)
        self._worker.progress.connect(self.progress.setValue)
        self._worker.status.connect(self.status_label.setText)
        self._worker.result.connect(self.on_processed)
        self._worker.start()


    def on_processed(self, result: Dict[str, Any]):
        self.process_btn.setEnabled(True)
        self.progress.setValue(100)
        self.status_label.setText("解析完成")

        tables = result.get("tables", [])
        if tables:
            self._show_processed_table(tables)
        else:
            self.processed_table.setColumnCount(0)
            self.processed_table.setRowCount(0)

        text = result.get("text", "")
        self.processed_text.setPlainText(text)

        if self._pending_keyword:
            self.tabs.setCurrentIndex(2)
            self.processed_text.find(self._pending_keyword)
            self._pending_keyword = None
        elif tables:
            self.tabs.setCurrentIndex(1)
        else:
            self.tabs.setCurrentIndex(2)

    def _show_processed_table(self, tables: List[Dict[str, Any]]):
        first = tables[0]
        rows = first.get("rows", [])
        columns = first.get("columns", [])

        if not rows:
            rows = [{"name": t.get("name", ""), "content": t.get("content", ""), "page": t.get("page", "")} for t in tables]
            columns = ["标准名称", "内容", "来源页"]

        self.processed_table.setColumnCount(len(columns))
        self.processed_table.setHorizontalHeaderLabels([str(c) for c in columns])
        self.processed_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            if isinstance(row, dict):
                for j, col in enumerate(columns):
                    self.processed_table.setItem(i, j, QTableWidgetItem(str(row.get(col, ""))))
            else:
                for j, cell in enumerate(row):
                    self.processed_table.setItem(i, j, QTableWidgetItem(str(cell)))
        self.processed_table.resizeColumnsToContents()


class RawPreviewWorker(QThread):
    result = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, file_path: str, parent=None):
        super().__init__(parent)
        self.file_path = file_path

    def run(self):
        service = PreviewService()
        try:
            preview = service.load_raw_preview(self.file_path)
            self.result.emit(preview.get("text", ""))
        except Exception as e:
            self.error.emit(f"无法加载预览: {e}")


class PreviewWorker(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    result = pyqtSignal(dict)

    def __init__(self, file_path: str, options: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.options = options

    def run(self):
        service = PreviewService(
            on_progress=self.progress.emit,
            on_status=self.status.emit,
        )
        try:
            result = service.process_preview(self.file_path, self.options)
            self.result.emit(result)
        except Exception as e:
            self.status.emit(f"解析失败: {e}")
            self.result.emit({"tables": [], "text": str(e)})
