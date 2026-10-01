# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QStackedWidget, QSplitter, QTreeWidget, QTreeWidgetItem,
    QFrame
)
from PyQt6.QtCore import Qt, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QFont

from gui.styles import global_style, TEXT, BG, PRIMARY, BORDER, CARD_BG
from gui.upload_page import UploadPage
from gui.preview_page import PreviewPage
from gui.result_page import ResultPage
from gui.preview_panel import PreviewPanel
from utils.helpers import get_document_page_count



class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("标准处理系统")
        self.setMinimumSize(1280, 760)
        self.setStyleSheet(global_style())
        self.setContentsMargins(0, 0, 0, 0)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 左侧导航栏
        sidebar = self._build_sidebar()
        main_layout.addWidget(sidebar)

        # 右侧区域：可分割的主内容 + 预览面板 + 项目树
        right_area = QWidget()
        right_layout = QHBoxLayout(right_area)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # 主内容栈
        self.stack = QStackedWidget()
        self.stack.setStyleSheet(f"background: {BG};")
        self.upload_page = UploadPage()
        self.preview_page = PreviewPage()
        self.result_page = ResultPage()

        self.stack.addWidget(self.upload_page)
        self.stack.addWidget(self.preview_page)
        self.stack.addWidget(self.result_page)

        # 项目树（默认折叠）
        self.project_tree_frame = QFrame()
        self.project_tree_frame.setObjectName("project_tree_frame")
        self.project_tree_frame.setStyleSheet(f"""
            QFrame#project_tree_frame {{
                background: {CARD_BG};
                border-left: 1px solid {BORDER};
                border-right: 1px solid {BORDER};
            }}
        """)
        tree_layout = QVBoxLayout(self.project_tree_frame)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(0)

        tree_header = QLabel(" 项目结构")
        tree_header.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {TEXT}; padding: 12px 8px;")
        tree_layout.addWidget(tree_header)

        self.project_tree = QTreeWidget()
        self.project_tree.setHeaderHidden(True)
        self.project_tree.setStyleSheet(f"""
            QTreeWidget {{
                background: {CARD_BG};
                border: none;
                outline: none;
            }}
            QTreeWidget::item {{
                padding: 6px 4px;
                border-radius: 4px;
            }}
            QTreeWidget::item:selected {{
                background: {BG};
                color: {PRIMARY};
            }}
        """)
        self.project_tree.itemClicked.connect(self.on_tree_item_clicked)
        tree_layout.addWidget(self.project_tree)

        self.project_tree_frame.setMaximumWidth(0)
        self.project_tree_frame.setMinimumWidth(0)

        # 文件预览面板
        self.preview_panel = PreviewPanel()
        self.preview_panel.setObjectName("preview_panel")
        self.preview_panel.setMinimumWidth(380)
        self.preview_panel.setMaximumWidth(520)

        # 用分割器组合：主内容 | 项目树 | 预览面板
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.addWidget(self.stack)
        self.main_splitter.addWidget(self.project_tree_frame)
        self.main_splitter.addWidget(self.preview_panel)
        self.main_splitter.setSizes([800, 0, 420])
        self.main_splitter.setHandleWidth(2)
        right_layout.addWidget(self.main_splitter, 1)

        main_layout.addWidget(right_area, 1)

        # 信号连接
        self.upload_page.files_selected.connect(self.on_files_selected)
        self.upload_page.files_cleared.connect(self.on_files_cleared)
        self.upload_page.file_selected.connect(self.preview_panel.set_file)
        self.upload_page.folder_loaded.connect(self.on_folder_loaded)
        self.preview_page.file_selected.connect(self.preview_panel.set_file)
        self.preview_page.start_processing.connect(self.on_start_processing)
        self.result_page.location_selected.connect(self.on_location_selected)

        self.preview_page.back_to_upload.connect(lambda: self.switch_page(0))
        self.result_page.back_to_upload.connect(lambda: self.switch_page(0))

        self._current_files = []
        self._has_results = False

        self.switch_page(0)

    def _build_sidebar(self):
        sidebar = QWidget()
        sidebar.setFixedWidth(200)
        sidebar.setStyleSheet(f"""
            QWidget {{
                background: {CARD_BG};
                border-right: 1px solid {BORDER};
            }}
        """)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(16, 24, 16, 20)
        sb_layout.setSpacing(12)

        title = QLabel("标准处理系统")
        title.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {TEXT};")
        sb_layout.addWidget(title)

        subtitle = QLabel("文档标准化处理平台")
        subtitle.setStyleSheet(f"font-size: 12px; color: #64748B;")
        sb_layout.addWidget(subtitle)
        sb_layout.addSpacing(24)

        self.nav_btns = []
        for idx, text in enumerate(["上传文件", "页面确认", "处理结果"]):
            btn = QPushButton(f" {text}")
            btn.setCheckable(True)
            btn.setFixedHeight(42)
            btn.setStyleSheet(f"""
                QPushButton {{
                    text-align: left;
                    background: transparent;
                    color: #475569;
                    border-radius: 8px;
                    font-size: 14px;
                    font-weight: 500;
                }}
                QPushButton:hover {{
                    background: #F1F5F9;
                }}
                QPushButton:checked {{
                    background: {PRIMARY};
                    color: white;
                }}
            """)
            btn.clicked.connect(lambda checked, i=idx: self.switch_page(i))
            self.nav_btns.append(btn)
            sb_layout.addWidget(btn)

        sb_layout.addStretch()
        status = QLabel("就绪")
        status.setStyleSheet(f"color: #64748B; font-size: 12px;")
        sb_layout.addWidget(status)
        self.status_label = status

        return sidebar

    def switch_page(self, index):
        for i, btn in enumerate(self.nav_btns):
            btn.setChecked(i == index)
        self.stack.setCurrentIndex(index)
        if index == 0:
            self.status_label.setText("等待上传文件")
        elif index == 1:
            self.status_label.setText("确认处理页面")
            files = self.upload_page.get_files()
            if files and files != self._current_files:
                self._current_files = list(files)
                self.preview_page.set_files(files)
        else:
            self.status_label.setText("查看处理结果")

    def on_files_selected(self, files):
        self._current_files = list(files)
        # 同步页面确认页数据，这会触发 file_selected 信号，进而在预览面板加载
        self.preview_page.set_files(files)

        # 切换到页面确认页，让用户自行选择页码范围和处理选项后点击开始处理
        self.switch_page(1)


    def on_files_cleared(self):
        self._current_files = []
        self._has_results = False

    def on_folder_loaded(self, tree_data):
        """加载文件夹结构并滑出项目结构树。"""
        items = tree_data.get("items", [])
        if not items:
            self.show_project_tree(False)
            return
        self.project_tree.clear()
        root = QTreeWidgetItem(self.project_tree)
        root.setText(0, tree_data.get("root", "项目"))
        root.setExpanded(True)
        self._build_tree(root, items)
        self.project_tree.addTopLevelItem(root)
        self.show_project_tree(True)

    def _build_tree(self, parent_item, items):
        for item in items:
            node = QTreeWidgetItem(parent_item)
            node.setText(0, item.get("name", ""))
            node.setData(0, Qt.ItemDataRole.UserRole, item)
            if item.get("type") == "folder":
                self._build_tree(node, item.get("items", []))

    def on_tree_item_clicked(self, item, column):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data.get("type") == "file":
            file_path = data.get("path", "")
            if file_path:
                self.preview_panel.set_file(file_path)

    def show_project_tree(self, visible: bool):
        if visible:
            if self.project_tree_frame.width() > 0:
                return
            self.project_tree_frame.setMaximumWidth(250)
            # 动画展开项目树
            self._tree_animation = QPropertyAnimation(self.project_tree_frame, b"minimumWidth")
            self._tree_animation.setDuration(300)
            self._tree_animation.setStartValue(0)
            self._tree_animation.setEndValue(220)
            self._tree_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._tree_animation.start()
            # 同时给预览面板更多空间：保持总宽度不变，压缩主内容
            sizes = self.main_splitter.sizes()
            if len(sizes) == 3 and sizes[1] == 0:
                self.main_splitter.setSizes([sizes[0] - 220, 220, sizes[2]])
        else:
            self.project_tree_frame.setMinimumWidth(0)
            self.project_tree_frame.setMaximumWidth(0)
            sizes = self.main_splitter.sizes()
            if len(sizes) == 3 and sizes[1] > 0:
                self.main_splitter.setSizes([sizes[0] + sizes[1], 0, sizes[2]])

    def on_location_selected(self, file_path, keyword):
        self.preview_panel.jump_to(file_path, keyword)

    def on_start_processing(self, task):
        self._has_results = True
        self.result_page.start_task(task)
        self.switch_page(2)
