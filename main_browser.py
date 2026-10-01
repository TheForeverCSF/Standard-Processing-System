#!/usr/bin/env python3
# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""标准处理系统 - 桌面内嵌 Web 版（无边框窗口）
涉密模式：设置环境变量 CLASSIFIED_MODE=true 可禁用所有联网功能。
启动方式：
1. 先启动后端: python web/server.py
2. 再启动本程序: python main_browser.py
或直接双击本程序（会自动启动后端）
"""
import sys
import os
import time as _time  # 涉密模式启动提示需要
# 在导入其他模块前先检测涉密模式
_RESOURCE_BASE = os.path.dirname(os.path.abspath(__file__)) if not hasattr(sys, '_MEIPASS') else os.path.dirname(sys.executable)
# 尽早导入防火墙（涉密模式下自动安装，阻断后续所有出站连接）
if os.environ.get("CLASSIFIED_MODE", "").strip().lower() in ("1", "true", "yes") or \
   os.environ.get("SECURITY_LEVEL", "").strip().lower() in ("classified", "top_secret", "secret") or \
   os.environ.get("AIR_GAPPED", "").strip().lower() in ("1", "true", "yes"):
    _ts = _time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{_ts}] [SECURITY] ╔══════════════════════════════════════════════════╗")
    print(f"[{_ts}] [SECURITY] ║            涉 密 模 式 已 激 活 !                 ║")
    print(f"[{_ts}] [SECURITY] ╚══════════════════════════════════════════════════╝")
    # 检测管理员权限
    _is_admin = False
    try:
        import ctypes
        _is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except: pass
    # 注意：这段横幅在模块导入期执行（早于任何异常兜底），且 Windows 控制台
    # 默认 GBK 编码 —— ✓ / ⚠ 无法编码会让 print 抛异常、程序直接起不来。
    # 因此状态符号一律用 ASCII（* = 正常，x = 禁用，! = 注意）。
    _wf_str = "Windows 防火墙规则 *" if _is_admin else "Windows 防火墙 ! 需管理员权限"
    print(f"[{_ts}] [SECURITY]                                                    ")
    print(f"[{_ts}] [SECURITY]  ┌─ 功能状态 ─────────────────────────────────────┐")
    print(f"[{_ts}] [SECURITY]  │  联网查标准      x 已禁用                      │")
    print(f"[{_ts}] [SECURITY]  │  AI 增强         x 已禁用                      │")
    print(f"[{_ts}] [SECURITY]  │  外部反馈通道    x 已禁用                      │")
    print(f"[{_ts}] [SECURITY]  │  日志路径脱敏    * 已启用                      │")
    print(f"[{_ts}] [SECURITY]  │  网络出站阻断    * 双层防火墙                  │")
    print(f"[{_ts}] [SECURITY]  │                  │  ├ 应用层（socket 拦截） *   │")
    print(f"[{_ts}] [SECURITY]  │                  │  └ {_wf_str}   │")
    print(f"[{_ts}] [SECURITY]  │  临时文件清理    * 已启用                      │")
    print(f"[{_ts}] [SECURITY]  │  OCR 引擎        * 不受影响（全部本地运行）     │")
    print(f"[{_ts}] [SECURITY]  │  PDF 处理        * 不受影响（全部本地运行）     │")
    print(f"[{_ts}] [SECURITY]  │  WebEngine 渲染  * 已加入防火墙规则            │")
    print(f"[{_ts}] [SECURITY]  │  Tesseract OCR   * 已加入防火墙规则            │")
    print(f"[{_ts}] [SECURITY]  └────────────────────────────────────────────────┘")
    print(f"[{_ts}] [SECURITY]                                                    ")
import sys
import os
# PyInstaller 打包后的资源路径修正
def _resource_path(relative_path):
    """返回资源文件的正确路径（兼容 PyInstaller 打包模式）"""
    if hasattr(sys, '_MEIPASS'):
        # 打包模式：数据文件在 _MEIPASS 同级根目录
        base = os.path.dirname(sys.executable)
    else:
        # 开发模式：相对于脚本所在目录
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)
os.environ['PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT'] = '0'
os.environ['FLAGS_use_mkldnn'] = '0'
os.environ['FLAGS_enable_pir_api'] = '0'
os.environ['PADDLE_DISABLE_ONEDNN'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['PADDLE_MKLDNN_THREAD_NUM'] = '1'
# ---- Qt 启动加速：阻止加载不必要的插件 ----
os.environ['QT_LOGGING_RULES'] = '*.debug=false;qt.qml.*=false'
os.environ['QML_DISABLE_DISK_CACHE'] = '1'
# 启用 Qt DPI 自适应缩放（必须开启，否则在系统 DPI>100% 时窗口与 CSS 像素不一致导致布局溢出）
os.environ['QT_ENABLE_HIGHDPI_SCALING'] = '1'
os.environ['QT_SCALE_FACTOR_ROUNDING_POLICY'] = 'Round'
os.environ['DONT_LOAD_UNNEEDED_PLUGINS'] = '1'
# 限制 Chromium / WebEngine 预取资源，加速首帧渲染
os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = '--disable-features=OptimizationGuideModelDownloading,OptimizationHintsFetching,OptimizationTargetPrediction --disable-background-networking --disable-component-update --disable-sync --disable-extensions'

import threading
import time
import webbrowser
import json
import argparse

from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEngineProfile
from PyQt6.QtGui import QPalette, QColor


def log_print(msg, tag="INFO"):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] [{tag}] {msg}")


def _user_data_dir() -> str:
    """可写用户数据目录（%LOCALAPPDATA%\\标准处理系统），首次运行自动迁移旧数据。

    以前 WebEngine 的 profile 目录（.webdata）放在 exe 同级目录，安装到
    %ProgramFiles% 且普通用户启动时写不进去，Chromium 会卡在初始化 ——
    表现为"进程起来了但主窗口和加载界面都不出现"。
    """
    try:
        from utils.helpers import user_data_dir
        return user_data_dir()
    except Exception:
        return _resource_path("")


_api_server = None
_api_server_error = None   # 后端启动失败原因（供启动兜底提示展示）


def start_api_server():
    global _api_server, _api_server_error
    # 延迟导入 web.server，避免阻塞主线程窗口创建（打包后此导入较慢）
    try:
        from web.server import start_server
        server = start_server(port=8765)
        if server is None:
            # 已有实例在跑：start_server 会返回 None，直接复用它的后端
            log_print("检测到已有实例，复用其本地服务", "INIT")
            return
        _api_server = server
        server.serve_forever()
    except Exception as e:
        _api_server_error = str(e)
        log_print(f"本地服务启动失败: {e}", "ERROR")


def is_api_server_ready(port: int = 8765, timeout: float = 0.3) -> bool:
    """非阻塞地探测本地后端是否已就绪。

    原先窗口一创建就 setUrl，而打包后 web.server 要先导入 PyMuPDF / PaddleOCR 等
    重量级依赖，启动往往要好几秒 —— 抢跑的结果就是 QtWebEngine 把
    「127.0.0.1 拒绝了我们的连接请求 / 拒绝访问」的错误页直接呈现给用户，
    看起来就像程序坏了。改为在 GUI 线程里轮询探测，就绪后才真正跳转。
    """
    import socket as _socket
    try:
        with _socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


_STARTUP_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>标准处理系统</title></head>
<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
             background:#F8FAFC;color:#334155;font-family:'Microsoft YaHei UI',sans-serif;">
  <div style="text-align:center;">
    <div style="font-size:20px;font-weight:700;color:#1E293B;">标准处理系统</div>
    <div style="margin-top:14px;font-size:14px;color:#64748B;">正在启动本地服务，请稍候…</div>
    <div style="margin-top:10px;font-size:12px;color:#94A3B8;">
      首次启动需要加载识别引擎，可能需要十几秒
    </div>
  </div>
</body></html>"""


