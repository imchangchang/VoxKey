#!/usr/bin/env python3
"""把设计稿 SVG 渲染成位图（图标 / 菜单栏图）。

为什么自己画而不用现成工具：QuickLook 在受限环境里起不来，而 cairosvg 要拖一整套 cairo。
设计稿只用了 `rect`（带圆角）和 `ellipse` 两种图元，没有 path / transform / 渐变，
用 AppKit 的 NSBezierPath 直译就够了——而且分辨率、描边粗细、是否染成模板图都完全可控。

**不支持的元素会直接报错，不会默默画错**：以后设计稿里出现 path，这里必须报出来，
不然图标会缺一块还没人发现。

用法：
    packaging/render_svg.py 输入.svg 输出.png --height 1024 [--template] [--stroke-scale 2.0]
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import AppKit

SVG_NS = "{http://www.w3.org/2000/svg}"

# 只会画这几种；出现别的就报错，别装作画好了
SUPPORTED = {"svg", "g", "rect", "ellipse", "circle"}


def _tag(el) -> str:
    return el.tag.replace(SVG_NS, "")


def _num(v, default=0.0) -> float:
    if v is None:
        return default
    return float(re.sub(r"[^0-9eE.+-]", "", v))


def parse(svg_path: Path):
    """解析出 (viewBox 宽高, [图元])。图元是 dict，已把 group 上的描边属性继承下来。"""
    root = ET.parse(svg_path).getroot()
    if _tag(root) != "svg":
        raise SystemExit(f"{svg_path} 的根元素不是 <svg>")

    vb = root.get("viewBox")
    if vb:
        _, _, vw, vh = (float(x) for x in vb.replace(",", " ").split())
    else:
        vw, vh = _num(root.get("width"), 100), _num(root.get("height"), 100)

    shapes: list[dict] = []

    def walk(el, inherited):
        t = _tag(el)
        if t not in SUPPORTED:
            raise SystemExit(f"设计稿里有没支持的元素 <{t}>，渲染器画不了——"
                             f"要么改设计稿，要么扩展 {__file__} 的图元支持")
        style = dict(inherited)
        for k in ("stroke", "fill", "stroke-width", "stroke-linecap", "stroke-linejoin"):
            if el.get(k) is not None:
                style[k] = el.get(k)
        if el.get("transform"):
            raise SystemExit("设计稿里有 transform，渲染器不支持（别默默画错）")

        if t == "rect":
            shapes.append(dict(kind="rect", style=style, x=_num(el.get("x")), y=_num(el.get("y")),
                               w=_num(el.get("width")), h=_num(el.get("height")),
                               rx=_num(el.get("rx")), ry=_num(el.get("ry"))))
        elif t == "ellipse":
            shapes.append(dict(kind="ellipse", style=style, cx=_num(el.get("cx")),
                               cy=_num(el.get("cy")), rx=_num(el.get("rx")), ry=_num(el.get("ry"))))
        elif t == "circle":
            r = _num(el.get("r"))
            shapes.append(dict(kind="ellipse", style=style, cx=_num(el.get("cx")),
                               cy=_num(el.get("cy")), rx=r, ry=r))
        for child in el:
            walk(child, style)

    walk(root, {})
    return vw, vh, shapes


def _color(name: str | None, fallback_black=True):
    if not name or name == "none":
        return None
    if name in ("#000000", "#000", "black"):
        return AppKit.NSColor.blackColor()
    if name in ("#ffffff", "#fff", "white"):
        return AppKit.NSColor.whiteColor()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", name)
    if m:
        v = int(m.group(1), 16)
        return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
            ((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255, 1.0)
    raise SystemExit(f"不支持的颜色 {name!r}（只认 #rrggbb / black / white / none）")


def render(svg_path: Path, out_png: Path, height: int, template: bool = False,
           stroke_scale: float = 1.0, width: int | None = None,
           crop: tuple[float, float, float, float] | None = None,
           min_stroke_px: float = 0.0, skip: set[int] | None = None,
           bg: str | None = None) -> tuple[int, int]:
    """渲染成 PNG。

    height/width 给的是画布像素尺寸；图形按 viewBox 等比缩放后**居中**放进去。
    crop=(x, y, w, h) 用设计稿自己的坐标系裁一块出来渲染，尺寸按裁出来的那块算。
    skip 是「不画的图元序号」（按解析顺序从 0 数）——做菜单栏那种十几像素的图时，
    设计稿里的细节（一排小键、下面的屏幕）缩下去只会糊成灰块，不如按序号剔掉。
    template=True 时丢掉背景填充，所有笔迹一律画成黑色——macOS 的模板图靠 alpha 取形，
    系统会自己按菜单栏明暗反色。
    min_stroke_px 给描边兜底：设计稿是线稿，等比缩到十几像素时 9 单位的描边只剩零点几像素，
    整张图会淡成一片灰。设一个下限让线至少看得见——代价是小尺寸下细节会糊在一起，
    这是线稿做图标的固有问题，不是这里的 bug。
    """
    vw, vh, shapes = parse(svg_path)
    if skip:
        shapes = [s for i, s in enumerate(shapes) if i not in skip]
    if crop:
        cx, cy, cw, ch = crop
        if cx or cy:
            for s in shapes:
                if s["kind"] == "rect":
                    s["x"] -= cx
                    s["y"] -= cy
                else:
                    s["cx"] -= cx
                    s["cy"] -= cy
        vw, vh = cw, ch

    scale = height / vh
    w = width or max(1, round(vw * scale))
    h = height
    ox = (w - vw * scale) / 2
    oy = (h - vh * scale) / 2

    # 直接画进 NSBitmapImageRep：给多少像素就出多少像素。
    # 走 NSImage + TIFFRepresentation 的话拿不到精确尺寸（会被设备缩放带跑），
    # 而图标对尺寸是敏感的（iconset 要求 16/32/128/256/512 各自精确）。
    rep = AppKit.NSBitmapImageRep.alloc()\
        .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, w, h, 8, 4, True, False, AppKit.NSCalibratedRGBColorSpace, 0, 0)
    ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(ctx)
    AppKit.NSColor.clearColor().set()
    AppKit.NSRectFill(AppKit.NSMakeRect(0, 0, w, h))     # 先擦成透明

    def to_rect(x, y, rw, rh):
        # SVG 原点在左上、AppKit 在左下，y 要翻过来
        return AppKit.NSMakeRect(ox + x * scale, h - (oy + (y + rh) * scale), rw * scale, rh * scale)

    # 背景垫底（可选）：设计稿是纯线稿、没有底色，而 macOS 的 App 图标惯例是实心底——
    # 透明底的黑色线稿摆在 Dock 里会像没做完。
    #
    # **必须铺满整个画布**：之前留了 18% 边距（Apple 图标网格那套 82% 比例），结果白底
    # 的四个圆角是透明的，透出系统的灰容器，图标看起来像"灰框套白框"，而别人的图标都铺满。
    # 边距交给系统去加，我们只管铺满。
    if bg and not template:
        _color(bg).set()
        AppKit.NSBezierPath.bezierPathWithRect_(
            AppKit.NSMakeRect(0, 0, w, h)).fill()

    for s in shapes:
        st = s["style"]
        if s["kind"] == "rect":
            r = to_rect(s["x"], s["y"], s["w"], s["h"])
            rx = s["rx"] or s["ry"]
            path = (AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, rx * scale, rx * scale)
                    if rx else AppKit.NSBezierPath.bezierPathWithRect_(r))
        else:
            r = to_rect(s["cx"] - s["rx"], s["cy"] - s["ry"], s["rx"] * 2, s["ry"] * 2)
            path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(r)

        fill = None if template else _color(st.get("fill"))
        if fill is not None:
            fill.set()
            path.fill()

        stroke = _color(st.get("stroke"))
        if stroke is not None:
            (AppKit.NSColor.blackColor() if template else stroke).set()
            lw = _num(st.get("stroke-width"), 1.0) * scale * stroke_scale
            if min_stroke_px:
                lw = max(lw, min_stroke_px)
            path.setLineWidth_(lw)
            path.setLineCapStyle_(2 if st.get("stroke-linecap") == "round" else 0)   # 2 = round
            path.setLineJoinStyle_(2 if st.get("stroke-linejoin") == "round" else 0)
            path.stroke()

    AppKit.NSGraphicsContext.restoreGraphicsState()

    out_png.parent.mkdir(parents=True, exist_ok=True)
    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    out_png.write_bytes(bytes(data))
    return w, h


def main() -> int:
    ap = argparse.ArgumentParser(description="把设计稿 SVG 渲染成 PNG")
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--height", type=int, required=True, help="输出画布高度（像素）")
    ap.add_argument("--width", type=int, default=None, help="输出画布宽度（默认按比例）")
    ap.add_argument("--template", action="store_true",
                    help="渲染成菜单栏用的模板图：丢掉背景、笔迹一律黑色")
    ap.add_argument("--stroke-scale", type=float, default=1.0,
                    help="描边加粗倍数（小尺寸下线条太细时用）")
    ap.add_argument("--min-stroke-px", type=float, default=0.0,
                    help="描边像素下限：线稿等比缩小后会淡成灰，给个下限兜底")
    ap.add_argument("--bg", default=None,
                    help="给 App 图标垫个底色（如 #ffffff）；纯线稿设计稿需要它，菜单栏图不要用")
    ap.add_argument("--skip", default=None,
                    help="不画的图元序号，如 3-14 或 3,4,5（按解析顺序从 0 数）")
    ap.add_argument("--crop", metavar="x,y,w,h", default=None,
                    help="按设计稿坐标系裁一块出来渲染（状态栏图取顶部大圆用）")
    a = ap.parse_args()
    crop = tuple(float(v) for v in a.crop.split(",")) if a.crop else None
    skip: set[int] = set()
    for part in (a.skip or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-")
            skip |= set(range(int(lo), int(hi) + 1))
        else:
            skip.add(int(part))
    w, h = render(a.src, a.out, a.height, a.template, a.stroke_scale, a.width, crop,
                  a.min_stroke_px, skip or None, a.bg)
    print(f"{a.out}  {w}x{h}  {a.out.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
