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

# 菜单栏图标：从设计稿里取「顶部那个大圆」（语音键的位置）。
# 整张缩到 18pt 的话 9 单位的描边只剩 0.16 像素、糊成一片灰；只取这个圆再把描边加粗 2.5 倍，
# @2x 下才够黑（实测均值 alpha 从 92 提到 170）。
MENUBAR_CROP = (141, 339, 471, 471)
MENUBAR_STROKE_SCALE = 2.5

# 小尺寸下给描边兜底：线稿等比缩小后必然淡掉，至少别让它完全消失
MIN_STROKE_PX = 0.9


def build_icns() -> None:
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for base, scale in SPECS:
            px = base * scale
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            render(SVG, iconset / name, px, width=px, min_stroke_px=MIN_STROKE_PX)
        r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICNS)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(r.stderr.strip())
    print(f"{ICNS.relative_to(ROOT)}  {ICNS.stat().st_size} 字节")


def build_menubar() -> None:
    for name, px in (("menubar.png", 18), ("menubar@2x.png", 36)):
        out = ASSETS / name
        render(SVG, out, px, width=px, template=True, crop=MENUBAR_CROP,
               stroke_scale=MENUBAR_STROKE_SCALE)
        print(f"{out.relative_to(ROOT)}  {px}x{px}  {out.stat().st_size} 字节")


def main() -> int:
    if not SVG.exists():
        raise SystemExit(f"找不到设计稿 {SVG}")
    build_icns()
    build_menubar()
    return 0


if __name__ == "__main__":
    sys.exit(main())
