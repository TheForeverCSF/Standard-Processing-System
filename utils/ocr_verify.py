# -*- coding: utf-8 -*-
# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""OCR（Tesseract）安装包校验 —— 安装程序与主程序**共用同一份实现**。

为什么要共用：

* Tesseract 安装包会以**管理员权限**启动（`os.startfile` / ShellExecuteW runas），
  是一个提权执行点。只要往程序目录丢一个同名 exe，就能借安装程序/主程序拿到管理员权限。
* 原先只有安装器侧做了哈希白名单，而主程序「设置 → 稍后安装 OCR」这条路径
  直接 `os.startfile(固定文件名)` —— 白名单被完全绕开，且是第二个提权执行点。
* 两处各写一份校验必然漂移，所以统一放在这里：只依赖标准库，安装器与主程序都能导入。

白名单维护方式（更新 OCR 包时）：
    PowerShell: Get-FileHash .\\tesseract-ocr-w64-setup-*.exe -Algorithm SHA256
    再把哈希加进 OCR_PINNED_SHA256，或写进 ocr_installers.json（便于不改代码追加）：
        {"installers": [{"name": "tesseract-ocr-w64-setup-5.5.0.exe", "sha256": "..."}]}
"""
import glob
import hashlib
import json
import os
import sys

# OCR 安装包文件名随版本变化，用通配匹配，避免写死版本号后失效
OCR_EXE_GLOB = "tesseract-ocr-*.exe"

# 最小体积：完整安装器约 20MB；发布包里 _internal 下的同名副本实测只有 123KB（残缺），
# 选到它会让"安装 OCR"必然失败，而界面只会报"装不上"，用户无从判断
MIN_OCR_INSTALLER_BYTES = 5 * 1024 * 1024

# 允许的哈希（内置常量，离线可用、不依赖 Authenticode 签名：
# 官方 Tesseract 的 Windows 构建未必带有效签名，强制验签会把正常用户的包也拦掉）
OCR_PINNED_SHA256 = {
    # tesseract-ocr-w64-setup-5.5.0.20241111.exe（21,381,872 字节）
    "f3fc4236425b690c8be756f35793f77394ee004be0a6460a440c754d892f68bc",
}
OCR_PINS_JSON = "ocr_installers.json"


def sha256_file(path, chunk=1 << 20) -> str:
    """计算文件 SHA-256（分块读取，20MB+ 的安装包也不占内存）"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def default_pin_dirs():
    """候选清单目录：模块所在目录、程序目录（exe 目录 / 项目根）、打包内置目录"""
    dirs = []
    try:
        dirs.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        try:
            dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
        except Exception:
            pass
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            dirs.append(mei)
    else:
        try:
            dirs.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        except Exception:
            pass
    out, seen = [], set()
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def allowed_ocr_hashes(pins_dirs=None) -> set:
    """允许的哈希集合 = 内置常量 ∪ 候选目录下的 ocr_installers.json（便于日后追加版本）"""
    allowed = {str(h).strip().lower() for h in OCR_PINNED_SHA256 if h}
    for d in (pins_dirs or default_pin_dirs()):
        try:
            p = os.path.join(d, OCR_PINS_JSON)
            if not os.path.isfile(p):
                continue
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            for item in (data.get("installers") or []):
                h = str((item or {}).get("sha256", "")).strip().lower()
                if len(h) == 64:
                    allowed.add(h)
        except Exception:
            continue
    return allowed


def verify_ocr_installer(path, pins_dirs=None):
    """校验 OCR 安装包是否可信，返回 (ok, 说明)。

    校验不过一律 **fail-closed**（拒绝以管理员权限运行），调用方必须把 why 原样告知用户，
    不要用"可稍后在设置中安装"之类的安慰话掩盖真因。
    """
    if not path or not os.path.isfile(path):
        return False, "OCR 安装包不存在"
    if not is_size_ok(path):
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        return False, (f"安装包体积异常（{size} 字节，远小于完整的 Tesseract 安装器）。"
                       f"该文件可能是残缺副本，请从官方渠道重新获取。")
    try:
        digest = sha256_file(path)
    except Exception as e:
        return False, f"无法读取 OCR 安装包：{e}"
    if digest.lower() in allowed_ocr_hashes(pins_dirs):
        return True, f"sha256={digest[:12]}…"
    return False, (f"安装包内容与预期不符（sha256={digest[:16]}…）。"
                   f"为安全起见已拒绝以管理员身份运行，请从官方渠道重新获取安装包。")


def is_size_ok(path) -> bool:
    """体积校验：小于 MIN_OCR_INSTALLER_BYTES 的一律视为残缺文件"""
    try:
        return os.path.getsize(path) >= MIN_OCR_INSTALLER_BYTES
    except OSError:
        return False


def find_ocr_installer(dirs):
    """在给定目录里定位可用的 OCR 安装包（优先 w64 安装包，其次任意体积达标者）。

    版本无关：文件名随 Tesseract 版本变化，所以用通配 + 体积筛选，
    避免写死 `tesseract-ocr-w64-setup-5.5.0.20241111.exe` 后升级即失效。
    """
    for d in (dirs or []):
        try:
            matches = sorted(glob.glob(os.path.join(d, OCR_EXE_GLOB)))
        except Exception:
            continue
        for m in matches:
            if is_size_ok(m) and os.path.basename(m).lower().startswith("tesseract-ocr-w64-setup"):
                return m
        for m in matches:
            if is_size_ok(m):
                return m
    return None
