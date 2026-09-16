#!/usr/bin/env python3
"""打包入口：PyInstaller 从这里起步，等价于 `python -m voxkey.app`。

为什么不直接拿 voxkey/app.py 当入口：那份文件用的是绝对导入（`from voxkey.audio import …`），
当脚本直接跑时 `voxkey` 包不在 sys.path 上。入口文件只管把 main 叫起来。
"""

from __future__ import annotations

import sys

from voxkey.app import main

if __name__ == "__main__":
    sys.exit(main())
