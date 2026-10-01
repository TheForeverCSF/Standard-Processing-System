# Copyright (c) 2026 The Forever CSF
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional terms: see 第二部分《标准处理系统 许可、隐私与免责说明》（AGPL-3.0 第 7 条附加条款）
"""进程级 stderr 静默（替代原先"每个文件 / 每页"都重定向一次的写法）。

背景
----
pyMuPDF(MuPDF) / PaddleOCR 会在 C 层直接往文件描述符 2（stderr）写警告，
刷屏且干扰日志，所以需要屏蔽。

原先的实现是在 `_suppress_stderr()` 里做
`os.dup(2) → os.dup2(devnull, 2) → … → os.dup2(old, 2) → os.close(old)`，
并且**每个文件、甚至每一页**都进出一次。多线程并行处理时就出事：

* 两个线程交错执行时，A 线程"恢复"用的 `old` 可能正是 B 线程刚 `close` 掉的
  描述符 → 抛 `OSError`，当前文件直接处理失败；
* 两个线程各自 `dup2`/`close` 交错，可能把 stderr 永久指向 `/dev/null`，
  之后所有报错都看不见了；
* 每页两次 `dup/dup2/close` 系统调用也是白白的开销。

现在改为：**整个进程只静默一次**，之后的调用全是空操作。既消除了竞态，
也省掉了每个文件、每一页的系统调用。
"""
import os
import tempfile
import threading

_lock = threading.Lock()
_quieted = False
_saved_fd = None        # 原始 stderr 的副本（保留以便临时恢复输出）
_log_path = None        # 被重定向到的文件（便于事后排查）


def _default_log_path() -> str:
    return os.path.join(tempfile.gettempdir(), "standard_system", "stderr.log")


def quiet_stderr_once(log_path: str = None) -> bool:
    """把 fd 2 重定向到日志文件。幂等、线程安全，失败也不抛异常。

    为什么不用 /dev/null：MuPDF / PaddleOCR 这些 C 扩展在底层崩溃时，
    崩溃信息是写到 stderr 的。直接丢进 /dev/null 会让"进程突然消失"这类
    问题完全没有线索（实测踩到过一次）。写进文件既不刷屏，又能事后查。

    Returns:
        True 表示 stderr 已重定向（或本来就已经重定向）。
    """
    global _quieted, _saved_fd, _log_path
    if _quieted:
        return True
    with _lock:
        if _quieted:
            return True
        try:
            _saved_fd = os.dup(2)
        except OSError:
            _saved_fd = None
        target = None
        try:
            path = log_path or _default_log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 截断重写：只保留本次运行的输出，避免无限增长
            target = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            _log_path = path
        except OSError:
            try:
                target = os.open(os.devnull, os.O_WRONLY)
                _log_path = os.devnull
            except OSError:
                target = None
        if target is not None:
            try:
                os.dup2(target, 2)
            except OSError:
                pass
            finally:
                try:
                    os.close(target)
                except OSError:
                    pass
        _quieted = True
        return True


def stderr_log_path():
    """返回当前 stderr 重定向到的文件路径（未重定向时为 None）"""
    return _log_path


class suppressed_stderr:
    """与旧用法完全兼容的上下文管理器。

    `with self._suppress_stderr():` 这样的大量既有调用点无需改动 ——
    真正的一次性重定向在第一次进入时完成，之后都是空操作。
    """

    def __enter__(self):
        quiet_stderr_once()
        return self

    def __exit__(self, *exc_info):
        return False    # 不吞异常


def original_stderr_fd():
    """返回原始 stderr 的 fd 副本（未静默过则为 None）。

    仅供需要临时把输出写回真实 stderr 的场景使用；调用方不要关闭它。
    """
    return _saved_fd


def is_quieted() -> bool:
    return _quieted
