# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QTableWidget, QTableWidgetItem, QTextEdit, QMessageBox, QTabWidget, QFileDialog
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread
import os
import datetime

from gui.styles import TEXT, SECONDARY, CARD_BG, BORDER, PRIMARY, SUCCESS, ERROR, WARNING, BG
from gui.components import Card
from core.processor import DocumentProcessor



class ResultPage(QWidget):
    back_to_upload = pyqtSignal()
    location_selected = pyqtSignal(str, str)  # file_path, keyword

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 24)
        layout.setSpacing(20)

        # 标题区
        title = QLabel("处理结果")
        title.setObjectName("title")
        layout.addWidget(title)

        # 进度与状态
        status_card = Card("处理进度")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        status_card.layout.addWidget(self.progress)
        self.status_label = QLabel("准备处理...")
        self.status_label.setStyleSheet(f"color: {SECONDARY};")
        status_card.layout.addWidget(self.status_label)
        layout.addWidget(status_card)

        # 结果标签页
        self.tabs = QTabWidget()

        self.table_tab = QTableWidget()
        self.table_tab.setColumnCount(4)
        self.table_tab.setHorizontalHeaderLabels(["标准名称", "内容", "来源页", "已审核"])
        self.table_tab.setColumnWidth(1, 400)
        self.table_tab.setColumnWidth(3, 80)
        self.tabs.addTab(self.table_tab, "标准表格")

        stats_widget = QWidget()
        stats_layout = QVBoxLayout(stats_widget)
        stats_layout.setContentsMargins(0, 0, 0, 0)

        self.stats_tab = QTableWidget()
        self.stats_tab.setColumnCount(3)
        self.stats_tab.setHorizontalHeaderLabels(["标准名称", "出现次数", "出现位置"])
        self.stats_tab.itemClicked.connect(self.on_stats_item_clicked)
        stats_layout.addWidget(self.stats_tab)

        self.locations_tab = QTableWidget()
        self.locations_tab.setColumnCount(3)
        self.locations_tab.setHorizontalHeaderLabels(["来源文档", "位置", "内容片段"])
        self.locations_tab.itemClicked.connect(self.on_location_item_clicked)
        self.locations_tab.setMaximumHeight(220)
        stats_layout.addWidget(self.locations_tab)

        self.tabs.addTab(stats_widget, "标准统计")

        self.text_tab = QTextEdit()
        self.text_tab.setReadOnly(True)
        self.tabs.addTab(self.text_tab, "提取文本")

        self.initial_hint = QLabel("暂无处理结果。请先回到「页面确认」页，点击「开始处理」按钮。")
        self.initial_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.initial_hint.setStyleSheet(f"color: {SECONDARY}; font-size: 14px; padding: 40px;")
        self.tabs.addTab(self.initial_hint, "提示")

        self.log_tab = QTextEdit()
        self.log_tab.setReadOnly(True)
        self.tabs.addTab(self.log_tab, "处理日志")

        layout.addWidget(self.tabs, 1)

        # 底部操作栏（固定可见）
        footer = QHBoxLayout()
        footer.setSpacing(12)
        back_btn = QPushButton("← 返回上传")
        back_btn.setObjectName("secondary")
        back_btn.clicked.connect(self.back_to_upload.emit)
        footer.addWidget(back_btn)
        footer.addStretch()
        self.export_btn = QPushButton("导出结果")
        self.export_btn.setObjectName("secondary")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_results)
        footer.addWidget(self.export_btn)
        layout.addLayout(footer)

        self._results = {"tables": [], "text": "", "logs": [], "stats": {}}
        self._worker = None
        self._current_stats = {}

    def start_task(self, task):
        import sys
        print(f"[DEBUG] start_task called, task={task.get('files')}", flush=True, file=sys.stdout)
        self.progress.setValue(0)
        self.status_label.setText("开始处理...")
        self.status_label.setStyleSheet(f"color: {SECONDARY};")
        self.table_tab.setRowCount(0)
        self.stats_tab.setRowCount(0)
        self.locations_tab.setRowCount(0)
        self.text_tab.clear()
        self.log_tab.clear()
        self.export_btn.setEnabled(False)
        self._results = {"tables": [], "text": "", "logs": [], "stats": {}}

        self._safe_stop_worker()

        # 处理开始时隐藏提示标签，切换到标准表格页
        idx = self.tabs.indexOf(self.initial_hint)
        if idx >= 0:
            self.tabs.removeTab(idx)

        self._worker = ProcessorWorker(task, self)
        self._worker.progress.connect(self.on_progress)
        self._worker.status.connect(self.on_status)
        self._worker.log.connect(self.on_log)
        self._worker.result_ready.connect(self.on_finished)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()
        print(f"[DEBUG] worker started, isRunning={self._worker.isRunning()}", flush=True, file=sys.stdout)

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


    def _on_worker_finished(self):
        self._worker = None

    def on_progress(self, value):
        self.progress.setValue(value)

    def on_status(self, text):
        self.status_label.setText(text)

    def on_log(self, text):
        self.log_tab.append(text)
        self._results["logs"].append(text)
        # 同时写入文件日志，便于排查卡住问题
        try:
            log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "process.log")
            log_path = os.path.abspath(log_path)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
        except Exception:
            pass


    def on_finished(self, results):
        logs = results.get("logs", [])
        if logs:
            self._results["logs"].extend(logs)
        self._results.update({k: v for k, v in results.items() if k != "logs"})

        has_error = any("[错误]" in log for log in self._results["logs"])
        has_tables = bool(self._results["tables"])
        text_len = len(self._results.get("text", ""))

        if has_error:
            self.status_label.setText("处理完成（存在错误，请查看处理日志）")
            self.status_label.setStyleSheet(f"color: {ERROR};")
        elif not has_tables:
            self.progress.setValue(100)
            self.status_label.setText(f"处理完成：未识别到标准（已提取 {text_len} 字符）")
            self.status_label.setStyleSheet(f"color: {WARNING};")
        else:
            self.progress.setValue(100)
            self.status_label.setText(f"处理完成：识别到 {len(self._results['tables'])} 条记录")
            self.status_label.setStyleSheet(f"color: {SUCCESS};")
        self.export_btn.setEnabled(True)

        self.text_tab.setPlainText(self._results.get("text", ""))
        self._fill_table(self._results.get("tables", []))
        self._fill_stats(self._results.get("stats", {}))

    def _fill_table(self, tables):
        self.table_tab.setRowCount(len(tables))
        for i, row in enumerate(tables):
            for j, key in enumerate(["name", "content", "page"]):
                self.table_tab.setItem(i, j, QTableWidgetItem(str(row.get(key, ""))))
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk_item.setCheckState(Qt.CheckState.Unchecked)
            self.table_tab.setItem(i, 3, chk_item)
        self.table_tab.resizeColumnsToContents()

    def _fill_stats(self, stats):
        self._current_stats = stats
        self.stats_tab.setRowCount(len(stats))
        for i, (name, data) in enumerate(stats.items()):
            self.stats_tab.setItem(i, 0, QTableWidgetItem(name))
            self.stats_tab.setItem(i, 1, QTableWidgetItem(str(data.get("count", 0))))
            locations = data.get("locations", [])
            loc_text = "; ".join(f"{loc.get('file_name', '')} {loc.get('page', '')}" for loc in locations[:3])
            if len(locations) > 3:
                loc_text += f" 等 {len(locations)} 处"
            self.stats_tab.setItem(i, 2, QTableWidgetItem(loc_text))
        self.stats_tab.resizeColumnsToContents()

    def on_stats_item_clicked(self, item):
        row = item.row()
        name_item = self.stats_tab.item(row, 0)
        if not name_item:
            return
        name = name_item.text()
        data = self._current_stats.get(name, {})
        locations = data.get("locations", [])

        self.locations_tab.setRowCount(len(locations))
        for i, loc in enumerate(locations):
            file_item = QTableWidgetItem(loc.get("file_name", ""))
            file_item.setData(Qt.ItemDataRole.UserRole, loc.get("file", ""))
            self.locations_tab.setItem(i, 0, file_item)
            self.locations_tab.setItem(i, 1, QTableWidgetItem(loc.get("page", "")))
            self.locations_tab.setItem(i, 2, QTableWidgetItem(loc.get("content", "")))
        self.locations_tab.resizeColumnsToContents()
        self.locations_tab.setProperty("current_keyword", name)

    def on_location_item_clicked(self, item):
        row = item.row()
        file_item = self.locations_tab.item(row, 0)
        if not file_item:
            return
        file_path = file_item.data(Qt.ItemDataRole.UserRole) or ""
        keyword = self.locations_tab.property("current_keyword") or ""
        if file_path and keyword:
            self.location_selected.emit(file_path, keyword)

    def export_results(self):
        path, selected = QFileDialog.getSaveFileName(
            self, "导出结果", "处理结果.txt",
            "文本文件 (*.txt);;CSV 文件 (*.csv)"
        )
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if not ext and selected.startswith("CSV"):
            ext = ".csv"
            path += ext
        try:
            lines = []
            lines.append("=== 处理结果 ===")
            lines.append("")
            lines.append("--- 标准统计 ---")
            for name, data in self._current_stats.items():
                lines.append(f"{name}: 出现 {data.get('count', 0)} 次")
                for loc in data.get("locations", []):
                    lines.append(f"    文件: {loc.get('file_name', '')} 位置: {loc.get('page', '')}")
            lines.append("")
            lines.append("--- 标准表格 ---")
            for row in self._results.get("tables", []):
                lines.append(f"{row.get('name', '')} | {row.get('page', '')} | {row.get('content', '')}")
            lines.append("")
            lines.append("--- 提取文本 ---")
            lines.append(self._results.get("text", ""))

            if ext == ".csv":
                import csv
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["标准名称", "来源页", "内容", "来源文件"])
                    for row in self._results.get("tables", []):
                        writer.writerow([
                            row.get("name", ""),
                            row.get("page", ""),
                            row.get("content", ""),
                            row.get("file_name", ""),
                        ])
            else:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
            QMessageBox.information(self, "导出成功", f"已保存到：{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))


class ProcessorWorker(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    log = pyqtSignal(str)
    result_ready = pyqtSignal(dict)

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task

    def run(self):
        import traceback
        processor = DocumentProcessor(
            on_progress=self.progress.emit,
            on_status=self.status.emit,
            on_log=self.log.emit
        )
        try:
            results = processor.process(self.task)
            self.result_ready.emit(results)
        except Exception as e:
            err = f"[错误] {e}\n{traceback.format_exc()}"
            self.log.emit(err)
            self.result_ready.emit({"tables": [], "text": "", "logs": [err], "stats": {}})
