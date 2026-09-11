"""悬浮状态条（对标 Wispr Flow 的 Flow Bar：贴屏幕底边、可左右拖、位置记住）。

为什么需要它：菜单栏在刘海屏上位置有限，本机实测右侧第三方区已排满（CPU/RAM/SSD 组件、
网速组件 + 一排应用图标），macOS 26 直接不给我们新状态项位置——连超宽文字测试项都画不出来。
所以常驻形态的「随时可见」不能只靠菜单栏图标，得有个不占位置的悬浮条。

架构（调研 boring.notch / DynamicNotchKit / NotchDrop 三个开源实现后定的，做法见 README 与提交历史）：**窗口固定尺寸、创建后绝不再改 frame；展开/收起动画
全部发生在窗口内容的 CALayer 上**（CASpringAnimation，弹簧在 Core Animation 渲染服务里插值，
Python 主线程不参与动画帧）。之前 60fps 每帧 `setFrame_display_` 改窗口尺寸的路线抖动严重，
调研确认三个成熟项目没有一个是这么做的——DynamicNotchKit 甚至把透明窗口开到半屏大，
它的 PR #44/#45 修的正是窗口动画 stutter。

- 透明像素自动点击穿透（borderless + clear 背景下窗口服务器按 alpha 命中），三个开源项目都
  靠这个；再叠一层 `PillView.hitTest_` 限定只有胶囊矩形内可拖/可点。
- 窗口 hasShadow=False（透明窗口的系统阴影会沿整个 frame 画大方框），阴影画在胶囊层上。
- 波形竖条 / 呼吸点是唯一需要连续变化的东西：一个只在录音/上屏中才跑的 30fps 定时器改
  几个子层的 bounds（纯 layer 属性，不碰窗口）。
- 两行布局：有内容（听写预览）时状态行（引导元素 +「听写中」）单独一行居中——标签本体永远
  在胶囊正中且胶囊在窗口里永远水平居中，所以**文字在屏幕上绝对不动**；内容在下面一行左右撑开。
"""

from __future__ import annotations

import math
import time

import objc
import Quartz
from AppKit import (NSBackingStoreBuffered, NSColor, NSFont, NSFloatingWindowLevel,
                    NSFontAttributeName, NSAttributedString, NSScreen, NSView, NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorStationary,
                    NSWindowStyleMaskBorderless, NSMakeRect)
from Foundation import NSObject, NSTimer

# 内容尺寸（胶囊实际大小按文字量出来，上限如下）
PAD_X, PAD_Y = 13.0, 6.0
ROW_GAP = 4.0                    # 状态行与内容行之间
MIN_W, MIN_H = 60.0, 30.0
MAX_W, MAX_LINES = 520.0, 4
MARGIN = 18.0                    # 胶囊离「可见区域底边」的距离（可见区域已排除 Dock 与菜单栏）

# 波形竖条（录音时显示，取代原来的圆点/呼吸点）
BAR_W, BAR_GAP, BAR_N = 3.0, 3.0, 5
BAR_MAX_H = 15.0
GAP = 7.0                        # 波形与文字之间

# 文本框要比「文字实测宽度」宽一点：文字引擎还有几 pt 边距，卡着实测宽会让最后一个字被裁
# （实测「听写中」量出 38pt，给 44pt 仍然只显示「听写」）。居中按实测文字的中心对齐。
SAFE_W, SLACK_H = 12.0, 3.0

# 固定窗口比最大内容大一圈：余量给层阴影（画在胶囊层上）和长文本
WIN_PAD_X = 30.0
WIN_BOTTOM = 20.0
WIN_W = MAX_W + 2 * WIN_PAD_X
WIN_H = 140.0

# 弹簧参数（CASpringAnimation）：约临界阻尼，到位置就停、不回弹
SPRING_STIFFNESS = 200.0
SPRING_DAMPING = 28.0

LEVEL_FULL_RMS = 0.05            # RMS 到这个值波形满格（本机实测正常说话 RMS 0.02~0.12）
LEVEL_FLOOR = 0.12               # 波形留一点底，不然静音时整条看起来像坏了

_STATUS_FONT = NSFont.systemFontOfSize_(12.5)


