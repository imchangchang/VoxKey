#!/usr/bin/env python3
"""从设计稿 SVG 生成图标资源。

产出两样：
1. `packaging/AppIcon.icns` —— App 图标（构建时用，不进仓库）
2. `src/voxkey/assets/menubar.png` / `menubar@2x.png` —— 菜单栏图标（进仓库，运行时读）

菜单栏那张为什么进仓库：它是**跑起来需要**的文件，不是中间产物。进仓库、再在冒烟里加一条
「资产和设计稿对得上」的断言，就不会出现「改了 SVG 忘了重新生成」这种漂移。

用法：.venv-build/bin/python packaging/make_icon.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_svg import render          # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SVG = HERE / "app-icon.svg"
ICNS = HERE / "AppIcon.icns"
ASSETS = ROOT / "src" / "voxkey" / "assets"

# iconset 要求的尺寸：(边长, 倍数)
SPECS = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]

# 菜单栏图标：**整张设计稿等比缩小**（用户要求「整个图标完整显示」，不裁不剔）。
#
# 代价得说清楚：设计稿是 768×2048 的竖构图，18pt 高时宽度只有约 6.8pt（@2x 也就 14px），
# 稿子里十几个图元在这个尺寸下**都是亚像素的**。实测（@2x，36px 高）：
#   描边不兜底 → 均值 alpha 99、实心 0%，整张糊成一片灰
#   兜到 1.0px  → 72% 的像素被墨盖住，并成一块
#   兜到 0.8px  → 均值 alpha 154、实心 23.6%，还看得出是线稿（取这档）
MENUBAR_MIN_STROKE_PX = 0.8
MENUBAR_HEIGHT_PT = 18          # 菜单栏图标惯例高度

# 小尺寸下给描边兜底：线稿等比缩小后必然淡掉，至少别让它完全消失
MIN_STROKE_PX = 0.9

# App 图标垫什么底色。设计稿是**纯线稿、没有底色**，而 macOS 的 App 图标惯例是实心底——
# 透明底的黑色线稿摆在 Dock 里会像没做完。做成常量方便改（换色只动这一行）。
# 想还原设计稿原样（透明底）就把这里设成 None。
APP_ICON_BG = "#ffffff"

# 菜单栏图**不能**垫底：那是模板图，垫了底就变成一个方块，而且不再跟随菜单栏明暗反色


def build_icns() -> None:
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for base, scale in SPECS:
            px = base * scale
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            render(SVG, iconset / name, px, width=px, min_stroke_px=MIN_STROKE_PX,
                   bg=APP_ICON_BG)
        r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICNS)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(r.stderr.strip())
    print(f"{ICNS.relative_to(ROOT)}  {ICNS.stat().st_size} 字节")


def build_menubar() -> None:
    """菜单栏图：整张稿子等比缩小，@1x 和 @2x 各一份，都是模板图。"""
    sizes = []
    for name, px in (("menubar.png", MENUBAR_HEIGHT_PT), ("menubar@2x.png", MENUBAR_HEIGHT_PT * 2)):
        out = ASSETS / name
        render(SVG, out, px, template=True, min_stroke_px=MENUBAR_MIN_STROKE_PX)
        sizes.append(f"{px}x{round(px * 768 / 2048)}")
        print(f"{out.relative_to(ROOT)}  {out.stat().st_size} 字节")
    print(f"  像素尺寸：{'、'.join(sizes)}（18pt 高时约 7pt 宽——竖构图缩到菜单栏就这么窄）")


def main() -> int:
    if not SVG.exists():
        raise SystemExit(f"找不到设计稿 {SVG}")
    build_icns()
    build_menubar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
