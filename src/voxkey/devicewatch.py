"""设备状态看守：一个线程盯着接收器和设备本体，把「设备现在到底什么状态」变成一个枚举。

为什么单独一个模块：以前这套逻辑长在主程序里，一半在 KeySupervisor（线程）、一半在
TrayApp（探测方法），改状态和刷 UI 搅在一起。现在这里只做一件事——**产出状态**：

    DISCONNECTED   接收器没插 / 句柄打不开 / 读线程死了
    CONNECTING     句柄拿到了，但设备还没证明自己能发按键
    READY          可以用了
    STANDBY_OR_OFF 接收器在、句柄在，但设备本体不应答（待机和真关机区分不了，见下）

对外只有一个回调 `on_state(state, reason)`，主程序拿到状态该干嘛干嘛（刷悬浮条、菜单），
这里完全不碰 UI。

实测事实（都是这台机器上踩出来的，别删注释）：
- 键盘集合「能打开」只是个句柄。插回接收器后约 3.6 秒内设备一个按键报文都不发——
  所以打开句柄后要先等 `ready_probe` 通过再报 READY，用户在提示消失的那一刻按键必须能用。
- 「接收器插着但设备本体关机」时，HID 枚举、键盘集合、CoreAudio 三样全都说「在」，
  只有厂商通道（0xFFFC）不应答；连发 heartbeat 也叫不醒。待机（闲置 300 秒，见
  getStandbyTime）和真关机的现象**完全一样**，区分不了——所以只有这一个状态。
- 待机时**第一次按键会被设备自己吞掉**，按第二次才生效（用户实测）。
"""

from __future__ import annotations

import threading
import time
from enum import Enum

from .logging import log
from .device.keyreader import DeviceKeyReader, find_keyboard_path


class DeviceState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    STANDBY_OR_OFF = "standby_or_off"


