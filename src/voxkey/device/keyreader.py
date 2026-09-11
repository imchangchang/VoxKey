"""直接读 AU05 键盘集合（report id 0x03）的按键报文。

为什么不用系统全局热键：
  · 全局热键要「辅助功能」权限，还会把按键泄露给前台 App（取消键 = Esc 就是这么打断对话的）；
  · 我们只关心这台设备自己发的键，直接读它的 HID 集合最干净——这也正是自研键盘
    「设备直连上位机」的形状：换成我们自己的板子时，这里替换成 Vendor 通道的事件帧。

报文布局（hidapi 返回 9 字节）：[report_id=0x03][修饰键][保留][key0…key5]
修饰键位：0x01 LCtrl / 0x02 LShift / 0x04 LOpt / 0x08 LCmd（设备只发左侧）。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import hid

VENDOR_ID = 0xFFF1
PRODUCT_ID = 0x00DD
KEYBOARD_REPORT_ID = 0x03

# 修饰键位
MOD_CTRL, MOD_SHIFT, MOD_OPT, MOD_CMD = 0x01, 0x02, 0x04, 0x08

# HID keycode（常用的几颗，够 demo 用）
KC_ENTER, KC_ESC = 0x28, 0x29
KC_F9, KC_F10, KC_F11, KC_F12 = 0x42, 0x43, 0x44, 0x45
KC_UP, KC_DOWN = 0x52, 0x51
# 语音键（麦克风图标那颗）的原生键码：真机实测，短按/长按都发 mods=0x08 + keycode=0x0b。
# 系统侧对它没反应（不隐藏窗口、不打字），我们的进程能读到——等于设备给上位机的私有事件。
KC_VOICE = 0x0B

KEY_LABELS = {
    KC_VOICE: "语音键", KC_ENTER: "确认(Enter)", KC_ESC: "取消(Esc)",
    KC_F9: "F9", KC_F10: "F10", KC_F11: "F11", KC_UP: "旋钮↑", KC_DOWN: "旋钮↓",
}


def find_keyboard_path() -> bytes | None:
    for d in hid.enumerate(VENDOR_ID, PRODUCT_ID):
        if d["usage_page"] == 1 and d["usage"] == 6:  # Generic Desktop / Keyboard
            return d["path"]
    return None


def parse_report(raw: bytes) -> tuple[int, list[int]] | None:
    """报文 → (修饰键位, 按下的 keycode 列表)；不是键盘报文返回 None。"""
    if len(raw) < 3 or raw[0] != KEYBOARD_REPORT_ID:
        return None
    mods = raw[1]
    keys = [k for k in raw[3:9] if k]
    return mods, keys


class DeviceKeyReader:
    """后台线程读键盘报文。每次状态变化回调 (modifiers, keycodes)。"""

    def __init__(self, on_state: Callable[[int, list[int]], None]):
        self.on_state = on_state
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dev: hid.device | None = None

    def start(self) -> "DeviceKeyReader":
        path = find_keyboard_path()
        if path is None:
            raise RuntimeError("没找到 AU05 的键盘集合（设备没插好？）")
        self._dev = hid.device()
        self._dev.open_path(path)
        self._dev.set_nonblocking(True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        last: tuple[int, tuple[int, ...]] | None = None
        while not self._stop.is_set():
            raw = self._dev.read(64)
            if not raw:
                time.sleep(0.004)
                continue
            parsed = parse_report(bytes(raw))
            if parsed is None:
                continue
            mods, keys = parsed
            state = (mods, tuple(keys))
            if state != last:  # 设备会重复发同样的报文，去重
                last = state
                self.on_state(mods, keys)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        if self._dev:
            self._dev.close()


if __name__ == "__main__":
    # 自检：直接打印设备发来的每一次按键状态变化
    print("读 AU05 键盘集合，按几下设备上的键（Ctrl+C 退出）…")
    reader = DeviceKeyReader(lambda m, k: print(f"  mods=0x{m:02x}  keys={[hex(x) for x in k]}"))
    reader.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        reader.stop()
        print("\n退出。")
