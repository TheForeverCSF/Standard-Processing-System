# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
from PyQt6.QtGui import QFont, QPalette, QColor
from PyQt6.QtCore import Qt

PRIMARY = "#2563EB"
PRIMARY_HOVER = "#1D4ED8"
SECONDARY = "#64748B"
BG = "#F8FAFC"
CARD_BG = "#FFFFFF"
TEXT = "#1E293B"
BORDER = "#E2E8F0"
SUCCESS = "#22C55E"
WARNING = "#F59E0B"
ERROR = "#EF4444"

FONT_FAMILY = "Microsoft YaHei UI, Segoe UI, PingFang SC, sans-serif"


def global_style():
    return f"""
    QWidget {{
        font-family: {FONT_FAMILY};
        color: {TEXT};
        background: {BG};
    }}
    QMainWindow {{
        background: {BG};
    }}
    QPushButton {{
        background: {PRIMARY};
        color: white;
        border: none;
        border-radius: 6px;
        padding: 10px 20px;
        font-size: 14px;
        font-weight: 500;
        min-height: 36px;
    }}
    QPushButton:hover {{
        background: {PRIMARY_HOVER};
    }}
    QPushButton:disabled {{
        background: {BORDER};
        color: {SECONDARY};
    }}
    QPushButton#secondary {{
        background: {CARD_BG};
        color: {TEXT};
        border: 1px solid {BORDER};
    }}
    QPushButton#secondary:hover {{
        background: {BG};
        border: 1px solid {PRIMARY};
        color: {PRIMARY};
    }}
    QPushButton#danger {{
        background: {ERROR};
    }}
    QPushButton#danger:hover {{
        background: #DC2626;
    }}
    QLineEdit, QTextEdit, QSpinBox, QComboBox {{
        background: {CARD_BG};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 8px 12px;
        font-size: 14px;
    }}
    QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{
        border: 1px solid {PRIMARY};
    }}
    QSpinBox::up-button, QSpinBox::down-button {{
        width: 20px;
        background: {BG};
        border: 1px solid {BORDER};
    }}
    QProgressBar {{
        border: none;
        border-radius: 4px;
        background: {BORDER};
        height: 8px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background: {PRIMARY};
        border-radius: 4px;
    }}
    QTableWidget {{
        background: {CARD_BG};
        border: 1px solid {BORDER};
        border-radius: 6px;
        gridline-color: {BORDER};
    }}
    QHeaderView::section {{
        background: {BG};
        padding: 8px;
        border: 1px solid {BORDER};
        font-weight: 600;
    }}
    QScrollArea {{
        border: none;
    }}
    QLabel#title {{
        font-size: 24px;
        font-weight: 700;
        color: {TEXT};
    }}
    QLabel#subtitle {{
        font-size: 14px;
        color: {SECONDARY};
    }}
    QLabel#card {{
        background: {CARD_BG};
        border: 1px solid {BORDER};
        border-radius: 8px;
        padding: 16px;
    }}
    QListWidget {{
        background: {CARD_BG};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 8px;
    }}
    QListWidget::item {{
        padding: 8px;
        border-radius: 4px;
    }}
    QListWidget::item:selected {{
        background: {PRIMARY};
        color: white;
    }}
    QCheckBox {{
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border-radius: 4px;
        border: 1px solid {BORDER};
        background: {CARD_BG};
    }}
    QCheckBox::indicator:checked {{
        background: {PRIMARY};
        border: 1px solid {PRIMARY};
    }}
    QSplitter::handle {{
        background: {BORDER};
    }}
    QSplitter::handle:horizontal {{
        width: 2px;
    }}
    QTabWidget::pane {{
        border: 1px solid {BORDER};
        border-radius: 8px;
        background: {CARD_BG};
    }}
    QTabBar::tab {{
        background: {BG};
        padding: 8px 16px;
        border: 1px solid {BORDER};
        border-bottom: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
    }}
    QTabBar::tab:selected {{
        background: {CARD_BG};
        color: {PRIMARY};
        font-weight: 600;
    }}
"""
