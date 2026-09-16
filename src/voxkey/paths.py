"""运行期路径：程序自己的数据/日志放哪、模型去哪找。

**便携布局**：打包之后，模型和日志都放在 `VoxKey.app` **旁边**的文件夹里——

    VoxKey/                 ← 用户拿到手就是这个文件夹
      VoxKey.app
      models/               ← 首启自动下载到这儿
      logs/                 ← 日志

这样卸载就是「把文件夹删掉」，不在 `~/Library` 里留任何东西。

**为什么不能放进 VoxKey.app 里面**：macOS 的 .app 是签名封起来的（sealed resource），
往里写任何文件签名立刻失效。实测：`codesign --verify` 从 "valid on disk" 变成
"a sealed resource is missing or invalid / file added: …"。签名坏了 Gatekeeper 会拦，
而且程序更新时整个包体本来就会被替换掉，模型放在里面也留不住。

兜底：如果用户只把 `VoxKey.app` 单独拖进了 `/Applications`（那里普通用户不可写），
就退回 `~/Library/Application Support/VoxKey/`，并在日志里说明。功能不受影响，只是不便携。

优先级一律是：环境变量 > 便携目录 > 系统用户目录 > 源码仓库。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _writable(d: Path) -> bool:
    """d（或它最近的已存在上级）能不能写。不产生副作用，只问权限。"""
    p = d
    while not p.exists() and p != p.parent:
        p = p.parent
    return os.access(p, os.W_OK)


def app_folder() -> Path | None:
    """打包后 `.app` 所在的那个文件夹（便携根目录）；源码运行时返回 None。

    `sys.executable` 是 `…/VoxKey/VoxKey.app/Contents/MacOS/VoxKey`，
    往上找到 `.app` 那一层，再取它的父目录。
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    for p in exe.parents:
        if p.suffix == ".app":
            return p.parent
    return exe.parent


def app_data_dir() -> Path:
    """系统用户目录：只在便携目录不可写时兜底（比如 App 被单独丢进 /Applications）。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VoxKey"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "VoxKey"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "VoxKey"


def app_log_dir() -> Path:
    """日志目录。便携布局下和模型放一起，卸载时一起清掉。"""
    env = os.environ.get("VOXKEY_LOG_DIR")
    if env:
        return Path(env).expanduser()
    folder = app_folder()
    if folder is not None and _writable(folder):
        return folder / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "VoxKey"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "VoxKey" / "Logs"
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "voxkey"


def default_models_dir() -> Path:
    """模型目录。"""
    env = os.environ.get("VOXKEY_MODELS_DIR")
    if env:
        return Path(env).expanduser()
    folder = app_folder()
    if folder is not None:
        if _writable(folder):
            return folder / "models"
        return app_data_dir() / "models"      # /Applications 下不可写的兜底
    return Path(__file__).resolve().parents[2] / "models"


def uses_portable_layout() -> bool:
    """现在是不是便携布局（给日志/文档用）。"""
    folder = app_folder()
    return folder is not None and _writable(folder)
