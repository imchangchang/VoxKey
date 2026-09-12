"""统一日志：所有模块都从这里拿 log，保证格式一致、能按模块过滤。

格式：`时秒.毫秒  标签(4字符)  消息`。标签就是模块名/环节名（设备/音频/按键/结果/悬浮条…），
排障时 `grep 标签 voxkey.log` 就能把一个环节的完整时间线拉出来。
"""

from __future__ import annotations

from datetime import datetime


def log(tag: str, msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {tag:4s}  {msg}", flush=True)
