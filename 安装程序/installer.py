#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""标准处理系统 - 现代化安装程序

界面：pywebview（系统 WebView2 内核）+ 内嵌 HTML，无边框窗口 720x600（逻辑像素）。
流程：Logo 视频开场 →「安装选项」→「安装进度」→「完成」，共 4 屏；
      说明文档与高级设置收进弹层 / 折叠区，不再逐页向导。
兜底：运行环境缺少 WebView2 / pywebview 时，自动退化为基础 Tk 界面（按默认设置安装）。

用法: python installer.py [安装包路径]
      安装包路径默认为同目录下的 Standard Processing System*.zip

调试: 设置环境变量 CSF_INSTALLER_SKIP_ELEVATE=1 可跳过自动提权（界面走查用）。
"""
import ctypes
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────
APP_NAME = "标准处理系统"
# 版本号**唯一来源**：项目根的 version.txt（与 标准处理系统.spec 的 _read_version 一致）。
# 原先这里是硬编码版本号，与 version.txt / spec 形成多个源头 —— 极易漂移
# （本轮审计发现交付件与源码版本不一致，正是这类多源头导致的）。
# installer.py 位于 安装程序/ 子目录，所以先找上一级。
_VERSION_FALLBACK = "2.0.0"      # 仅在 version.txt 缺失时兜底，**不是**第二来源


def _version_candidates(default: str = _VERSION_FALLBACK):
    """version.txt 的候选路径（按优先级）。

    打包后（PyInstaller）版本号靠 installer.spec 把 version.txt 打进包里，
    因此必须按 frozen 模式去找：exe 同级 → _MEIPASS（单文件包解包目录）；
    源码模式则是 安装程序/../version.txt（项目根）。
    """
    cands = []
    if getattr(sys, "frozen", False):
        try:
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            cands.append(os.path.join(exe_dir, "version.txt"))
        except Exception:
            pass
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            cands.append(os.path.join(mei, "version.txt"))
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        cands.append(os.path.join(base, os.pardir, "version.txt"))
        cands.append(os.path.join(base, "version.txt"))
    except Exception:
        pass
    return cands


def _read_version(default: str = _VERSION_FALLBACK) -> str:
    try:
        for cand in _version_candidates(default):
            if os.path.isfile(cand):
                with open(cand, "r", encoding="utf-8-sig") as f:
                    v = f.read().strip()
                if v:
                    return v
    except Exception:
        pass
    # 读不到就如实说出来，不要静默用一个"看着正常"的版本号
    print(f"[警告] 未能读取 version.txt，版本号回退为 {default}"
          f"（打包时请把 version.txt 一起分发：见 installer.spec 的 datas）")
    return default


APP_VERSION = _read_version()
APP_EXE = "标准处理系统.exe"
APP_PUBLISHER = "The Forever CSF"
DEFAULT_INSTALL_DIR = os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), APP_NAME)
USER_INSTALL_DIR = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), APP_NAME)
START_MENU_DIR = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", APP_NAME)

# ── 安装目录安全校验（H2）───────────────────────────────
# 卸载脚本会 `rmdir /s /q` 整个安装目录。若允许装到 D:\ 这类自定义根级目录，
# 卸载就等于把该目录下的所有东西一起删掉（审计 H2：装到 D:\ 后卸载连内容一起删）。
_FORBIDDEN_DIRS_CACHE = None


def _forbidden_dirs():
    """不允许"直接作为安装目录"的关键目录（它们**的子目录**是允许的）"""
    global _FORBIDDEN_DIRS_CACHE
    if _FORBIDDEN_DIRS_CACHE is not None:
        return _FORBIDDEN_DIRS_CACHE
    up = os.environ.get("USERPROFILE", "")
    cands = [
        os.environ.get("SystemRoot", r"C:\Windows"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramData", r"C:\ProgramData"),
        os.environ.get("APPDATA", ""),
        os.environ.get("LOCALAPPDATA", ""),
        up,
    ]
    if up:
        for sub in ("Desktop", "Documents", "Downloads", "桌面", "文档", "下载", "桌面文件夹"):
            cands.append(os.path.join(up, sub))
    _FORBIDDEN_DIRS_CACHE = {os.path.normcase(os.path.abspath(c)) for c in cands if c}
    return _FORBIDDEN_DIRS_CACHE


def validate_install_dir(path):
    """校验安装目录是否安全，返回 (ok, 原因)；原因可直接展示给用户。

    三条硬规则：
      1) 拒绝盘符根（D:\\）、UNC 共享根、以及系统/用户资料目录**本身**
         （如 C:\\Windows、C:\\Users\\me、桌面、文档）——这些位置一旦被
         `rmdir /s /q` 就是灾难；
      2) 路径不得含 cmd/PowerShell 元字符（dest 会被拼进卸载用 .bat，
         含 `"` 甚至能破坏批处理结构）；
      3) 已存在且非空的目录，必须"看起来就是本程序的安装目录"（含 APP_EXE），
         否则拒绝 —— 这条最实用，直接堵住"装到有其它文件的目录、卸载时一锅端"。
    """
    p = (path or "").strip().strip('"')
    if not p:
        return False, "请先选择安装位置。"
    if any(ch in p for ch in '"%&|<>^'):
        return False, '安装路径不能包含 " % & | < > ^ 这些字符（会导致卸载脚本异常）。'
    drive, tail = os.path.splitdrive(p)
    if not drive:
        return False, "请选择完整的绝对路径（例如 D:\\标准处理系统）。"
    ap = os.path.abspath(p)
    if not tail.strip("\\/"):
        return False, ("不能把程序直接装在盘符根目录（如 D:\\）——"
                       "卸载时会清空整个盘的内容。请新建一个子目录，例如 D:\\标准处理系统。")
    if drive.startswith("\\\\") and tail.strip("\\/").count("\\") <= 1:
        return False, "不能把程序直接装在网络共享的根目录。"
    nc = os.path.normcase(ap)
    sysroot = os.path.normcase(os.path.abspath(os.environ.get("SystemRoot", r"C:\Windows")))
    if nc == sysroot or nc.startswith(sysroot + os.sep):
        return False, "不能安装在 Windows 系统目录下。"
    if nc in _forbidden_dirs():
        return False, (f"不能直接安装在 {ap}（这是系统或用户资料目录本身），"
                       f"请改到它的子目录下。")
    if os.path.isdir(ap):
        try:
            entries = os.listdir(ap)
        except Exception:
            entries = []
        if entries and not os.path.isfile(os.path.join(ap, APP_EXE)):
            return False, ("该目录已存在且有其它文件，不是本程序的安装目录。"
                           "为避免日后卸载时误删你的文件，请选择一个空目录或新建子目录。")
    return True, ""

ZIP_NAME = "Standard Processing System.zip"
UI_HTML = "installer_ui.html"
INTRO_VIDEO = "The Forever CSF.mp4"
LOGO_ICO = "logo.ico"

# ── 协议资源（新版优先，自动回退旧版）─────────────────────
# 新版：《标准处理系统 许可、隐私与免责说明》（AGPL-3.0，取代旧《用户协议》）
# 旧版：《标准处理系统 用户协议》（已废止，内容与实现存在多处不符，不应继续分发）
# 把新版转成图片放进 AGREEMENT_DIR_CANDIDATES[0] 目录、把 PDF 以
# AGREEMENT_PDF_CANDIDATES[0] 为名放进项目根目录，即自动生效。
AGREEMENT_DIR_CANDIDATES = ("标准处理系统 许可隐私与免责说明", "标准处理系统 用户协议")
AGREEMENT_PDF_CANDIDATES = ("标准处理系统 许可、隐私与免责说明.pdf",
                            "标准处理系统 用户协议.pdf")
AGREEMENT_DIR = AGREEMENT_DIR_CANDIDATES[0]   # 保留既有引用，默认取新版目录名

DB_NAME = "标准名称字典.db"
SETTINGS_NAME = ".app_settings.json"
UNINSTALL_BAT = "卸载标准处理系统.bat"

# ── 用户数据位置（必须与主程序一致）─────────────────────
# 主程序把设置、标准名称字典、WebEngine 缓存放在 %LOCALAPPDATA%\标准处理系统，
# 因为安装目录可能在 %ProgramFiles% 下不可写。安装程序必须按同一规则读写：
#   · 覆盖安装前把用户数据备份到「我的文档」，避免用户积累的词典被不可逆删除；
#   · 装完把设置/词典同时落到数据目录与安装目录（兼容旧版本程序）；
#   · 旧用户数据在安装目录时，顺带迁移到数据目录，保证新版本能读到。
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)
USER_DATA_FILES = (
    SETTINGS_NAME,
    DB_NAME, "标准名称字典.db-wal", "标准名称字典.db-shm",
    "builtin_prefixes.json", ".tutorial_state.json", ".pending_import.json",
    "标准名称字典.txt", "标准名称字典_待审核.txt",
)
BACKUP_DIR_NAME = f"{APP_NAME}备份"


def documents_dir():
    """「我的文档」实际路径（跟随用户的文档重定向 / OneDrive）"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            val, _ = winreg.QueryValueEx(k, "Personal")
        path = os.path.expandvars(str(val))
        if path:
            return path
    except Exception:
        pass
    return os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Documents")

WINDOW_WIDTH, WINDOW_HEIGHT = 720, 600

ELEVATED = '--elevated' in sys.argv
OCR_TRUST_LOG = "ocr_trust.log"


