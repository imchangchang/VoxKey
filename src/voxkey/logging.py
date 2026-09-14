"""统一日志：所有模块都从这里拿 log，保证格式一致、能按模块过滤、**多线程不串行**。

格式：`时秒.毫秒  标签(4字符)  消息`。标签就是模块名/环节名（设备/音频/按键/结果/悬浮条…），
排障时 `grep 标签 voxkey.log` 就能把一个环节的完整时间线拉出来。

为什么要加锁、而且整行一次写出：`print()` 是分几次写的（正文、分隔符、换行各写一次），
两个线程同时打日志就会交错——真出现过两条日志叠在同一行里，排障时这种日志等于没有。
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime

_lock = threading.Lock()


def log(tag: str, msg: str) -> None:
    """打一行日志。整行一次 write 出去（加锁），多线程下不会和别的行交错。"""
    line = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {tag:4s}  {msg}\n"
    with _lock:
        sys.stdout.write(line)
        sys.stdout.flush()
