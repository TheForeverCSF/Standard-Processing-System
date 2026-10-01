# -*- mode: python ; coding: utf-8 -*-
# 优化版本：strip=True + optimize=2 + 排除无关 Qt 模块 + 精简打包

import os
import re
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
    StringStruct, VarFileInfo, VarStruct,
)

# ── 版本信息（动态生成，单一来源：根目录 version.txt）──────
# 升级版本时只需修改 version.txt 的内容，无需改动下面的字段
_SPEC_DIR = globals().get('SPECPATH') or os.getcwd()


def _read_version(default='1.2.0'):
    try:
        with open(os.path.join(_SPEC_DIR, 'version.txt'), encoding='utf-8') as f:
            v = f.read().strip()
        return v or default
    except Exception:
        return default


APP_VERSION = _read_version()
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
                    StringStruct('FileDescription', '标准处理系统'),
                    StringStruct('FileVersion', APP_VERSION),
                    StringStruct('ProductName', '标准处理系统'),
                    StringStruct('ProductVersion', APP_VERSION),
                    StringStruct('LegalCopyright', 'Copyright (c) 2026 The Forever CSF'),
                    StringStruct('OriginalFilename', '标准处理系统.exe'),
                ]
            ),
        ]),
        VarFileInfo([VarStruct('Translation', [2052, 1200])]),
    ],
)


# ── 数据文件清单（随包分发的资源）──────────────────────────
_datas = [
    ('web', 'web'),
    ('logo.ico', '.'),
    ('logo_标准处理系统.png', '.'),
    ('CSF徽标.png', '.'),
    ('tesseract-ocr-w64-setup-5.5.0.20241111.exe', '.'),
    ('builtin_prefixes.json', '.'),
    ('更新内容.txt', '.'),
    ('qt.conf', '.'),
    ('version.txt', '.'),
    # 许可相关（法务要求随发行包分发；界面里的"许可证原文"就是从这里读的）
    # 注意：PyInstaller 6 的 onedir 布局会把 datas 放进 _internal/，
    # 而应用侧会同时查「exe 目录」与「_internal」两处（见 web/server.py 的
    # /api/license.txt），所以放这里就不会出现"打包后读不到 LICENSE"。
    ('LICENSE', '.'),
    # 第三方许可声明在 安装程序/ 目录下（与打包脚本同源），必须用相对路径引用；
    # 写成根目录名会让 PyInstaller 直接报 "Unable to find" 而中止打包。
    (os.path.join('安装程序', 'THIRD-PARTY-NOTICES.txt'), '.'),
]

# ── 协议 / 使用说明文件 ────────────────────────────────────
# 优先使用新版《许可、隐私与免责说明》，回退旧版《用户协议》。
#
# 【如何切换到新版】把《标准处理系统_许可隐私与免责说明_v3.0.md》转成 PDF，
#   以 _AGREEMENT_PREFERRED 的名字放到项目根目录即可，无需修改本文件。
#   旧版《用户协议》内容与本系统实现存在多处不符，切换后请不要继续随包分发。
_AGREEMENT_PREFERRED = '标准处理系统 许可、隐私与免责说明.pdf'
_AGREEMENT_LEGACY = '标准处理系统 用户协议.pdf'

_agreement = next((f for f in (_AGREEMENT_PREFERRED, _AGREEMENT_LEGACY)
                   if os.path.exists(os.path.join(_SPEC_DIR, f))), None)
if _agreement is None:
    print('!!! 警告：未找到协议文件 —— 打出的安装包将不含协议，安装程序无法展示。')
elif _agreement == _AGREEMENT_LEGACY:
    print('!!! 警告：仍在打包【旧版】《标准处理系统 用户协议》。')
    print('          该版本已被《许可、隐私与免责说明》取代，且内容与实现不符，不应继续分发。')
    print('          请将新版转为 PDF 后以 %r 为名放入项目根目录。' % _AGREEMENT_PREFERRED)
else:
    print('[打包] 协议文件：%s' % _agreement)
if _agreement:
    _datas.append((_agreement, '.'))