def _fade(layer, to: float, duration: float = 0.18) -> None:
    """透明度淡入淡出：符号出现/消失不做瞬时跳变（用户反馈「符号像从别处刷出来」）。"""
    anim = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
    anim.setDuration_(duration)
    anim.setFromValue_(layer.opacity())
    anim.setToValue_(to)
    layer.addAnimation_forKey_(anim, "opacity")
    layer.setOpacity_(to)


def _spring(layer, key: str, setter, to_value) -> None:
    """给 layer 的属性挂 CASpringAnimation 并落到目标值（渲染服务插值，不经 Python 主线程）。

    from 取「当前显示中的值」（presentationLayer）而不是模型值——连续两次目标变化时，
    新动画才不会从上一个目标值跳过去。
    """
    from Foundation import NSValue
    presented = layer.presentationLayer() or layer
    cur = (presented.bounds() if key == "bounds"
           else presented.position() if key == "position"
           else presented.cornerRadius())
    if key == "bounds":
        frm, to = NSValue.valueWithRect_(cur), NSValue.valueWithRect_(to_value)
    elif key == "position":
        frm, to = NSValue.valueWithPoint_(cur), NSValue.valueWithPoint_(to_value)
    else:                        # cornerRadius / opacity 等标量
        frm = cur
        to = to_value
    anim = Quartz.CASpringAnimation.animationWithKeyPath_(key)
    anim.setStiffness_(SPRING_STIFFNESS)
    anim.setDamping_(SPRING_DAMPING)
    anim.setMass_(1.0)
    anim.setDuration_(anim.settlingDuration())
    anim.setFromValue_(frm)
    anim.setToValue_(to)
    layer.addAnimation_forKey_(anim, key)
    setter(to_value)


class PillView(NSView):
    """整条都可点：左键弹菜单；按住拖动时自由跟手，松手吸附回底边（Wispr 的手感）。

    hitTest 限定只有胶囊矩形（留 4pt 余量）可命中——窗口比胶囊大很多，透明区域的点击必须
    让给下层 App：窗口服务器对 alpha=0 的像素本来就穿透，这里再挡一层保证透明区不触发拖拽。
    """

    def initWithHandler_(self, handler):
        self = objc.super(PillView, self).initWithFrame_(NSMakeRect(0, 0, 10, 10))
        if self is None:
            return None
        self.handler = handler
        self.on_moved = None          # 拖动结束时回调，用来记住位置
        self.hit = None               # 胶囊在窗口坐标里的矩形，由 Pill._apply 更新
        self._drag_origin = None
        return self

    def hitTest_(self, point):
        r = self.hit
        if r is None:
            return None
        if r[0] - 4 <= point.x <= r[0] + r[2] + 4 and r[1] - 4 <= point.y <= r[1] + r[3] + 4:
            return self
        return None

    def mouseDown_(self, event):
        self._drag_origin = (
            event.locationInWindow().x, event.locationInWindow().y,
            self.window().frame().origin.x, self.window().frame().origin.y)

    def mouseDragged_(self, event):
        if not self._drag_origin:
            return
        ox, oy, wx, wy = self._drag_origin
        dx = event.locationInWindow().x - ox
        dy = event.locationInWindow().y - oy
        if abs(dx) + abs(dy) < 3:      # 轻微抖动不算拖动，避免误伤单击
            return
        self.window().setFrameOrigin_((wx + dx, wy + dy))

    def mouseUp_(self, event):
        moved = self._drag_origin and (abs(self.window().frame().origin.x - self._drag_origin[2]) > 3
                                       or abs(self.window().frame().origin.y - self._drag_origin[3]) > 3)
        self._drag_origin = None
        if not moved:
            if self.handler:
                self.handler()
            return
        if isinstance(self.window(), PillWindow):
            self.window().snap_to_bottom()          # 松手吸附回底边，只保留横向位置
        if self.on_moved:
            f = self.window().frame()
            self.on_moved(f.origin.x + WIN_W / 2)            # 回报胶囊中心（窗口永远居中装胶囊）


class PillWindow(NSWindow):
    """不抢焦点：canBecomeKeyWindow 恒 False（否则点一下就把用户的输入焦点抢走）。"""

    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False

    def snap_to_bottom(self) -> None:
        """吸附到 Dock 上方的底边，横向夹在屏幕内（Wispr 的条就是贴着底边左右滑）。"""
        from AppKit import NSScreen
        v = NSScreen.mainScreen().visibleFrame()
        x = min(max(self.frame().origin.x, v.origin.x + 8 - WIN_PAD_X),
                v.origin.x + v.size.width - WIN_W + WIN_PAD_X - 8)
        self.setFrameOrigin_((x, v.origin.y + self.margin))


