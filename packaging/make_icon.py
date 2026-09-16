#!/usr/bin/env python3
"""生成 packaging/AppIcon.icns：圆角方块 + SF Symbol 的话筒。

为什么用脚本画、不塞一个 .icns 进仓库：图标要跟系统风格一致，SF Symbol 是现成的；
而 .icns 是二进制，进仓库以后每改一次都得 diff 一堆字节。

用法：.venv-build/bin/python packaging/make_icon.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import AppKit

HERE = Path(__file__).resolve().parent
OUT = HERE / "AppIcon.icns"

# iconset 要求的尺寸（文件名里的 1x/2x 由 iconutil 认）
SPECS = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]

BG = (0.10, 0.11, 0.14, 1.0)      # 深色底：菜单栏图标是浅色的，App 图标给个对比
GLYPH = "mic.fill"


def _tint(sym, color, px: int):
    """把符号染成指定颜色：先画出来，再用 sourceAtop 只覆盖有像素的地方。"""
    out = AppKit.NSImage.alloc().initWithSize_(AppKit.NSMakeSize(px, px))
    out.lockFocus()
    sym.drawInRect_(AppKit.NSMakeRect(0, 0, px, px))
    color.set()
    AppKit.NSRectFillUsingOperation(AppKit.NSMakeRect(0, 0, px, px),
                                    AppKit.NSCompositingOperationSourceAtop)
    out.unlockFocus()
    return out


def render(px: int) -> bytes:
    """画一张 px×px 的 PNG。"""
    size = AppKit.NSMakeSize(px, px)
    img = AppKit.NSImage.alloc().initWithSize_(size)
    img.lockFocus()

    radius = px * 0.225                     # 和 macOS 自己的圆角比例接近
    path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        AppKit.NSMakeRect(0, 0, px, px), radius, radius)
    AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*BG).set()
    path.fill()

    sym = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(GLYPH, None)
    if sym is None:
        raise SystemExit(f"系统里没有 SF Symbol「{GLYPH}」（macOS 11+ 才有）")
    cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
        px * 0.50, AppKit.NSFontWeightSemibold, 3)
    sym = sym.imageWithSymbolConfiguration_(cfg)
    # 话筒在方块里略偏上一点点（下面留出「杆」的视觉重量）
    box = px * 0.56
    rect = AppKit.NSMakeRect((px - box) / 2, (px - box) / 2 - px * 0.015, box, box)
    tinted = AppKit.NSImage.alloc().initWithSize_(AppKit.NSMakeSize(px, px))
    tinted.lockFocus()
    sym.drawInRect_(rect)
    AppKit.NSColor.whiteColor().set()
    AppKit.NSRectFillUsingOperation(AppKit.NSMakeRect(0, 0, px, px),
                                    AppKit.NSCompositingOperationSourceAtop)
    tinted.unlockFocus()
    tinted.drawInRect_(AppKit.NSMakeRect(0, 0, px, px))

    img.unlockFocus()

    rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    return bytes(data)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for base, scale in SPECS:
            px = base * scale
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            (iconset / name).write_bytes(render(px))
        r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stderr.strip(), file=sys.stderr)
            return r.returncode
    print(f"{OUT}  {OUT.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