class DeviceWatch(threading.Thread):
    """每 2 秒看一眼接收器；句柄打开后用 ready_probe 判「能不能用」；之后周期探测本体在不在。"""

    POLL_S = 2.0              # 主循环间隔（查枚举、查读线程还活着没）
    READY_TIMEOUT_S = 5.0     # 等设备证明自己可用的上限；超时按 STANDBY_OR_OFF 处理（开机会被周期探测纠正）
    READY_SETTLE_S = 1.0      # 句柄打开后至少等这么久再判就绪（纯保险）
    POWER_PROBE_EVERY_S = 3.0 # 周期探测设备本体的间隔
    POWER_FAILS_TO_OFF = 2    # 连续几次不应答才判 STANDBY_OR_OFF，防单次抖动误报

    def __init__(self, on_key, on_state, stop_event: threading.Event,
                 ready_probe=None, power_probe=None):
        super().__init__(daemon=True)
        self.on_key = on_key                      # (mods, keys) 按键报文
        self.on_state = on_state                  # (DeviceState, reason) 状态变化才回调
        self.stop_event = stop_event
        self.ready_probe = ready_probe            # () -> bool：设备真的能用了么
        self.power_probe = power_probe            # () -> str：""=在线，否则为什么问不到
        self.reader: DeviceKeyReader | None = None
        self._last_reason: str | None = None      # 上次上报的原因（配合 _state 去重）
        self._state: DeviceState | None = None
        self._power_fails = 0
        self._last_power_probe = 0.0
        self._last_alive = time.monotonic()       # 最后一次设备应答的时刻
        self._silence_logged = 0.0

    # ------------------------------------------------------------ 线程主体

    def run(self) -> None:
        while not self.stop_event.is_set():
            self._poll_once()
            self.stop_event.wait(self.POLL_S)

    def _poll_once(self) -> None:
        """主循环走一步。单独拆出来是为了能不连设备直接断言状态转移（见 tools/smoke.py）。"""
        if find_keyboard_path() is None:
            self._drop("设备未连接")
            return
        if self.reader is not None and not self.reader.is_alive():
            # 设备还在（枚举得到），但读线程已经死了：多半是拔插过接收器，hidapi 句柄失效。
            err = self.reader.error
            self._drop(f"按键读取中断（{err}），等你插回来" if err else "按键读取中断，等你插回来")
            return
        if self.reader is None:
            self._open_and_wait_ready()
            return
        self._check_power()

    def _open_and_wait_ready(self) -> None:
        try:
            self.reader = DeviceKeyReader(self.on_key).start()
        except Exception as e:
            self._emit(DeviceState.DISCONNECTED, f"打不开：{e}")
            self.stop_event.wait(self.POLL_S)
            return
        # 句柄有了 ≠ 能用。先报 CONNECTING，等设备证明自己再报 READY。
        self._emit(DeviceState.CONNECTING)
        log("设备", "键盘集合已打开，等设备就绪…")
        t0 = time.monotonic()
        ok = self._wait_ready()
        ms = (time.monotonic() - t0) * 1000
        if ok:
            self._emit(DeviceState.READY)
            log("设备", f"就绪（探测通过，用时 {ms:.0f}ms），语音键监听中")
        else:
            # 接收器在、句柄也开得了，唯一解释就是设备本体没开机/没电。
            # 不关 reader：设备一开机，同一个句柄就能重新收到按键。
            self._emit(DeviceState.STANDBY_OR_OFF, "设备已关机")
            log("设备", f"打开句柄用了 {ms:.0f}ms，但设备本体不应答——关机/没电？等它开机")

    def _wait_ready(self) -> bool:
        t0 = time.monotonic()
        while not self.stop_event.is_set() and time.monotonic() - t0 < self.READY_TIMEOUT_S:
            settled = (time.monotonic() - t0) >= self.READY_SETTLE_S
            if settled and (self.ready_probe is None or self.ready_probe()):
                return True
            self.stop_event.wait(0.25)
        return False

    # ------------------------------------------------------------ 周期功率/在线探测

    def _check_power(self) -> None:
        """厂商通道答不答话是「设备本体在不在线」的唯一判据（详见模块注释）。"""
        if self.power_probe is None or self.reader is None:
            return
        now = time.monotonic()
        if now - self._last_power_probe < self.POWER_PROBE_EVERY_S:
            return
        self._last_power_probe = now
        why = self.power_probe()          # ""=在线
        if not why:
            self._last_alive = now
            self._power_fails = 0
            if self._state is DeviceState.STANDBY_OR_OFF:
                self._emit(DeviceState.READY)
                log("设备", "设备本体应答了，语音键监听中")
            return
        self._power_fails += 1
        silence = now - self._last_alive
        if self._power_fails == 1:
            log("设备", f"厂商通道第一次问不到（{why}）——继续观察（可能只是待机）")
        if self._power_fails >= self.POWER_FAILS_TO_OFF and self._state is not DeviceState.STANDBY_OR_OFF:
            self._emit(DeviceState.STANDBY_OR_OFF, "设备已关机")
            log("设备", f"连续 {self._power_fails} 次问不到，已沉默 {silence:.0f}s（{why}）"
                        f"——待机或关机（区分不了），等它醒")
        elif self._state is DeviceState.STANDBY_OR_OFF and now - self._silence_logged >= 60.0:
            # 长时间沉默每分钟记一条，对着时间轴能看出沉默是不是卡在待机阈值上
            self._silence_logged = now
            log("设备", f"仍问不到（已沉默 {silence:.0f}s，最后一次应答在 "
                        f"{time.strftime('%H:%M:%S', time.localtime(time.time() - silence))}）")

    # ------------------------------------------------------------ 状态上报

    def _emit(self, state: DeviceState, reason: str = "") -> None:
        """上报状态。状态和原因都没变就不重复报（主循环每 2 秒会再走到同一判定）。"""
        if self._state is state and reason == self._last_reason:
            return
        self._state = state
        self._last_reason = reason
        self.on_state(state, reason)

    def _drop(self, reason: str) -> None:
        had_reader = self.reader is not None
        if had_reader:
            try:
                self.reader.stop()
            except Exception:
                pass
            self.reader = None
        self._power_fails = 0
        self._emit(DeviceState.DISCONNECTED, reason)
        log("设备", reason)

    def reconnect(self) -> None:
        self._drop("手动重连")
