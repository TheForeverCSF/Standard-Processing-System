# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QSpinBox, QCheckBox, QGroupBox, QMessageBox, QScrollArea
)
from PyQt6.QtCore import Qt, pyqtSignal
import os

from gui.styles import TEXT, SECONDARY, CARD_BG, BORDER, PRIMARY, ERROR, BG
from gui.components import Card
from utils.helpers import get_document_page_count


class PreviewPage(QWidget):
    start_processing = pyqtSignal(dict)
    back_to_upload = pyqtSignal()
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._files = []
        self._file_pages = {}
        self._max_pages = 1

        # 使用滚动区域包装内容，防止窗口较小时底部按钮被截断
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent; border: none;")

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(32, 32, 32, 24)
        layout.setSpacing(20)

        # 标题区
        title = QLabel("确认需要处理的页面")
        title.setObjectName("title")
        layout.addWidget(title)

        sub = QLabel("选择要处理的文件页码范围及处理选项")
        sub.setObjectName("subtitle")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        # 文件列表卡片
        file_card = Card("已选文件")
        self.file_list = QListWidget()
        self.file_list.setFixedHeight(160)
        self.file_list.itemClicked.connect(self.on_item_clicked)
        self.file_list.itemSelectionChanged.connect(self.on_selection_changed)
        file_card.layout.addWidget(self.file_list)

        self.page_hint = QLabel("请选择文件查看页数")
        self.page_hint.setStyleSheet(f"color: {SECONDARY}; font-size: 12px;")
        self.page_hint.setWordWrap(True)
        file_card.layout.addWidget(self.page_hint)
        layout.addWidget(file_card)

        # 页码设置卡片
        page_card = Card("页码范围")
        page_layout = QHBoxLayout()
        page_layout.setSpacing(16)

        start_box = QVBoxLayout()
        start_box.addWidget(QLabel("起始页"))
        self.start_page = QSpinBox()
        self.start_page.setMinimum(1)
        self.start_page.setValue(1)
        self.start_page.setMaximum(1)
        self.start_page.setFixedWidth(120)
        start_box.addWidget(self.start_page)
        page_layout.addLayout(start_box)

        end_box = QVBoxLayout()
        end_box.addWidget(QLabel("结束页"))
        self.end_page = QSpinBox()
        self.end_page.setMinimum(1)
        self.end_page.setValue(1)
        self.end_page.setMaximum(1)
        self.end_page.setFixedWidth(120)
        end_box.addWidget(self.end_page)
        page_layout.addLayout(end_box)

        self.total_label = QLabel("共 1 页")
        self.total_label.setStyleSheet(f"color: {SECONDARY}; padding-top: 20px;")
        page_layout.addWidget(self.total_label)
        page_layout.addStretch()
        page_card.layout.addLayout(page_layout)
        layout.addWidget(page_card)

        # 处理选项卡片
        opts_card = Card("处理选项")
        opts = QHBoxLayout()
        opts.setSpacing(20)
        self.table_cb = QCheckBox("提取表格")
        self.table_cb.setChecked(True)
        self.body_cb = QCheckBox("提取正文")
        self.body_cb.setChecked(True)
        self.image_cb = QCheckBox("识别图片")
        self.image_cb.setChecked(True)
        self.textbox_cb = QCheckBox("提取文本框")
        self.textbox_cb.setChecked(True)
        for cb in [self.table_cb, self.body_cb, self.image_cb, self.textbox_cb]:
            opts.addWidget(cb)
        opts.addStretch()
        opts_card.layout.addLayout(opts)
        layout.addWidget(opts_card)

        # 底部操作栏（固定在最外侧，始终可见）
        footer = QHBoxLayout()
        footer.setSpacing(12)
        back_btn = QPushButton("← 返回上传")
        back_btn.setObjectName("secondary")
        back_btn.clicked.connect(self.back_to_upload.emit)
        footer.addWidget(back_btn)
        footer.addStretch()
        self.start_btn = QPushButton("开始处理")
        self.start_btn.setMinimumWidth(140)
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self.confirm)
        footer.addWidget(self.start_btn)

        scroll.setWidget(container)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll, 1)
        main_layout.addLayout(footer)

    def set_files(self, files):
        self._files = list(files)
        self._file_pages = {}
        self.file_list.clear()

        max_pages = 1
        for f in files:
            pages = get_document_page_count(f)
            self._file_pages[f] = pages
            max_pages = max(max_pages, pages)
            text = f"{f}  ({pages} 页)"
            self.file_list.addItem(text)

        self._max_pages = max(1, max_pages)
        self.start_page.setMaximum(self._max_pages)
        self.end_page.setMaximum(self._max_pages)
        self.start_page.setValue(1)
        self.end_page.setValue(self._max_pages)
        self.total_label.setText(f"共 {self._max_pages} 页")

        # 默认选中第一个文件
        if files:
            self.file_list.setCurrentRow(0)
            self.file_selected.emit(files[0])

    def on_item_clicked(self, item):
        file_path = self._path_from_item_text(item.text())
        if file_path:
            self.file_selected.emit(file_path)
            self._sync_page_to_file(file_path)

    def on_selection_changed(self):
        selected = self.file_list.selectedItems()
        if selected:
            self.on_item_clicked(selected[0])

    def _path_from_item_text(self, text: str) -> str:
        # 列表项格式："{path}  ({pages} 页)"
        suffix = "  ("
        if suffix in text:
            return text.rsplit(suffix, 1)[0]
        return text

    def _sync_page_to_file(self, file_path: str):
        pages = self._file_pages.get(file_path, 1)
        self.total_label.setText(f"当前文件共 {pages} 页，全部文件最大 {self._max_pages} 页")
        # 限制当前页码不超过该文件页数
        self.start_page.setMaximum(pages)
        self.end_page.setMaximum(pages)
        self.start_page.setValue(1)
        self.end_page.setValue(pages)

    def confirm(self):
        if not self._files:
            QMessageBox.warning(self, "提示", "请先上传文件")
            return
        start_p = self.start_page.value()
        end_p = self.end_page.value()
        if end_p < start_p:
            QMessageBox.warning(self, "提示", "结束页不能小于起始页")
            return
        task = {
            "files": self._files,
            "start_page": start_p,
            "end_page": end_p,
            "extract_table": self.table_cb.isChecked(),
            "extract_body": self.body_cb.isChecked(),
            "extract_image": self.image_cb.isChecked(),
            "extract_textbox": self.textbox_cb.isChecked(),
            "ai_enhance": True,
            "ocr_images": True,
        }
        self.start_processing.emit(task)