def check_ocr_engine():
    try:
        from core.ocr_handler import OcrHandler
        handler = OcrHandler(on_log=log_print)
        available = handler.is_available()
        if available:
            log_print(f"OCR 引擎检测通过: {handler._engine_type}", "INIT")
        else:
            log_print("OCR 引擎未安装或不可用", "WARN")
            if handler._init_error:
                log_print(f"  原因: {handler._init_error}", "WARN")
            log_print("  扫描页（图片型PDF）将无法识别文字，纯文字PDF不受影响", "WARN")
    except Exception as e:
        log_print(f"OCR 引擎检测异常: {e}", "WARN")


def check_pyqt_webengine():
    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView as _
        return True
    except ImportError:
        return False


class TitleBar(QtWidgets.QWidget):
    """自定义无边框标题栏，支持深色/浅色模式切换"""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.setFixedHeight(38)
        self._current_mode = "light"
        self._build_ui()

    def _colors(self):
        """返回当前模式下的颜色配置"""
        if self._current_mode == "dark":
            return {
                "bg": "#0F172A", "border": "#334155",
                "title": "#F1F5F9",
                "btn": "#94A3B8", "btn_hover_bg": "rgba(255,255,255,0.1)",
                "btn_hover_fg": "#F1F5F9",
            }
        return {
            "bg": "#FFFFFF", "border": "#E2E8F0",
            "title": "#1E293B",
            "btn": "#94A3B8", "btn_hover_bg": "rgba(0,0,0,0.08)",
            "btn_hover_fg": "#1E293B",
        }

    def _build_ui(self):
        c = self._colors()
        # 直接设置 widget 自身的背景色（最高优先级，绕过所有 QSS 继承问题）
        self.setObjectName("CustomTitleBar")
        self.setAutoFillBackground(True)
        # 1) QPalette 设背景
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(c['bg']))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(c['title']))
        self.setPalette(palette)
        # 2) QSS 用 !important 强制覆盖
        self.setStyleSheet(f"""
            #CustomTitleBar {{
                background-color: {c['bg']} !important;
                border-bottom: 1px solid {c['border']};
            }}
            #CustomTitleBar QLabel {{
                color: {c['title']} !important;
                background-color: transparent !important;
            }}
            #CustomTitleBar QPushButton {{
                color: {c['btn']} !important;
                background-color: transparent !important;
                border: none !important;
            }}
        """)
        # 3) 立即重绘
        self.repaint()

        # 复用已有 layout（防止 QLayout warning）
        layout = self.layout()
        if layout is None:
            layout = QtWidgets.QHBoxLayout(self)
        else:
            # 清空残留控件（set_mode 已清，但防御性保留）
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()
        layout.setContentsMargins(14, 0, 0, 0)
        layout.setSpacing(0)

        icon_label = QtWidgets.QLabel()
        icon_path = _resource_path("logo.ico")
        if os.path.exists(icon_path):
            pixmap = QtGui.QPixmap(icon_path).scaled(
                20, 20, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
            icon_label.setPixmap(pixmap)
        icon_label.setFixedSize(24, 24)
        layout.addWidget(icon_label)
        layout.addSpacing(10)

        title = QtWidgets.QLabel("标准处理系统")
        # 关键修复：用 inline style 强制设置颜色，避免被 QSS 继承或选择器优先级搞混
        title.setStyleSheet(
            f"color: {c['title']} !important; "
            f"background-color: transparent !important; "
            f"font-size: 14px !important; "
            f"font-weight: 700 !important;"
        )
        layout.addWidget(title)

        # ── 教程入口：标题右侧（点击后驱动网页里的教程模式）──
        tut_btn = QtWidgets.QPushButton("🎓 教程")
        tut_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        tut_btn.setFixedHeight(24)
        tut_btn.setToolTip("使用教程（每次更新、重新安装或初始化后会自动播放）")
        tut_btn.setStyleSheet(
            f"QPushButton{{color:{c['btn']} !important; background:transparent !important;"
            f" border:1px solid {c['border']} !important; border-radius:6px !important;"
            f" padding:0 10px !important; font-size:12px !important; margin-left:12px !important;}}"
            f"QPushButton:hover{{background:{c['btn_hover_bg']} !important;"
            f" color:{c['btn_hover_fg']} !important; border-color:{c['btn_hover_fg']} !important;}}"
        )
        tut_btn.clicked.connect(self.start_tutorial)
        layout.addWidget(tut_btn)

        layout.addStretch()

        self._add_button(layout, "\u2014", self.parent.window_minimize, c)
        self._add_button(layout, "\u25a1", self.parent.window_maximize_restore, c)
        self._add_button(layout, "\u2715", self.parent.window_close, c,
                         hover_bg="#EF4444", hover_fg="white")

    def start_tutorial(self):
        """在网页里播放使用教程。

        标题栏是 Qt 控件、位于网页之外，教程的聚光灯高亮不到它，
        因此只能由这里作为入口去驱动网页里的 startTutorial()。
        """
        try:
            view = getattr(self.parent, "webview", None)
            if view is None:
                log_print("未使用内嵌浏览器，无法播放教程", "WARN")
                return
            view.page().runJavaScript(
                "if (typeof startTutorial === 'function') { startTutorial(); }")
        except Exception as e:
            log_print(f"启动教程失败: {e}", "WARN")

    def _add_button(self, layout, text, callback, c, hover_bg=None, hover_fg=None):
        btn = QtWidgets.QPushButton(text)
        btn.setFixedSize(44, 38)
        btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(callback)
        normal = f"color:{c['btn']} !important;background:transparent !important;border:none !important;font-size:14px !important;"
        hb = hover_bg or c['btn_hover_bg']
        hf = hover_fg or c['btn_hover_fg']
        hover = f"background:{hb} !important;color:{hf} !important;"
        btn.setStyleSheet(f"QPushButton{{{normal}}}QPushButton:hover{{{hover}}}")
        layout.addWidget(btn)

    def set_mode(self, mode):
        """由外部调用以切换深色/浅色模式"""
        if mode == self._current_mode:
            return
        self._current_mode = mode
        # 清空旧控件
        old = self.layout()
        if old:
            while old.count():
                item = old.takeAt(0)
                w = item.widget()
                if w:
                    w.deleteLater()
        self._build_ui()
        # 强制立即重绘（不用 update() 因为 update 是异步的）
        self.repaint()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            if self.parent._is_maximized:
                # 最大化状态下：不还原，只记录按下时的全局位置和点击比例
                # 等 mouseMoveEvent 真正拖动时才还原窗口
                self._press_global_pos = event.globalPosition()
                self._press_ratio = event.position().x() / self.width()
            else:
                self.parent._drag_offset = event.globalPosition().toPoint() - self.parent.pos()
            self.parent._dragging = True
            event.accept()

    def mouseDoubleClickEvent(self, event):
        """双击标题栏切换最大化/还原"""
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.parent.window_maximize_restore()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.parent._dragging:
            if self.parent._is_maximized:
                # 从最大化状态拖动：先还原，再计算正确的 offset
                self.parent.window_maximize_restore()
                # 根据之前按下的位置计算还原后的窗口位置
                restored_w = self.parent.geometry().width()
                new_x = self._press_global_pos.x() - self._press_ratio * restored_w
                self.parent.move(int(new_x), int(self._press_global_pos.y()) - 10)
                self.parent._drag_offset = QtCore.QPoint(
                    int(self._press_global_pos.x() - new_x),
                    int(self._press_global_pos.y() - self.parent.y()))
            self.parent.move(event.globalPosition().toPoint() - self.parent._drag_offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.parent._dragging = False
            event.accept()


class BrowserWindow(QtWidgets.QMainWindow):
    """无边框主窗口"""

    def __init__(self):
        super().__init__()
        self._dragging = False
        self._drag_offset = QtCore.QPoint(0, 0)
        self._is_maximized = False
        self._saved_geometry = None

        self.setWindowFlags(QtCore.Qt.WindowType.FramelessWindowHint)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumSize(1000, 650)

        # 根据屏幕尺寸决定窗口大小（占比 92%，最大不超过 1920x1080）
        scr = self.screen()
        if scr:
            sg = scr.availableGeometry()
            _win_w = min(int(sg.width() * 0.92), 1920)
            _win_h = min(int(sg.height() * 0.92), 1080)
        else:
            _win_w, _win_h = 1600, 1000
        self.resize(_win_w, _win_h)

        if scr:
            self.move(sg.x() + (sg.width() - _win_w) // 2, sg.y() + (sg.height() - _win_h) // 2)

        central = QtWidgets.QWidget()
        central.setStyleSheet("background: #F8FAFC;")
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.titlebar = TitleBar(self)
        outer.addWidget(self.titlebar)

        if check_pyqt_webengine():
            log_print("检测到 PyQt6-WebEngine，使用内嵌浏览器", "INIT")
            # 设置 localStorage 持久化存储路径（避免关闭窗口后设置丢失）
            profile = QWebEngineProfile.defaultProfile()
            storage_path = os.path.join(_user_data_dir(), ".webdata")
            profile.setPersistentStoragePath(storage_path)
            profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
            self.webview = QWebEngineView()
            settings = self.webview.settings()
            if settings:
                settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
                settings.setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, True)
            # 先显示"正在启动"占位页，等后端就绪再跳转；加载失败会自动重试，
            # 避免用户看到"127.0.0.1 拒绝访问"的浏览器错误页
            self._load_retry = 0
            self._err_shown = False
            self.webview.setHtml(_STARTUP_HTML)
            self.webview.loadFinished.connect(self._on_web_load_finished)
            # 在 GUI 线程里用定时器轮询后端就绪状态再跳转。
            # 不能用"后台线程 + QTimer.singleShot"：子线程没有事件循环，
            # 那个回调永远不会触发，页面会一直停在占位页（已实测）。
            self._boot_waited = 0.0
            self._boot_timer = QtCore.QTimer(self)
            self._boot_timer.setInterval(300)
            self._boot_timer.timeout.connect(self._on_boot_tick)
            self._boot_timer.start()
            outer.addWidget(self.webview, stretch=1)
        else:
            log_print("未检测到 PyQt6-WebEngine，将使用系统浏览器", "WARN")
            QtWidgets.QMessageBox.information(
                self, "提示",
                "未安装 PyQt6-WebEngine，将使用系统浏览器打开。\n"
                "如需内嵌浏览器，请运行：pip install PyQt6-WebEngine")
            webbrowser.open("http://127.0.0.1:8765/")
            label = QtWidgets.QLabel("已在外部浏览器中打开应用，请切换到浏览器窗口。")
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet("color:#64748B;font-size:14px;padding:40px;")
            outer.addWidget(label, stretch=1)

        self.setCentralWidget(central)
        log_print("无边框窗口已创建", "INIT")

    # ── 启动期的加载编排 ──
    def _start_boot_poll(self):
        """重新开始"等待后端就绪"的轮询（首次启动与用户点「重试」时共用）"""
        self._boot_waited = 0.0
        self._err_shown = False
        if getattr(self, "_boot_timer", None) is None:
            self._boot_timer = QtCore.QTimer(self)
            self._boot_timer.setInterval(300)
            self._boot_timer.timeout.connect(self._on_boot_tick)
        self._boot_timer.start()

    def _on_boot_tick(self, port: int = 8765):
        """GUI 线程定时器：后端就绪就跳转，超时或启动失败则给出兜底提示"""
        if _api_server_error:
            self._boot_timer.stop()
            self._show_startup_error()
            return
        if is_api_server_ready(port):
            self._boot_timer.stop()
            log_print("本地服务已就绪，加载界面", "INIT")
            self.webview.setUrl(QtCore.QUrl(f"http://127.0.0.1:{port}/"))
            return
        self._boot_waited += self._boot_timer.interval() / 1000.0
        if self._boot_waited >= 60.0:
            self._boot_timer.stop()
            self._show_startup_error()

    def _on_web_load_finished(self, ok: bool):
        """网页加载失败时自动重试若干次，仍失败才给出可操作的提示"""
        if ok:
            self._load_retry = 0
            return
        self._load_retry = getattr(self, "_load_retry", 0) + 1
        if self._load_retry <= 5:
            log_print(f"页面加载失败，正在重试（第 {self._load_retry}/5 次）", "WARN")
            QtCore.QTimer.singleShot(
                1500, lambda: self.webview.setUrl(self.webview.url()))
        else:
            self._show_startup_error()

    def _show_startup_error(self):
        """本地服务/界面加载不成功时的兜底提示。

        用 Qt 原生对话框而不是网页错误页：这种情况下网页本身就加载不出来，
        只让用户对着「127.0.0.1 拒绝访问」发呆是最糟的体验。
        """
        if getattr(self, "_err_shown", False):
            return
        self._err_shown = True

        reason = _api_server_error or "本地服务未能在预期时间内启动"
        residual_tip = ""
        try:
            from core.security_config import detect_residual_rules, _check_admin
            res = detect_residual_rules()
            if res.get("present") and not _check_admin():
                residual_tip = (
                    f"\n\n⚠ 检测到 {res['count']} 条上次遗留的 Windows 防火墙规则，"
                    f"它可能挡住了本程序访问自己的本地界面，而当前没有管理员权限无法清理。")
        except Exception:
            pass

        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Critical)
        box.setWindowTitle("标准处理系统 - 界面未能加载")
        box.setText("无法连接本地服务（127.0.0.1:8765），界面没有加载出来。")
        box.setInformativeText(
            f"原因：{reason}{residual_tip}\n\n"
            f"可以依次尝试：\n"
            f"· 点「重试」再等一次（首次启动要加载识别引擎，可能较慢）；\n"
            f"· 点「以管理员身份重启」清理防火墙残留规则；\n"
            f"· 关闭其它本程序窗口后重新打开。")
        btn_retry = box.addButton("重试", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        btn_elev = box.addButton("以管理员身份重启", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        box.addButton("退出", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()

        if clicked is btn_retry:
            self._load_retry = 0
            self.webview.setHtml(_STARTUP_HTML)
            self._start_boot_poll()
        elif clicked is btn_elev:
            try:
                from core.security_config import relaunch_as_admin_cleanup
                relaunch_as_admin_cleanup()
                import os as _os
                _os._exit(0)
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "提权失败", f"无法以管理员身份重启：{e}")
        else:
            self.window_close()

    def window_minimize(self):
        self.showMinimized()

    def window_maximize_restore(self):
        if self._is_maximized:
            if self._saved_geometry:
                self.setGeometry(self._saved_geometry)
            else:
                scr = self.screen()
                if scr:
                    sg = scr.availableGeometry()
                    self.resize(min(int(sg.width() * 0.92), 1920), min(int(sg.height() * 0.92), 1080))
                else:
                    self.resize(1200, 800)
            self._is_maximized = False
        else:
            self._saved_geometry = self.geometry()
            scr = self.screen()
            if scr:
                sg = scr.availableGeometry()
                self.setGeometry(sg)
            else:
                self.showMaximized()
            self._is_maximized = True

    def window_close(self):
        """关闭窗口 - 清理涉密模式防火墙后强杀进程"""
        # 防止重复进入
        if getattr(self, '_closing', False):
            return
        self._closing = True

        # 只有"本进程真正持有后端"时才做清理。
        # 若本窗口是复用别人后端的第二个实例（_api_server is None），
        # 清理临时目录会删掉对方正在处理的文件副本，
        # 卸载防火墙会静默关掉对方正在生效的涉密防护。
        if _api_server is not None:
            self._shutdown_server()

            # 退出前清理涉密模式的 Windows 防火墙规则，避免残留下次启动阻止联网
            try:
                from core.security_config import remove_windows_firewall_rules
                remove_windows_firewall_rules()
            except Exception:
                pass
        else:
            log_print("本窗口复用了已有实例的后端，退出时不做清理", "INIT")

        # 跳过 Qt 的 close()/quit()——它们可能因等待 WebEngine 子进程而阻塞。
        # 直接 os._exit(0) 强制结束，操作系统会清理所有子进程和资源。
        import os as _os
        _os._exit(0)

    def _shutdown_server(self):
        """关闭后端服务器并清理临时文件"""
        from web.server import _cleanup_temp_dirs
        global _api_server
        # 涉密模式是运行时状态，关闭程序时自动退出（用户要求）。
        # 卸载防火墙并重置 .app_settings.json 中的 classifiedMode，
        # 防止残留规则/状态在下次启动时误判导致联网被阻断。
        try:
            from core.security_config import set_classified_mode, remove_firewall, is_classified
            if is_classified():
                remove_firewall()
                set_classified_mode(False)
                log_print("涉密模式已随程序退出自动关闭", "SECURITY")
                # 同步重置持久化的涉密标记
                try:
                    # 2-9：必须与后端 server 的**写入位置**一致
                    # （utils.helpers.user_data_dir() = %LOCALAPPDATA%\标准处理系统）。
                    # 原来按 exe 目录/项目根目录算，而安装到 %ProgramFiles% 时那里
                    # 普通用户不可写 → 这段"重置涉密标记"静默失效（还被 except: pass
                    # 吞掉，用户完全看不到）。属于"写一个目录、读另一个目录"家族。
                    from utils.helpers import user_data_dir as _udd
                    _sjson = os.path.join(_udd(), ".app_settings.json")
                    if os.path.exists(_sjson):
                        with open(_sjson, "r", encoding="utf-8") as _sf:
                            _sd = json.load(_sf)
                        if _sd.get("classifiedMode"):
                            _sd["classifiedMode"] = False
                            with open(_sjson, "w", encoding="utf-8") as _wf:
                                json.dump(_sd, _wf, ensure_ascii=False, indent=2)
                            log_print("已重置 .app_settings.json 的涉密标记", "SECURITY")
                except Exception:
                    pass
        except Exception:
            pass
        if _api_server:
            try:
                log_print("正在关闭后端服务器...", "INIT")
                # 先停止接收新请求
                _api_server.shutdown()
                # 等待已接收的请求处理完毕
                import time as _t
                _t.sleep(0.5)
                _api_server.server_close()
                log_print("后端服务器已关闭", "INIT")
            except Exception as e:
                log_print(f"关闭服务器异常: {e}", "WARN")
        # 清理临时缓存
        try:
            _cleanup_temp_dirs()
        except Exception:
            pass


def handle_cli_args():
    """解析命令行参数（右键菜单传入的文件/文件夹路径），
    将路径写入 .pending_import.json 供服务器读取。"""
    # 先清理上次残留的挂起导入文件（如果进程异常退出可能留下）
    stale_pending = os.path.join(_resource_path(''), '.pending_import.json')
    if os.path.exists(stale_pending):
        try:
            os.remove(stale_pending)
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="标准处理系统 - 文档标准提取工具",
        add_help=False,  # 避免与 Qt 参数冲突
    )
    parser.add_argument('paths', nargs='*', help='文件或文件夹路径')
    # 解析时忽略未知参数（Qt 可能传入自己的参数如 --style）
    args, _ = parser.parse_known_args()

    if not args.paths:
        return

    input_paths = []
    for p in args.paths:
        ap = os.path.abspath(p)
        if os.path.isfile(ap) and ap.lower().endswith(('.pdf', '.docx', '.doc')):
            input_paths.append(ap)
        elif os.path.isdir(ap):
            input_paths.append(ap)

    if not input_paths:
        return

    # 分类文件和文件夹
    files = [p for p in input_paths if os.path.isfile(p)]
    folders = [p for p in input_paths if os.path.isdir(p)]

    pending = {'files': files, 'folders': folders}
    pending_path = os.path.join(_resource_path(''), '.pending_import.json')
    try:
        with open(pending_path, 'w', encoding='utf-8') as f:
            json.dump(pending, f, ensure_ascii=False)
        log_print(f"从文件资源管理器接收：{len(files)} 个文件, {len(folders)} 个文件夹", "INIT")
    except Exception as e:
        log_print(f"写入挂起导入文件失败: {e}", "WARN")


class SplashScreen(QtWidgets.QWidget):
    """启动闪屏：双击程序后立即显示，让用户感知程序在运行"""

    def __init__(self):
        super().__init__(None, QtCore.Qt.WindowType.FramelessWindowHint |
                         QtCore.Qt.WindowType.WindowStaysOnTopHint)
        self._steps = ["正在加载核心模块...", "正在初始化识别引擎...", "正在启动内置浏览器...", "即将完成..."]
        self._step_idx = 0
        self._timer = None

        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(380, 240)
        self._init_ui()

        # 居中显示
        scr = self.screen()
        if scr:
            sg = scr.availableGeometry()
            self.move(sg.center().x() - 190, sg.center().y() - 120)

        # 进度轮播定时器
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.nextStep)
        self._timer.start(700)

    def nextStep(self):
        """推进到下一个加载步骤"""
        self._step_idx += 1
        if self._step_idx < len(self._steps):
            self._status.setText(self._steps[self._step_idx])
        self._bar.setValue(min(25 + self._step_idx * 25, 95))

    def _init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 圆角背景
        bg = QtWidgets.QWidget(self)
        bg.setObjectName("splashBg")
        bg.setStyleSheet("""
            #splashBg { background: #FFFFFF; border-radius: 16px;
                        border: 1px solid #E2E8F0; }
        """)
        bg_layout = QtWidgets.QVBoxLayout(bg)
        bg_layout.setContentsMargins(24, 24, 24, 24)
        bg_layout.setSpacing(12)
        bg_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        # Logo
        logo_path = _resource_path("logo_标准处理系统.png")
        logo = QtWidgets.QLabel()
        logo.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        if os.path.exists(logo_path):
            try:
                pixmap = QtGui.QPixmap(logo_path)
                if not pixmap.isNull():
                    scaled = pixmap.scaled(96, 96, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                           QtCore.Qt.TransformationMode.SmoothTransformation)
                    logo.setPixmap(scaled)
            except Exception:
                pass
        bg_layout.addWidget(logo)

        # 标题
        title = QtWidgets.QLabel("标准处理系统")
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #0F172A;")
        bg_layout.addWidget(title)

        # 步骤文字
        self._status = QtWidgets.QLabel(self._steps[0])
        self._status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet("font-size: 13px; color: #64748B;")
        bg_layout.addWidget(self._status)

        # 进度条
        self._bar = QtWidgets.QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(15)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        self._bar.setStyleSheet("""
            QProgressBar { background: #E2E8F0; border: none; border-radius: 3px; }
            QProgressBar::chunk { background: #2563EB; border-radius: 3px; }
        """)
        bg_layout.addWidget(self._bar)

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.addWidget(bg)

    def complete(self):
        """标记加载完成，更新为 100% 后短暂停留再关闭"""
        self._bar.setValue(100)
        self._status.setText("加载完成")
        QtCore.QTimer.singleShot(350, self.close)


def main():
    # 立即输出启动信息，让用户感知程序在运行
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INIT] 标准处理系统正在启动...", flush=True)
    log_print("=" * 60, "INIT")

    # 处理命令行参数（右键菜单传入的路径）
    handle_cli_args()

    # 后台启动 API 服务器（web.server 延迟导入，不阻塞窗口创建）
    server_thread = threading.Thread(target=start_api_server, daemon=True)
    server_thread.start()
    log_print("API 服务器线程已启动", "INIT")

    # OCR 引擎检测放到后台线程，不阻塞窗口创建（打包后此检测可能较慢）
    threading.Thread(target=check_ocr_engine, daemon=True).start()

    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QtGui.QFont("Microsoft YaHei UI", 11))

    # 管理员清理模式：只清残留防火墙规则后退出。
    # 由启动失败对话框的「以管理员身份重启」拉起（relaunch_as_admin_cleanup）。
    if "--clean-firewall" in sys.argv:
        log_print("以管理员身份清理残留防火墙规则...", "SECURITY")
        try:
            from core.security_config import cleanup_firewall_and_report
            info = cleanup_firewall_and_report()
            QtWidgets.QMessageBox.information(
                None, "标准处理系统",
                f"防火墙规则清理完成。\n\n"
                f"清理前：{info['before']} 条\n清理后：{info['after']} 条\n"
                f"管理员权限：{'是' if info['admin'] else '否'}\n\n"
                f"如果界面仍然打不开，请手动在「Windows 防火墙 → 高级设置 → 出站规则」中"
                f"删除名称以「标准处理系统_涉密模式」开头的规则。")
        except Exception as e:
            QtWidgets.QMessageBox.warning(None, "标准处理系统", f"清理失败：{e}")
        return

    # 立即显示启动闪屏，让用户感知程序在运行
    splash = SplashScreen()
    splash.show()
    app.processEvents()

    # 预初始化 QtWebEngine 默认 profile，让 Chromium 在后台启动，
    # 避免 BrowserWindow 创建时才初始化（能缩短约 1 秒）。
    try:
        from PyQt6.QtWebEngineCore import QWebEngineProfile
        _profile = QWebEngineProfile.defaultProfile()
        if _profile is not None:
            _storage = os.path.join(_user_data_dir(), ".webdata")
            _profile.setPersistentStoragePath(_storage)
            _profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
            )
    except Exception:
        pass
    splash.nextStep()
    app.processEvents()

    window = BrowserWindow()
    # 主窗口创建完成，关闭闪屏并显示主窗口
    splash.complete()
    window.show()
    app.processEvents()
    log_print("进入 Qt 主循环", "INIT")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
