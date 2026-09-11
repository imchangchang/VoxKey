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
        ("空闲", W, Pill.LEAD_NONE, None),
        ("听写中", RED, Pill.LEAD_WAVE, "今天下午三点"),
        ("听写中", RED, Pill.LEAD_WAVE, "今天下午三点开会记得带电脑和充电器"),
        ("上屏中", BLUE, Pill.LEAD_NONE, None),
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
