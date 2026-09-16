"""运行期路径：程序自己的数据/日志放哪、模型去哪找。

为什么单独一个模块：打包成 .app 之后，`__file__` 指向的是包体内部（只读，而且签名后
一改就废），不能再拿它推目录。所以凡是「要写盘的东西」都得走这里，按平台给用户目录。

三个函数的优先级都是：环境变量 > 打包后的用户目录 > 源码仓库。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_data_dir() -> Path:
    """用户数据目录。模型这类大文件放这儿，不放 .app 包里。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VoxKey"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "VoxKey"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "VoxKey"


def app_log_dir() -> Path:
    """日志目录。macOS 的惯例是 ~/Library/Logs/<App>，不是塞进 Application Support。"""
    env = os.environ.get("VOXKEY_LOG_DIR")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "VoxKey"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "VoxKey" / "Logs"
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "voxkey"


def default_models_dir() -> Path:
    """模型目录。

    打包后必须换地方：PyInstaller 把包解到 .app 内部，`__file__` 往上两级也在包体里，
    往那儿写模型既占包体，又会在签名校验时出问题（Gatekeeper 认签名，包内容一变就废）。
    """
    env = os.environ.get("VOXKEY_MODELS_DIR")
    if env:
        return Path(env).expanduser()
    if getattr(sys, "frozen", False):
        return app_data_dir() / "models"
    return Path(__file__).resolve().parents[2] / "models"
