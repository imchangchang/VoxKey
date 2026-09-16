"""统一日志：所有模块都从这里拿 log，保证格式一致、能按模块过滤、**多线程不串行**。

格式：`时秒.毫秒  标签(4字符)  消息`。标签就是模块名/环节名（设备/音频/按键/结果/悬浮条…），
排障时 `grep 标签 voxkey.log` 就能把一个环节的完整时间线拉出来。

为什么要加锁、而且整行一次写出：`print()` 是分几次写的（正文、分隔符、换行各写一次），
两个线程同时打日志就会交错——真出现过两条日志叠在同一行里，排障时这种日志等于没有。

打包成 .app 之后**没有终端**（`sys.stdout` 是 None，从 Finder 启动时更是如此），
所以那份日志必须同时落盘：不然用户报「没反应」的时候，我们手里一行日志都没有。
路径见 voxkey.paths.app_log_dir；写满 MAX_BYTES 就轮转成 .1，免得常驻几个月把盘写满。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from .paths import app_log_dir

MAX_BYTES = 8 << 20       # 单份日志上限，超了轮转一次（只留一份旧的）
_lock = threading.Lock()
_file = None


class _Null:
    """兜底：连临时目录都写不了的时候用它，至少别让打日志把程序搞崩。"""

    name = "（无处可写）"

    def write(self, _s: str) -> int:
        return len(_s)

    def flush(self) -> None:
        pass


def _file_sink():
    """打开（并缓存）日志文件。只在没有终端、或程序打包之后才用得上。

    绝不抛异常：这是启动路径上的东西，用户家目录只读、磁盘满、目录被占成文件……任何一种
    都不该让一个语音输入软件打不开。
    """
    global _file
    if _file is not None:
        return _file
    for p in (app_log_dir() / "voxkey.log", Path(tempfile.gettempdir()) / "voxkey.log"):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > MAX_BYTES:
                os.replace(p, p.with_name("voxkey.log.1"))
            _file = open(p, "a", buffering=1, encoding="utf-8", errors="replace")
            return _file
        except OSError:
            continue
    _file = _Null()
    return _file


def log_target() -> str:
    """日志到底落在哪。报障时让用户直接照着找，别让他猜。"""
    if getattr(sys, "frozen", False):
        return str(_file_sink().name)
    return "标准输出"


def _sinks() -> list:
    """这一行要写到哪几个地方。

    每次都现取 `sys.stdout`——测试用 `redirect_stdout` 换掉它，缓存住就接不到了。
    """
    outs = [sys.stdout] if sys.stdout is not None else []
    if getattr(sys, "frozen", False):
        outs.append(_file_sink())      # 打包后一律留一份文件日志，终端那条只是附赠
    return outs


def log(tag: str, msg: str) -> None:
    """打一行日志。整行一次 write 出去（加锁），多线程下不会和别的行交错。"""
    line = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {tag:4s}  {msg}\n"
    with _lock:
        outs = _sinks()
        if not outs:               # 没终端又不是打包运行（比如 pythonw）：落到文件里
            outs = [_file_sink()]
        for out in outs:
            try:
                out.write(line)
                out.flush()
            except Exception:
                # 终端被关掉、管道断了都不该让程序崩在打日志上——换文件再试一次就算了
                try:
                    _file_sink().write(line)
                except Exception:
                    pass
