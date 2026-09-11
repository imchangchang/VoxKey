#!/usr/bin/env python3
"""命令行 demo：不启动常驻软件，直接用设备跑通「按住说话 → 上屏」。

常驻软件（`voxkey.app`）走的是「合成按键直接打进光标、不碰剪贴板」；这个 demo 留着是为了
在没有托盘/没有辅助功能权限的机器上验证硬件链路，所以它的上屏是剪贴板 + 模拟 ⌘V。

用法（仓库根目录，PYTHONPATH 指向 src，或用 pip install -e .）：
  python tools/ptt_demo.py --help
  python tools/ptt_demo.py                          # 按住设备语音键说话（默认）
  python tools/ptt_demo.py --trigger enter          # 回车切换，不用设备
  python tools/ptt_demo.py --trigger handsfree      # 免按键，说话自动起止
  python tools/ptt_demo.py --no-paste               # 只进剪贴板，不自动粘贴

权限：麦克风（终端）。自动粘贴还要「系统设置 → 隐私与安全性 → 辅助功能」里勾上终端。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

import numpy as np
import sounddevice as sd

from voxkey.audio import BLOCK, Recorder, find_input_device
from voxkey.device import protocol as P
from voxkey.device.device import VibeKey, VibeKeyNotFound
from voxkey.device.keyreader import (KC_ESC, KC_F9, KC_F11, KC_VOICE, MOD_CMD, MOD_CTRL,
                                     MOD_OPT, DeviceKeyReader)
from voxkey.models import ModelNotAvailable, load_recognizer
from voxkey.transcribe import SAMPLE_RATE, Decoder

SENTINEL_DEFAULT = ("ctrl", "alt", "cmd", "F9")


class DeviceTrigger:
    """读设备自己的 HID 按键报文（不需要辅助功能权限）。

    recording 在「哨兵组合键按下」时置位，松开任一键清除；cancel 由取消哨兵键触发。
    """

    def __init__(self, main_code: int = KC_VOICE, cancel_codes=(KC_ESC, KC_F11),
                 sentinel_code: int = KC_F9, debug: bool = False):
        self.main = main_code
        self.cancel_codes = set(cancel_codes)
        self.sentinel = sentinel_code
        self.sentinel_mods = MOD_CTRL | MOD_OPT | MOD_CMD
        self.debug = debug
        self.recording = threading.Event()
        self.cancel = threading.Event()
        self.reader: DeviceKeyReader | None = None

    def _on_state(self, mods: int, keys: list[int]) -> None:
        if self.debug:
            print(f"\r\033[K  [按键报文] mods=0x{mods:02x} keys={[hex(k) for k in keys]}", flush=True)
        if self.cancel_codes & set(keys):
            self.cancel.set()
            return
        ptt = (self.main in keys
               or (self.sentinel in keys and (mods & self.sentinel_mods) == self.sentinel_mods))
        if ptt:
            self.recording.set()
        else:
            self.recording.clear()

    def start(self) -> None:
        self.reader = DeviceKeyReader(self._on_state).start()

    def wait_for_press(self) -> None:
        self.recording.wait()

    def wait_for_release(self) -> None:
        while self.recording.is_set():
            time.sleep(0.01)

    def stop(self) -> None:
        if self.reader:
            self.reader.stop()


class HotkeyTrigger:
    """全局热键：哨兵组合键按下=开始，松开（或松开任一修饰键）=停止。

    需要「辅助功能」权限；没权限时 pynput 会打印 not trusted，这里直接报错让用户换模式。
    """

    def __init__(self, combo: tuple[str, ...]):
        from pynput import keyboard as kb
        self.kb = kb
        self.main = combo[-1]
        self.mods = set(combo[:-1])
        self.down_mods: set[str] = set()
        self.recording = threading.Event()
        self.listener = None

    def _mod_name(self, key) -> str | None:
        kb = self.kb
        table = {
            kb.Key.ctrl: "ctrl", kb.Key.ctrl_l: "ctrl", kb.Key.ctrl_r: "ctrl",
            kb.Key.alt: "alt", kb.Key.alt_l: "alt", kb.Key.alt_r: "alt", kb.Key.alt_gr: "alt",
            kb.Key.cmd: "cmd", kb.Key.cmd_l: "cmd", kb.Key.cmd_r: "cmd",
            kb.Key.shift: "shift", kb.Key.shift_l: "shift", kb.Key.shift_r: "shift",
        }
        return table.get(key)

    def _is_main(self, key) -> bool:
        name = getattr(key, "name", None)  # Key.f9 -> "f9"
        if name is None:
            return False
        return name.upper() == self.main.upper()

    def _on_press(self, key):
        mod = self._mod_name(key)
        if mod:
            self.down_mods.add(mod)
            return
        if self._is_main(key) and self.mods <= self.down_mods:
            self.recording.set()

    def _on_release(self, key):
        mod = self._mod_name(key)
        if mod:
            self.down_mods.discard(mod)
            if mod in self.mods and self.recording.is_set():
                self.recording.clear()  # 先松修饰键也算松手
            return
        if self._is_main(key):
            self.recording.clear()

    def start(self) -> None:
        self.listener = self.kb.Listener(on_press=self._on_press, on_release=self._on_release)
        self.listener.start()

    def wait_for_press(self) -> None:
        self.recording.wait()

    def wait_for_release(self) -> None:
        while self.recording.is_set():
            time.sleep(0.02)

    def stop(self) -> None:
        if self.listener:
            self.listener.stop()


class EnterTrigger:
    """终端回车：按一次开始，再按一次结束。零权限依赖，用来验证链路。"""

    def __init__(self):
        self.state = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            if sys.stdin.readline() == "":
                return
            if self.state.is_set():
                self.state.clear()
            else:
                self.state.set()

    def wait_for_press(self) -> None:
        while not self.state.is_set():
            time.sleep(0.02)

    def wait_for_release(self) -> None:
        while self.state.is_set():
            time.sleep(0.02)

    def stop(self) -> None:
        pass


def paste_clipboard(text: str, restore: bool = True, settle: float = 0.25) -> None:
    """剪贴板 + 模拟 Cmd+V。restore=True 时粘贴后把原剪贴板换回去。"""
    import pyperclip
    from pynput.keyboard import Controller, Key

    old = None
    if restore:
        try:
            old = pyperclip.paste()
        except Exception:
            old = None
    pyperclip.copy(text)
    kb = Controller()
    with kb.pressed(Key.cmd):
        kb.press("v")
        kb.release("v")
    if restore and old is not None and old != text:
        time.sleep(settle)
        pyperclip.copy(old)



def main() -> int:
    ap = argparse.ArgumentParser(description="AU05 按住说话 demo")
    ap.add_argument("--model", default="funasr-nano-int8", help="离线模型名（伪流式预览也用它）")
    ap.add_argument("--trigger", default="device",
                    choices=["device", "hotkey", "enter", "handsfree"],
                    help="device=读设备按键报文按住说话（默认）/ hotkey=系统全局热键 / "
                         "enter=回车切换 / handsfree=免按键")
    ap.add_argument("--combo", default="+".join(SENTINEL_DEFAULT),
                    help=f"哨兵组合键（device/hotkey 模式用），默认 {'+'.join(SENTINEL_DEFAULT)}")
    ap.add_argument("--device", default="AU05", help="录音设备名关键字，空串=系统默认")
    ap.add_argument("--no-paste", action="store_true", help="只写剪贴板，不自动 Cmd+V")
    ap.add_argument("--keep-clipboard", action="store_true", help="粘贴后不恢复原剪贴板")
    ap.add_argument("--debug-keys", action="store_true", help="打印设备发来的每一次按键报文")
    args = ap.parse_args()

    # 设备信息（厂商通道；读不到不影响 demo）
    try:
        with VibeKey() as vk:
            bat = vk.battery()
            print(f"设备 AU05  固件 {vk.version()}  "
                  f"电量 {f'{bat[0]}%' if bat else '?'}  SN {vk.serial() or '?'}")
    except VibeKeyNotFound as e:
        print(f"（厂商通道未打开：{e}）")

    device = find_input_device(args.device)
    if device is not None:
        print(f"录音设备 #{device} {sd.query_devices(device)['name']}")
    else:
        print("没找到 AU05 录音设备，用系统默认输入")

    try:
        decoder = Decoder(load_recognizer(args.model))
    except ModelNotAvailable as e:
        sys.exit(str(e))
    rec = Recorder(decoder, device)

    if args.trigger == "device":
        try:
            trigger = DeviceTrigger(debug=args.debug_keys)
            trigger.start()
        except Exception as e:
            sys.exit(f"读设备按键失败（{e}）。设备插好了吗？也可以先用 --trigger enter")
        print("\n就绪。按住设备上的「语音键」说话，松手上屏；按「取消键」丢掉这次。Ctrl+C 退出。")
        print("（语音键还没烧哨兵键的话：python tools/device_probe.py set-key "
              f"--slot 0 --combo {args.combo}）\n")
    elif args.trigger == "hotkey":
        try:
            trigger = HotkeyTrigger(tuple(p.strip().lower() for p in args.combo.split("+")))
            trigger.start()
        except Exception as e:
            sys.exit(f"热键监听起不来（{e}）。检查辅助功能权限，或用 --trigger enter")
        print(f"\n就绪。按住 {P.tokens_to_display([t for t in args.combo.split('+')])} 说话，松手上屏。"
              "Ctrl+C 退出。")
        print("（哨兵键还没烧的话：python tools/device_probe.py set-key --slot 0 "
              f"--combo {args.combo}）\n")
    elif args.trigger == "enter":
        trigger = EnterTrigger()
        trigger.start()
        print("\n就绪。回车开始说话，再回车结束并上屏。Ctrl+C 退出。\n")
    else:
        trigger = None
        print("\n就绪。免按键模式：直接说话，停 0.8 秒自动结束并上屏。Ctrl+C 退出。\n")

    try:
        while True:
            t_press = t_release = None
            if args.trigger == "handsfree":
                state = {"started": False, "silent": 0}
                rec.start()
                print("监听中…（说话自动开始，停 0.8 秒自动结束）", flush=True)

                def hs_stop() -> bool:
                    tail = rec.samples[-BLOCK:]
                    rms = float(np.sqrt(np.mean(tail ** 2))) if len(tail) else 0.0
                    if not state["started"]:
                        if rms > 0.01:
                            state["started"] = True
                            state["t0"] = time.monotonic()
                            print("检测到说话…", flush=True)
                        return False
                    state["silent"] = state["silent"] + 1 if rms < 0.01 else 0
                    return state["silent"] >= 8

                samples = rec.run_until(hs_stop)
                if not state["started"]:
                    continue  # 到 60 秒上限还没开口，重新开始听
                t_press, t_release = state["t0"], time.monotonic()
            else:
                if args.trigger == "hotkey":
                    trigger.wait_for_press()
                elif args.trigger == "device":
                    trigger.cancel.clear()
                    trigger.wait_for_press()
                else:
                    print("按回车开始…", end="", flush=True)
                    trigger.wait_for_press()
                t_press = time.monotonic()
                rec.start()
                print("\r\033[K录音中…（松手结束）", flush=True)

                def stop_now() -> bool:
                    if args.trigger == "hotkey":
                        return not trigger.recording.is_set()
                    if args.trigger == "device":
                        return not trigger.recording.is_set() or trigger.cancel.is_set()
                    return not trigger.state.is_set()

                samples = rec.run_until(stop_now)
                t_release = time.monotonic()
                if args.trigger == "device" and trigger.cancel.is_set():
                    trigger.cancel.clear()
                    print("\r\033[K（取消键：这次丢掉）\n")
                    continue
                print(f"\r\033[K录了 {(t_release - t_press):.1f}s，松手处理中…", flush=True)

            dur = len(samples) / SAMPLE_RATE
            if dur < 0.2:
                print("（太短，忽略）\n")
                continue

            text = rec.decode(samples)
            t_text = time.monotonic()
            if not text:
                print(f"（{dur:.1f}s，没听清）\n")
                continue

            print(f"结果: {text}")
            print(f"耗时: 松手→出字 {(t_text - t_release) * 1000:.0f}ms（音频 {dur:.1f}s）", end="")
            if args.no_paste:
                import pyperclip
                pyperclip.copy(text)
                print("  → 已进剪贴板（未粘贴）\n")
            else:
                paste_clipboard(text, restore=not args.keep_clipboard)
                print(f"  → 松手→上屏 {(time.monotonic() - t_release) * 1000:.0f}ms\n")
    except KeyboardInterrupt:
        print("\n退出。")
    finally:
        if trigger:
            trigger.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
