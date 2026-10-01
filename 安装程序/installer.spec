# -*- mode: python ; coding: utf-8 -*-
# 单文件打包模式：输出一个独立的 exe
# 界面：pywebview（系统 WebView2 内核）渲染 installer_ui.html

import os
import re

import webview  # 用于定位 pywebview 自带的 js / lib 资源
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
    StringStruct, VarFileInfo, VarStruct,
)

_WEBVIEW_DIR = os.path.dirname(webview.__file__)

# ── 版本信息（动态生成，单一来源：项目根 version.txt）──
# 升级版本时只需改 version.txt，无需改动下面的字段，也不会有第二个源头。
# 同样把 version.txt 打进包里：安装器运行时的 _read_version() 就是从这里读的
# （否则打包后只能回退到兜底版本号，并打印告警）。
_SPEC_DIR = globals().get('SPECPATH') or os.getcwd()
_PROJECT_ROOT = os.path.abspath(os.path.join(_SPEC_DIR, os.pardir))
_VERSION_TXT = os.path.join(_PROJECT_ROOT, 'version.txt')


def _read_app_version(default='2.0.0'):
    # 1) 项目根 version.txt（唯一来源）
    try:
        with open(_VERSION_TXT, encoding='utf-8-sig') as f:
            v = f.read().strip()
        if v:
            return v
    except Exception:
        pass
    # 2) 兜底：installer.py 里的字面量（若将来改回硬编码）
    try:
        with open(os.path.join(_SPEC_DIR, 'installer.py'), encoding='utf-8') as f:
            m = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        return m.group(1) if m else default
    except Exception:
        return default


APP_VERSION = _read_app_version()
print(f'[spec] 版本号来源 {_VERSION_TXT} -> {APP_VERSION}')

# 安装包 ZIP 名跟随版本号：名字与版本不一致时**立刻中止打包**，避免打出一个
# 版本号对不上的安装器（审计 0-2：ZIP 版本一致性）
_ZIP_NAME = f'Standard Processing SystemV{APP_VERSION}.zip'
if not os.path.isfile(os.path.join(_SPEC_DIR, _ZIP_NAME)):
    _found = sorted(f for f in os.listdir(_SPEC_DIR)
                    if f.startswith('Standard Processing System') and f.endswith('.zip'))
    raise SystemExit(
        f'[spec] 找不到本版本的安装包：{_ZIP_NAME}\n'
        f'       {_SPEC_DIR} 现有：{_found}\n'
        f'       请先把应用打包成 Standard Processing SystemV{APP_VERSION}.zip，'
        f'或核对 version.txt 与包内版本是否一致。')
_VER_NUMS = [int(x) for x in re.findall(r'\d+', APP_VERSION)] or [1, 0, 0]
_VER_TUPLE = tuple((_VER_NUMS + [0, 0, 0, 0])[:4])  # 补齐/截断为 4 段

version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=_VER_TUPLE,
        prodvers=_VER_TUPLE,
        mask=0x3f, flags=0x0, OS=0x40004,
        fileType=0x1, subtype=0x0, date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable(
                '080404b0',
                [
                    StringStruct('CompanyName', 'The Forever CSF'),
                    StringStruct('FileDescription', '标准处理系统 安装程序'),
                    StringStruct('FileVersion', APP_VERSION),
                    StringStruct('ProductName', '标准处理系统'),
                    StringStruct('ProductVersion', APP_VERSION),
                    StringStruct('LegalCopyright', 'Copyright (c) 2026 The Forever CSF'),
                    StringStruct('OriginalFilename', '标准处理系统_安装程序.exe'),
                ]
            ),
        ]),
        VarFileInfo([VarStruct('Translation', [2052, 1200])]),
    ],
)


a = Analysis(
    ['installer.py'],
    # pathex 加项目根：安装器与主程序共用 utils/ocr_verify.py（同一份 OCR 校验实现）
    pathex=['.', _PROJECT_ROOT],
    binaries=[],
    # 安装包 ZIP + 界面资源（HTML / 开场视频 / 徽标 / 图标 / 协议图片）
    # + version.txt（版本号单一来源，运行时要读）
    # 以及 pywebview 的 js、lib（内含 WebView2 托管程序集）
    datas=[
        (_ZIP_NAME, '.'),
        (_VERSION_TXT, '.'),
        # 第三方组件许可声明（法务要求随发行包分发）
        ('THIRD-PARTY-NOTICES.txt', '.'),
        ('licenses', 'licenses'),
        ('installer_ui.html', '.'),
        ('The Forever CSF.mp4', '.'),
        ('CSF徽标.png', '.'),
        ('logo.ico', '.'),
        ('logo_标准处理系统.png', '.'),
        # 《许可、隐私与免责说明》：图片版（弹层里逐页显示）+ PDF 版（"打开 PDF 原版"，
        # 安装包漏带 PDF 时作为安装器自带回退；两层名字都与内容一致）
        ('标准处理系统 许可隐私与免责说明', '标准处理系统 许可隐私与免责说明'),
        ('标准处理系统 许可、隐私与免责说明.pdf', '.'),
        (os.path.join(_WEBVIEW_DIR, 'js'), 'webview/js'),
        (os.path.join(_WEBVIEW_DIR, 'lib'), 'webview/lib'),
    ],
    hiddenimports=[
        'win32com.client',
        'PIL', 'PIL.Image',
        # OCR 安装包校验：与主程序共用的模块，必须显式声明（installer.py 里是 try/except 导入）
        'utils.ocr_verify',
        # pywebview 的 Windows 后端：由 guilib 动态导入，需显式声明
        'webview.platforms.winforms',
        'webview.platforms.edgechromium',
        # WebView2 宿主依赖 .NET（pythonnet / clr）
        'clr', 'clr_loader', 'pythonnet',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PyQt6', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineCore',
        'torch', 'torchvision', 'scipy', 'sklearn',
        'matplotlib', 'notebook', 'jupyter', 'pytest',
        'customtkinter', 'cv2',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='标准处理系统_安装程序',
    version=version_info,
    icon='logo.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