a = Analysis(
    ['main_browser.py'],
    pathex=['.'],
    binaries=[],
    # ⚠ 注意：**不打包 标准名称字典.db** —— 字典与设置是用户自己积累/配置的数据，
    # 随包分发会覆盖或污染老用户的字典（用户明确要求不打包）。
    # 程序侧建库用的是 CREATE TABLE IF NOT EXISTS（utils/helpers.py:373-380、
    # core/import_handler.py:487-498），所以新装用户首次运行会自动生成空库，
    # 位置在 %LOCALAPPDATA%\标准处理系统\（见 helpers.user_data_dir()）。
    datas=_datas,
    # paddleocr / paddlepaddle 仅在 try/except 中动态导入，无需打包
    # pytesseract 也是动态导入但体积小，保留以支持 Tesseract 路径
    hiddenimports=['PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineCore', 'win32com.client', 'tkinter', 'csv', 'pytesseract', 'requests', 'openpyxl', 'win10toast',
    # OCR 安装包校验（与安装程序共用的模块，import 写在函数体内，显式声明更稳妥）
    'utils.ocr_verify',
                   # HTTPS/SSL 相关：确保联网查询、反馈等依赖 SSL 的功能可用
                   'ssl', '_ssl', '_hashlib', '_socket', 'socket', 'select',
                   'http.client', 'urllib.request', 'urllib3', 'charset_normalizer', 'idna']
    # 反馈凭据的私有模块（_feedback_private.py）：仅在构建机存在时才打包。
    # 公开源码仓库不含该文件，从公开源码构建时本项为空列表，构建照常成功；
    # 此时反馈功能会引导用户改用邮箱（见 core/security_config.py）。
    + (['_feedback_private']
       if os.path.exists(os.path.join(_SPEC_DIR, '_feedback_private.py')) else []),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ---- pygame / SDL2 全家桶 ----
        # 与本程序业务无关（游戏与多媒体库），全库无任何 import pygame；
        # 且 pygame 为 LGPL-2.1，会额外带来"允许使用者替换该库"的合规义务。
        # 曾因构建环境残留被误打包进发行版（_internal/pygame + SDL2*.dll）。
        'pygame',
        # ---- PaddleOCR 全家桶（动态导入，不打包） ----
        'paddle', 'paddleocr', 'paddlepaddle', 'paddle2onnx',
        # ---- PaddlePaddle 的庞大依赖 ----
        'onnx', 'onnxruntime',
        # ---- 用户不需要的重型科学计算包 ----
        'torch', 'torchvision', 'torchaudio',
        'scipy',
        'sklearn', 'sklearn.metrics', 'sklearn.tree',
        'pandas',
        'cv2',
        'matplotlib', 'PIL.ImageShow', 'PIL.ImageQt',
        # ---- AI/ML 框架 ----
        'transformers', 'modelscope', 'datasets',
        'sentencepiece', 'tokenizers', 'accelerate',
        'tensorboard', 'tensorflow',
        # ---- 用户不需要的开发/测试工具 ----
        'notebook', 'jupyter', 'jupyter_client', 'ipykernel',
        'pytest', 'nose',
        'sympy', 'networkx', 'numba',
        # ---- PyInstaller 误扫的无关模块 ----
        'tkinter.test', 'tkinter.tix', 'turtledemo',
        # ---- 文档处理无关的 ----
        'shapely', 'shapely.geometry',
        'rarfile', 'py7zr', 'patool',
        # ---- 本系统不使用的 Qt 模块（可安全排除，WebEngine 不依赖它们） ----
        'PyQt6.QtMultimedia',
        'PyQt6.QtMultimediaWidgets',
        'PyQt6.QtSvg',
        'PyQt6.QtSvgWidgets',
        'PyQt6.QtSql',
        'PyQt6.QtTest',
        'PyQt6.QtBluetooth',
        'PyQt6.QtNfc',
        'PyQt6.QtPositioning',
        'PyQt6.QtSensors',
        'PyQt6.QtCharts',
        'PyQt6.QtDataVisualization',
        'PyQt6.QtQuick3D',
        'PyQt6.QtRemoteObjects',
        'PyQt6.QtScxml',
        'PyQt6.QtHttpServer',
        'PyQt6.QtSpatialAudio',
        # 保留 WebEngine 必需的：QtQml, QtQuick, QtWebChannel, QtNetwork
    ],
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='标准处理系统',
    version=version_info,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,          # 关闭 strip：strip 会损坏 MSVC 编译的 _ssl.pyd，
                          # 导致打包后出现 "SSL module is not available"（联网查询/反馈失效）
    upx=False,            # 当前环境无法获取 UPX，用后期 NTFS 压缩替代
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['logo.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,          # 同上：不再对 dll/pyd 去符号，保证 _ssl.pyd 等二进制可正常加载
    upx=False,
    upx_exclude=[],
    name='标准处理系统',
)