def _record_trust_event(path, why, digest):
    """"用户确认信任未通过校验的安装包"必须留痕。

    安装程序是 console=False 的 GUI 程序，print 出来的日志在用户机器上等于丢掉，
    所以这条**安全例外**要单独落到数据目录的 ocr_trust.log：谁、什么时候、
    信任了哪个文件、它的 sha256 是什么。事后排查"这台机器为什么被提权执行了未知程序"
    时，这是唯一的线索。
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, OCR_TRUST_LOG), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 用户确认信任未通过校验的 OCR 安装包\n"
                    f"    文件   : {path}\n"
                    f"    原因   : {why}\n"
                    f"    sha256 : {digest}\n")
    except Exception as e:
        log(f"[安全] 写入信任记录失败（不影响本次操作）: {e}")


# ── OCR 安装包完整性校验（H1）───────────────────────────
# 该文件会以**管理员权限**启动（ShellExecuteW runas），是提权执行点。
# 校验实现**与主程序共用** utils/ocr_verify.py —— 主程序「设置 → 稍后安装 OCR」
# 原本直接 startfile 固定名 exe，会把这里的白名单整个绕开，两边必须同一份实现。
_INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_INSTALLER_DIR)
for _p in (_PROJECT_ROOT, _INSTALLER_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

def _ocr_pin_dirs():
    """附加哈希清单的查找目录：安装器同目录 / exe 目录 / 项目根。

    保留"往安装器同目录放 ocr_installers.json 即可追加新版本哈希"的扩展方式，
    同时共用主程序那份内置清单，两边不会各写一套。
    """
    dirs = []
    for d in (base_dir(), _INSTALLER_DIR, _PROJECT_ROOT):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


try:
    from utils import ocr_verify as _ocr_verify          # type: ignore
    from utils.ocr_verify import (                       # type: ignore
        OCR_EXE_GLOB, MIN_OCR_INSTALLER_BYTES, OCR_PINNED_SHA256, OCR_PINS_JSON,
        find_ocr_installer, is_size_ok as _is_size_ok, sha256_file,
    )

    def _allowed_ocr_hashes():
        return _ocr_verify.allowed_ocr_hashes(_ocr_pin_dirs())

    def verify_ocr_installer(path):
        """校验 OCR 安装包（与主程序同一实现；额外认安装器同目录的扩展清单）"""
        return _ocr_verify.verify_ocr_installer(path, pins_dirs=_ocr_pin_dirs())

except Exception as _e:                 # 打包漏带模块时**fail-closed**，绝不放行提权
    _OCR_VERIFY_IMPORT_ERROR = str(_e)
    OCR_EXE_GLOB = "tesseract-ocr-*.exe"
    MIN_OCR_INSTALLER_BYTES = 5 * 1024 * 1024
    OCR_PINNED_SHA256 = set()
    OCR_PINS_JSON = "ocr_installers.json"

    def sha256_file(path, chunk=1 << 20) -> str:
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(chunk), b""):
                h.update(b)
        return h.hexdigest()

    def _is_size_ok(path) -> bool:
        try:
            return os.path.getsize(path) >= MIN_OCR_INSTALLER_BYTES
        except OSError:
            return False

    def find_ocr_installer(dirs):
        for d in (dirs or []):
            try:
                matches = sorted(glob.glob(os.path.join(d, OCR_EXE_GLOB)))
            except Exception:
                continue
            for m in matches:
                if _is_size_ok(m) and os.path.basename(m).lower().startswith("tesseract-ocr-w64-setup"):
                    return m
            for m in matches:
                if _is_size_ok(m):
                    return m
        return None

    def verify_ocr_installer(path, pins_dirs=None):
        return False, (f"OCR 校验模块不可用（{_OCR_VERIFY_IMPORT_ERROR}）。"
                       f"为安全起见已拒绝以管理员身份运行，请重新运行官方安装程序。")


def _safe_extract_target(dest, rel):
    """把 ZIP 成员名解析成安全的落地路径；越界/可疑一律返回 None。

    安全审计（C3）：原实现把成员名直接 `os.path.join(dest, rel)` 后写盘，
    而安装器会在启动时自动 UAC 提权 —— 构造一个含 `..\\..\\`、绝对路径、
    盘符（`C:...`）或 UNC（`\\\\server\\share\\...`）成员名的 ZIP，
    就能写到系统目录 / 启动目录，等同本地提权。
    （Python 的 `ZipFile.extract()` 会清洗这些名字，但本处是手工 `open()` 落盘的，
      所以那层保护没有生效。）

    三重校验：
      1) 拒绝绝对路径、盘符、UNC、`..` 片段与空名；
      2) `realpath`（会解析符号链接与 8.3 短名）后必须仍落在 `realpath(dest)` 内；
      3) 只允许落在目标目录之下的相对路径。
    """
    if not rel or not isinstance(rel, str):
        return None
    rel = rel.replace('/', os.sep)
    if os.path.isabs(rel) or rel.startswith(os.sep):
        return None
    if os.path.splitdrive(rel)[0]:          # C: / D: 之类
        return None
    if rel.startswith("\\\\") or rel.startswith("//") or ":" in rel.split(os.sep)[0]:
        return None
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    root = os.path.realpath(dest)
    target = os.path.realpath(os.path.join(root, *parts))
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def find_unsafe_zip_members(dest, names, zip_top=""):
    """返回压缩包里所有"会落到目标目录之外"的成员名（空列表=安全）。

    抽成独立函数是为了能被测试直接调用（安装流程本身要 UAC + 完整 UI，无法单测）。
    """
    unsafe = []
    for m in names or []:
        if not m or m.endswith('/'):
            continue
        rel = m
        if zip_top and rel.startswith(zip_top + '/'):
            rel = rel[len(zip_top) + 1:]
        if _safe_extract_target(dest, rel) is None:
            unsafe.append(m)
    return unsafe
# 调试用：跳过自动提权
SKIP_ELEVATE = os.environ.get("CSF_INSTALLER_SKIP_ELEVATE", "").strip().lower() in ("1", "true", "yes")


def log(msg):
    print(f"[安装程序] {msg}")


def _hidden_kwargs() -> dict:
    """隐藏子进程控制台窗口所需的 subprocess 参数（安装程序同样是 console=False）"""
    if os.name != 'nt':
        return {}
    kwargs = {}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs['startupinfo'] = si
    except Exception:
        pass
    try:
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    except Exception:
        pass
    return kwargs


# ── 资源路径（开发 / PyInstaller 打包 统一处理）──────────
def base_dir():
    """打包后为 _MEIPASS，开发时为脚本所在目录"""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """按「打包目录 → 脚本目录 → 可执行文件目录」顺序查找资源，找不到返回 None"""
    candidates = [os.path.join(base_dir(), name)]
    try:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), name))
    except Exception:
        pass
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), name))
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _pick_latest_zip(matches):
    """从多个 Standard Processing System*.zip 中按版本号选择最新的一个。

    只挑结构完整的包（排除拷贝中断导致的截断包），全都损坏时仍返回版本号最大的，
    由后续流程给出明确的错误提示。
    """
    def ver(p):
        m = re.search(r'(\d+)\.(\d+)\.(\d+)', os.path.basename(p))
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    ordered = sorted(matches, key=ver, reverse=True)
    for p in ordered:
        try:
            if zipfile.is_zipfile(p):
                return p
        except Exception:
            continue
    return ordered[0] if ordered else None


def resolve_zip_path():
    """定位安装包 ZIP：打包模式取 _MEIPASS，脚本模式取参数或脚本同目录"""
    if getattr(sys, 'frozen', False):
        candidate = os.path.join(base_dir(), ZIP_NAME)
        if not os.path.exists(candidate):
            matches = glob.glob(os.path.join(base_dir(), "Standard Processing System*.zip"))
            if matches:
                candidate = _pick_latest_zip(matches) or candidate
        return candidate

    user_args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if user_args:
        return os.path.abspath(user_args[0])
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(script_dir, ZIP_NAME)
    if not os.path.exists(candidate):
        matches = glob.glob(os.path.join(script_dir, "Standard Processing System*.zip"))
        candidate = (_pick_latest_zip(matches) if matches else None) or candidate
    return os.path.abspath(candidate)


ZIP_PATH = resolve_zip_path()


# ── 权限 ──────────────────────────────────────────────
def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate_self():
    """以管理员权限重新启动自身"""
    if getattr(sys, 'frozen', False):
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, "--elevated", None, 1)
    else:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            f'"{os.path.abspath(__file__)}" --elevated', None, 1
        )
    sys.exit(0)


def message_box(text, title=f"{APP_NAME} 安装程序", flags=0x10):
    """原生消息框（打包后无控制台，用于必须让用户看到的错误）"""
    try:
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flags)
    except Exception:
        print(f"[{title}] {text}")


# ── 图标 ──────────────────────────────────────────────
def set_app_user_model_id(appid="TheForeverCSF.StandardProcessingSystem.Installer"):
    """显式指定任务栏分组，避免调试运行时被并入 python.exe 的图标"""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


def set_window_icon(title, ico_path):
    """把 .ico 设置为窗口图标：无边框窗口的标题栏用不到，但任务栏 / Alt-Tab 会用它"""
    if not ico_path or not os.path.exists(ico_path):
        return False
    try:
        user32 = ctypes.windll.user32
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x00000010
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        ok = False
        for size, kind in ((16, ICON_SMALL), (32, ICON_BIG)):
            hicon = user32.LoadImageW(None, ico_path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if hicon:
                user32.SendMessageW(hwnd, WM_SETICON, kind, hicon)
                ok = True
        return ok
    except Exception as e:
        log(f"设置窗口图标失败: {e}")
        return False


# ── 系统集成 ──────────────────────────────────────────
def create_shortcut(target_path, shortcut_path, description="", working_dir="", icon_path=""):
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(shortcut_path)
        shortcut.TargetPath = target_path
        shortcut.WorkingDirectory = working_dir or os.path.dirname(target_path)
        shortcut.Description = description or APP_NAME
        if icon_path:
            shortcut.IconLocation = icon_path
        shortcut.Save()
        return True
    except Exception as e:
        log(f"创建快捷方式失败: {e}")
        return False


def register_context_menu(exe_path):
    """注册资源管理器右键菜单（HKCU，无需管理员）"""
    try:
        ps_script = f'''
$exe = '{exe_path}'
$icon = '{exe_path}'
$entries = @(
    @{{"Path"="HKCU:\\Software\\Classes\\Folder\\shell\\{APP_NAME}"; "MUIVerb"="使用{APP_NAME}处理此文件夹"; "Cmd"="`"$exe`" `"%1`""}},
    @{{"Path"="HKCU:\\Software\\Classes\\Directory\\Background\\shell\\{APP_NAME}"; "MUIVerb"="使用{APP_NAME}打开此位置"; "Cmd"="`"$exe`" `"%V`""}}
)
# 各支持格式的右键菜单（与 web/server.py 的注册逻辑保持一致）
foreach ($ext in @(".pdf",".doc",".docx",".docm",".xls",".xlsx",".xlsm",
                   ".ppt",".pptx",".pptm",".txt",".csv",".md",".log",".rtf")) {{
    $entries += @{{"Path"="HKCU:\\Software\\Classes\\SystemFileAssociations\\$ext\\shell\\{APP_NAME}"; "MUIVerb"="使用{APP_NAME}处理"; "Cmd"="`"$exe`" `"%1`""}}
}}
foreach ($e in $entries) {{
    if (-not (Test-Path $e.Path)) {{ New-Item -Path $e.Path -Force | Out-Null }}
    Set-ItemProperty -Path $e.Path -Name "MUIVerb" -Value $e.MUIVerb
    Set-ItemProperty -Path $e.Path -Name "Icon" -Value $icon
    $cmdPath = "$($e.Path)\\command"
    if (-not (Test-Path $cmdPath)) {{ New-Item -Path $cmdPath -Force | Out-Null }}
    Set-ItemProperty -Path $cmdPath -Name "(default)" -Value $e.Cmd
}}'''
        subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
            capture_output=True, timeout=30,
            **_hidden_kwargs(),
        )
        return True
    except Exception as e:
        log(f"右键菜单注册失败: {e}")
        return False


def detect_tesseract():
    """检测本机是否已安装 Tesseract OCR 引擎"""
    if find_tesseract_path():
        return True
    try:
        path = os.environ.get("PATH", "")
        for p in path.split(";"):
            if p and os.path.exists(os.path.join(p, "tesseract.exe")):
                return True
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Tesseract-OCR", r"SOFTWARE\Tesseract"):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        winreg.QueryValueEx(k, "Path")
                        return True
                except OSError:
                    pass
    except Exception:
        pass
    return False


def find_tesseract_path():
    for c in (r"C:\Program Files\Tesseract-OCR", r"C:\Program Files (x86)\Tesseract-OCR"):
        if os.path.exists(os.path.join(c, "tesseract.exe")):
            return c
    return None


# ── 安装核心（与界面完全解耦）──────────────────────────
class InstallError(Exception):
    pass


class InstallCore:
    """执行实际安装：释放文件、创建快捷方式、写设置、装 OCR。

    进度 / 状态 / 日志保存在实例上，界面按需轮询 snapshot()，无需线程间回调。
    """

    def __init__(self, zip_path, dest, options=None):
        self.zip_path = zip_path
        self.dest = dest
        self.opt = dict(options or {})
        self._lock = threading.Lock()
        self.progress = 0.0
        self.stage = "prepare"
        self.status = "准备安装…"
        self.logs = []
        self.done = False
        self.error = None
        # 旧版本检测结果
        self.has_old = False
        self.has_old_dict = False
        self.old_settings = None
        # OCR
        self.ocr_state = "skipped"
        self.ocr_manual_path = None
        # 完成摘要
        self.finish_info = {}
        self.backup_dir = None          # 我的文档备份目录（有备份时才有值）
        self._zip_top = None
        self._userdata_backup = None

    # ── 状态上报 ──
    def log(self, msg):
        with self._lock:
            self.logs.append(str(msg))

    def set_progress(self, value, status=None, stage=None):
        with self._lock:
            self.progress = max(0.0, min(1.0, float(value)))
            if status:
                self.status = status
            if stage:
                self.stage = stage

    def snapshot(self, cursor=0):
        with self._lock:
            total = len(self.logs)
            return {
                "progress": round(self.progress, 4),
                "stage": self.stage,
                "status": self.status,
                "logs": self.logs[cursor:total],
                "cursor": total,
                "done": self.done,
                "error": self.error,
                "ocrState": self.ocr_state,
                "ocrManualPath": self.ocr_manual_path,
                "finishInfo": dict(self.finish_info),
            }

    # ── 检测 ──
    @staticmethod
    def detect_old_install(install_dir):
        """检测是否已有旧版本，返回 dict。

        用户数据可能在两处：安装目录（老版本）与 %LOCALAPPDATA%\\标准处理系统
        （新版本）。两处都要看，否则新版用户"词典已被程序搬到数据目录"时，
        安装程序会以为没有词典可保留，于是既不备份也不恢复。
        """
        result = {"hasOld": False, "settings": None, "hasDict": False,
                  "dictPath": None, "settingsPath": None, "dataDirDict": False}
        try:
            if not install_dir or not os.path.isdir(install_dir):
                return result
            if not os.path.exists(os.path.join(install_dir, APP_EXE)):
                return result
            result["hasOld"] = True

            # 设置：数据目录优先（新版本写入位置），其次安装目录
            for p in (os.path.join(DATA_DIR, SETTINGS_NAME),
                      os.path.join(install_dir, SETTINGS_NAME)):
                if os.path.isfile(p):
                    result["settingsPath"] = p
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            result["settings"] = json.load(f)
                    except Exception:
                        pass
                    break

            # 词典：两处都识别，并记录来源
            for p, is_data in ((os.path.join(install_dir, DB_NAME), False),
                               (os.path.join(DATA_DIR, DB_NAME), True)):
                if os.path.isfile(p):
                    result["hasDict"] = True
                    result["dictPath"] = p
                    result["dataDirDict"] = is_data
                    break
        except Exception:
            pass
        return result

    @staticmethod
    def find_existing_install():
        """在常见位置查找已安装的旧版本，用于覆盖安装时自动回到原位置"""
        for c in (DEFAULT_INSTALL_DIR,
                  os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), APP_NAME),
                  USER_INSTALL_DIR):
            if os.path.isfile(os.path.join(c, APP_EXE)):
                return c
        return None

    # ── 主流程 ──
    def run(self):
        try:
            self._run()
            self.set_progress(1.0, "安装完成", "done")
            self.log("安装完成！")
        except InstallError as e:
            self.error = str(e)
            self.log(f"错误: {e}")
        except Exception as e:
            import traceback
            self.error = f"{e}"
            self.log(f"安装失败: {e}")
            self.log(traceback.format_exc())
        finally:
            with self._lock:
                self.done = True

    def _run(self):
        dest = self.dest
        opt = self.opt

        # ── 1. 检查安装包 ──
        self.set_progress(0.02, "正在检查安装包…", "prepare")
        if not os.path.exists(self.zip_path):
            raise InstallError(f"安装包不存在：{self.zip_path}\n请将安装包放在安装程序同目录下。")
        if not zipfile.is_zipfile(self.zip_path):
            raise InstallError(
                f"安装包损坏或不完整：{self.zip_path}\n"
                f"可能是拷贝过程中断导致的，请重新拷贝完整的安装包后重试。")
        self.log(f"安装包: {self.zip_path}")
        self.log(f"目标目录: {dest}")
        os.makedirs(dest, exist_ok=True)

        # ── 2. 统计文件 / 识别顶层版本目录 ──
        with zipfile.ZipFile(self.zip_path, 'r') as z:
            names = z.namelist()
            members = [m for m in names if not m.endswith('/')]
            total = len(members) or 1
            tops = sorted(set(m.split('/')[0] for m in names if '/' in m))
            self._zip_top = tops[0] if len(tops) == 1 else None

            # 安全（C3 + 顺序修正）：成员校验**必须在任何删除动作之前**。
            # 原实现把这段放在"删除旧目录"之后，一旦安装包被判越界而中止，
            # 旧版本已被 rmtree 删掉、新版本又没装上 —— 用户程序直接不可用。
            # 现在预检提前到覆盖安装准备（第 3 步）之前，中止时旧版本保持原样。
            bad = find_unsafe_zip_members(dest, names, self._zip_top)
            if bad:
                self.log(f"[安全] 安装包含 {len(bad)} 个越界成员，已中止安装：{bad[:3]}")
                raise InstallError(
                    f"安装包校验失败：含 {len(bad)} 个越界成员（如 {bad[0]}）。\n"
                    f"为安全起见已中止安装，请使用官方发布的安装包。\n"
                    f"（旧版本未被改动，可继续正常使用。）")

            # 磁盘空间预检：空间不足是最常见的"删了旧的、装不上新的"原因，
            # 提前算出来，避免把旧版本删掉之后才在释放文件时失败。
            try:
                need = sum(i.file_size for i in z.infolist()) * 1.05 + (64 << 20)
                free = shutil.disk_usage(dest).free
                if need > free:
                    raise InstallError(
                        f"目标磁盘空间不足：需要约 {need / 1048576:.0f} MB，"
                        f"可用 {free / 1048576:.0f} MB。\n"
                        f"请清理磁盘后重试。\n"
                        f"（旧版本未被改动，可继续正常使用。）")
            except InstallError:
                raise
            except Exception:
                pass
        self.log(f"共 {total} 个文件需要释放")
        if self._zip_top:
            self.log(f"安装包顶层目录: {self._zip_top}")
        self.log("安装包预检通过（结构 / 成员路径校验）")

        # ── 3. 覆盖安装准备：备份用户数据 → 删除旧目录 ──
        old = self.detect_old_install(dest)
        self.has_old = old["hasOld"]
        self.old_settings = old["settings"]
        self.has_old_dict = old["hasDict"]
        if self.has_old:
            self.set_progress(0.04, "正在备份已有数据…", "prepare")
            self._overwrite_prepare(dest)

        # ── 4. 释放文件 ──
        self.set_progress(0.06, "正在释放文件…", "extract")
        preserved = set()
        if self.has_old and self.has_old_dict and opt.get("useOldDict", True):
            preserved.add(DB_NAME)
        with zipfile.ZipFile(self.zip_path, 'r') as z:
            names = z.namelist()
            # 安全（C3）：预检已在第 2 步拦过一次，这里复检一遍
            # （防止预检之后安装包被替换）。此刻旧目录可能已被删除，
            # 所以这里绝不允许"跳过继续装"，只能中止并如实说明状态。
            bad = find_unsafe_zip_members(dest, names, self._zip_top)
            if bad:
                self.log(f"[安全] 释放阶段复检发现 {len(bad)} 个越界成员，已中止：{bad[:3]}")
                raise InstallError(
                    f"安装包校验失败：含 {len(bad)} 个越界成员（如 {bad[0]}）。\n"
                    f"已中止安装，请使用官方发布的安装包重试。")

            for i, member in enumerate(names):
                if member.endswith('/'):
                    continue
                basename = os.path.basename(member)
                if basename in preserved:
                    self.log(f"[{i + 1}/{total}] {member}（保留旧版本用户数据）")
                    continue
                rel = member
                if self._zip_top and rel.startswith(self._zip_top + '/'):
                    rel = rel[len(self._zip_top) + 1:]
                target = _safe_extract_target(dest, rel)
                if target is None:      # 双保险（上面已整体拦过一次）
                    self.log(f"[安全] 跳过可疑成员: {member}")
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(member) as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                self.set_progress(0.06 + (i + 1) / total * 0.46, stage="extract")
                if (i + 1) % 50 == 0 or i == total - 1:
                    # 字幕只给「文件计数」，百分比统一由顶部进度条表达，避免两个百分比打架
                    self.set_progress(0.06 + (i + 1) / total * 0.46,
                                      f"正在释放文件…　{i + 1}/{total}", "extract")
                    self.log(f"[{i + 1}/{total}] {member}")

        # ── 5. 恢复用户数据（保留旧词典时）／按选择替换数据目录词典（不保留时）──
        self._restore_userdata(dest)
        self._handle_dict_choice(dest)

        app_dir = dest
        exe_path = os.path.join(app_dir, APP_EXE)

        # ── 6. 桌面快捷方式 ──
        # 如实上报：create_shortcut 返回 False（权限/杀软拦截等）时必须明说，
        # 不能一律记"已创建"，否则用户桌面找不到图标却以为装好了。
        self.set_progress(0.55, "正在创建快捷方式…", "config")
        desktop_ok = None
        if opt.get("desktopShortcut", True):
            desktop = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop")
            desktop_ok = bool(create_shortcut(exe_path, os.path.join(desktop, f"{APP_NAME}.lnk"),
                                              working_dir=app_dir))
            self.log("桌面快捷方式已创建" if desktop_ok else
                     "桌面快捷方式创建失败（可稍后手动创建）")
        self.set_progress(0.62, "正在创建开始菜单快捷方式…", "config")

        # ── 7. 开始菜单快捷方式 + 卸载程序 ──
        start_ok, uninst_ok = False, False
        try:
            os.makedirs(START_MENU_DIR, exist_ok=True)
            start_ok = bool(create_shortcut(exe_path,
                                            os.path.join(START_MENU_DIR, f"{APP_NAME}.lnk"),
                                            working_dir=app_dir))
        except Exception as e:
            self.log(f"创建开始菜单目录失败: {e}")
        uninst_path = os.path.join(dest, UNINSTALL_BAT)
        try:
            with open(uninst_path, "w", encoding="utf-8") as f:
                f.write(self._uninstall_bat(dest))
            uninst_ok = bool(create_shortcut(uninst_path,
                                             os.path.join(START_MENU_DIR, f"卸载{APP_NAME}.lnk"),
                                             working_dir=dest))
        except Exception as e:
            self.log(f"写入卸载程序失败: {e}")
        self.log("开始菜单快捷方式已创建" if start_ok else "开始菜单快捷方式创建失败")
        if not uninst_ok:
            self.log(f"「卸载{APP_NAME}」快捷方式创建失败（可运行 {uninst_path} 卸载）")
        self.set_progress(0.70, "正在注册右键菜单…", "config")

        # ── 8. 右键菜单 ──
        menu_ok = None
        if opt.get("contextMenu", True):
            menu_ok = bool(register_context_menu(exe_path))
            self.log("资源管理器右键菜单已注册" if menu_ok else
                     "资源管理器右键菜单注册失败（权限/安全软件拦截）")
        self.set_progress(0.80, "正在写入卸载信息…", "config")

        # ── 9. 卸载信息 ──
        reg_ok = bool(self._write_uninstall_reg(dest, exe_path))
        self.log("已注册卸载信息（控制面板可见）" if reg_ok else
                 "注册卸载信息失败（控制面板里可能看不到本程序）")
        self.set_progress(0.86, "正在写入系统设置…", "config")

        # ── 10. 系统设置 ──
        self._write_settings(app_dir)
        self.set_progress(0.92, "正在完成配置…", "config")

        # ── 11. OCR 引擎 ──
        if opt.get("installOcr", False):
            self._install_ocr(dest)
        self.set_progress(0.98, "正在收尾…", "config")

        # ── 12. 完成摘要（全部按实际结果上报）──
        self.finish_info = {
            "dir": dest,
            "desktop": desktop_ok,
            "startMenu": start_ok,
            "uninstallLink": uninst_ok,
            "contextMenu": menu_ok,
            "uninstallReg": reg_ok,
            "ocr": self.ocr_state,
            "backupDir": self.backup_dir or "",
            "dataDir": DATA_DIR,
        }

    def _handle_dict_choice(self, dest):
        """按用户选择决定**数据目录**里的词典（主程序实际读取的那份）。

        必须连数据目录一起处理：主程序从 %LOCALAPPDATA%\\标准处理系统 读词典，
        只替换安装目录里的副本，等于"用户明明选不保留，程序还在用旧词典"。
        """
        if not self.has_old or not self.has_old_dict:
            return
        if self.opt.get("useOldDict", True):
            return                                  # 保留：数据目录里的用户词典不动
        shipped = os.path.join(dest, DB_NAME)
        if not os.path.isfile(shipped):
            return
        target = os.path.join(DATA_DIR, DB_NAME)
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            # 旧库的 -wal/-shm 必须先删：它属于旧词典，与新库混在一起会读出错数据
            for suffix in ("-wal", "-shm"):
                p = target + suffix
                if os.path.exists(p):
                    os.remove(p)
            shutil.copy2(shipped, target)
            self.log("按你的选择未沿用旧词典：数据目录已替换为随包词典")
        except Exception as e:
            self.log(f"替换数据目录词典失败（旧词典仍在，备份见我的文档）: {e}")

    # ── 覆盖安装：备份 / 恢复用户数据 ──
    def _backup_userdata(self, dest, tag="安装备份"):
        """把用户数据复制到「我的文档\\<APP_NAME>备份\\<tag>_时间戳\\」（只复制不删除）。

        任何删除动作之前都必须先走这一步：即使用户随后选择"不沿用旧词典"、
        或安装中途失败/断电，数据也还能从我的文档取回（此前是直接 rmtree，
        用户积累的《标准名称字典.db》不可逆丢失）。
        数据目录（%LOCALAPPDATA%）里的副本优先，其次是安装目录里的老文件。
        """
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(documents_dir(), BACKUP_DIR_NAME, f"{tag}_{stamp}")
        copied = []
        for name in USER_DATA_FILES:
            for p in (os.path.join(DATA_DIR, name), os.path.join(dest, name)):
                if os.path.isfile(p):
                    try:
                        os.makedirs(out_dir, exist_ok=True)
                        shutil.copy2(p, os.path.join(out_dir, name))
                        copied.append(name)
                    except Exception as e:
                        self.log(f"备份 {name} 到我的文档失败: {e}")
                    break
        if copied:
            try:
                with open(os.path.join(out_dir, "说明.txt"), "w", encoding="utf-8") as f:
                    f.write(
                        f"{APP_NAME} 安装前自动备份\n"
                        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"来源：{dest}\n\n"
                        "包含文件：\n"
                        + "".join(f"  {n}\n" for n in copied) +
                        "\n恢复方法：把需要的文件复制回下面的目录（同名覆盖），再重启程序。\n"
                        f"  {DATA_DIR}\n")
            except Exception:
                pass
            self.backup_dir = out_dir
            self.log(f"已备份用户数据到我的文档: {out_dir}")
        else:
            self.log("未发现需要备份的用户数据")
        return copied

    def _overwrite_prepare(self, dest):
        """先备份用户数据 → 再删除整个旧安装目录 → 重建空目录。

        这样旧桌面/开始菜单快捷方式指向的 exe 路径（dest\\标准处理系统.exe）保持不变。
        """
        # 1) 不可逆操作之前，先做「我的文档」安全备份（无论后面怎么选都能找回）
        self._backup_userdata(dest, "覆盖安装备份")

        # 2) 临时备份：删除目录后立刻恢复用
        self._userdata_backup = None
        backup_dir = tempfile.mkdtemp(prefix="csf_bak_")
        backed = []
        for name in (SETTINGS_NAME, DB_NAME, "builtin_prefixes.json"):
            if name == DB_NAME and not self.opt.get("useOldDict", True):
                continue           # 用户明确不沿用旧词典：不恢复（我的文档里仍有备份）
            for p in (os.path.join(DATA_DIR, name), os.path.join(dest, name)):
                if os.path.isfile(p):
                    try:
                        shutil.copy2(p, os.path.join(backup_dir, name))
                        backed.append(name)
                    except Exception as e:
                        self.log(f"备份 {name} 失败: {e}")
                    break
        try:
            if os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
            self.log(f"已清理旧安装目录（临时保留: {', '.join(backed) or '无'}），准备覆盖安装")
        except Exception as e:
            self.log(f"清理旧安装目录失败，将直接覆盖: {e}")
        os.makedirs(dest, exist_ok=True)
        self._userdata_backup = backup_dir if backed else None

    def _restore_userdata(self, dest):
        """把临时备份的用户数据恢复到**数据目录**（主程序读取位置）与安装目录。

        数据目录是权威位置（新版主程序从 %LOCALAPPDATA% 读），安装目录里留一份
        是为了兼容仍从 exe 同级读取的老版本程序。
        """
        backup_dir = self._userdata_backup
        if not backup_dir or not os.path.isdir(backup_dir):
            return
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
        except Exception as e:
            self.log(f"创建用户数据目录失败: {e}")
        for name in os.listdir(backup_dir):
            for target_dir in (DATA_DIR, dest):
                try:
                    shutil.copy2(os.path.join(backup_dir, name), os.path.join(target_dir, name))
                    self.log(f"已恢复用户数据: {name} → {target_dir}")
                except Exception as e:
                    self.log(f"恢复用户数据失败: {name} → {target_dir} — {e}")
        shutil.rmtree(backup_dir, ignore_errors=True)
        self._userdata_backup = None

    # ── 设置 ──
    def _write_settings(self, app_dir):
        opt = self.opt
        settings = {}
        if self.has_old and opt.get("useOldSettings", True) and self.old_settings:
            settings = dict(self.old_settings)
            self.log("已沿用上一版本设置作为基础")
        else:
            self.log("使用安装程序默认设置")
        settings.update({
            "autoTheme": bool(opt.get("autoTheme", True)),
            "ocrDefault": bool(opt.get("ocrDefault", True)),
            "autoLookup": bool(opt.get("autoLookup", True)),
            "splitDefault": bool(opt.get("splitDefault", True)),
            "pdfQuality": str(opt.get("pdfQuality", "balanced")),
            "classifiedMode": bool(opt.get("classifiedMode", False)),
            "enableQHighlight": bool(opt.get("enableQHighlight", False)),
            "enableQStandard": bool(opt.get("enableQStandard", False)),
            "preciseYear": bool(opt.get("preciseYear", False)),
            "stripYear": bool(opt.get("stripYear", False)),
        })
        # 界面未提供的键：有旧值则沿用，否则用安装程序默认
        settings.setdefault("lightTime", "06:00")
        settings.setdefault("darkTime", "18:00")
        settings.setdefault("customPrefixes", [])
        # 同时写两处：数据目录（主程序读取位置）+ 安装目录（兼容老版本程序）
        wrote = []
        targets = [DATA_DIR]
        if os.path.abspath(app_dir) != os.path.abspath(DATA_DIR):
            targets.append(app_dir)
        for target in targets:
            try:
                os.makedirs(target, exist_ok=True)
                with open(os.path.join(target, SETTINGS_NAME), "w", encoding="utf-8") as f:
                    json.dump(settings, f, ensure_ascii=False, indent=2)
                wrote.append(target)
            except Exception as e:
                self.log(f"写入设置失败（{target}）: {e}")
        if wrote:
            self.log(f"系统设置已写入: {'；'.join(wrote)}")

    # ── OCR ──
    def _locate_ocr_installer(self, dest):
        """按版本无关的方式定位 Tesseract 安装包。

        依次查找：安装目录 → _internal → 安装程序同目录 → 安装包同目录 → 从 ZIP 释放。
        """
        search_dirs = [dest, os.path.join(dest, "_internal"), base_dir()]
        try:
            search_dirs.append(os.path.dirname(self.zip_path))
        except Exception:
            pass
        # 目录搜索走共享实现（与主程序同一套规则：通配 + 体积筛选 + 优先 w64）
        found = find_ocr_installer(search_dirs)
        if found:
            return found
        # 都找不到：从安装包里释放一份到安装目录，方便立即安装 / 以后手动安装
        try:
            if os.path.exists(self.zip_path):
                with zipfile.ZipFile(self.zip_path, 'r') as z:
                    cands = [m for m in z.namelist()
                             if os.path.basename(m).lower().startswith("tesseract-ocr")
                             and m.lower().endswith(".exe")
                             and z.getinfo(m).file_size >= MIN_OCR_INSTALLER_BYTES]
                    # 取体积最大的那个：发布包里同时存在
                    # 顶层完整包（约 20MB）与 _internal 下的残缺副本（约 123KB），
                    # 按名字取第一个可能拿到残缺包，装 OCR 会静默失败。
                    if cands:
                        member = max(cands, key=lambda m: z.getinfo(m).file_size)
                        target = os.path.join(dest, os.path.basename(member))
                        with z.open(member) as src, open(target, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                        if self._ocr_installer_size_ok(target):
                            self.log(f"已从安装包释放 OCR 安装程序: {target}")
                            return target
                        self.log(f"释放出的 OCR 安装程序体积异常，已忽略: {target}")
        except Exception as e:
            self.log(f"释放 OCR 安装程序失败: {e}")
        return None

    @staticmethod
    def _ocr_installer_size_ok(path) -> bool:
        """体积校验：小于 MIN_OCR_INSTALLER_BYTES 的一律视为残缺文件。

        发布包里的 _internal 副本实测只有 123 KB（完整包约 20 MB），
        选到它会让"安装 OCR"必然失败，而且界面只报"装不上"，用户无从判断。
        """
        return _is_size_ok(path)

    @staticmethod
    def launch_ocr_installer(path, force=False):
        """启动 OCR 安装包。

        用 ShellExecute（os.startfile）而不是 CreateProcess：安装包需要管理员权限时，
        CreateProcess 会静默失败（错误 740 且不弹 UAC），ShellExecute 才会弹出提权提示。

        安全（H1）：这里是"以管理员权限启动外部程序"的唯一收口点（自动安装与完成页
        的手动安装都走它），所以启动前必须校验安装包内容哈希，校验不过直接拒绝。

        force=True：用户在完成页看到明确的失败原因后，主动点了
        「我信任此文件，仍要运行」并二次确认。此路径仍然会把实际 sha256 记进日志，
        便于事后追责；除这种显式确认外一律不放行。
        """
        ok, why = verify_ocr_installer(path)
        if not ok and not force:
            log(f"[安全] 拒绝启动未通过校验的 OCR 安装包: {path}")
            log(f"[安全] 原因: {why}")
            return False
        if ok:
            log(f"OCR 安装包完整性校验通过（{why}）")
        else:
            try:
                digest = sha256_file(path)
            except Exception:
                digest = "?"
            log(f"[安全] 用户已确认信任该文件，跳过完整性校验: {path}")
            log(f"[安全] 校验未通过原因: {why}")
            log(f"[安全] 实际 sha256={digest}（已记录，便于日后核查）")
            _record_trust_event(path, why, digest)
        workdir = os.path.dirname(path) or None
        try:
            os.startfile(path)
            return True
        except Exception as e:
            log(f"startfile 启动失败: {e}")
        for verb in ("open", "runas"):
            try:
                rc = ctypes.windll.shell32.ShellExecuteW(None, verb, path, None, workdir, 1)
                if rc > 32:
                    return True
                log(f"ShellExecuteW({verb}) 返回码: {rc}")
            except Exception as e:
                log(f"ShellExecuteW({verb}) 失败: {e}")
        try:
            subprocess.Popen([path], cwd=workdir)
            return True
        except Exception as e:
            log(f"启动 OCR 安装程序失败: {e}")
        return False

    def _install_ocr(self, dest):
        self.set_progress(0.93, "正在准备 OCR 引擎安装程序…", "ocr")
        ocr_installer = self._locate_ocr_installer(dest)
        if not ocr_installer:
            self.ocr_state = "not_found"
            self.log("未找到 OCR 安装包（tesseract-ocr-*.exe），已跳过")
            return
        # 安全（H1）：该安装包将以管理员权限启动，先做完整性校验，
        # 并把"校验失败"和"启动失败"区分开，让用户看到真正的原因
        ok, why = verify_ocr_installer(ocr_installer)
        if not ok:
            # 保留路径，界面才能给出"我信任此文件，仍要运行（二次确认）"这条出口；
            # 并把真因写进日志（界面也会原样显示，不再用"可稍后安装"掩盖）
            self.ocr_state = "untrusted"
            self.ocr_manual_path = ocr_installer
            self.log(f"[安全] 已阻止启动未通过校验的 OCR 安装包: {ocr_installer}")
            self.log(f"[安全] {why}")
            self.log("可在完成页选择「我信任此文件，仍要运行」并二次确认后继续。")
            return
        self.set_progress(0.95, "正在启动 OCR 引擎安装程序…", "ocr")
        self.log(f"启动 OCR 引擎安装程序: {ocr_installer}")
        if self.launch_ocr_installer(ocr_installer):
            self.ocr_state = "launched"
            self.log("OCR 安装程序已启动（请在弹出的窗口中完成安装）")
        else:
            self.ocr_state = "failed"
            self.ocr_manual_path = ocr_installer
            self.log("OCR 安装程序启动失败，可在完成页点击按钮手动安装")

    # ── 卸载 ──
    def _uninstall_bat(self, dest):
        """生成卸载脚本。

        三条硬要求：
          1) 必须先弹窗询问是否保留用户数据（默认「保留」），且**无论选什么都先备份到我的文档**；
          2) 需要管理员权限才能删 %ProgramFiles% 下的程序目录 —— 旧脚本没有提权，
             从开始菜单点卸载时 rmdir 会静默失败却仍打印"卸载完成"；
          3) 每一步都如实报告结果（目录是否真的删掉、快捷方式是否真的删掉）。
        """
        tpl = '''@echo off
chcp 65001 >nul
setlocal
title 卸载 __APP_NAME__
echo ========================================
echo   正在卸载 __APP_NAME__
echo ========================================
echo.

rem ── 0. 提权：删除 Program Files 下的目录必须有管理员权限 ──
net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo 需要管理员权限，正在请求提权...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath '%~f0' -Verb RunAs } catch { exit 1 }"
    if errorlevel 1 (
        echo.
        echo [提示] 提权被取消或失败。请右键本文件选择「以管理员身份运行」再试。
        pause
    )
    exit /b
)

rem ── 1. 询问是否保留用户数据（默认「保留」）──
set "KEEP=1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.Windows.Forms; $r = [System.Windows.Forms.MessageBox]::Show('卸载前是否保留你的「标准名称字典」与设置？`n`n  「是」（推荐）：保留，并备份到「我的文档\\__BACKUP_DIR_NAME__」，下次安装自动继续使用`n  「否」：一并删除（同样会先备份到「我的文档」，可随时找回）', '卸载 __APP_NAME__', [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Question, [System.Windows.Forms.MessageBoxDefaultButton]::Button1); if ($r -eq 'Yes') { exit 10 } else { exit 20 }"
if "%errorlevel%"=="10" set "KEEP=1"
if "%errorlevel%"=="20" set "KEEP=0"

rem ── 2. 备份用户数据到「我的文档」（无论选保留还是删除，都先备份）──
powershell -NoProfile -ExecutionPolicy Bypass -Command "$doc = [Environment]::GetFolderPath('MyDocuments'); $ts = Get-Date -Format 'yyyyMMdd_HHmmss'; $dst = Join-Path $doc '__BACKUP_DIR_NAME__\\卸载备份_' + $ts; New-Item -ItemType Directory -Force -Path $dst | Out-Null; $names = @('.app_settings.json', '__DB__', '__DB__-wal', '__DB__-shm', 'builtin_prefixes.json', '.tutorial_state.json', '.pending_import.json'); $n = 0; foreach ($src in @('__DATA_DIR__', '__DEST__')) { foreach ($f in $names) { $p = Join-Path $src $f; $t = Join-Path $dst $f; if ((Test-Path $p) -and -not (Test-Path $t)) { Copy-Item $p $t -Force -ErrorAction SilentlyContinue; $n++ } } }; if ($n -gt 0) { Set-Content -Path (Join-Path $dst '说明.txt') -Value ('__APP_NAME__ 卸载前自动备份，共 ' + $n + ' 个文件。\\n恢复方法：把需要的文件复制回 __DATA_DIR__ 后重启程序。') -Encoding UTF8; Write-Host ('  已备份 ' + $n + ' 个文件到: ' + $dst) } else { Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue; Write-Host '  未发现需要备份的用户数据' }"

rem ── 3. 清理右键菜单 ──
echo 正在清理右键菜单...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$keys = @( 'HKCU:\\Software\\Classes\\Folder\\shell\\__APP_NAME__', 'HKCU:\\Software\\Classes\\Directory\\Background\\shell\\__APP_NAME__' ); foreach ($ext in @('.pdf','.doc','.docx','.docm','.xls','.xlsx','.xlsm','.ppt','.pptx','.pptm','.txt','.csv','.md','.log','.rtf')) { $keys += 'HKCU:\\Software\\Classes\\SystemFileAssociations\\' + $ext + '\\shell\\__APP_NAME__' }; foreach ($k in $keys) { if (Test-Path $k) { Remove-Item -Path $k -Recurse -Force -ErrorAction SilentlyContinue } }"

rem ── 4. 删除程序目录（先确认该目录确实是本程序目录）──
echo 正在删除程序目录...
rem 安全：只有确认含 __EXE__ 才递归删除，避免安装目录被改到别处时误删用户文件
if exist "__DEST__\\__EXE__" (
    rmdir /s /q "__DEST__"
)
if exist "__DEST__\\__EXE__" (
    echo [警告] 程序目录未能完全删除：__DEST__
    echo         可能被占用，请关闭相关窗口/程序后手动删除。
) else (
    echo   程序目录已删除。
)

rem ── 5. 卸载信息与快捷方式 ──
reg delete "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\__APP_NAME__" /f >nul 2>&1
if exist "__START_MENU__\\" ( rmdir /s /q "__START_MENU__" )
if exist "__START_MENU__\\" (
    echo [警告] 开始菜单快捷方式未能删除：__START_MENU__
) else (
    echo   开始菜单快捷方式已删除。
)
if exist "%USERPROFILE%\\Desktop\\__APP_NAME__.lnk" ( del /f /q "%USERPROFILE%\\Desktop\\__APP_NAME__.lnk" )
echo   桌面快捷方式已清理。

rem ── 6. 用户数据目录（%LOCALAPPDATA%）──
if "%KEEP%"=="1" (
    echo   已保留用户数据目录：__DATA_DIR__
    echo     （下次安装会自动继续使用；不需要时可手动删除）
) else (
    if exist "__DATA_DIR__\\" ( rmdir /s /q "__DATA_DIR__" )
    if exist "__DATA_DIR__\\" (
        echo [警告] 用户数据目录未能删除：__DATA_DIR__
    ) else (
        echo   用户数据目录已删除（备份仍在「我的文档」）。
    )
)
echo.
echo 卸载完成。
pause
'''
        repl = {
            "__APP_NAME__": APP_NAME,
            "__APP_EXE__": APP_EXE,
            "__EXE__": APP_EXE,
            "__DEST__": dest,
            "__START_MENU__": START_MENU_DIR,
            "__DATA_DIR__": DATA_DIR,
            "__DB__": DB_NAME,
            "__SETTINGS__": SETTINGS_NAME,
            "__BACKUP_DIR_NAME__": BACKUP_DIR_NAME,
        }
        out = tpl
        for k, v in repl.items():
            out = out.replace(k, v)
        return out

    def _write_uninstall_reg(self, install_dir, exe_path):
        """写入控制面板卸载信息，返回是否成功（界面按实际结果上报）"""
        try:
            import winreg
            key_path = f"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{APP_NAME}"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
                winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, exe_path)
                winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
                winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, APP_PUBLISHER)
                winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, install_dir)
                winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                                  os.path.join(install_dir, UNINSTALL_BAT))
                winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
            return True
        except Exception as e:
            log(f"写入卸载注册表失败: {e}")
            return False


# ── 辅助：从安装包读取文本 / 释放协议 PDF ───────────────
def load_changelog_from_zip(zip_path):
    """从安装包中读取 更新内容.txt"""
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            for name in z.namelist():
                if name.endswith('更新内容.txt'):
                    data = z.read(name)
                    try:
                        return data.decode('utf-8-sig')
                    except UnicodeDecodeError:
                        return data.decode('gbk', errors='replace')
    except Exception:
        pass
    local = resource_path("更新内容.txt")
    if local:
        try:
            with open(local, 'r', encoding='utf-8-sig') as f:
                return f.read()
        except Exception:
            pass
    return None


def _resolve_agreement_dir():
    """返回实际存在的协议图片目录（新版优先，回退旧版）；都不存在返回 None。"""
    for name in AGREEMENT_DIR_CANDIDATES:
        p = resource_path(name)
        if p and os.path.isdir(p):
            return p
    return None


def extract_agreement_pdf(zip_path):
    """释放《标准处理系统 许可、隐私与免责说明》PDF，返回可直接打开的路径。

    来源顺序（新版优先）：
      1) 安装包内的 PDF（随应用包分发的那份）；
      2) 安装器自带的那份（installer.spec 的 datas 会把打包目录里的
         「标准处理系统 许可、隐私与免责说明.pdf」一并打进安装程序），
         安装包漏带时也能打开，不至于让用户点了没反应。
    """
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            names = z.namelist()
            member = None
            for suffix in AGREEMENT_PDF_CANDIDATES:
                member = next((m for m in names if m.endswith(suffix)), None)
                if member:
                    break
            if member:
                tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
                tmp.write(z.read(member))
                tmp.close()
                return tmp.name
    except Exception:
        pass
    for cand in AGREEMENT_PDF_CANDIDATES:
        p = resource_path(cand)
        if p and os.path.isfile(p):
            log(f"安装包内未找到说明 PDF，改用安装器自带副本: {p}")
            return p
    return None


def collect_agreement_images():
    """收集协议图片：优先程序目录，其次从安装包释放"""
    img_dir = _resolve_agreement_dir()
    if img_dir:
        images = sorted(os.path.join(img_dir, f) for f in os.listdir(img_dir)
                        if f.lower().endswith(('.jpg', '.jpeg', '.png')))
        if images:
            return images
    # 回退：从 ZIP 中释放协议图片（任一候选目录名都接受）
    try:
        out_dir = os.path.join(tempfile.gettempdir(), "csf_agreement_img")
        os.makedirs(out_dir, exist_ok=True)
        with zipfile.ZipFile(ZIP_PATH, 'r') as z:
            members = [m for m in z.namelist()
                       if any(d in m for d in AGREEMENT_DIR_CANDIDATES)
                       and m.lower().endswith(('.jpg', '.jpeg', '.png'))]
            for i, m in enumerate(sorted(members)):
                target = os.path.join(out_dir, f"agree_{i:02d}" + os.path.splitext(m)[1])
                if not os.path.exists(target):
                    with z.open(m) as src, open(target, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
        return sorted(os.path.join(out_dir, f) for f in os.listdir(out_dir))
    except Exception:
        return []


# ── pywebview 界面 ────────────────────────────────────
class InstallerUI:
    """基于 WebView2 + 内嵌 HTML 的安装界面。

    本类直接作为 pywebview 的 js_api：所有实例属性以 `_` 开头（不会被暴露给 JS），
    所有公开方法即 JS 可调用的接口。
    """

    def __init__(self, zip_path):
        self._zip = zip_path
        self._title = f"{APP_NAME} v{APP_VERSION} 安装程序"
        self._window = None
        self._core = None
        self._lock = threading.Lock()
        self._installing = False
        self._shown = False
        self._preview_dir = None
        self._tess_path = find_tesseract_path()
        self._existing = InstallCore.find_existing_install()
        self._default_dir = self._existing or DEFAULT_INSTALL_DIR
        self._old = InstallCore.detect_old_install(self._default_dir)
        self._changelog = load_changelog_from_zip(zip_path)

    # ══════ JS 接口 ══════
    def get_state(self):
        old_settings = self._old.get("settings") or {}
        defaults = {
            "autoTheme": True, "ocrDefault": True, "autoLookup": True, "splitDefault": True,
            "pdfQuality": "balanced", "enableQHighlight": False, "enableQStandard": False,
            "preciseYear": False, "stripYear": False, "classifiedMode": False,
        }
        vals = {}
        for k in defaults:
            if k not in old_settings:
                continue
            vals[k] = str(old_settings[k]) if k == "pdfQuality" else bool(old_settings[k])
        return {
            "appName": APP_NAME,
            "version": APP_VERSION,
            "exeName": APP_EXE,
            "defaultDir": self._default_dir,
            "userDir": USER_INSTALL_DIR,
            "detectedDir": self._existing,
            "hasOld": bool(self._old.get("hasOld")),
            "hasOldDict": bool(self._old.get("hasDict")),
            "tesseract": {"installed": bool(self._tess_path), "path": self._tess_path},
            "admin": is_admin(),
            "defaults": defaults,
            "oldValues": vals,
            "changelog": self._changelog or "",
            "dataDir": DATA_DIR,                                     # 用户数据目录
            "backupRoot": os.path.join(documents_dir(), BACKUP_DIR_NAME),  # 我的文档备份根目录
        }

    def check_dir(self, path):
        """检查目录是否可写、是否已有旧版本。

        注意：这里只做探测，不创建目录（用户可能正在输入路径，不能留下半截目录）。
        目录不存在时，改为检查最近的已存在上级目录是否可写。
        """
        path = (path or "").strip().strip('"')
        if not path:
            return {"ok": False, "message": "请先选择安装位置。", "suggestDir": USER_INSTALL_DIR,
                    "hasOld": False, "hasOldDict": False}
        # 安全（H2）：先做目录安全性硬校验（根目录 / 系统目录 / 非空且非本程序目录一律拒绝），
        # 让用户在"选目录"阶段就看到原因，而不是装完才发现卸载会误删
        _dir_ok, _dir_why = validate_install_dir(path)
        if not _dir_ok:
            return {"ok": False, "message": _dir_why, "suggestDir": USER_INSTALL_DIR,
                    "hasOld": False, "hasOldDict": False}
        probe = path
        while probe and not os.path.isdir(probe):
            parent = os.path.dirname(probe.rstrip("\\/"))
            if parent == probe:
                break
            probe = parent
        ok, message = True, ""
        if not probe or not os.path.isdir(probe):
            ok, message = False, "路径无效，请重新选择安装位置。"
        else:
            test_file = os.path.join(probe, ".write_test_std_installer")
            try:
                with open(test_file, "w") as f:
                    f.write("test")
                os.remove(test_file)
            except PermissionError:
                ok, message = False, "当前目录需要管理员权限才能写入。"
            except Exception as e:
                ok, message = False, f"无法写入该目录：{e}"
        info = InstallCore.detect_old_install(path)
        return {
            "ok": ok, "message": message,
            "hasOld": bool(info.get("hasOld")), "hasOldDict": bool(info.get("hasDict")),
            "suggestDir": USER_INSTALL_DIR,
            "needAdmin": not is_admin(),
        }

    def browse_dir(self, current=""):
        try:
            import webview
            current = (current or "").strip().strip('"')
            result = self._window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=current if os.path.isdir(current) else os.path.expanduser("~"),
            )
            if result:
                return {"ok": True, "path": result[0] if isinstance(result, (list, tuple)) else result}
            return {"ok": False, "path": ""}
        except Exception as e:
            log(f"目录选择失败: {e}")
            return {"ok": False, "path": "", "message": str(e)}

    def start_install(self, options=None):
        options = dict(options or {})
        dest = str(options.get("dir", "")).strip().strip('"')
        with self._lock:
            if self._installing:
                return {"ok": False, "message": "安装已在进行中。"}
            if not dest:
                return {"ok": False, "code": "nodir", "message": "请先选择安装位置。"}
            chk = self.check_dir(dest)
            if not chk["ok"]:
                return {"ok": False, "code": "eacces", "message": chk["message"],
                        "suggestDir": USER_INSTALL_DIR}
            self._core = InstallCore(self._zip, dest, options)
            self._installing = True
        threading.Thread(target=self._core.run, name="csf-install", daemon=True).start()
        return {"ok": True}

    def progress(self, cursor=0):
        try:
            cursor = max(0, int(cursor))
        except Exception:
            cursor = 0
        core = self._core
        if core is None:
            return {"progress": 0, "stage": "prepare", "status": "准备安装…", "logs": [],
                    "cursor": 0, "done": False, "error": None,
                    "ocrState": "skipped", "ocrManualPath": None, "finishInfo": {}}
        snap = core.snapshot(cursor)
        if snap["done"]:
            with self._lock:
                self._installing = False
        return snap

    def agreement_images(self):
        """协议图片（超长扫描件会先生成缩略预览，避免 WebView 解码超大位图）"""
        images = collect_agreement_images()
        if not images:
            return {"ok": False, "message": "安装包中未找到说明文档图片。", "images": []}
        previews = []
        try:
            from PIL import Image
            Image.MAX_IMAGE_PIXELS = None  # 协议为超长扫描件，主动放宽 Pillow 限制
            out_dir = os.path.join(tempfile.gettempdir(), "csf_agreement_prev")
            os.makedirs(out_dir, exist_ok=True)
            for i, src_path in enumerate(images):
                dst = os.path.join(out_dir, f"page_{i:02d}.jpg")
                if not os.path.exists(dst):
                    im = Image.open(src_path)
                    im.load()
                    if im.mode != 'RGB':
                        im = im.convert('RGB')
                    w, h = im.size
                    target_w = 640
                    if w > target_w:
                        if w > target_w * 2:  # 超高分辨率扫描件：先快速降采样
                            quick = (target_w * 2) / w
                            im = im.resize((int(w * quick), int(h * quick)), Image.BOX)
                            w, h = im.size
                        im = im.resize((target_w, int(h * target_w / w)), Image.LANCZOS)
                    im.save(dst, "JPEG", quality=82, optimize=True)
                previews.append(dst)
        except Exception as e:
            log(f"生成协议预览失败（改用原图）: {e}")
            previews = images
        urls = []
        for p in previews:
            try:
                urls.append(Path(p).as_uri())
            except Exception:
                urls.append("file:///" + os.path.abspath(p).replace("\\", "/").replace(" ", "%20"))
        return {"ok": True, "images": urls, "message": ""}

    def open_agreement_pdf(self):
        pdf = extract_agreement_pdf(self._zip)
        if not pdf:
            return {"ok": False, "message": "安装包中未找到说明文档 PDF。"}
        try:
            os.startfile(pdf)
            threading.Timer(30.0, lambda p=pdf: os.path.exists(p) and os.unlink(p)).start()
            return {"ok": True, "message": ""}
        except Exception as e:
            return {"ok": False, "message": f"无法打开说明文件：{e}"}

    def install_ocr_manually(self, force=False):
        """完成页「手动安装 OCR 引擎」。

        force=False：正常路径，走完整性校验，校验不过**如实返回原因**（不再用
                    "可稍后在设置中安装"掩盖真因）。
        force=True ：用户看到真因后，主动选择「我信任此文件，仍要运行」并二次确认。
                    仍然记录实际 sha256，便于事后核查。
        """
        core = self._core
        path = core.ocr_manual_path if core else None
        if not path or not os.path.exists(path):
            ok, why = (False, "")
            try:
                ok, why = verify_ocr_installer(path) if path else (False, "未找到 OCR 安装包")
            except Exception:
                pass
            return {"ok": False,
                    "message": why or "未找到 OCR 安装包，可稍后在程序「设置」中安装。"}
        if not force:
            ok, why = verify_ocr_installer(path)
            if not ok:
                return {"ok": False, "untrusted": True,
                        "message": f"{why}\n\n如果你确认这个文件来自可信来源，"
                                   f"可在下方选择「我信任此文件，仍要运行」。"}
        if InstallCore.launch_ocr_installer(path, force=bool(force)):
            return {"ok": True, "message": ""}
        return {"ok": False, "message": f"无法启动 OCR 安装程序：\n{path}"}

    def finish(self, launch=False):
        dest = self._core.dest if self._core else self._default_dir
        if launch:
            exe = os.path.join(dest, APP_EXE)
            if os.path.exists(exe):
                try:
                    # 用 os.startfile 而不是 Popen(shell=True)：shell=True 会先起 cmd.exe，
                    # 在 console=False 的打包程序里必然闪一下黑色控制台窗口
                    os.startfile(exe)   # noqa: S606 - 本地固定路径
                except Exception as e:
                    log(f"启动程序失败: {e}")
        self._destroy()
        return {"ok": True}

    def minimize_window(self):
        try:
            self._window.minimize()
        except Exception:
            pass
        return {"ok": True}

    def close_window(self):
        self._destroy()
        return {"ok": True}

    def ui_ready(self):
        """界面脚本就绪：确保窗口可见"""
        self._show()
        return {"ok": True}

    # ══════ 窗口 ══════
    def _show(self):
        if self._shown:
            return
        self._shown = True
        try:
            self._window.show()
        except Exception:
            pass
        # 窗口出现后设置任务栏 / Alt-Tab 图标（无边框窗口没有原生标题栏图标）
        set_window_icon(self._title, resource_path(LOGO_ICO))

    def _destroy(self):
        try:
            self._window.destroy()
        except Exception:
            pass

    def run(self):
        """启动界面；返回 False 表示需要退化到基础界面"""
        html = resource_path(UI_HTML)
        if not html:
            log(f"未找到界面文件 {UI_HTML}")
            return False
        try:
            import webview
        except ImportError:
            log("未安装 pywebview，改用基础界面")
            return False
        # 用 file:// URI 直接加载本地页面：不启动内置 HTTP 服务，相对资源（视频/徽标）照常可用
        try:
            page_url = Path(html).as_uri()
        except Exception:
            page_url = html
        debug_hash = os.environ.get("CSF_INSTALLER_UI_PREVIEW", "").strip()
        if debug_hash:
            page_url += "?preview=1#" + debug_hash
        # 独立的任务栏分组（调试运行时不被并入 python.exe）
        set_app_user_model_id()
        try:
            self._window = webview.create_window(
                self._title,
                url=page_url,
                js_api=self,
                width=WINDOW_WIDTH,
                height=WINDOW_HEIGHT,
                min_size=(WINDOW_WIDTH, WINDOW_HEIGHT),
                resizable=False,
                frameless=True,
                easy_drag=False,
                shadow=True,
                background_color="#05070b",
                text_select=True,
                zoomable=False,
                hidden=True,
            )
            self._window.events.loaded += self._show
            # 兜底：即使 loaded 事件未触发，也要把窗口显示出来
            timer = threading.Timer(6.0, self._show)
            timer.daemon = True
            timer.start()
            webview.start(gui="edgechromium", icon=resource_path(LOGO_ICO), private_mode=True)
            return True
        except Exception as e:
            log(f"WebView2 界面启动失败（改用基础界面）: {e}")
            self._destroy()
            return False


# ── 兜底界面（无 WebView2 时使用）────────────────────
class FallbackUI:
    """基础 Tk 界面：按默认设置直接安装，保证任何环境下都能完成安装"""

    def __init__(self, zip_path):
        self._zip = zip_path

    def run(self):
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title(f"{APP_NAME} v{APP_VERSION} 安装程序")
        root.geometry("520x280")
        root.resizable(False, False)
        root.configure(bg="#0f172a")
        try:
            ico = resource_path(LOGO_ICO)
            if ico:
                root.iconbitmap(ico)
        except Exception:
            pass
        try:
            root.eval('tk::PlaceWindow . center')
        except Exception:
            pass

        tk.Label(root, text=f"正在安装 {APP_NAME}", bg="#0f172a", fg="#f1f5f9",
                 font=("Microsoft YaHei UI", 15, "bold")).pack(pady=(26, 6))
        tk.Label(root, text="当前环境不支持内嵌浏览器界面，将以默认设置安装。",
                 bg="#0f172a", fg="#94a3b8", font=("Microsoft YaHei UI", 10)).pack()
        status = tk.StringVar(value="准备安装…")
        tk.Label(root, textvariable=status, bg="#0f172a", fg="#cbd5e1",
                 font=("Microsoft YaHei UI", 10)).pack(pady=(18, 6))
        bar = ttk.Progressbar(root, orient="horizontal", length=420, mode="determinate", maximum=100)
        bar.pack(pady=6)
        log_var = tk.StringVar(value="")
        tk.Label(root, textvariable=log_var, bg="#0f172a", fg="#64748b",
                 font=("Microsoft YaHei UI", 9), wraplength=460, justify="left").pack(pady=(14, 0))

        state = {"core": None}

        def worker(dest):
            try:
                os.makedirs(dest, exist_ok=True)
            except Exception as e:
                state["core"] = None
                root.after(0, lambda: (status.set(f"无法写入 {dest}：{e}"),
                                       log_var.set("请以管理员身份重新运行安装程序。")))
                return
            core = InstallCore(self._zip, dest, {
                "desktopShortcut": True, "contextMenu": True, "installOcr": False,
                "autoTheme": True, "ocrDefault": True, "autoLookup": True,
                "splitDefault": True, "pdfQuality": "balanced",
            })
            state["core"] = core
            core.run()

        dest = InstallCore.find_existing_install() or DEFAULT_INSTALL_DIR
        root.after(120, lambda: threading.Thread(target=worker, args=(dest,), daemon=True).start())

        cursor = {"n": 0}

        def tick():
            core = state["core"]
            if core is None:
                root.after(200, tick)
                return
            snap = core.snapshot(cursor["n"])
            cursor["n"] = snap["cursor"]
            bar["value"] = snap["progress"] * 100
            status.set(snap["status"])
            if snap["logs"]:
                log_var.set(snap["logs"][-1])
            if snap["done"]:
                if snap["error"]:
                    status.set("安装失败")
                    log_var.set(snap["error"].splitlines()[0])
                else:
                    bar["value"] = 100
                    status.set("安装完成")
                    log_var.set(f"已安装到 {dest}")
                    btn.configure(text="完成", command=root.destroy)
                return
            root.after(200, tick)

        btn = tk.Button(root, text="安装中，请稍候…", state="disabled", width=16,
                        bg="#2563eb", fg="#ffffff", activebackground="#1d4ed8",
                        relief="flat", font=("Microsoft YaHei UI", 10), command=root.destroy)
        btn.pack(pady=16)
        root.after(200, tick)
        root.mainloop()
        return True


# ── 入口 ──────────────────────────────────────────────
def main():
    if not SKIP_ELEVATE and not is_admin() and not ELEVATED:
        elevate_self()

    if not os.path.exists(ZIP_PATH):
        message_box(f"安装包不存在：\n{ZIP_PATH}\n\n请将安装包（Standard Processing System*.zip）"
                    f"放在安装程序同目录下。", f"{APP_NAME} 安装程序")
        sys.exit(1)

    log(f"安装包: {ZIP_PATH}")
    if not InstallerUI(ZIP_PATH).run():
        log("使用基础安装界面")
        FallbackUI(ZIP_PATH).run()


if __name__ == "__main__":
    main()
