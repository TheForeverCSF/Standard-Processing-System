# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QStackedWidget, QFrame, QFileDialog, QProgressBar, QTableWidget,
    QTableWidgetItem, QMessageBox, QLineEdit, QListWidget, QSpinBox,
    QCheckBox, QComboBox, QTextEdit, QScrollArea, QGridLayout
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QDragEnterEvent, QDropEvent

from gui.styles import CARD_BG, BORDER, PRIMARY, TEXT, SECONDARY, BG, SUCCESS, WARNING, ERROR, FONT_FAMILY


class Card(QFrame):
    """白色卡片容器，带标题。"""

    def __init__(self, title=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setStyleSheet(f"""
            #Card {{
                background: {CARD_BG};
                border: 1px solid {BORDER};
                border-radius: 10px;
            }}
        """)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(20, 20, 20, 20)
        self.layout.setSpacing(16)
        if title:
            lbl = QLabel(title)
            lbl.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {TEXT};")
            self.layout.addWidget(lbl)


class DropArea(QFrame):
    """文件拖拽区域，点击可打开文件选择。"""

    dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(160)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._set_normal_style()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)

        icon = QLabel("📁")
        icon.setStyleSheet("font-size: 44px; background: transparent;")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)

        text = QLabel("拖拽文件或文件夹到此处，或点击选择")
        text.setStyleSheet(f"font-size: 14px; color: {SECONDARY}; background: transparent;")
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(text)

        sub = QLabel("支持 DOC、DOCX、XLSX、XLS、PDF")
        sub.setStyleSheet(f"font-size: 12px; color: {SECONDARY}; background: transparent;")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(sub)

        self.setLayout(layout)

    def _set_normal_style(self):
        self.setStyleSheet(f"""
            DropArea {{
                background: {BG};
                border: 2px dashed {BORDER};
                border-radius: 10px;
            }}
        """)

    def _set_hover_style(self):
        self.setStyleSheet(f"""
            DropArea {{
                background: {BG};
                border: 2px dashed {PRIMARY};
                border-radius: 10px;
            }}
        """)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_hover_style()

    def dragLeaveEvent(self, event):
        self._set_normal_style()

    def dropEvent(self, event: QDropEvent):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        self.dropped.emit(files)
        self._set_normal_style()

    def mousePressEvent(self, event):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", "",
            "文档文件 (*.doc *.docx *.xlsx *.xls *.pdf);;所有文件 (*.*)"
        )
        if files:
            self.dropped.emit(files)
