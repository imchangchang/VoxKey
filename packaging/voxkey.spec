# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（macOS arm64）。

产物：packaging/dist/VoxKey.app
跑法：packaging/build_macos.sh（推荐，会顺带签名/公证），或
     .venv-build/bin/pyinstaller packaging/voxkey.spec --noconfirm

这里只负责「把东西凑齐」，签名和公证交给 build_macos.sh——PyInstaller 自己也会签一道，
但它签的那份不带 entitlements、也没有 hardened runtime，公证那关过不去。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).parent          # SPECPATH = packaging/
VERSION = "0.1.0"                     # 发版时和 pyproject.toml 一起改
BUNDLE_ID = "com.imchangchang.voxkey"
ICON = ROOT / "packaging" / "AppIcon.icns"

datas = [
    # sounddevice 自带的 PortAudio 是运行时用 `_sounddevice_data.__path__` 找的
    # （portaudio-binaries/libportaudio.dylib），不在依赖图里。不收就会「找不到 libportaudio」，
    # 表现是能启动、一录音就崩。
    *collect_data_files("_sounddevice_data"),
]

binaries = [
    # sherpa-onnx 的 3 个 dylib（c-api / cxx-api / onnxruntime）躺在包目录里、由扩展模块
    # 运行时加载，同样不在依赖图里。只收 .dylib：那个 .so 扩展模块 PyInstaller 会自己按模块处理，
    # 重复收会打架。
    *collect_dynamic_libs("sherpa_onnx", search_patterns=["*.dylib"]),
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    # 菜单里「复制上次结果」是运行期 import pyperclip（没装也不报错），静态分析看不见
    hiddenimports=["pyperclip"],
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "PyQt5", "PySide6"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VoxKey",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                     # 菜单栏程序，不要终端窗口
    argv_emulation=False,
    target_arch="arm64",
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VoxKey")

APP_KWARGS = dict(
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    info_plist={
        # 常驻菜单栏程序：不进 Dock、不抢前台（用户要求：常驻但不碍事）
        "LSUIElement": True,
        "CFBundleDisplayName": "VoxKey",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # 没有这条，macOS 会在要麦克风时**直接拒绝且不给任何提示**
        "NSMicrophoneUsageDescription":
            "VoxKey 需要麦克风录下你按住语音键说的那段话。识别全部在本机完成，音频不上传。",
    },
)
if ICON.exists():
    APP_KWARGS["icon"] = str(ICON)

app = BUNDLE(coll, name="VoxKey.app", **APP_KWARGS)
