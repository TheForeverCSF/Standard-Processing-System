# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import os
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QFileDialog
)
from PyQt6.QtCore import Qt, pyqtSignal

from gui.styles import TEXT, SECONDARY, BORDER, CARD_BG, BG, PRIMARY
from gui.components import Card, DropArea


class UploadPage(QWidget):
    files_selected = pyqtSignal(list)
    files_cleared = pyqtSignal()
    file_selected = pyqtSignal(str)  # 点击文件列表时触发
    folder_loaded = pyqtSignal(dict)  # 文件夹上传时触发，包含项目结构

    def __init__(self, parent=None):
        super().__init__(parent)
        self._files = []
        self._folder_mode = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 24)
        layout.setSpacing(20)

        # 标题
        title = QLabel("上传待处理文件")
        title.setObjectName("title")
        layout.addWidget(title)

        sub = QLabel("支持 DOC、DOCX、XLSX、XLS、PDF 格式，可拖拽文件或文件夹，或点击选择")
        sub.setObjectName("subtitle")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        # 拖拽区域
        self.drop = DropArea()
        self.drop.dropped.connect(self.add_paths)
        self.drop.setFixedHeight(180)
        layout.addWidget(self.drop)

        # 已选文件卡片
        card = Card("已选文件")
        self.list_widget = QListWidget()
        self.list_widget.setFixedHeight(140)
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        card.layout.addWidget(self.list_widget)

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        add_file_btn = QPushButton("添加文件")
        add_file_btn.setObjectName("secondary")
        add_file_btn.clicked.connect(self.browse_files)
        add_folder_btn = QPushButton("添加文件夹")
        add_folder_btn.setObjectName("secondary")
        add_folder_btn.clicked.connect(self.browse_folder)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("danger")
        clear_btn.clicked.connect(self.clear_files)
        btn_layout.addWidget(add_file_btn)
        btn_layout.addWidget(add_folder_btn)
        btn_layout.addWidget(clear_btn)
        btn_layout.addStretch()
        card.layout.addLayout(btn_layout)
        layout.addWidget(card, 1)

        # 底部操作栏（固定，不添加 stretch）
        footer = QHBoxLayout()
        footer.setSpacing(12)
        footer.addStretch()
        self.next_btn = QPushButton("确认文件 →")
        self.next_btn.setEnabled(False)
        self.next_btn.setMinimumWidth(140)
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self.confirm)
        footer.addWidget(self.next_btn)
        layout.addLayout(footer)

    def add_paths(self, paths):
        """支持文件或文件夹路径，文件夹会递归扫描支持的文档。"""
        files = []
        for p in paths:
            if os.path.isdir(p):
                files.extend(self._collect_files_from_folder(p))
                self._folder_mode = True
            elif self._is_supported_file(p):
                files.append(p)
        self.add_files(files)
        if self._folder_mode:
            self._emit_folder_structure()

    def _is_supported_file(self, path: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        return ext in (".doc", ".docx", ".xlsx", ".xls", ".pdf")

    def _collect_files_from_folder(self, folder: str) -> list:
        collected = []
        for root, dirs, files in os.walk(folder):
            for f in files:
                path = os.path.join(root, f)
                if self._is_supported_file(path):
                    collected.append(path)
        return sorted(collected)

    def _emit_folder_structure(self):
        if not self._files:
            return
        try:
            common_root = os.path.commonpath(self._files)
            if os.path.isfile(common_root):
                common_root = os.path.dirname(common_root)
        except ValueError:
            common_root = os.path.dirname(self._files[0])
        tree = {
            "root": os.path.basename(common_root) or common_root,
            "items": self._build_tree_items(common_root, self._files)
        }
        self.folder_loaded.emit(tree)

    def _build_tree_items(self, root: str, files: list) -> list:
        """构建嵌套的项目结构。"""
        rel_files = [os.path.relpath(f, root) for f in files]
        tree = {}
        for rel in rel_files:
            parts = rel.split(os.sep)
            node = tree
            for i, part in enumerate(parts):
                key = part
                if i == len(parts) - 1:
                    node[key] = {"type": "file", "path": os.path.join(root, rel)}
                else:
                    if key not in node or not isinstance(node[key], dict):
                        node[key] = {}
                    node = node[key]

        items = []
        for name, value in sorted(tree.items()):
            if isinstance(value, dict) and "type" not in value:
                items.append({
                    "type": "folder",
                    "name": name,
                    "items": self._dict_to_tree_items(value)
                })
            else:
                items.append({
                    "type": "file",
                    "name": name,
                    "path": value.get("path", "")
                })
        return items

    def _dict_to_tree_items(self, node: dict) -> list:
        items = []
        for name, value in sorted(node.items()):
            if isinstance(value, dict) and "type" not in value:
                items.append({
                    "type": "folder",
                    "name": name,
                    "items": self._dict_to_tree_items(value)
                })
            else:
                items.append({
                    "type": "file",
                    "name": name,
                    "path": value.get("path", "")
                })
        return items

    def add_files(self, files):
        for f in files:
            if f not in self._files:
                self._files.append(f)
                self.list_widget.addItem(f)
        self.next_btn.setEnabled(bool(self._files))

    def browse_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", "",
            "文档文件 (*.doc *.docx *.xlsx *.xls *.pdf);;所有文件 (*.*)"
        )
        if files:
            self._folder_mode = False
            self.add_files(files)

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            self._files.clear()
            self.list_widget.clear()
            self._folder_mode = True
            self.add_paths([folder])

    def clear_files(self):
        self._files.clear()
        self.list_widget.clear()
        self.next_btn.setEnabled(False)
        self._folder_mode = False
        self.folder_loaded.emit({"root": "", "items": []})
        self.files_cleared.emit()

    def get_files(self):
        return list(self._files)

    def confirm(self):
        if self._files:
            self.files_selected.emit(self._files)

    def on_item_clicked(self, item):
        self.file_selected.emit(item.text())
