# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""涉密模式安全配置模块 + 应用层网络防火墙

控制系统中所有可能的数据外泄通道，并提供 Python 层面的网络监控与阻断。
涉密模式通过环境变量 CLASSIFIED_MODE=true 或配置文件启用。

防火墙原理：
  在不依赖系统防火墙规则的前提下，通过 Monkey Patch Python 的 socket.connect /
  socket.create_connection，在进程内部拦截所有出站 TCP 连接。
  只放行 127.0.0.1 / localhost / ::1 的本机通信，阻断所有到外网的连接。
  所有被阻断的尝试会被记录，可通过 query_firewall_log() 查询。
"""

import os
import re
import sys
import json
import time
import socket as _socket
import threading
import traceback
from typing import List, Dict, Optional

# ── 全局状态 ──────────────────────────────────────────────
_classified_mode = None  # None=未初始化, True/False
_init_lock = threading.Lock()


# ── PowerShell 调用辅助（隐藏控制台窗口）─────────────────
def _ps_hidden_kwargs() -> dict:
    """返回隐藏子进程控制台窗口所需的 subprocess 参数。

    本程序以 console=False 打包运行，调用 powershell.exe 时若不隐藏，
    会弹出大量黑色控制台窗口（例如"安全"页逐进程查询防火墙规则时）。
    """
    import subprocess
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


def _user_data_dir() -> str:
    """返回用户数据目录（可写）。

    统一走 utils.helpers.user_data_dir()：%LOCALAPPDATA%\\标准处理系统，
    并自带旧数据一次性迁移。安装目录在 %ProgramFiles% 时普通用户不可写，
    以前把 .classified_pending.json / firewall_managed.json 写在那里会静默失效。
    """
    try:
        from utils.helpers import user_data_dir as _udd
        return _udd()
    except Exception:
        # 极端情况下（导入路径未就绪）退回旧逻辑，保证功能不中断
        if hasattr(sys, '_MEIPASS'):
            return os.path.dirname(sys.executable)
        return os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".."
        ))


def _pending_flag_path() -> str:
    """返回涉密提权标志文件路径（位于用户数据目录）"""
    return os.path.normpath(os.path.join(_user_data_dir(), ".classified_pending.json"))


def is_classified() -> bool:
    """判断当前是否运行在涉密模式。

    检测顺序：
      1. 环境变量 CLASSIFIED_MODE=true
      2. 环境变量 SECURITY_LEVEL=classified（或 top_secret）
      3. 环境变量 AIR_GAPPED=true
    """
    global _classified_mode
    if _classified_mode is not None:
        return _classified_mode

    with _init_lock:
        if _classified_mode is not None:
            return _classified_mode

        env_val = os.environ.get("CLASSIFIED_MODE", "").strip().lower()
        if env_val in ("1", "true", "yes"):
            _classified_mode = True
            return True

        sec_level = os.environ.get("SECURITY_LEVEL", "").strip().lower()
        if sec_level in ("classified", "top_secret", "secret"):
            _classified_mode = True
            return True

        air_gapped = os.environ.get("AIR_GAPPED", "").strip().lower()
        if air_gapped in ("1", "true", "yes"):
            _classified_mode = True
            return True

        _classified_mode = False
        return False


def set_classified_mode(enabled: bool):
    """运行时动态设置涉密模式（用于设置界面切换）"""
    global _classified_mode
    with _init_lock:
        _classified_mode = enabled


# ── 功能开关检查 ──────────────────────────────────────────

def allow_network_lookup() -> bool:
    """是否允许联网查询标准名称"""
    return not is_classified()


def allow_feedback_webhook() -> bool:
    """是否允许发送钉钉 Webhook 反馈"""
    return not is_classified()


def allow_ai_enhance() -> bool:
    """是否允许 AI 增强（含本地 AI 调用）"""
    return not is_classified()


def allow_outbound_http() -> bool:
    """是否允许任何出站 HTTP 请求"""
    return not is_classified()


# ── 日志清理 ──────────────────────────────────────────────

# 敏感路径模式：盘符路径 + UNC 路径 + POSIX 路径
# 复审批 3-9：原来只匹配 `C:\...`，涉密模式下脱敏后仍会残留：
#   · UNC   ：\\fileserver\共享\机密.docx
#   · POSIX ：/home/user/secret.pdf、/mnt/c/Users/x/…
# 这些都是同等敏感的位置信息，必须一并屏蔽。
# POSIX 分支用 (?<![\w:/]) 前置断言：避免把 URL 里的路径段
# （http://host/api/file）当成文件路径反复屏蔽，只匹配"独立出现"的路径。
_PATH_PATTERN = re.compile(
    r'[a-zA-Z]:\\(?:[^\\/:*?"<>|\r\n]+\\)*[^\\/:*?"<>|\r\n]*'
    r'|\\\\[^\s\\/]+\\[^\s\\/:*?"<>|\r\n]+(?:\\[^\s\\/:*?"<>|\r\n]+)*'
    r'|(?<![\w:/])/(?:[^\s\\/:*?"<>|\r\n]+/)+[^\s\\/:*?"<>|\r\n]*'
)


def sanitize_log(msg: str) -> str:
    """清理日志中的敏感信息：
    - 文件路径 → [路径已屏蔽]
    - 文件列表 → [文件列表已屏蔽]
    - 标准编号内容过长 → 截断
    """
    if not is_classified():
        return msg

    # 屏蔽所有磁盘路径
    msg = _PATH_PATTERN.sub("[路径已屏蔽]", msg)

    # 屏蔽文件列表
    msg = re.sub(
        r'\bfiles["\']?\s*[:=]\s*\[.*?\]',
        '"files": ["[文件列表已屏蔽]"]',
        msg,
        count=1,
        flags=re.DOTALL,
    )

    # 屏蔽过长的 content 字段
    msg = re.sub(
        r'("content"\s*:\s*")[^"]{200,}(")',
        lambda m: m.group(1) + "[内容过长已截断]" + m.group(2),
        msg,
    )

    return msg


# ── 钉钉凭据管理 ──────────────────────────────────────────

# 构建期注入的私有凭据。
#
# 【重要】公开源码仓库 **不包含** 任何 Webhook 凭据（见 .gitignore）。
# 构建者若需要反馈功能，在项目根目录自行创建 _feedback_private.py：
#
#     FEEDBACK_DEFAULT_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=..."
#     FEEDBACK_DEFAULT_SECRET  = "SEC..."
#
# 该文件不会进入版本库。从公开源码构建的副本因缺少它，
# 反馈功能会引导用户改用邮箱（FEEDBACK_FALLBACK_EMAIL）。
def _load_private_feedback_credentials() -> tuple:
    try:
        from _feedback_private import (  # type: ignore
            FEEDBACK_DEFAULT_WEBHOOK as _w,
            FEEDBACK_DEFAULT_SECRET as _s,
        )
        return (_w or "").strip(), (_s or "").strip()
    except Exception:
        # 未注入私有凭据：属正常情况（例如使用者从公开源码自行构建）
        return "", ""


_private_feedback_webhook, _private_feedback_secret = _load_private_feedback_credentials()

# 反馈通道不可用时的替代联系方式（与《许可、隐私与免责说明》第三十九条一致）
FEEDBACK_FALLBACK_EMAIL = "the_forever_csf@126.com"


def get_feedback_credentials() -> tuple:
    """获取钉钉 Webhook 凭据。

    优先级：
      1. 环境变量 DINGTALK_WEBHOOK_URL / DINGTALK_WEBHOOK_SECRET
      2. 构建期注入的私有模块 _feedback_private.py（公开源码中不存在）

    两者均未配置时返回 (None, None)；涉密模式下始终返回 (None, None)。

    环境变量：
      DINGTALK_WEBHOOK_URL  — 完整 Webhook URL（含 access_token）
      DINGTALK_WEBHOOK_SECRET — 签名 Secret
    """
    if not allow_feedback_webhook():
        return None, None

    webhook = os.environ.get("DINGTALK_WEBHOOK_URL", "").strip()
    secret = os.environ.get("DINGTALK_WEBHOOK_SECRET", "").strip()

    if webhook and secret:
        return webhook, secret

    # 回退：构建期注入的私有凭据
    if _private_feedback_webhook and _private_feedback_secret:
        return _private_feedback_webhook, _private_feedback_secret

    # 未配置（例如从公开源码自行构建）—— 不发送
    return None, None


def has_feedback_config() -> bool:
    """检查是否配置了反馈通道（环境变量或构建期注入的私有凭据）"""
    wh, sec = get_feedback_credentials()
    return bool(wh and sec)


# ╔══════════════════════════════════════════════════════════════╗
# ║  应用层网络防火墙（Monkey Patch）                            ║
# ║  原理：拦截 Python socket.connect / create_connection        ║
# ║  适用范围：requests / urllib / http.client / 原始 socket     ║
# ╚══════════════════════════════════════════════════════════════╝

# 防火墙状态
_firewall_active = False
_firewall_log: List[Dict] = []          # 被阻断的连接记录
_firewall_log_lock = threading.Lock()
_firewall_log_max = 500                 # 最多保留的记录数

# 保存原始函数引用
_original_socket_connect = _socket.socket.connect
_original_create_connection = _socket.create_connection

# 允许的本机地址白名单（不阻断）
_LOCAL_ALLOWED = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _is_local_address(address) -> bool:
    """判断地址是否为允许的本机地址"""
    if isinstance(address, tuple):
        host = address[0]
    elif isinstance(address, str):
        host = address
    else:
        return False
    # 去掉 IPv6 前缀
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in _LOCAL_ALLOWED


def _patched_connect(self, address, *args, **kwargs):
    """替换 socket.socket.connect：非本机地址一律阻断并记录"""
    if not _is_local_address(address):
        _record_block(address, "socket.connect")
        raise BlockedConnectionError(f"[防火墙] 已阻断出站连接: {address}")
    return _original_socket_connect(self, address, *args, **kwargs)


def _patched_create_connection(address, *args, **kwargs):
    """替换 socket.create_connection：非本机地址一律阻断并记录"""
    if not _is_local_address(address):
        _record_block(address, "socket.create_connection")
        raise BlockedConnectionError(f"[防火墙] 已阻断出站连接: {address}")
    return _original_create_connection(address, *args, **kwargs)


class BlockedConnectionError(ConnectionError):
    """应用层防火墙阻断的自定义异常"""
    pass


def _record_block(address, source: str):
    """记录一次被阻断的连接尝试"""
    timestamp = time.time()
    # 提取调用栈（跳过内部帧）
    stack = traceback.extract_stack()
    # 过滤掉防火墙自身和 socket 内部帧
    callers = []
    for frame in stack:
        fname = frame.filename.replace("\\", "/")
        if "security_config" in fname and "_record_block" in frame.name:
            continue
        if "security_config" in fname and frame.name.startswith("_patched"):
            continue
        if "socket.py" in fname:
            continue
        callers.append(f"{frame.filename}:{frame.lineno} in {frame.name}")
        if len(callers) >= 3:
            break

    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp)),
        "target": repr(address),
        "source": source,
        "callers": callers,
    }

    with _firewall_log_lock:
        _firewall_log.append(entry)
        if len(_firewall_log) > _firewall_log_max:
            _firewall_log.pop(0)

    # 打印警告到控制台
    caller_str = " <- ".join(reversed(callers))
    print(f"\033[91m[防火墙] ⛔ 阻断出站连接: {address}")
    print(f"         来源: {caller_str}\033[0m")


def install_firewall():
    """安装防火墙：Monkey Patch socket 的出站方法

    仅阻断出站连接（connect / create_connection），
    不干扰本机监听（bind / listen）和本地 HTTP 服务通信。
    可重复调用，不会重复安装。
    """
    global _firewall_active

    if _firewall_active:
        return

    _socket.socket.connect = _patched_connect
    _socket.create_connection = _patched_create_connection
    _firewall_active = True

    print(f"[防火墙] [OK] 已激活，正在监控并阻断所有非本机出站连接")


def remove_firewall():
    """卸载防火墙：恢复原始 socket 方法"""
    global _firewall_active

    if not _firewall_active:
        return

    _socket.socket.connect = _original_socket_connect
    _socket.create_connection = _original_create_connection
    _firewall_active = False

    print(f"[防火墙] ○ 已卸载")


def query_firewall_log(clear: bool = False) -> List[Dict]:
    """查询被防火墙阻断的所有出站连接记录

    Args:
        clear: 是否在查询后清空日志

    Returns:
        阻断记录列表，每项含 time/target/source/callers 字段
    """
    with _firewall_log_lock:
        result = list(_firewall_log)
        if clear:
            _firewall_log.clear()
    return result


def get_firewall_status() -> Dict:
    """获取防火墙状态信息"""
    with _firewall_log_lock:
        blocked_count = len(_firewall_log)

    return {
        "active": _firewall_active,
        "blocked_total": blocked_count,
        # 统计被阻断最多的目标
        "top_targets": _get_top_targets(5),
    }


def _get_top_targets(n: int) -> List[Dict]:
    """获取被阻断最多的前 N 个目标"""
    with _firewall_log_lock:
        counts = {}
        for entry in _firewall_log:
            target = entry["target"]
            counts[target] = counts.get(target, 0) + 1
    sorted_targets = sorted(counts.items(), key=lambda x: -x[1])
    return [{"target": t, "count": c} for t, c in sorted_targets[:n]]


# ╔══════════════════════════════════════════════════════════════╗
# ║  Windows 防火墙规则管理                                     ║
# ║  原理：通过 PowerShell 创建/删除出站阻止规则                 ║
# ║  覆盖进程：主程序 / Tesseract OCR / QtWebEngine / Python    ║
# ║  需要管理员权限（无权限时静默降级为应用层防火墙）            ║
# ╚══════════════════════════════════════════════════════════════╝

# 防火墙规则命名前缀（便于批量清理）
_WF_RULE_PREFIX = "标准处理系统_涉密模式"
# Windows 防火墙规则状态
_wf_rules_active = False
# 本次安装的规则里，有多少条已限定「仅拦外网」（不影响 127.0.0.1 本地界面）
_wf_loopback_safe = 0
_wf_admin_available = None  # None=未检测, True/False


def relaunch_as_admin_cleanup():
    """以管理员身份重新启动本程序，仅用于清理残留防火墙规则。

    与 restart_as_admin() 的区别：**不写入涉密模式标志**（那会让新实例开启涉密模式），
    新实例带 --clean-firewall 参数，启动后清规则并退出。
    """
    import ctypes
    if getattr(sys, 'frozen', False):
        exe = sys.executable
        params = "--clean-firewall"
    else:
        exe = sys.executable
        params = f'"{os.path.abspath(sys.argv[0])}" --clean-firewall'
    print(f"[防火墙] 以管理员身份重启以清理残留规则: {exe} {params}")
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)


def cleanup_firewall_and_report() -> dict:
    """（管理员专用的 --clean-firewall 入口）清理本系统的全部 Windows 防火墙规则"""
    before = detect_residual_rules(ttl=0)
    remove_windows_firewall_rules()
    invalidate_rule_names_cache()
    after = detect_residual_rules(ttl=0)
    return {"before": before.get("count", 0), "after": after.get("count", 0),
            "admin": _check_admin()}


def _check_admin() -> bool:
    """检测当前进程是否有管理员权限"""
    global _wf_admin_available
    if _wf_admin_available is not None:
        return _wf_admin_available
    try:
        import ctypes
        _wf_admin_available = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        _wf_admin_available = False
    return _wf_admin_available


def restart_as_admin():
    """以管理员权限重新启动当前程序（UAC 提权）"""
    import sys as _sys

    # 写入标志文件，告诉新实例启动后自动开启涉密模式
    try:
        flag_path = _pending_flag_path()
        with open(flag_path, "w", encoding="utf-8") as _f:
            import json as _json
            _json.dump({"classified_mode": True}, _f)
    except Exception:
        pass

    # 构造重启命令
    exe = _sys.executable
    if hasattr(_sys, '_MEIPASS'):
        cmd = [exe]
    else:
        script = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "main_browser.py"
        ))
        cmd = [exe, script]

    # 用 runas 提权启动
    try:
        import ctypes as _ctypes
        _ctypes.windll.shell32.ShellExecuteW(
            None, "runas", cmd[0], " ".join(cmd[1:]), None, 1
        )
        # 关闭当前进程
        os._exit(0)
    except Exception as _e:
        print(f"[防火墙] 提权重启失败: {_e}")


def check_classified_pending():
    """检查是否有提权重启后的标志文件，有则自动进入涉密模式并删除标志"""
    flag_path = _pending_flag_path()
    if os.path.exists(flag_path):
        try:
            with open(flag_path, "r", encoding="utf-8") as _f:
                import json as _json
                data = _json.load(_f)
            os.remove(flag_path)
            if data.get("classified_mode"):
                os.environ["CLASSIFIED_MODE"] = "true"
                return True
        except Exception:
            try: os.remove(flag_path)
            except: pass
    return False


def _is_python_interpreter(path: str) -> bool:
    """判断路径是否为 Python 解释器本体（python.exe / pythonw.exe 等）"""
    if not path:
        return False
    name = os.path.basename(str(path)).lower()
    return name in ("python.exe", "pythonw.exe", "python3.exe", "python3w.exe")


def _get_processes_to_block() -> list:
    """返回需要阻止联网的进程路径列表"""
    paths = set()
    import sys as _sys

    # 1. 当前进程（源码运行=python.exe / 打包后=标准处理系统.exe）
    exe = _sys.executable
    if exe:
        paths.add(exe.lower())

    # 2. 打包后主 exe（如果有的话）
    if hasattr(_sys, '_MEIPASS'):
        base = os.path.dirname(_sys.executable)
        main_exe = os.path.join(base, "标准处理系统.exe")
        if os.path.exists(main_exe):
            paths.add(main_exe.lower())
        web_exe = os.path.join(base, "标准处理系统_web.exe")
        if os.path.exists(web_exe):
            paths.add(web_exe.lower())

    # 3. Tesseract OCR 引擎
    for tess_path in [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]:
        if os.path.exists(tess_path):
            paths.add(tess_path.lower())

    # 4. QtWebEngineProcess（Chromium 子进程，负责 PDF.js 渲染和网络）
    #    查找 PyQt6 安装目录下的 QtWebEngineProcess
    try:
        import PyQt6.QtCore as _qtc
        qt_dir = os.path.dirname(os.path.dirname(_qtc.__file__))
        webengine_candidates = [
            os.path.join(qt_dir, "Qt6", "bin", "QtWebEngineProcess.exe"),
            os.path.join(qt_dir, "bin", "QtWebEngineProcess.exe"),
            os.path.join(os.path.dirname(_sys.executable), "QtWebEngineProcess.exe"),
        ]
        for wp in webengine_candidates:
            if os.path.exists(wp):
                paths.add(wp.lower())
    except Exception:
        pass

    return list(paths)


def _build_rule_name(suffix: str) -> str:
    """生成防火墙规则名称"""
    return f"{_WF_RULE_PREFIX}_{suffix}"


def install_windows_firewall_rules() -> tuple:
    """安装 Windows 防火墙出站阻止规则

    Returns:
        (成功数, 失败数, 是否拥有管理员权限)
    """
    global _wf_rules_active, _wf_loopback_safe

    if _wf_rules_active:
        return (0, 0, _check_admin())

    if not _check_admin():
        print("[防火墙] [注意] 无管理员权限，跳过 Windows 防火墙规则安装，使用应用层防火墙")
        return (0, 0, False)

    processes = _get_processes_to_block()

    # ⚠ 关键安全策略：源码运行（未打包为 exe）时，不要把 Python 解释器本身
    # 加入 Windows 防火墙规则。
    # 原因：本程序以 "python.exe main_browser.py" 方式运行，若按 python.exe
    # 建出站阻止规则，会连带阻断该解释器运行的**所有其他 Python 程序**
    # （用户机器上的其它 Python 应用/服务），属于严重误伤。
    # 本进程的网络拦截已由应用层防火墙（socket 拦截）精确覆盖，无需 Windows 规则。
    # 打包运行（sys.executable 即本系统 exe）时不存在该问题，正常阻断本 exe。
    _frozen = hasattr(sys, '_MEIPASS')
    if not _frozen:
        _filtered = [p for p in processes if not _is_python_interpreter(p)]
        _skipped = [os.path.basename(p) for p in processes if _is_python_interpreter(p)]
        if _skipped:
            print(f"[防火墙] 源码运行模式：跳过 Windows 规则 {_skipped}"
                  f"（避免影响其它 Python 程序，本进程由应用层防火墙拦截）")
        processes = _filtered

    success = 0
    failed = 0

    for proc_path in processes:
        proc_name = os.path.basename(proc_path)
        rule_name = _build_rule_name(proc_name.replace(".", "_"))
        _base = (
            f'New-NetFirewallRule '
            f'-DisplayName "{rule_name}" '
            f'-Direction Outbound '
            f'-Program "{proc_path}" '
            f'-Action Block '
            f'-Description "标准处理系统涉密模式 - 禁止 {proc_name} 联网" '
        )
        # ⚠ 关键修复：WFP 的程序级「出站阻断」规则**连 127.0.0.1 回环一起挡**，
        # 而本程序的界面正是通过 http://127.0.0.1:8765 加载的（由
        # QtWebEngineProcess.exe 发起）。若不加限制，一开涉密模式界面立刻挂掉，
        # 用户看到的就是"127.0.0.1 拒绝访问"。
        # 先把阻断范围限定为 Internet（不含本机回环），旧系统不支持该关键字时
        # 再退回不限范围（保持原有安全强度），并额外补一条显式放行回环的规则。
        _attempts = (
            _base + '-RemoteAddress Internet -ErrorAction Stop',
            _base + '-ErrorAction SilentlyContinue',
        )
        try:
            import subprocess
            ok = False
            for _i, ps_cmd in enumerate(_attempts):
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=10,
                    **_ps_hidden_kwargs(),
                )
                if result.returncode == 0:
                    if _i == 1 and not _block_rule_exists(rule_name):
                        # 第 2 次尝试用的是 -ErrorAction SilentlyContinue，
                        # PowerShell 里"非终止错误"不会改变退出码（仍为 0），
                        # 所以 returncode==0 并不能说明规则真的建好了 ——
                        # 不复核就会出现"界面显示涉密模式已启用、实际一条规则都没有"
                        # 的纸面安全。这里回查一次规则是否真的存在。
                        print(f"[防火墙] 阻断规则未生效（已回退但仍失败）: {proc_name}")
                        continue
                    ok = True
                    if _i == 0:
                        _wf_loopback_safe += 1
                    elif _i == 1:
                        # 退回了不限范围：必须显式放行回环，否则界面会被自己挡死
                        _allow_loopback_rule(proc_path, proc_name)
                    break
                if _i == 0:
                    print(f"[防火墙] 该进程不支持 Internet 范围限定，退回全范围阻断: {proc_name}")
            if ok:
                success += 1
            else:
                failed += 1
                print(f"[防火墙] 规则安装失败: {proc_name} - {result.stderr.strip()}")
        except Exception as e:
            failed += 1
            print(f"[防火墙] 规则安装异常: {proc_name} - {e}")

    _wf_rules_active = (success > 0)
    invalidate_rule_names_cache()
    print(f"[防火墙] Windows 防火墙规则: {success} 条已安装, {failed} 条失败"
          f"（其中 {_wf_loopback_safe} 条已限定为仅拦外网，不影响本地界面）")
    print(f"         覆盖进程: {[os.path.basename(p) for p in processes]}")
    return (success, failed, True)


def _block_rule_exists(rule_name: str) -> bool:
    """回查某条防火墙规则是否真的存在（用于识别"被吞掉的失败"）"""
    try:
        import subprocess
        ps_cmd = (f'Get-NetFirewallRule -DisplayName "{rule_name}" '
                  f'-ErrorAction SilentlyContinue | Measure-Object | '
                  f'Select-Object -ExpandProperty Count')
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
            **_ps_hidden_kwargs(),
        )
        return r.returncode == 0 and int((r.stdout or "0").strip() or 0) > 0
    except Exception:
        return False


def _allow_loopback_rule(proc_path: str, proc_name: str):
    """为指定进程补一条"放行 127.0.0.1 出站"的允许规则。

    仅在阻断规则无法限定为 Internet 时兜底使用：显式放行规则范围更具体，
    优先级高于同进程的阻断规则，保证本地界面（127.0.0.1:8765）永远可用。
    规则名同样以 _WF_RULE_PREFIX 开头，清理时会被一并删除。
    """
    try:
        import subprocess
        allow_name = _build_rule_name("loopback_" + proc_name.replace(".", "_"))
        ps_cmd = (
            f'New-NetFirewallRule '
            f'-DisplayName "{allow_name}" '
            f'-Direction Outbound '
            f'-Program "{proc_path}" '
            f'-RemoteAddress 127.0.0.1 '
            f'-Action Allow '
            f'-Description "标准处理系统 - 允许 {proc_name} 访问本机界面(127.0.0.1)" '
            f'-ErrorAction SilentlyContinue'
        )
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
            **_ps_hidden_kwargs(),
        )
    except Exception as e:
        print(f"[防火墙] 回环放行规则创建失败: {proc_name} - {e}")


# ── 残留规则检测（读取规则不需要管理员权限）───────────────
_residual_cache = {"ts": 0.0, "info": None}
_residual_lock = threading.Lock()


def detect_residual_rules(ttl: float = 30.0) -> dict:
    """检测是否有本系统遗留的 Windows 防火墙规则残留。

    典型故障场景：上次以管理员身份开过涉密模式，程序异常退出后规则残留；
    之后以普通权限启动时 remove_windows_firewall_rules() 会因为
    _check_admin() 为 False 直接返回，规则永远清不掉 —— 出站阻断把
    127.0.0.1 也挡掉，用户看到"127.0.0.1 拒绝访问"却完全找不到原因。
    这里提供免管理员的检测，供启动自检与前端提示使用。
    """
    now = time.time()
    with _residual_lock:
        if _residual_cache["info"] is not None and (now - _residual_cache["ts"]) < ttl:
            return _residual_cache["info"]
    info = {"present": False, "count": 0, "can_clean": _check_admin(), "checked": False}
    try:
        import subprocess
        ps_cmd = (f'Get-NetFirewallRule -DisplayName "{_WF_RULE_PREFIX}_*" '
                  f'-ErrorAction SilentlyContinue | Measure-Object | '
                  f'Select-Object -ExpandProperty Count')
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
            **_ps_hidden_kwargs(),
        )
        if r.returncode == 0:
            info["count"] = int((r.stdout or "0").strip() or 0)
            info["present"] = info["count"] > 0
            info["checked"] = True
    except Exception as e:
        info["error"] = str(e)
    with _residual_lock:
        _residual_cache["ts"] = now
        _residual_cache["info"] = info
    return info


def remove_windows_firewall_rules():
    """删除本系统创建的所有 Windows 防火墙规则"""
    global _wf_rules_active

    if not _check_admin():
        return

    ps_cmd = f'Get-NetFirewallRule -DisplayName "{_WF_RULE_PREFIX}_*" | Remove-NetFirewallRule -ErrorAction SilentlyContinue'
    try:
        import subprocess
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, timeout=10,
            **_ps_hidden_kwargs(),
        )
        print(f"[防火墙] Windows 防火墙规则已全部清理")
    except Exception as e:
        print(f"[防火墙] 清理规则失败: {e}")

    _wf_rules_active = False
    invalidate_rule_names_cache()


def get_windows_firewall_status() -> dict:
    """查询 Windows 防火墙规则的安装状态"""
    if not _check_admin():
        return {"active": False, "reason": "无管理员权限"}

    try:
        import subprocess
        ps_cmd = f'Get-NetFirewallRule -DisplayName "{_WF_RULE_PREFIX}_*" | Measure-Object | Select-Object -ExpandProperty Count'
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
            **_ps_hidden_kwargs(),
        )
        count = int(result.stdout.strip() or 0)
        return {"active": count > 0, "rules_count": count}
    except Exception as e:
        return {"active": False, "error": str(e)}


# ╔══════════════════════════════════════════════════════════════╗
# ║  防火墙管理（设置页"防火墙管理"面板）                          ║
# ║  允许用户手动查看/管理涉及本系统及第三方程序的防火墙规则        ║
# ║  可独立于涉密模式使用，也可在涉密模式下补充管理                ║
# ╚══════════════════════════════════════════════════════════════╝

# 用户手动管理的进程（独立于涉密模式自动收集的进程）
# 格式：{程序路径: {"action": "block"/"allow", "note": "用户备注"}}
# 持久化到 .app_settings.json 或单独文件
_MANAGED_PROGRAMS = {}
_MANAGED_PROGRAMS_LOCK = threading.Lock()


def _managed_programs_path() -> str:
    """返回手动管理程序配置文件的路径"""
    # 与 .app_settings.json 同目录（exe 同级 / 项目根）
    return os.path.normpath(os.path.join(_user_data_dir(), "firewall_managed.json"))


def _load_managed_programs():
    """加载手动管理的程序配置"""
    global _MANAGED_PROGRAMS
    try:
        p = _managed_programs_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                _MANAGED_PROGRAMS = json.load(f)
        else:
            _MANAGED_PROGRAMS = {}
    except Exception:
        _MANAGED_PROGRAMS = {}


def _save_managed_programs():
    """保存手动管理的程序配置"""
    try:
        p = _managed_programs_path()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(_MANAGED_PROGRAMS, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def list_manageable_processes() -> list:
    """列出所有可管理的进程（本系统自动识别 + 用户手动添加）"""
    # 本系统自动识别的进程
    known = _get_processes_to_block()
    result = []
    seen = set()

    # 1. 系统自动识别的进程
    for proc in known:
        if proc in seen:
            continue
        seen.add(proc)
        result.append(_build_process_info(proc, source="system"))

    # 2. 用户手动添加的程序（可能在系统识别列表外）
    with _MANAGED_PROGRAMS_LOCK:
        for path, info in _MANAGED_PROGRAMS.items():
            if path in seen:
                continue
            seen.add(path)
            result.append(_build_process_info(path, source="user", note=info.get("note", "")))

    return result


def _build_process_info(proc_path: str, source="system", note="") -> dict:
    """构建单个进程的规则状态信息"""
    name = os.path.basename(proc_path)
    # 该进程当前是否有阻止规则
    rule_name = _build_rule_name(name.replace(".", "_"))
    has_block = _has_rule(rule_name)
    # 该进程是否处于涉密模式的自动阻止列表
    auto_block = proc_path in _get_processes_to_block() and is_classified()

    # 是否本系统主程序（源码运行=python.exe，打包运行=标准处理系统.exe）
    is_self = False
    try:
        is_self = os.path.normcase(os.path.abspath(proc_path)) == \
                  os.path.normcase(os.path.abspath(sys.executable))
    except Exception:
        pass

    # Windows 防火墙规则是否适用于该进程：
    # 源码运行时，本进程(python.exe)不建 Windows 规则（避免误伤其它 Python 程序），
    # 改由应用层防火墙拦截。
    frozen = hasattr(sys, '_MEIPASS')
    wf_applicable = True
    note_extra = ""
    if is_self:
        if frozen:
            note_extra = "本系统主程序（打包运行）"
        else:
            note_extra = "本系统主程序（源码运行，由应用层防火墙拦截，不建Windows规则以免影响其它Python程序）"
            wf_applicable = False

    return {
        "program": proc_path,
        "name": name,
        "source": source,
        "note": note,
        "blocked": has_block,
        "auto_blocked": auto_block,
        "is_self": is_self,
        "windows_rule_applicable": wf_applicable,
        "hint": note_extra,
    }


# 规则名集合缓存：安全页会逐进程判断规则是否存在，
# 若不缓存则每个进程都要启动一次 PowerShell（既慢又会刷出大量窗口）。
_rule_names_cache: Optional[set] = None
_rule_names_cache_ts = 0.0
_rule_names_lock = threading.Lock()


def _get_rule_names_cached(ttl: float = 5.0) -> set:
    """一次性获取本系统相关防火墙规则的名称集合（带 TTL 缓存）"""
    global _rule_names_cache, _rule_names_cache_ts
    if not _check_admin():
        return set()
    now = time.time()
    with _rule_names_lock:
        if _rule_names_cache is not None and (now - _rule_names_cache_ts) < ttl:
            return _rule_names_cache
        names = set()
        try:
            import subprocess
            ps_cmd = (f'Get-NetFirewallRule -DisplayName "{_WF_RULE_PREFIX}_*" | '
                      f'Select-Object -ExpandProperty DisplayName')
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=10,
                **_ps_hidden_kwargs(),
            )
            for line in (result.stdout or "").splitlines():
                n = line.strip()
                if n:
                    names.add(n)
        except Exception:
            pass
        _rule_names_cache = names
        _rule_names_cache_ts = now
        return names


def invalidate_rule_names_cache():
    """使规则名缓存失效（新增/删除规则后调用）"""
    global _rule_names_cache
    with _rule_names_lock:
        _rule_names_cache = None


def _has_rule(rule_name: str) -> bool:
    """检查指定名称的防火墙规则是否存在（批量查询 + 缓存）"""
    if not _check_admin():
        return False
    return rule_name in _get_rule_names_cached()


def _ps_quote(value) -> str:
    """把任意值转成 PowerShell **单引号字面量**（内部单引号翻倍）。

    为什么必须这样传值：原来是把值拼进 PS 的双引号串（`-Program "C:\\x\\a.exe"`），
    而 PS 双引号串会**求值** `$(...)`、反引号与 `$var`，且一个 `"` 就能闭合字符串
    执行任意命令 —— 实测 `C:\\x\\a.exe"; Write-Host INJECTED; #` 与
    `C:\\tmp\\$(Start-Process calc)\\a.exe` 都能逃逸（审计已动态验证）。
    单引号字面量不做任何求值，是 PowerShell 里最稳的传值方式。
    """
    return "'" + str(value).replace("'", "''") + "'"


def _is_safe_program_path(path) -> bool:
    """程序路径安全校验：必须是真实存在的文件，且不含会破坏 PS 解析的字符。

    审计指出原实现"不要求文件存在"，因此连不存在的路径也能拿来注入。
    """
    if not path or not isinstance(path, str):
        return False
    if any(c in path for c in ('"', "'", "$", "`", ";", "|", "&", "\n", "\r")):
        return False
    try:
        return os.path.isfile(path)
    except Exception:
        return False


def block_program(proc_path: str, note="") -> bool:
    """为指定程序添加出站阻止规则（手动管理，脱离涉密模式也可用）"""
    if not _check_admin():
        return False
    # 双保险：入口先做路径校验（拒绝不存在的文件与危险字符），
    # 拼脚本时再用 _ps_quote 包成单引号字面量。
    if not _is_safe_program_path(proc_path):
        print(f"[防火墙管理] 拒绝不安全的程序路径: {proc_path!r}")
        return False
    proc_path = os.path.normpath(proc_path)
    name = os.path.basename(proc_path)
    rule_name = _build_rule_name(name.replace(".", "_"))
    ps_cmd = (
        f'New-NetFirewallRule '
        f'-DisplayName {_ps_quote(rule_name)} '
        f'-Direction Outbound '
        f'-Program {_ps_quote(proc_path)} '
        f'-Action Block '
        f'-Description {_ps_quote("标准处理系统防火墙管理 - 禁止 " + name + " 联网")} '
        f'-ErrorAction SilentlyContinue'
    )
    try:
        import subprocess
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=10,
            **_ps_hidden_kwargs(),
        )
        if result.returncode == 0:
            with _MANAGED_PROGRAMS_LOCK:
                _MANAGED_PROGRAMS[proc_path] = {"action": "block", "note": note}
            _save_managed_programs()
            invalidate_rule_names_cache()
            print(f"[防火墙管理] 已阻止 {name} 联网")
            return True
    except Exception as e:
        print(f"[防火墙管理] 阻止 {name} 失败: {e}")
    return False


def allow_program(proc_path: str) -> bool:
    """放行指定程序（删除其阻止规则）。

    复审批 2-1：与 block_program 对称，先做路径校验、再单引号字面量化。
    原实现把用户可控的 basename 直接拼进**双引号**里，路径含分号即可逃逸：
      C:\\x\\a"; Start-Process calc; "
      → Get-NetFirewallRule -DisplayName "标准处理系统_涉密模式_a"; Start-Process calc; "" | …
    该路径不要求文件存在，且涉密模式下天然满足管理员上下文 → 提权命令注入。
    """
    if not _is_safe_program_path(proc_path):
        print(f"[防火墙管理] 拒绝不安全的程序路径: {proc_path!r}")
        return False
    proc_path = os.path.normpath(proc_path)
    name = os.path.basename(proc_path)
    rule_name = _build_rule_name(name.replace(".", "_"))
    removed = False
    if _check_admin():
        ps_cmd = (f'Get-NetFirewallRule -DisplayName {_ps_quote(rule_name)}'
                  f' | Remove-NetFirewallRule -ErrorAction SilentlyContinue')
        try:
            import subprocess
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, timeout=10,
                **_ps_hidden_kwargs(),
            )
            invalidate_rule_names_cache()
            removed = True
        except Exception:
            removed = False
    # 无论是否管理员权限，都从手动管理列表中移除（应用层规则也会被 remove_firewall 处理）
    with _MANAGED_PROGRAMS_LOCK:
        if proc_path in _MANAGED_PROGRAMS:
            del _MANAGED_PROGRAMS[proc_path]
    _save_managed_programs()
    print(f"[防火墙管理] 已放行 {name} 联网")
    return removed or not _check_admin()


def add_third_party_program(proc_path: str, note="") -> bool:
    """手动添加任意第三方程序到管理列表（不立即阻止，仅登记）"""
    if not os.path.exists(proc_path):
        return False
    proc_path = os.path.normpath(proc_path)
    with _MANAGED_PROGRAMS_LOCK:
        if proc_path not in _MANAGED_PROGRAMS:
            _MANAGED_PROGRAMS[proc_path] = {"action": "managed", "note": note}
    _save_managed_programs()
    return True


def get_managed_programs_info() -> list:
    """返回所有手动管理的程序信息"""
    _load_managed_programs()
    result = []
    with _MANAGED_PROGRAMS_LOCK:
        for path, info in _MANAGED_PROGRAMS.items():
            result.append(_build_process_info(path, source="user", note=info.get("note", "")))
    return result


# ── 整合：安装/卸载所有防火墙层 ─────────────────────────────

def _firewall_info() -> dict:
    """返回当前所有防火墙层的状态信息"""
    wf_status = get_windows_firewall_status()
    residual = detect_residual_rules()
    return {
        "app_layer_active": _firewall_active,
        "windows_firewall_active": wf_status.get("active", False),
        "windows_firewall_rules": wf_status.get("rules_count", 0),
        "admin_available": _check_admin(),
        "blocked_total": len(_firewall_log),
        # 规则是否已限定为"仅拦外网"。为 False 时说明存在把 127.0.0.1 一起挡掉的风险，
        # 涉密模式下本地界面可能加载不出来（表现为"127.0.0.1 拒绝访问"）。
        "loopback_safe": _wf_loopback_safe > 0,
        # 残留规则：无管理员权限时清不掉，是"拒绝访问"最常见的成因
        "residual_rules": residual,
    }


# ── 重写 install_firewall / remove_firewall ─────────────────
#     保留原函数名以兼容已引用的代码，增强功能

_original_install_firewall = install_firewall
_original_remove_firewall = remove_firewall


def install_firewall():
    """安装所有防火墙层：
       - 应用层防火墙（Monkey Patch socket，始终启用）
       - Windows 防火墙规则（需管理员权限，守护 OCR/WebEngine 进程）
    """
    # 1. 应用层防火墙（始终启用）
    _original_install_firewall()

    # 2. Windows 防火墙规则（尝试安装）
    wf_ok, wf_fail, is_admin = install_windows_firewall_rules()
    if is_admin:
        if wf_ok > 0:
            print(f"[防火墙] [OK] 双层防火墙已就绪（Windows防火墙 + 应用层监控）")
        else:
            print(f"[防火墙] [注意] Windows 防火墙规则安装部分失败（{wf_fail} 条失败）")
    else:
        print(f"[防火墙] [OK] 应用层防火墙已就绪（无管理员权限，仅应用层防护）")


def remove_firewall():
    """卸载所有防火墙层"""
    # 1. 应用层防火墙
    _original_remove_firewall()

    # 2. Windows 防火墙规则
    remove_windows_firewall_rules()

    print(f"[防火墙] ○ 所有防火墙已卸载")


# ── 在涉密模式下自动安装防火墙 ──────────────────────────────

def _auto_install_firewall():
    """在涉密模式初始化后自动安装防火墙"""
    if is_classified():
        install_firewall()

# 检查是否有提权重启后的标志文件（在 _auto_install_firewall 之前执行）
check_classified_pending()

# 模块导入时自动检测并安装
_auto_install_firewall()