class Pill(NSObject):
    # 引导元素：只有「波形」一种（录音时显示）。其余状态就是纯文字居中——圆点挂在文字边上
    # 永远不可能同时「符号居中」和「文字居中」，用户拍板去掉（2026-09-10）。
    LEAD_NONE, LEAD_WAVE = "none", "wave"
    # 兼容旧常量
    LEAD_DOT, LEAD_PULSE = LEAD_NONE, LEAD_NONE

    def initWithHandler_(self, handler):
        self = objc.super(Pill, self).init()
        if self is None:
            return None
        self.center_x = None
        self.margin = MARGIN - WIN_BOTTOM     # 窗口底边相对可见区域底边的距离

        self.win = PillWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIN_W, WIN_H), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False)
        self.win.setLevel_(NSFloatingWindowLevel)
        self.win.setOpaque_(False)
        self.win.setBackgroundColor_(NSColor.clearColor())
        self.win.setHasShadow_(False)         # 透明窗口的系统阴影会沿整个 frame 画大方框
        self.win.setMovableByWindowBackground_(False)
        self.win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary)
        self.win.margin = self.margin

        self.view = PillView.alloc().initWithHandler_(handler)
        self.view.setFrame_(NSMakeRect(0, 0, WIN_W, WIN_H))
        self.view.setWantsLayer_(True)
        self.win.setContentView_(self.view)

        # 胶囊：一个 CALayer，尺寸/位置用 CASpringAnimation 动（渲染服务插值，不经 Python）
        self.capsule = Quartz.CALayer.layer()
        self.capsule.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.08, 0.82).CGColor())
        self.capsule.setShadowOpacity_(0.30)
        self.capsule.setShadowRadius_(8.0)
        self.capsule.setShadowOffset_((0.0, -2.0))
        self.view.layer().addSublayer_(self.capsule)

        # 裁剪层：与胶囊同尺寸同步弹簧，masksToBounds 把文字/波形框住。
        # 没有它，状态切换时子层已经摆在「最终位置」，胶囊还在从小往大弹——波形会短暂
        # 露在框外面（用户反馈）。阴影留在胶囊层上（masksToBounds 会裁掉自己层的阴影）。
        self.clip = Quartz.CALayer.layer()
        self.clip.setMasksToBounds_(True)
        self.view.layer().addSublayer_(self.clip)

        # 两行文字是胶囊的子 layer，跟着胶囊走
        # 文字/波形挂「窗口层」而不是胶囊层：胶囊的尺寸弹簧动画就不会带着它们滑——
        # 它们的屏幕位置只跟胶囊最终中心（恒定）挂钩，与动画完全解耦（用户要求「符号和
        # 听写中绑在一起，不随其他动画动」）。
        self.t_status, self.t_detail = self._new_text(), self._new_text()
        self.bars = [self._new_bar(NSColor.whiteColor()) for _ in range(BAR_N)]

        self._status = ""
        self._detail = ""
        self._color = NSColor.whiteColor()
        self._lead = self.LEAD_NONE
        self._pulse = 0.0
        self._rms = 0.0                  # 音频线程写、UI tick 读（单个 float 赋值，CPython 下原子）
        self._level = 0.0                # 平滑后的显示电平（快起慢落）
        self._meas: dict = {}
        self._timer = None
        return self

    @objc.python_method
    def _new_text(self):
        tl = Quartz.CATextLayer.layer()
        tl.setFont_(_STATUS_FONT)
        tl.setFontSize_(12.5)
        tl.setAlignmentMode_("center")
        tl.setWrapped_(True)   # 注意：CATextLayer 的属性名是 isWrapped，setter 是 setWrapped_
        tl.setTruncationMode_("none")
        tl.setContentsScale_(NSScreen.mainScreen().backingScaleFactor())
        tl.setForegroundColor_(NSColor.whiteColor().CGColor())
        self.clip.addSublayer_(tl)
        return tl

    @objc.python_method
    def _new_bar(self, color):
        lay = Quartz.CALayer.layer()
        lay.setBackgroundColor_(color.CGColor())
        lay.setCornerRadius_(1.5)
        lay.setOpacity_(0.0)
        lay.setBounds_(NSMakeRect(0, 0, BAR_W, 6.0))
        self.clip.addSublayer_(lay)
        return lay

    # ---------- 度量 ----------
    @objc.python_method
    def _measure(self, layer, text: str, max_inner: float):
        """量一段文字：返回（单行自然宽，按文本框实际宽度折行后的高）。

        用 boundingRect 而不是 cellSizeForBounds：后者会多算约 8pt 内边距，导致内容整体偏左
        （实测「空闲」量出 33pt、实际只有 25pt）。按 (哪行, 文本, 可用宽) 缓存——预览每 0.8 秒
        才变一次，没必要反复量。
        """
        key = (layer is self.t_detail, text, round(max_inner, 1))
        hit = self._meas.get(key)
        if hit is not None:
            return hit
        if not text:
            return 0.0, 0.0
        from AppKit import (NSStringDrawingUsesFontLeading, NSStringDrawingUsesLineFragmentOrigin)
        from Foundation import NSMakeSize
        opts = NSStringDrawingUsesLineFragmentOrigin | NSStringDrawingUsesFontLeading

        # 不用 CATextLayer.attributedString（PyObjC 没暴露 getter），直接用同字体构造
        att = NSAttributedString.alloc().initWithString_attributes_(
            text, {NSFontAttributeName: _STATUS_FONT})

        def box(width):
            r = att.boundingRectWithSize_options_(NSMakeSize(width, 10000.0), opts)
            return float(r.size.width), float(r.size.height)

        tw, th = box(max_inner)
        if tw <= 0:                                   # 空串等退化情况
            tw, th = 15.0, 15.0
        tw = min(float(math.ceil(tw)), max_inner)
        if tw > 0.5 * max_inner:                      # 长文本：按文本框实际宽度重量一次高度
            _, th = box(min(max_inner, tw + SAFE_W))
        if len(self._meas) > 96:
            self._meas.clear()
        self._meas[key] = (tw, th)
        return tw, th

    @objc.python_method
    def _targets(self):
        """这一帧的目标几何（都是「最终值」，交给动画去插值）。

        胶囊在窗口里永远水平居中（中心 x = WIN_W/2，恒定）、底边固定在 WIN_BOTTOM——
        所以内容只要以胶囊中心定位，屏幕位置就与胶囊宽度无关，动画碰不到它。
        """
        bars_w = BAR_N * BAR_W + (BAR_N - 1) * BAR_GAP
        lead_w = bars_w if self._lead == self.LEAD_WAVE else 0.0
        gap = GAP if lead_w else 0.0                    # 没有符号就不留符号间距
        inner = MAX_W - 2 * PAD_X
        status_w, status_h = self._measure(self.t_status, self._status, inner - lead_w - gap)
        status_h = status_h or 15.0
        row1_w = (lead_w + gap + status_w) if self._status else 0.0
        detail_w, detail_h = self._measure(self.t_detail, self._detail, inner)
        stacked = bool(self._detail)
        row2_w, row2_h = (detail_w, detail_h + SLACK_H) if stacked else (0.0, 0.0)

        cw = max(MIN_W, min(MAX_W, max(row1_w, row2_w) + 2 * PAD_X))
        ch = max(MIN_H, PAD_Y * 2 + status_h + SLACK_H + ((ROW_GAP + row2_h) if stacked else 0.0))
        return {
            "cap": NSMakeRect((WIN_W - cw) / 2, WIN_BOTTOM, cw, ch),
            "status_w": status_w,
            "status_h": status_h,
            "status_h1": status_h + SLACK_H,
            "single_h": self._measure(self.t_status, self._status, 10000.0)[1] or 15.0,
            "lead_w": lead_w,
            "row1_w": row1_w,
            "detail_w": detail_w,
            "detail_h": detail_h,
            "stacked": stacked,
        }

    # ---------- 对外 API ----------
    @objc.python_method
    def set_status(self, text: str, color=None, lead: str = LEAD_NONE,
                   detail: str | None = None) -> None:
        """设置状态行（`text` + 引导元素）与内容行（`detail`）。

        `detail` 非空就走两行：状态行居中在上面（标签本体永远在胶囊正中，屏幕上不动），
        内容在下面一行左右撑开。预览每 0.8 秒更新一次，每次只在这里设一次动画目标；
        什么都不变时直接返回（主程序每 0.12 秒会调一次）。
        """
        if color is not None:
            self._color = color
        detail = detail or ""
        changed_status = (text != self._status) or (lead != self._lead)
        changed_detail = detail != self._detail
        if not changed_status and not changed_detail:
            if color is not None:
                self._apply_style()
            return
        self._status, self._detail, self._lead = text, detail, lead
        # 文字先落到 CATextLayer，度量（attributedString）才是新文本
        self.t_status.setString_(self._status)
        self.t_detail.setString_(self._detail)
        self._apply_style()
        self._apply()
        self._log_geometry()
        if self._lead in (self.LEAD_WAVE, self.LEAD_PULSE):
            self._ensure_timer()
        else:
            self._stop_timer()

    @objc.python_method
    def _apply_style(self) -> None:
        self.t_status.setForegroundColor_(self._color.CGColor())
        self.t_detail.setForegroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.92).CGColor())
        for lay in self.bars:
            lay.setBackgroundColor_(self._color.CGColor())

    @objc.python_method
    def set_text(self, text: str, color=None) -> None:
        """兼容旧调用：只换状态行文字/颜色，引导元素与内容行保持当前状态。"""
        self.set_status(text, color, self._lead, self._detail)

    @objc.python_method
    def set_level(self, rms: float) -> None:
        """音频回调线程每块调一次（实时 RMS）。只存值，30fps tick 自己读。"""
        self._rms = float(rms)
        if self._lead == self.LEAD_WAVE:
            self._ensure_timer()

    @objc.python_method
    def place_bottom(self, center_x: float | None = None) -> None:
        """贴屏幕底部（Dock 上方）；center_x 不传就用屏幕中心。存的是「中心」不是左边缘，
        这样宽度变化时以中心为锚左右对称展开，不会越展越偏。"""
        self.center_x = center_x
        self._place_window()
        self._apply()

    # 兼容旧调用
    @objc.python_method
    def place_top_center(self) -> None:
        self.place_bottom()

    @objc.python_method
    def menu_anchor(self):
        """胶囊左下角在窗口坐标里的位置（弹菜单用）。窗口是固定大框，不能再拿 (0,0) 当锚点。"""
        t = self._targets()
        return (t["cap"].origin.x, t["cap"].origin.y - 6)

    @objc.python_method
    def _place_window(self) -> None:
        from AppKit import NSScreen
        scr = NSScreen.mainScreen().visibleFrame()
        cx = self.center_x if self.center_x is not None else scr.origin.x + scr.size.width / 2
        x = min(max(cx - WIN_W / 2, scr.origin.x + 8 - WIN_PAD_X),
                scr.origin.x + scr.size.width - WIN_W + WIN_PAD_X - 8)
        self.win.setFrameOrigin_((x, scr.origin.y + self.margin))

    @objc.python_method
    def show(self) -> None:
        self.win.orderFrontRegardless()
        self._place_window()
        self._apply()

    @objc.python_method
    def hide(self) -> None:
        self.win.orderOut_(None)
        self._stop_timer()

    @objc.python_method
    def set_visible(self, visible: bool) -> None:
        self.show() if visible else self.hide()

    # ---------- 布局：只在状态/预览变化时设一次动画目标 ----------
    @objc.python_method
    def _apply(self) -> None:
        """把当前状态的目标几何交给 Core Animation。只在状态/预览变化时调用（约 0.8 秒一次），
        绝不做每帧循环——弹簧由渲染服务插值，Python 主线程不参与动画。"""
        t = self._targets()
        cap = t["cap"]
        cw, ch = cap.size.width, cap.size.height
        cx, cy = WIN_W / 2, WIN_BOTTOM + ch / 2      # 胶囊中心：窗口坐标里的恒定量

        Quartz.CATransaction.begin()
        for lay in (self.capsule, self.clip):
            _spring(lay, "bounds", lay.setBounds_, NSMakeRect(0, 0, cw, ch))
            _spring(lay, "position", lay.setPosition_, (cx, cy))
            _spring(lay, "cornerRadius", lay.setCornerRadius_, min(15.0, ch / 2))
        Quartz.CATransaction.commit()

        # 内容（文字/波形）也用同参数弹簧动起来：胶囊 bounds 和内容 position 以同样的
        # 弹簧插值，内容在屏幕上永远「贴」在胶囊中心——形状怎么弹，内容就怎么跟，不分家。
        # 坐标是胶囊本地系（相对 clip.bounds 原点），超出部分被 masksToBounds 裁掉
        # （修「波形短暂超出框」）。
        # 内容挂在 clip 下 → 用**胶囊本地坐标**（本地中心 = cw/2）；加上与胶囊同参数的
        # 位置弹簧后，内容的屏幕位置 = 窗口中心，恒定不动，且永远被框住。
        lcx = cw / 2
        label_w = t["status_w"] + SAFE_W
        gap = GAP if t["lead_w"] else 0.0
        row1_left = lcx - t["row1_w"] / 2                    # 状态行左缘（组居中）
        status_cx = row1_left + t["lead_w"] + gap + t["status_w"] / 2  # 文字视觉中心
        status_cy = ch - PAD_Y - t["status_h1"] / 2          # 状态行文字中心（本地）
        detail_cy = PAD_Y + (t["detail_h"] + SLACK_H) / 2
        bars_w = BAR_N * BAR_W + (BAR_N - 1) * BAR_GAP
        bars_right = row1_left + t["lead_w"]

        Quartz.CATransaction.begin()
        _spring(self.t_status, "position", self.t_status.setPosition_, (status_cx, status_cy))
        _spring(self.t_detail, "position", self.t_detail.setPosition_, (lcx, detail_cy))
        wave = 1.0 if self._lead == self.LEAD_WAVE else 0.0
        for i, bar in enumerate(self.bars):
            _spring(bar, "position", bar.setPosition_,
                    (bars_right - bars_w + i * (BAR_W + BAR_GAP) + BAR_W / 2, status_cy))
            _fade(bar, wave)
        self.t_status.setBounds_(NSMakeRect(0, 0, label_w, t["status_h1"]))
        self.t_detail.setBounds_(NSMakeRect(0, 0, t["detail_w"] + SAFE_W,
                                            t["detail_h"] + SLACK_H))
        self.t_detail.setHidden_(not t["stacked"])
        self.t_detail.setOpacity_(1.0 if t["stacked"] else 0.0)
        Quartz.CATransaction.commit()

        # 命中区域跟随胶囊（留 4pt 手感余量）
        self.view.hit = (cap.origin.x - 4, cap.origin.y - 4,
                         cap.size.width + 8, cap.size.height + 8)

    # ---------- 30fps tick：只更新波形/呼吸的 bounds（纯 layer 属性，不碰窗口） ----------
    @objc.python_method
    def _ensure_timer(self) -> None:
        if self._timer is None and self.win.isVisible():
            self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1 / 30.0, self, "tick:", None, True)

    @objc.python_method
    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.invalidate()
            self._timer = None

    def tick_(self, _timer):
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        if self._lead == self.LEAD_WAVE:
            raw = min(1.0, max(0.0, (self._rms / LEVEL_FULL_RMS) ** 0.6))
            self._level = max(raw, self._level * 0.82)
            for i, bar in enumerate(self.bars):
                ph = 0.5 + 0.5 * math.sin(time.monotonic() * 6.0 + i * 0.9)   # 每根条不同相位，像波形
                bh = 6.0 + (BAR_MAX_H - 6.0) * (LEVEL_FLOOR + (1 - LEVEL_FLOOR) * self._level) * ph
                bar.setBounds_(NSMakeRect(0, 0, BAR_W, bh))
        else:                                     # 上屏中：文字轻微呼吸，当「工作中」的反馈
            self._pulse += 0.06
            self.t_status.setOpacity_(0.62 + 0.38 * abs(math.sin(self._pulse)))
        Quartz.CATransaction.commit()

    @objc.python_method
    def _log_geometry(self) -> None:
        """几何日志：只在目标宽度明显变化时打一行（排查「看起来不居中」用）。"""
        t = self._targets()
        cap = t["cap"]
        if abs(cap.size.width - getattr(self, "_logged_w", -999)) < 8:
            return
        self._logged_w = cap.size.width
        from datetime import datetime as _dt
        tail = f"内容 {self._detail[:16]!r}" if t["stacked"] else "单行"
        print(f"{_dt.now().strftime('%H:%M:%S')}  悬浮条 目标 {cap.size.width:.0f}x{cap.size.height:.0f}"
              f"（状态 {self._status[:12]!r}；{tail}）", flush=True)
