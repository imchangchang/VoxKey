#!/usr/bin/env python3
"""冒烟自检：改完代码先跑这个。不连设备、不需要权限，几秒钟出结果。

覆盖：
1. 包内模块全部可导入（含设备监视/探针这类独立入口）
2. 悬浮条四态的几何断言：文字/波形都落在裁剪层内、水平居中（不是「看起来差不多」，是数值）
3. 模型能加载（需要模型目录存在；用 VOXKEY_MODELS_DIR 指到别处也行）
4. 命令行入口的 --help 能跑

用法（仓库根目录）：
  PYTHONPATH=src python tools/smoke.py              # 全部
  PYTHONPATH=src python tools/smoke.py --no-model   # 跳过模型加载（没下模型时）
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
GREEN, RED, DIM, END = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def check(name: str, fn) -> bool:
    t0 = time.monotonic()
    try:
        detail = fn() or ""
        print(f"{GREEN}✓{END} {name} {DIM}{(time.monotonic() - t0) * 1000:.0f}ms{END} {detail}")
        return True
    except Exception as e:  # noqa: BLE001 —— 自检脚本，失败就把原因原样打出来
        print(f"{RED}✗{END} {name}：{type(e).__name__}: {e}")
        return False


def imports_ok() -> str:
    import voxkey.app, voxkey.audio, voxkey.inject, voxkey.models  # noqa: F401
    import voxkey.pill, voxkey.transcribe, voxkey.device.monitor  # noqa: F401
    return ""


def pill_geometry() -> str:
    """四种状态下：文字/波形都在裁剪层内；无符号时文字居中偏差 0pt，有波形时整组居中 <5pt。"""
    import AppKit
    from Foundation import NSTimer, NSObject
    from voxkey.pill import Pill

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    pill = Pill.alloc().initWithHandler_(lambda: None)
    pill.place_bottom(None)
    pill.show()
    W, RED, BLUE = (AppKit.NSColor.whiteColor(), AppKit.NSColor.systemRedColor(),
                    AppKit.NSColor.systemBlueColor())
    cases = [
        ("空闲", W, Pill.LEAD_NONE, None, False),
        ("听写中", RED, Pill.LEAD_WAVE, "今天下午三点", False),
        ("听写中", RED, Pill.LEAD_WAVE, "今天下午三点开会记得带电脑和充电器", False),
        ("上屏中", BLUE, Pill.LEAD_NONE, None, True),
        ("空闲", W, Pill.LEAD_NONE, None, False),
    ]
    # 不能用 app.run()：没有 app bundle/delegate 时 terminate_ 退不出来，函数会卡死。
    # 手动泵 run loop 更可控——定时器照常触发，Python 侧还能自己看超时。
    from Foundation import NSDate, NSRunLoop

    t0, cur, done, fails = time.monotonic(), [-1], set(), []

    class Probe(NSObject):
        def tick_(_self, _t):
            try:
                _self._tick()
            except Exception as e:               # 定时器里抛异常会被 AppKit 吞掉，这里显式暴露
                fails.append(f"{type(e).__name__}: {e}")
                done.add(-1)

        def _tick(_self):
            el = time.monotonic() - t0
            k = min(int(el / 1.2), len(cases) - 1)
            if cur[0] != k:
                cur[0] = k
                pill.set_status(*cases[k])
            if el > 1.2 * k + 0.9 and k not in done:
                cw = pill.capsule.bounds().size.width
                st = pill.t_status.frame()
                wave = cases[k][2] == Pill.LEAD_WAVE
                if not (0 <= st.origin.x and st.origin.x + st.size.width <= cw):
                    fails.append(f"{cases[k][0]} 文字出框")
                if wave:
                    bf = [b.frame() for b in pill.bars]
                    bl = min(f.origin.x for f in bf)
                    br = max(f.origin.x + f.size.width for f in bf)
                    if bl < -0.01 or br > cw + 0.01:
                        fails.append(f"{cases[k][0]} 波形出框")
                    off = (bl + st.origin.x + st.size.width) / 2 - cw / 2
                    if abs(off) > 5:
                        fails.append(f"{cases[k][0]} 状态组偏心 {off:+.1f}pt")
                else:
                    off = st.origin.x + st.size.width / 2 - cw / 2
                    if abs(off) > 0.6:
                        fails.append(f"{cases[k][0]} 文字偏心 {off:+.1f}pt")
                # 30fps 定时器只在需要动的时候开：有波形或呼吸态开着，其余必须停。
                # （曾经因为拿引导元素常量当状态判断，条件恒真、定时器永不停，空闲文字一直在闪）
                want_timer = wave or cases[k][4]
                if want_timer and pill._timer is None:
                    fails.append(f"{cases[k][0]} 该动却没开定时器")
                if not want_timer and pill._timer is not None:
                    fails.append(f"{cases[k][0]} 不该动却还开着定时器")
                if not cases[k][4] and pill.t_status.opacity() < 0.999:
                    fails.append(f"{cases[k][0]} 呼吸没复位（opacity={pill.t_status.opacity():.2f}）")
                done.add(k)

    probe = Probe.alloc().init()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1 / 30.0, probe, "tick:", None, True)
    deadline = time.monotonic() + 12
    while len(done) < len(cases) and time.monotonic() < deadline:
        NSRunLoop.currentRunLoop().runMode_beforeDate_(
            "NSDefaultRunLoopMode", NSDate.dateWithTimeIntervalSinceNow_(0.02))
    if not done or -1 in done:
        raise AssertionError("；".join(fails) or "没有量到任何状态")
    if len(done) < len(cases):
        fails.append(f"只量到 {len(done)}/{len(cases)} 个状态（超时）")
    if fails:
        raise AssertionError("；".join(fails))
    return "四态内容均在框内且居中"


def pill_long_preview() -> str:
    """长语音预览：内容行不能超过 MAX_LINES 行、不能顶出窗口，超出时保留最近的（尾巴）。"""
    import AppKit
    from voxkey import pill as P

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    p = P.Pill.alloc().initWithHandler_(lambda: None)
    p.place_bottom(None)
    p.show()

    def check_case(text, want_ellipsis):
        p.set_status("听写中", AppKit.NSColor.systemRedColor(), P.Pill.LEAD_WAVE, text)
        t = p._targets()
        cap_h = t["cap"].size.height
        shown = p._shown_detail
        if t["detail_h"] > P.MAX_LINES * P.LINE_H + 0.5:
            raise AssertionError(f"内容 {len(shown)} 字却有 {t['detail_h']}pt（> {P.MAX_LINES} 行）")
        if cap_h > P.WIN_H - P.WIN_BOTTOM:
            raise AssertionError(f"胶囊 {cap_h}pt 顶出窗口（窗口 {P.WIN_H}）")
        if want_ellipsis:
            if not shown.startswith("…"):
                raise AssertionError("超长内容没有省略号标记")
            if not text.endswith(shown[1:]):
                raise AssertionError("裁掉的不是开头（应保留最近说的）")
        elif shown != text:
            raise AssertionError(f"短内容被改了：{shown!r} != {text!r}")
        return len(shown)

    # 一句短预览：原样显示
    check_case("今天下午三点开会记得带电脑和充电器", False)
    # 长语音：40 秒上下的话，远超 6 行 → 保留尾巴
    long_text = ("今天下午三点开会记得带电脑和充电器顺便把周报发给我" * 12)
    kept = check_case(long_text, True)
    return f"短句原样显示；{len(long_text)} 字裁到 {kept} 字（保留尾部）"


def pill_wanted_table() -> str:
    """悬浮条该不该出现：真值表断言（用户要求——空闲收起不占位置，听写/上屏/异常才出现）。"""
    from voxkey.app import FAULT_HOLD_S, pill_wanted

    base = {"phase": "idle", "connected": True, "paused": False, "post_ok": True,
            "mic_ok": True, "injected": "", "reason": "", "preview": "", "last_text": "",
            "device_off": False}
    # (说明, 状态补丁, 距上次结果多久, 期望可见, auto)
    cases = [
        ("空闲且一切正常 → 收起", {}, 99.0, False, True),
        ("听写中 → 出现", {"phase": "rec"}, 99.0, True, True),
        ("上屏中 → 出现", {"phase": "proc"}, 99.0, True, True),
        ("设备掉线 → 出现（指示没插入）", {"connected": False}, 99.0, True, True),
        ("设备还在探测 → 出现", {"connected": None}, 99.0, True, True),
        ("接收器插着但设备关机 → 出现", {"connected": False, "device_off": True}, 99.0, True, True),
        ("暂停监听 → 出现", {"paused": True}, 99.0, True, True),
        ("缺辅助功能权限 → 出现", {"post_ok": False}, 99.0, True, True),
        ("麦克风没权限 → 出现", {"mic_ok": False}, 99.0, True, True),
        ("模型加载失败 → 出现", {"phase": "err"}, 99.0, True, True),
        ("刚上屏失败 → 出现一会儿", {"injected": "未上屏：没有辅助功能权限"}, 1.0, True, True),
        ("上屏失败已过去 → 收起",
         {"injected": "未上屏：没有辅助功能权限"}, FAULT_HOLD_S + 1.0, False, True),
        ("菜单里关掉自动显示 → 永不出现", {"phase": "rec"}, 0.0, False, False),
    ]
    fails = []
    for name, patch, age, want, auto in cases:
        got = pill_wanted({**base, **patch}, age, auto)
        if got != want:
            fails.append(f"{name}：期望 {want} 实得 {got}")
    if fails:
        raise AssertionError("；".join(fails))
    return f"{len(cases)} 条规则全对"


def pill_show_hide() -> str:
    """收起再显示之后，波形定时器必须自己起来（隐藏期间 set_status 建不了它）。"""
    import AppKit
    from voxkey import pill as P

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    p = P.Pill.alloc().initWithHandler_(lambda: None)
    p.place_bottom(None)
    p.show()
    p.hide()
    if p.win.isVisible():
        raise AssertionError("hide() 之后窗口还可见")
    # 隐藏状态下切到「听写中」：这时窗口还没出来，定时器不该建
    p.set_status("听写中", AppKit.NSColor.systemRedColor(), P.Pill.LEAD_WAVE, "今天下午三点")
    if p._timer is not None:
        raise AssertionError("窗口不可见时不该起 30fps 定时器")
    p.show()
    if not p.win.isVisible():
        raise AssertionError("show() 之后窗口还不可见")
    if p._timer is None:
        raise AssertionError("重新显示后波形定时器没起来（波形会冻住）")
    p.hide()
    if p._timer is not None:
        raise AssertionError("收起后定时器没停")
    return "隐藏时不建定时器；重新显示后自己起来；收起时停掉"


def pill_meta() -> str:
    """角标（左上版本 / 右上电量）：不出框、不压状态文字、中间的状态组仍然居中。"""
    import AppKit
    from voxkey import pill as P

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    p = P.Pill.alloc().initWithHandler_(lambda: None)
    p.place_bottom(None)
    p.show()

    # 窄状态（上屏中那种小条）最容易挤：角标必须有地方放，且不能压到状态文字
    for text, detail in (("上屏中", None), ("听写中", "今天下午三点开会记得带电脑和充电器")):
        p.set_meta("v4.4.0", "30%")
        p.set_status(text, AppKit.NSColor.systemBlueColor(), P.Pill.LEAD_NONE, detail)
        t = p._targets()
        cw = t["cap"].size.width
        sf = p.t_status.frame()
        ml, mr = p.t_meta_l.frame(), p.t_meta_r.frame()
        if mr.origin.x + mr.size.width > cw + 0.01 or ml.origin.x < -0.01:
            raise AssertionError(f"{text}：角标出框（胶囊 {cw:.0f}，左 {ml.origin.x:.1f} 右 {mr.origin.x + mr.size.width:.1f}）")
        if sf.origin.x < ml.origin.x + ml.size.width - 0.01:
            raise AssertionError(f"{text}：状态文字压到左上角标")
        if sf.origin.x + sf.size.width > mr.origin.x + 0.01:
            raise AssertionError(f"{text}：状态文字压到右上角标")
        off = sf.origin.x + sf.size.width / 2 - cw / 2
        if abs(off) > 0.6:
            raise AssertionError(f"{text}：加了角标之后状态文字不再居中（偏 {off:+.1f}pt）")

    # 同一个状态对比：带角标 vs 清空角标，胶囊应该收窄
    p.set_meta("v4.4.0", "30%")
    p.set_status("上屏中", AppKit.NSColor.systemBlueColor(), P.Pill.LEAD_NONE, None)
    with_meta = p._targets()["cap"].size.width
    p.set_meta("", "")
    without = p._targets()["cap"].size.width
    if not (p.t_meta_l.isHidden() and p.t_meta_r.isHidden()):
        raise AssertionError("角标清空之后没有藏起来")
    if without >= with_meta:
        raise AssertionError(f"角标清空之后胶囊没收窄（{with_meta:.0f} → {without:.0f}）")
    return f"角标不出框、不压状态文字；清空后藏起来并收窄（{with_meta:.0f}→{without:.0f}）"


def model_ok() -> str:
    from voxkey.models import MODELS, load_recognizer
    if not MODELS.is_dir():
        raise FileNotFoundError(f"模型目录不存在：{MODELS}（用 VOXKEY_MODELS_DIR 覆盖）")
    rec = load_recognizer("funasr-nano-int8")
    return type(rec).__name__


def cli_help(cmd: list[str]) -> str:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": "src"})
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1])
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="VoxKey 冒烟自检")
    ap.add_argument("--no-model", action="store_true", help="跳过模型加载（没下模型时）")
    args = ap.parse_args()

    ok = True
    ok &= check("包导入", imports_ok)
    ok &= check("悬浮条几何断言", pill_geometry)
    ok &= check("长语音预览裁剪", pill_long_preview)
    ok &= check("悬浮条显示规则", pill_wanted_table)
    ok &= check("悬浮条收起再显示", pill_show_hide)
    ok &= check("悬浮条角标（版本/电量）", pill_meta)
    if args.no_model:
        print(f"{DIM}— 跳过模型加载{END}")
    else:
        ok &= check("模型加载（funasr-nano-int8）", model_ok)
    for name, cmd in (("tools/ptt_demo.py", [PY, "tools/ptt_demo.py", "--help"]),
                      ("tools/device_probe.py", [PY, "tools/device_probe.py", "--help"]),
                      ("voxkey.device.monitor", [PY, "-m", "voxkey.device.monitor", "--help"]),
                      ("tools/verify/run_verify.py", [PY, "tools/verify/run_verify.py", "--help"])):
        ok &= check(f"{name} --help", lambda c=cmd: cli_help(c))
    print(f"\n{'全部通过' if ok else '有失败项'}（{'跳过模型' if args.no_model else '含模型'}）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
