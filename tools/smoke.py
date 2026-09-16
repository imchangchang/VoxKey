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



def pipeline_decoder_source() -> str:
    """pipeline 的解码器必须是「用时现取」，不能自己存一份。

    真踩过：构造 SpeakingPipeline 时传 decoder=None 打算「模型加载完再补」，结果忘了补，
    于是模型明明加载成功，一说完整句就 AttributeError（炸在子线程里，用户只看到「处理中」卡住）。
    现在要求 provider 形式，并就地把「provider 返回 None 时必须明确报错」验一遍。
    """
    import numpy as np
    from voxkey.pipeline import PipelineConfig, SpeakingPipeline

    class Inj:
        last_newlines = 0
        def inject(self, t): return "已上屏"

    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return None          # 模拟模型还没加载完

    p = SpeakingPipeline(get_decoder=provider, config=PipelineConfig(0.5), injector=Inj())
    try:
        p.transcribe(np.zeros(16000, dtype="float32"))
    except RuntimeError as e:
        if "模型" not in str(e):
            raise AssertionError(f"报错信息看不懂：{e}")
        return f"provider 形式且空解码器会明确报错（调用了 {calls['n']} 次）"
    except AttributeError as e:
        raise AssertionError(f"还是 AttributeError 的老毛病：{e}")
    raise AssertionError("空解码器居然没报错")


