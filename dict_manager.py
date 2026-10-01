#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""
标准名称字典管理工具
功能：
  1. 审核待处理标准（审核 → 移入主库 / 拒绝删除）
  2. 主字典浏览（搜索 / 新增 / 删除）
"""
import sys, os, re, sqlite3
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QSplitter, QTextEdit,
    QMessageBox, QTabWidget, QFrame
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

# ── 数据库 ────────────────────────────────────────────
# 主程序把用户数据（含本字典）放在 %LOCALAPPDATA%\标准处理系统；老版本放在程序目录。
# 这里跟随同一规则，否则管理工具会改到"程序根本不读"的那份文件。
if hasattr(sys, '_MEIPASS'):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

try:
    sys.path.insert(0, _BASE)
    from utils.helpers import user_data_dir as _udd
    _DB_PATH = os.path.join(_udd(), "标准名称字典.db")
    if not os.path.exists(_DB_PATH):        # 尚未迁移过 → 退回程序目录里的旧库
        _DB_PATH = os.path.join(_BASE, "标准名称字典.db")
except Exception:
    _DB_PATH = os.path.join(_BASE, "标准名称字典.db")


def _get_conn():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _db_load_review():
    """加载待审核列表"""
    conn = _get_conn()
    rows = conn.execute("SELECT standard_no, display_name FROM review ORDER BY standard_no").fetchall()
    conn.close()
    return [(r["display_name"], r["standard_no"], r["display_name"].split("《")[1].rstrip("》") if "《" in r["display_name"] else "") for r in rows]


def _db_load_main():
    """加载主库，返回 {标准号: 完整显示名}"""
    conn = _get_conn()
    rows = conn.execute("SELECT standard_no, display_name FROM main ORDER BY standard_no").fetchall()
    conn.close()
    return {r["standard_no"]: r["display_name"] for r in rows}


S1 = ("QPushButton { padding: 10px 28px; font-size: 14px; border: 1px solid #CBD5E1; "
      "border-radius: 6px; background: white; color: #475569; } "
      "QPushButton:hover { background: #F1F5F9; }")
S2 = ("QPushButton { padding: 10px 28px; font-size: 14px; border: none; border-radius: 6px; "
      "background: #FEE2E2; color: #DC2626; font-weight: bold; } "
      "QPushButton:hover { background: #FECACA; }")
S3 = ("QPushButton { padding: 10px 28px; font-size: 14px; border: none; border-radius: 6px; "
      "background: #DCFCE7; color: #16A34A; font-weight: bold; } "
      "QPushButton:hover { background: #BBF7D0; }")


class DictManager(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("标准名称字典管理")
        self.setMinimumSize(800, 560)
        self.resize(900, 620)

        tabs = QTabWidget()
        tabs.addTab(ReviewTab(), "审核标准")
        tabs.addTab(EditorTab(), "字典浏览/编辑")
        self.setCentralWidget(tabs)


class ReviewTab(QWidget):
    def __init__(self):
        super().__init__()
        self.items = []
        self.idx = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        t = QLabel("审核标准名称")
        t.setStyleSheet("font-size: 20px; font-weight: bold; color: #1E293B;")
        layout.addWidget(t)

        self.prog = QLabel("")
        self.prog.setStyleSheet("font-size: 13px; color: #2563EB; margin: 6px 0;")
        layout.addWidget(self.prog)

        card = QFrame()
        card.setStyleSheet("QFrame { background: white; border: 1px solid #E2E8F0; border-radius: 8px; padding: 20px; }")
        cl = QVBoxLayout(card)
        self.num = QLabel("")
        self.num.setStyleSheet("font-size: 12px; color: #94A3B8;")
        cl.addWidget(self.num)
        self.std = QLabel("")
        self.std.setStyleSheet("font-size: 20px; font-weight: bold; color: #1E293B; padding: 8px 0;")
        self.std.setWordWrap(True)
        cl.addWidget(self.std)
        self.nam = QLabel("")
        self.nam.setStyleSheet("font-size: 15px; color: #475569;")
        self.nam.setWordWrap(True)
        cl.addWidget(self.nam)
        layout.addWidget(card, stretch=1)

        bl = QHBoxLayout()
        bl.addStretch()
        self.b_skip = QPushButton("⏭ 跳过")
        self.b_skip.setStyleSheet(S1)
        self.b_skip.clicked.connect(self._skip)
        bl.addWidget(self.b_skip)
        bl.addSpacing(10)
        self.b_rej = QPushButton("✕ 拒绝")
        self.b_rej.setStyleSheet(S2)
        self.b_rej.clicked.connect(self._reject)
        bl.addWidget(self.b_rej)
        bl.addSpacing(10)
        self.b_app = QPushButton("✓ 通过")
        self.b_app.setStyleSheet(S3)
        self.b_app.clicked.connect(self._approve)
        bl.addWidget(self.b_app)
        bl.addStretch()
        layout.addLayout(bl)

        self.st = QLabel("")
        self.st.setStyleSheet("font-size: 12px; color: #64748B; padding: 4px 0;")
        layout.addWidget(self.st)
        self._load()

    def _load(self):
        self.items = _db_load_review()
        self.idx = 0
        self._show()

    def _show(self):
        if self.idx < len(self.items):
            line, n, na = self.items[self.idx]
            self.num.setText("第 {}/{} 条".format(self.idx + 1, len(self.items)))
            self.std.setText(n)
            self.nam.setText("《{}》".format(na))
            self.prog.setText("待审核: {} 条  |  当前第 {} 条".format(len(self.items), self.idx + 1))
            self.st.setText("")
        else:
            self.num.setText("完成")
            self.std.setText("🎉 所有待审核项已处理完毕")
            self.nam.setText("")
            self.prog.setText("待审核: 0 条")
            self.st.setText("")
            self.b_app.setEnabled(False)
            self.b_rej.setEnabled(False)
            self.b_skip.setEnabled(False)

    def _approve(self):
        if self.idx >= len(self.items):
            return
        line, n, na = self.items[self.idx]
        from utils.helpers import add_standard_to_main
        if add_standard_to_main(line):
            self._remove()
        else:
            QMessageBox.warning(self, "错误", "写入主库失败")
            return

    def _reject(self):
        if self.idx >= len(self.items):
            return
        self._remove()

    def _skip(self):
        self.idx += 1
        self._show()

    def _remove(self):
        line = self.items[self.idx][0]
        self.items.pop(self.idx)
        from utils.helpers import _remove_from_review
        _remove_from_review(line)
        self._show()


class EditorTab(QWidget):
    def __init__(self):
        super().__init__()
        self.all_items = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)

        t = QLabel("标准名称字典（主库）")
        t.setStyleSheet("font-size: 20px; font-weight: bold; color: #1E293B;")
        layout.addWidget(t)

        sl = QHBoxLayout()
        self.si = QTextEdit()
        self.si.setPlaceholderText("搜索标准号或名称...")
        self.si.setMaximumHeight(32)
        self.si.setStyleSheet("QTextEdit { border: 1px solid #CBD5E1; border-radius: 6px; padding: 4px 8px; font-size: 13px; background: white; }")
        sl.addWidget(self.si)
        b1 = QPushButton("搜索")
        b1.setStyleSheet("QPushButton { padding: 6px 20px; background: #2563EB; color: white; border: none; border-radius: 6px; font-size: 13px; } QPushButton:hover { background: #1D4ED8; }")
        b1.clicked.connect(self._search)
        sl.addWidget(b1)
        b2 = QPushButton("重置")
        b2.setStyleSheet("QPushButton { padding: 6px 20px; background: #F1F5F9; color: #475569; border: 1px solid #CBD5E1; border-radius: 6px; font-size: 13px; } QPushButton:hover { background: #E2E8F0; }")
        b2.clicked.connect(self._load)
        sl.addWidget(b2)
        layout.addLayout(sl)

        sp = QSplitter(Qt.Orientation.Horizontal)
        self.lw = QListWidget()
        self.lw.setStyleSheet("QListWidget { border: 1px solid #E2E8F0; border-radius: 6px; font-size: 13px; } QListWidget::item { padding: 8px 12px; border-bottom: 1px solid #F1F5F9; } QListWidget::item:selected { background: #EFF6FF; color: #1D4ED8; }")
        self.lw.currentItemChanged.connect(self._on_sel)
        sp.addWidget(self.lw)

        dw = QWidget()
        dl = QVBoxLayout(dw)
        dl.setContentsMargins(8, 0, 0, 0)
        self.dlbl = QLabel("选择一个条目查看详情")
        self.dlbl.setStyleSheet("font-size: 14px; color: #64748B;")
        dl.addWidget(self.dlbl)
        self.de = QTextEdit()
        self.de.setPlaceholderText("标准编号《名称》格式，如:\nGB/T 12345-2020《标准名称》")
        self.de.setStyleSheet("QTextEdit { border: 1px solid #E2E8F0; border-radius: 6px; padding: 8px; font-size: 13px; background: white; }")
        dl.addWidget(self.de, stretch=1)

        br = QHBoxLayout()
        br.addStretch()
        ba = QPushButton("➕ 添加新条目")
        ba.setStyleSheet("QPushButton { padding: 8px 20px; background: #2563EB; color: white; border: none; border-radius: 6px; font-size: 13px; font-weight: bold; } QPushButton:hover { background: #1D4ED8; }")
        ba.clicked.connect(self._add)
        br.addWidget(ba)
        bd = QPushButton("🗑 删除选中")
        bd.setStyleSheet("QPushButton { padding: 8px 20px; background: #FEE2E2; color: #DC2626; border: none; border-radius: 6px; font-size: 13px; } QPushButton:hover { background: #FECACA; }")
        bd.clicked.connect(self._del)
        br.addWidget(bd)
        dl.addLayout(br)
        sp.addWidget(dw)
        sp.setSizes([300, 400])
        layout.addWidget(sp, stretch=1)

        self.sb = QLabel("")
        self.sb.setStyleSheet("font-size: 12px; color: #64748B; padding: 4px 0;")
        layout.addWidget(self.sb)
        self._load()

    def _load(self):
        self.lw.clear()
        self.all_items = []
        self.si.setPlainText("")
        try:
            data = _db_load_main()
            for k, v in sorted(data.items()):
                self.all_items.append((v, k, v))
            for item in self.all_items:
                self.lw.addItem(item[0])
            self.sb.setText("共 {} 条记录".format(len(self.all_items)))
        except Exception as e:
            self.sb.setText("加载失败: {}".format(e))

    def _search(self):
        kw = self.si.toPlainText().strip().lower()
        self.lw.clear()
        if not kw:
            for item in self.all_items:
                self.lw.addItem(item[0])
            return
        for item in self.all_items:
            if kw in item[0].lower():
                self.lw.addItem(item[0])

    def _on_sel(self, cur, prev):
        if cur:
            self.dlbl.setText("当前条目")
            self.de.setPlainText(cur.text())

    def _add(self):
        text = self.de.toPlainText().strip()
        if not text or "《" not in text:
            QMessageBox.warning(self, "格式错误", "请输入 标准编号《名称》格式")
            return
        m = re.match(r'^(.+?)《', text)
        std_no = m.group(1).strip() if m else ""
        if not std_no:
            return
        data = _db_load_main()
        if std_no in data:
            QMessageBox.information(self, "提示", "该标准编号已存在")
            return
        try:
            conn = _get_conn()
            conn.execute("INSERT INTO main (standard_no, display_name) VALUES (?, ?)", (std_no, text))
            conn.commit()
            conn.close()
            self._load()
            self.sb.setText("✅ 已添加: {}".format(text))
        except Exception as e:
            QMessageBox.warning(self, "错误", "写入失败: {}".format(e))

    def _del(self):
        cur = self.lw.currentItem()
        if not cur:
            QMessageBox.information(self, "提示", "请先选择一个条目")
            return
        text = cur.text()
        r = QMessageBox.question(self, "确认删除", "确定要删除「{}」吗？".format(text),
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        m = re.match(r'^(.+?)《', text)
        std_no = m.group(1).strip() if m else text
        try:
            conn = _get_conn()
            conn.execute("DELETE FROM main WHERE standard_no = ?", (std_no,))
            conn.commit()
            conn.close()
            self._load()
            self.sb.setText("🗑 已删除: {}".format(text))
        except Exception as e:
            QMessageBox.warning(self, "错误", "删除失败: {}".format(e))


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    w = DictManager()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
