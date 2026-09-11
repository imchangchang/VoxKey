#!/usr/bin/env python3
"""实时监视：设备发来的按键 + 麦克风转写，全都实时打在一行行日志里。

没有时序要求——想按就按、想说什么说什么，屏幕会自己显示收到了什么。
用来回答「这颗键到底有没有发东西给上位机」和「说完能不能出字」。

用法（仓库根目录，PYTHONPATH=src 或 pip install -e .）：
  python -m voxkey.device.monitor              # 只监视，不上屏
  python -m voxkey.device.monitor --paste      # 松手后自动上屏（要辅助功能权限）
  python -m voxkey.device.monitor --copy       # 松手后复制到剪贴板（要 pyperclip）
  python -m voxkey.device.monitor --raw        # 连原始报文一起打（排查用）
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime

import numpy as np
import sounddevice as sd

from voxkey.audio import Recorder, find_input_device
from voxkey.device import protocol as P
from voxkey.device.device import VibeKey, VibeKeyNotFound
from voxkey.device.keyreader import (KC_ESC, KC_F9, KC_F11, KC_VOICE, KEY_LABELS, MOD_CMD,
                                     MOD_CTRL, MOD_OPT, MOD_SHIFT, DeviceKeyReader)
from voxkey.inject import Injector
from voxkey.models import ModelNotAvailable, load_recognizer
from voxkey.transcribe import Decoder

KEY_NAMES = dict(KEY_LABELS)
MOD_NAMES = [(MOD_CTRL, "⌃"), (MOD_OPT, "⌥"), (MOD_CMD, "⌘"), (MOD_SHIFT, "⇧")]


def log(tag: str, msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {tag:4s}  {msg}", flush=True)


def combo_name(mods: int, keys: list[int]) -> str:
    mods_s = "".join(s for bit, s in MOD_NAMES if mods & bit)
    keys_s = "+".join(KEY_NAMES.get(k, f"0x{k:02x}") for k in keys)
    return f"{mods_s}{'+' if mods_s and keys_s else ''}{keys_s}" or "（无）"


def main() -> int:
    ap = argparse.ArgumentParser(description="AU05 按键 + 语音实时监视")
    ap.add_argument("--model", default="funasr-nano-int8")
    ap.add_argument("--device", default="AU05", help="录音设备名关键字，空串=系统默认")
    ap.add_argument("--paste", action="store_true",
                    help="松手后自动上屏（跟常驻软件同一条路：AX 直写 → 合成按键，要辅助功能权限）")
    ap.add_argument("--copy", action="store_true", help="松手后把结果复制到剪贴板（要 pyperclip）")
    ap.add_argument("--raw", action="store_true", help="打印原始按键报文")
    ap.add_argument("--save-audio", metavar="DIR", default=None,
                    help="把每句话的音频存成 wav（调 ASR 用，如 --save-audio /tmp/utt）")
    ap.add_argument("--segment-s", type=float, default=22.0,
                    help="长语音切段长度（秒）。funasr-nano 上限约 28s，调小可减少长段重复")
    args = ap.parse_args()

    print("=" * 68)
    try:
        with VibeKey() as vk:
            bat = vk.battery()
            print(f"设备     : AU05  固件 {vk.version()}  电量 {f'{bat[0]}%' if bat else '?'}"
                  f"  SN {vk.serial() or '?'}")
            for slot, name in [(0, "语音键"), (1, "确认键"), (2, "取消键"), (3, "旋钮按下")]:
                sc = vk.get_shortcut(slot)
                ff = vk.get_fixed_function(slot)
                sc_s = "（读失败）" if sc is None else (P.tokens_to_display(sc) or "（空）")
                print(f"  槽[{slot}] {name:6s} 快捷键={sc_s:18s} 原厂固定功能="
                      f"{'无' if not ff else f'0x{ff:02x}'}")
    except VibeKeyNotFound as e:
        print(f"厂商通道没打开：{e}")

    device = find_input_device(args.device)
    print(f"麦克风   : {sd.query_devices(device)['name'] if device is not None else '（系统默认）'}")
    try:
        decoder = Decoder(load_recognizer(args.model), max_segment_s=args.segment_s)
    except ModelNotAvailable as e:
        sys.exit(str(e))
    injector = Injector()
    print("=" * 68)
    log("就绪", "按设备上的键就会出现在下面；按住语音键说话，松手出字。Ctrl+C 退出。")
    print(flush=True)

    # 每次说话用一个独立的 Recorder（自己的音频流和缓冲），解码由 Decoder 串行化。
    # 这样「上一句还在出字、下一句已经开始录」不会互相踩——之前就是这里并发用模型崩的。
    state: dict = {"stop": None, "cancel": threading.Event()}
    utt_no = 0

    def handle_utterance(cap: Recorder, stop_ev: threading.Event) -> None:
        nonlocal utt_no
        """后台线程：录一句 → 出字。主线程继续收按键。"""
        cap.start()
        samples = cap.run_until(lambda: stop_ev.is_set() or state["cancel"].is_set())
        t_release = time.monotonic()
        cancelled = state["cancel"].is_set()
        if cancelled:
            state["cancel"].clear()
            log("取消", "这次丢掉")
            return
        dur = len(samples) / 16000
        if dur < 0.2:
            log("忽略", f"只有 {dur:.2f}s")
            return
        if args.save_audio:
            import wave
            from pathlib import Path as _P
            utt_no += 1
            d = _P(args.save_audio)
            d.mkdir(parents=True, exist_ok=True)
            wav = d / f"utt{utt_no:03d}_{datetime.now().strftime('%H%M%S')}_{dur:.1f}s.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
            log("存档", str(wav))
        text = cap.decode(samples)
        ms = (time.monotonic() - t_release) * 1000
        if not text:
            log("结果", f"（{dur:.1f}s 没听清）")
            return
        seg = f"{decoder.last_segments} 段，" if decoder.last_segments > 1 else ""
        log("结果", f"{text}   [音频 {dur:.1f}s，{seg}松手→出字 {ms:.0f}ms]")
        if args.paste:
            # 跟上屏走同一条路（AX 直写 → 合成 Unicode 按键），不碰剪贴板——见 voxkey.inject
            injected = injector.inject(text)
            log("上屏", f"{injected}（松手→上屏 {(time.monotonic() - t_release) * 1000:.0f}ms）")
        elif args.copy:
            try:
                import pyperclip
            except ImportError:
                log("剪贴板", "没装 pyperclip（pip install -e '.[tools]'）")
            else:
                pyperclip.copy(text)
                log("剪贴板", "已复制")

    def on_state(mods: int, keys: list[int]) -> None:
        if args.raw:
            log("报文", f"mods=0x{mods:02x} keys={[hex(k) for k in keys]}")
        # 语音键的原生键码 0x0b；同时兼容写进 0x50 表的哨兵组合 ⌃⌥⌘F9 / F11。
        ptt = (KC_VOICE in keys
               or (KC_F9 in keys and (mods & (MOD_CTRL | MOD_OPT | MOD_CMD)) == (MOD_CTRL | MOD_OPT | MOD_CMD)))
        cancel = KC_ESC in keys or KC_F11 in keys
        if ptt and state["stop"] is None:
            stop_ev = threading.Event()
            state["stop"] = stop_ev
            state["cancel"].clear()
            cap = Recorder(decoder, device, on_preview=lambda t: log("预览", t))
            threading.Thread(target=handle_utterance, args=(cap, stop_ev),
                             daemon=True).start()
            log("按键", f"{combo_name(mods, keys)}  ← 按下，开始录音")
            return
        if ptt:
            return  # 按住期间的重复报文
        if cancel:
            log("按键", f"{combo_name(mods, keys)}  ← 取消键（这次丢掉）")
            state["cancel"].set()
            if state["stop"] is not None:
                state["stop"].set()
                state["stop"] = None
            return
        if state["stop"] is not None:
            state["stop"].set()   # 让那一句的录音线程收尾（不置位就会一直录到 60 秒上限）
            state["stop"] = None
            log("松开", "结束录音，处理中…")
        if keys:
            log("按键", combo_name(mods, keys))

    reader = DeviceKeyReader(on_state)
    try:
        reader.start()
    except Exception as e:
        print(f"读设备按键失败：{e}")
        return 1

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        log("退出", "拜拜")
    finally:
        reader.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