def log_lines_not_interleaved() -> str:
    """多线程打日志不能串行（两条日志叠成一行）。

    print() 分几次写（正文、分隔符、换行），多线程下会交错——真出现过两条日志挤在一行，
    排障时没法看。现在 log() 加锁并整行一次写出，这里用并发压一压验证。
    """
    import io
    import threading
    from contextlib import redirect_stdout
    from voxkey.logging import log

    buf = io.StringIO()
    with redirect_stdout(buf):
        threads = [threading.Thread(target=lambda i=i: [log("测试", f"线程{i}-第{j}条") for j in range(50)])
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    lines = buf.getvalue().splitlines()
    n = 8 * 50
    if len(lines) != n:
        raise AssertionError(f"期望 {n} 行，实际 {len(lines)} 行（说明有交错）")
    bad = [l for l in lines if l.count("  ") < 2 or "测试" not in l]
    if bad:
        raise AssertionError(f"格式不对：{bad[:2]}")
    return f"8 线程 × 50 条 = {n} 行，无交错"


def structure_rules() -> str:
    """静态结构约定（扫源码，不连设备）：

    1) `threading.Thread(args=(x))` 漏掉尾逗号 = args 不是元组。Thread 自己不校验，
       等 start 时才在子线程里炸，静默失效；
    2) `self.current_stop = (x)` 同理（它本该是单元素元组）。
    这两条都是真踩过的：一次用正则清悬空逗号时把 `(stop_ev,)` 改成了 `(stop_ev)`，
    整个听写功能失效，而且炸在子线程里、日志只留下一句「按键读取中断」，很难看出真因。
    用正则而不是 AST，因为 AST 里 `(x)` 和 `x` 是同一个节点，分不出来。
    """
    import re
    import pathlib as _p

    root = _p.Path(__file__).resolve().parents[1] / "src" / "voxkey"
    rules = [
        (r"args=\(\s*[A-Za-z_][\w.]*\s*\)", "Thread(args=) 是单个名字且没尾逗号，不是元组"),
        (r"current_stop\s*=\s*\(\s*[A-Za-z_][\w.]*\s*\)",
         "current_stop 赋成了非元组（漏了尾逗号）"),
    ]
    fails = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat, msg in rules:
                if re.search(pat, line):
                    fails.append(f"{path.name}:{lineno} {msg}")
    if fails:
        raise AssertionError("；".join(fails))
    return f"{len(rules)} 条结构约定都还在"


def key_callback_survives() -> str:
    """按键回调抛异常时，读线程必须继续活着（曾经回调里的类型错误把线程搞死了）。"""
    import threading
    from voxkey.device.keyreader import DeviceKeyReader, parse_report

    calls = {"n": 0}

    def boom(mods, keys):
        calls["n"] += 1
        raise TypeError("模拟回调里的类型错误")

    r = DeviceKeyReader(boom)

    class FakeDev:
        def read(self, n):
            # report id 3 + 修饰位 + 保留 + 语音键
            return bytes([3, 0x08, 0, 0x0B, 0, 0, 0, 0, 0])

    r._dev = FakeDev()
    r._thread = threading.Thread(target=r._loop, daemon=True)
    r._thread.start()
    time.sleep(0.15)
    alive = r.is_alive()
    r._stop.set()
    r._thread.join(timeout=1)
    if calls["n"] == 0:
        raise AssertionError("回调根本没被调用（假报文没走通）")
    if not alive:
        raise AssertionError("回调抛异常把读线程带走了")
    if r.error is None:
        raise AssertionError("回调异常没被记下来")
    return f"回调抛了 {calls['n']} 次异常，线程仍活着"


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


def pill_long_detail() -> str:
    """长语音内容裁剪：内容行不能超过 MAX_LINES 行、不能顶出窗口，超出时保留最近的（尾巴）。"""
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
    """悬浮条该不该出现：真值表断言（空闲收起不占位置；听写/处理/异常/充电才出现）。

    这里有两个不同的「年龄」，别混：
      fault_age   距上一次「未上屏」结果多久（决定失败提示还留不留）
      batt_age    距上一次读到电量/充电状态多久（不再周期探测，久了就不信它）
    """
    from voxkey.app import BATTERY_FRESH_S, FAULT_HOLD_S, pill_wanted

    base = {"phase": "idle", "connected": True, "paused": False, "post_ok": True,
            "mic_ok": True, "injected": "", "reason": "", "last_text": "",
            "device_off": False, "battery_charging": False}
    OLD = 1e9
    # (说明, 状态补丁, fault_age, 期望可见, auto, batt_age)
    cases = [
        ("空闲且一切正常 → 收起", {}, OLD, False, True, OLD),
        ("首启下模型 → 出现（让用户看得见在下载）", {"phase": "setup"}, OLD, True, True, OLD),
        ("听写中 → 出现", {"phase": "rec"}, OLD, True, True, OLD),
        ("处理中 → 出现", {"phase": "proc"}, OLD, True, True, OLD),
        ("设备掉线 → 出现（指示没插入）", {"connected": False}, OLD, True, True, OLD),
        ("设备还在探测 → 出现", {"connected": None}, OLD, True, True, OLD),
        ("接收器插着但设备关机 → 出现", {"connected": False, "device_off": True}, OLD, True, True, OLD),
        ("暂停监听 → 出现", {"paused": True}, OLD, True, True, OLD),
        ("缺辅助功能权限 → 出现", {"post_ok": False}, OLD, True, True, OLD),
        ("麦克风没权限 → 出现（且浮窗要写明是麦克风）", {"mic_ok": False}, OLD, True, True, OLD),
        ("模型加载失败 → 出现", {"phase": "err"}, OLD, True, True, OLD),
        ("刚上屏失败 → 出现一会儿", {"injected": "未上屏：没有辅助功能权限"},
         FAULT_HOLD_S - 1.0, True, True, OLD),
        ("上屏失败已过去 → 收起", {"injected": "未上屏：没有辅助功能权限"},
         FAULT_HOLD_S + 1.0, False, True, OLD),
        ("充电中且读数新鲜 → 显示（用户要求）", {"battery_charging": True},
         OLD, True, True, BATTERY_FRESH_S - 1.0),
        ("充电但读数过期 → 不显示（不周期探测了，别拿旧数据钉住浮窗）",
         {"battery_charging": True}, OLD, False, True, BATTERY_FRESH_S + 1.0),
        ("菜单里关掉自动显示 → 永不出现", {"phase": "rec"}, 0.0, False, False, 0.0),
    ]
    fails = []
    for name, patch, fault_age, want, auto, batt_age in cases:
        got = pill_wanted({**base, **patch}, fault_age, batt_age, auto)
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
    p.hide(animated=False)          # 这个用例只关心定时器，先要立刻收起
    if p.win.isVisible():
        raise AssertionError("hide(animated=False) 之后窗口还可见")
    # 隐藏状态下切到「听写中」：这时窗口还没出来，定时器不该建
    p.set_status("听写中", AppKit.NSColor.systemRedColor(), P.Pill.LEAD_WAVE, "今天下午三点")
    if p._timer is not None:
        raise AssertionError("窗口不可见时不该起 30fps 定时器")
    p.show()
    if not p.win.isVisible():
        raise AssertionError("show() 之后窗口还不可见")
    if p._timer is None:
        raise AssertionError("重新显示后波形定时器没起来（波形会冻住）")
    p.hide(animated=False)
    if p._timer is not None:
        raise AssertionError("收起后定时器没停")
    return "隐藏时不建定时器；重新显示后自己起来；收起时停掉"


def pill_fade() -> str:
    """收起是淡出（不是啪一下没）；淡出没结束又被 show() 的话，窗口不能被老回调收掉。"""
    import AppKit
    from Foundation import NSDate, NSRunLoop
    from voxkey import pill as P

    def pump(sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                "NSDefaultRunLoopMode", NSDate.dateWithTimeIntervalSinceNow_(0.02))

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    p = P.Pill.alloc().initWithHandler_(lambda: None)
    p.place_bottom(None)
    p.show()
    p.hide()
    if not p.win.isVisible():
        raise AssertionError("hide() 是瞬间收起，不是淡出")
    pump(P.FADE_OUT_S + 0.4)
    if p.win.isVisible():
        raise AssertionError("淡出结束后窗口还挂着")

    # 淡出中途再 show()：老的淡出回调不能把它收掉
    p.show()
    p.hide()
    pump(P.FADE_OUT_S / 2)
    p.show()
    pump(P.FADE_OUT_S + 0.4)
    if not p.win.isVisible():
        raise AssertionError("淡出中途重新 show() 之后，窗口被老回调收掉了")
    if abs(p.win.alphaValue() - 1.0) > 0.01:
        raise AssertionError(f"重新显示后透明度没复位（{p.win.alphaValue():.2f}）")
    p.hide(animated=False)
    return f"收起是 {P.FADE_OUT_S}s 淡出；中途重新显示不会被老回调收掉"


def pill_meta() -> str:
    """角标（左上版本 / 右上电量）：不出框、不压状态文字、中间的状态组仍然居中。"""
    import AppKit
    from voxkey import pill as P

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    p = P.Pill.alloc().initWithHandler_(lambda: None)
    p.place_bottom(None)
    p.show()

    # 窄状态（上屏中那种小条）最容易挤：角标必须有地方放，且不能压到状态文字。
    # 电量用最长的现实文本「100% · 4.20V」，宽度上限就在这里。
    for text, detail in (("上屏中", None), ("听写中", "今天下午三点开会记得带电脑和充电器")):
        p.set_meta("v4.4.0", "100% · 4.20V")
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


def linger_rule() -> str:
    """收场倒计时规则：由「该显示」变「不该显示」的那一刻才开始计时，中途不能被清掉。"""
    from voxkey.app import LINGER_S, next_linger

    fails = []
    # 一直有正经理由显示 → 不计时（0 表示没在计时）
    if next_linger(True, True, 0.0, 100.0, LINGER_S) != 0.0:
        fails.append("还在显示时不该开始倒计时")
    # 刚由真变假 → 从现在开始计时
    if next_linger(False, True, 0.0, 100.0, LINGER_S) != 100.0 + LINGER_S:
        fails.append("由真变假没有开始倒计时")
    # 已经是假、且正在计时 → 不能重置（否则永远收不起来）
    if next_linger(False, False, 123.0, 100.0, LINGER_S) != 123.0:
        fails.append("倒计时中途被重置了")
    # 倒计时中途又变回「该显示」→ 作废
    if next_linger(True, False, 123.0, 100.0, LINGER_S) != 0.0:
        fails.append("重新有理由显示时没作废倒计时")
    if fails:
        raise AssertionError("；".join(fails))
    return f"4 条规则全对（收起前留 {LINGER_S}s 显示收场状态）"


def speech_gate() -> str:
    """说话检测闸门：稳态噪声/咔哒声不能当说话，安静的正常说话要放过。

    阈值是拿本机 100 多条按键录音标定的（真话最短的一声「哎。」有效语音 0.46s、峰值 0.097；
    已知两条幻听是 0.28~0.44s、峰值 0.02~0.07）。这里用合成信号把规则钉住。
    """
    import numpy as np
    from voxkey.audio import has_speech

    sr = 16000
    rng = np.random.default_rng(0)

    def noise(sec, amp):
        return (rng.standard_normal(int(sr * sec)) * amp).astype(np.float32)

    def speech_like(burst_amp, gap_amp, total=1.6, on=0.15, off=0.08):
        """模拟说话：一阵一阵的（有声段 + 停顿），而不是一直平稳。"""
        out, t = [], 0.0
        while t < total:
            out.append(noise(on, burst_amp))
            out.append(noise(off, gap_amp))
            t += on + off
        return np.concatenate(out)

    cases = [
        ("纯静音", np.zeros(int(sr * 1.5), dtype=np.float32), False),
        ("稳态底噪 0.005", noise(1.5, 0.005), False),
        ("稳态底噪 0.02", noise(1.5, 0.02), False),
        ("稳态噪声 0.05（按键盘那一下）", noise(1.0, 0.05), False),
        ("正常说话（有停顿、有起伏）", speech_like(0.15, 0.001), True),
        ("小声说话（峰值只有 0.04）", speech_like(0.04, 0.002), True),
    ]
    fails = []
    for name, audio, want in cases:
        ok, sec, peak = has_speech(audio)
        if ok != want:
            fails.append(f"{name}：期望{'放过' if want else '拦下'}，实得{'放过' if ok else '拦下'}"
                         f"（有效语音 {sec:.2f}s 峰值 {peak:.4f}）")
    if fails:
        raise AssertionError("；".join(fails))
    return f"{len(cases)} 条合成用例全对"


def model_ok() -> str:
    from voxkey.models import MODELS, load_recognizer
    if not MODELS.is_dir():
        raise FileNotFoundError(f"模型目录不存在：{MODELS}（用 VOXKEY_MODELS_DIR 覆盖）")
    rec = load_recognizer("funasr-nano-int8")
    return type(rec).__name__


def model_download_flow() -> str:
    """首启下模型的全流程：下载 → sha256 校验 → 解压 → 认得出装好了 → 清掉中间产物。

    用 `file://` 当镜像、拿几百字节的假模型跑，不碰真的 800MB。假模型的目录名和必需文件
    都按真模型的形状造，所以 `is_installed` 的判据也一并被验了。
    """
    import hashlib
    import shutil
    import tarfile
    import tempfile
    from voxkey import modeldl as M

    tmp = Path(tempfile.mkdtemp())
    seen: list[str] = []
    try:
        src = tmp / "src" / M.DIR_NAME
        src.mkdir(parents=True)
        for f in M.REQUIRED:
            if "." in f:
                (src / f).write_bytes(b"fake onnx")
            else:
                (src / f).mkdir()
                (src / f / "tokenizer.json").write_bytes(b"{}")
        mirror = tmp / "mirror"
        mirror.mkdir()
        tarball = mirror / M.TARBALL
        with tarfile.open(tarball, "w:bz2") as tf:
            tf.add(src, arcname=src.name)

        real = (M.SIZE, M.SHA256)
        M.SIZE, M.SHA256 = tarball.stat().st_size, hashlib.sha256(tarball.read_bytes()).hexdigest()
        os.environ["VOXKEY_MODEL_MIRROR"] = mirror.as_uri()
        models = tmp / "models"
        try:
            if M.is_installed(models):
                raise AssertionError("空目录被判成「已装好」")
            M.ensure(models_dir=models, on_progress=lambda s, d, t: seen.append(s))
            if not M.is_installed(models):
                raise AssertionError("ensure 跑完还是没认出模型")
            if not seen:
                raise AssertionError("一次进度回调都没有（用户会对着没反应的图标干等）")
            if (models / M.TARBALL).exists():
                raise AssertionError("装好后没删 tar.bz2（白占一份几百 MB）")
            # 镜像内容不对必须当场失败，不能装上一个来路不明的模型
            M.SHA256 = "0" * 64
            try:
                M.ensure(models_dir=tmp / "models2")
            except M.ModelDownloadError:
                pass
            else:
                raise AssertionError("sha256 不对却没报错")
        finally:
            M.SIZE, M.SHA256 = real
            os.environ.pop("VOXKEY_MODEL_MIRROR", None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f"假模型走通下载/校验/解压/清理，进度回调 {len(seen)} 次"


def model_dir_rules() -> str:
    """运行期路径的规则：环境变量 > 打包后的用户目录 > 源码仓库。"""
    from voxkey import paths

    old = os.environ.pop("VOXKEY_MODELS_DIR", None)
    try:
        import voxkey
        repo_models = Path(voxkey.__file__).resolve().parents[2] / "models"
        if paths.default_models_dir() != repo_models:
            raise AssertionError(f"源码运行时应指向 {repo_models}，实得 {paths.default_models_dir()}")
        os.environ["VOXKEY_MODELS_DIR"] = "~/somewhere-else"
        if paths.default_models_dir() != Path.home() / "somewhere-else":
            raise AssertionError("VOXKEY_MODELS_DIR 没生效（也没展开 ~）")
        os.environ.pop("VOXKEY_MODELS_DIR")
        # 打包后必须离开 .app 包体：包是只读的，往里面写模型会毁掉签名
        sys.frozen = True                                     # type: ignore[attr-defined]
        try:
            packed = paths.default_models_dir()
        finally:
            del sys.frozen                                    # type: ignore[attr-defined]
        if packed != paths.app_data_dir() / "models":
            raise AssertionError(f"打包后应指向用户数据目录，实得 {packed}")
        if str(packed).startswith(str(repo_models.parent)):
            raise AssertionError("打包后仍指向仓库/包体")
        # 日志也不能落进包体
        if str(paths.app_log_dir()).startswith(str(repo_models.parent)):
            raise AssertionError(f"日志目录落在仓库里了：{paths.app_log_dir()}")
    finally:
        os.environ.pop("VOXKEY_MODELS_DIR", None)
        if old is not None:
            os.environ["VOXKEY_MODELS_DIR"] = old
    return "环境变量 / 源码 / 打包三种情况都对"


def log_survives_bad_path() -> str:
    """日志目录写不了时不能把程序搞崩。

    打包成 .app 之后日志走文件，而这是启动路径上的东西：用户家目录只读、磁盘满、
    Logs 被占成一个文件……任何一种都不该让一个语音输入软件打不开。这里把日志目录
    指到一个建不出来的地方，验证它会退到临时目录、且一行都不抛出来。
    """
    from voxkey import logging as L

    old_env = os.environ.get("VOXKEY_LOG_DIR")
    saved_file = L._file
    os.environ["VOXKEY_LOG_DIR"] = "/dev/null/cannot-exist"
    L._file = None
    sys.frozen = True                                     # type: ignore[attr-defined]
    try:
        L.log("测试", "日志目录不可写时这一行也得打出去，而且不能抛异常")
        where = L._file_sink().name
        if where.startswith("/dev/null"):
            raise AssertionError(f"没有退回可写的位置：{where}")
        if not Path(where).exists():
            raise AssertionError(f"日志文件没真的建出来：{where}")
    finally:
        del sys.frozen                                    # type: ignore[attr-defined]
        L._file = saved_file
        os.environ.pop("VOXKEY_LOG_DIR", None)
        if old_env is not None:
            os.environ["VOXKEY_LOG_DIR"] = old_env
    return f"退回到 {where}"


def release_consistency() -> str:
    """发版链路里的名字必须对得上：CI 的资产名、构建脚本产出的压缩包、静态页的下载链接。

    静态页用的是 GitHub 的固定跳转 `releases/latest/download/<文件名>`——文件名差一个字符
    页面上就是 404，而且**本地跑什么都发现不了**，要等真发一版才暴露。所以在这里钉死。
    """
    import re

    root = Path(__file__).resolve().parent.parent
    wf = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    sh = (root / "packaging" / "build_macos.sh").read_text(encoding="utf-8")
    page = (root / "site" / "index.html").read_text(encoding="utf-8")

    m = re.search(r"^\s*ASSET:\s*(\S+)\s*$", wf, re.M)
    if not m:
        raise AssertionError("release.yml 里找不到 ASSET 定义")
    asset = m.group(1)

    if f"/{asset}\"" not in sh:
        raise AssertionError(f"build_macos.sh 压出来的文件名不是 {asset}")
    if f"/releases/latest/download/{asset}" not in page:
        raise AssertionError(f"静态页的下载链接没指向 releases/latest/download/{asset}")
    if "imchangchang/VoxKey" not in page:
        raise AssertionError("静态页里的仓库地址不对")
    # 打包用的版本号得能从环境变量传进来（CI 按 tag 传），否则包里的版本永远停在默认值
    spec = (root / "packaging" / "voxkey.spec").read_text(encoding="utf-8")
    if "VOXKEY_VERSION" not in spec:
        raise AssertionError("spec 没读 VOXKEY_VERSION，CI 传的 tag 版本号进不去")
    return f"资产名 {asset} 三处一致"


def shell_var_before_cjk() -> str:
    """shell 脚本里 `$VAR` 后面紧跟中文时必须写成 `${VAR}`。

    CI 上的 locale 是 C，bash 会把变量名后面的多字节字节当成名字的一部分：
    `echo "… $VENV（用 x）"` 直接报 `VENV（用: unbound variable` 退出。
    本机 locale 是 UTF-8，同一个脚本一点问题没有——这类只在 CI 炸的坑，钉在这里。
    """
    import re
    import subprocess

    root = Path(__file__).resolve().parent.parent
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.split()
    pat = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]")
    bad = []
    for f in files:
        if not f.endswith((".sh", ".yml", ".yaml")):
            continue
        for i, line in enumerate((root / f).read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            bad += [f"{f}:{i} 用了 {m.group(0)!r}（要写成 ${{…}}）" for m in pat.finditer(line)]
    if bad:
        raise AssertionError("；".join(bad[:4]))
    return f"扫了 {len([f for f in files if f.endswith(('.sh', '.yml', '.yaml'))])} 个脚本，没有裸 $VAR 接中文"


def icon_assets_fresh() -> str:
    """菜单栏图标资源必须和设计稿对得上。

    图标是「改了 SVG 忘了重新生成」的经典漂移点，而它不会报错——只会一直显示旧图标。
    这里按同样的参数重画一遍做逐字节比较（AppKit 出 PNG 是稳定的，实测两次 sha256 一致）。
    """
    import importlib.util
    import sys as _sys
    import tempfile

    root = Path(__file__).resolve().parent.parent
    pkg = root / "packaging"
    spec = importlib.util.spec_from_file_location("_vk_make_icon", pkg / "make_icon.py")
    mod = importlib.util.module_from_spec(spec)
    _sys.modules["_vk_make_icon"] = mod
    spec.loader.exec_module(mod)                       # 里面有 sys.path.insert，能 import 到 render_svg

    bad = []
    with tempfile.TemporaryDirectory() as td:
        for name, px in (("menubar.png", 18), ("menubar@2x.png", 36)):
            committed = root / "src" / "voxkey" / "assets" / name
            if not committed.exists():
                bad.append(f"{name} 不存在（跑 packaging/make_icon.py 生成）")
                continue
            tmp = Path(td) / name
            mod.render(mod.SVG, tmp, px, template=True, min_stroke_px=mod.MENUBAR_MIN_STROKE_PX)
            if tmp.read_bytes() != committed.read_bytes():
                bad.append(f"{name} 和设计稿对不上（改了 SVG 就重新跑 packaging/make_icon.py）")
    if bad:
        raise AssertionError("；".join(bad))

    # 光「文件对得上」不够：还要确认真的能加载成 18pt 高的模板图。
    # 菜单栏图标在没接通屏幕的机器上根本看不见，肉眼验不了。
    from voxkey.app import load_menubar_image
    img = load_menubar_image()
    if img is None:
        raise AssertionError("load_menubar_image() 返回 None（资源在但没加载成功）")
    if not img.isTemplate():
        raise AssertionError("菜单栏图不是模板图：不反色的话深色菜单栏下会看不见")
    reps = img.representations()
    # 设计稿是 768×2048 的竖构图，18pt 高时宽度只有 7pt 左右——断言的是「高度 18pt、
    # 宽度按原稿比例」，不是方图。@1x/@2x 各一份，宽度按 768:2048 折出来。
    want = sorted(((round(18 * 768 / 2048), 18), (round(36 * 768 / 2048), 36)))
    sizes = sorted((r.pixelsWide(), r.pixelsHigh()) for r in reps)
    if sizes != want:
        raise AssertionError(f"图标该是 {want}（18pt 高、按设计稿比例），实得 {sizes}")
    for r in reps:
        if (r.size().width, r.size().height) != (18.0, 18.0):
            raise AssertionError(f"表示图的点尺寸应为 18pt 高，实得 {r.size()}")
    return f"与设计稿逐字节一致，且能加载成模板图（{len(reps)} 档密度，{sizes[-1][0]}×{sizes[-1][1]} 像素那档）"


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
    ok &= check("日志多线程不串行", log_lines_not_interleaved)
    ok &= check("日志目录不可写也不崩", log_survives_bad_path)
    ok &= check("结构约定（线程 args/元组）", structure_rules)
    ok &= check("pipeline 解码器现取", pipeline_decoder_source)
    ok &= check("按键回调异常不杀线程", key_callback_survives)
    ok &= check("悬浮条几何断言", pill_geometry)
    ok &= check("长语音内容裁剪", pill_long_detail)
    ok &= check("悬浮条显示规则", pill_wanted_table)
    ok &= check("收起前的收场提示", linger_rule)
    ok &= check("说话检测闸门", speech_gate)
    ok &= check("模型目录规则", model_dir_rules)
    ok &= check("首启下载模型全流程", model_download_flow)
    ok &= check("发版链路名字一致", release_consistency)
    ok &= check("shell 变量不裸接中文", shell_var_before_cjk)
    ok &= check("菜单栏图标与设计稿一致", icon_assets_fresh)
    ok &= check("悬浮条收起再显示", pill_show_hide)
    ok &= check("悬浮条淡出收起", pill_fade)
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
