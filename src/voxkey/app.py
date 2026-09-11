#!/usr/bin/env python3
"""VoxKey 常驻主程序：按住设备语音键说话 → 松手 → 文字进当前光标。

跑法（仓库根目录下）：
  PYTHONPATH=src python -m voxkey.app               # 默认模型 funasr-nano-int8
  PYTHONPATH=src python -m voxkey.app --no-device   # 不读按键，用菜单手动开始/结束

一次说话的状态机（悬浮条四态，状态靠文字 + 颜色表达，不用 emoji）：
  空闲 → 听写中（波形跟实时电平跳，下面一行实时预览）→ 上屏中 → 空闲；
  中途按取消键则丢弃这一句。上屏成功后不再复读结果——文字已经打进屏幕里了。

权限（启动体检，结果进菜单）：
  麦克风（AVCaptureDevice）：采设备音频必需，未授权会弹系统框；
  辅助功能（CGPreflightPostEventAccess）：上屏（AX 直写 / 合成按键）必需，没有就明说「未上屏」；
  输入监控：读设备自己的 HID 按键报文必需。

已知边界：
  还没打包 .app（权限归属、开机自启待做）；设备键盘集合同时只能被一个进程打开，
  先退掉别的手工脚本；设备上「确认/取消」两键由本程序转发（见 ROUTE_KEYS 注释）。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import objc
import sounddevice as sd

import AppKit  # noqa: E402
import AVFoundation  # noqa: E402
import Foundation  # noqa: E402
import Quartz  # noqa: E402

from voxkey.audio import Recorder, find_input_device
from voxkey.device import protocol as P
from voxkey.device.device import VibeKey, VibeKeyNotFound
from voxkey.device.keyreader import (KC_ESC, KC_F9, KC_F11, KC_VOICE, MOD_CMD, MOD_CTRL,
                                     MOD_OPT, DeviceKeyReader, find_keyboard_path)
from voxkey.inject import Injector, ax_trusted, frontmost_info, reactivate
from voxkey.models import load_recognizer
from voxkey.pill import Pill
from voxkey.transcribe import Decoder

# 单实例锁放临时目录：home 目录在受限环境下不一定可写；正式版会挪到 ~/Library/Application Support/VoxKey/
LOCK_FILE = Path(tempfile.gettempdir()) / "voxkey.lock"
SAMPLE_RATE = 16000

PHASE_IDLE, PHASE_REC, PHASE_PROC, PHASE_ERR = "idle", "rec", "proc", "err"

# 状态指示统一用「圆圈 + 文字」：
#   ○ 空闲 → ● 听写中（按住） → ◐ 上屏中（松开后转写+注入，转圈动画） → ○ 空闲
# 菜单栏用 SF Symbol 的圆（template 图，系统自动反色）；悬浮条用文本圆圈。
# 颜色：空闲=默认、听写中=红、上屏中=蓝、未上屏/异常=橙。
SPINNER = "◐◓◑◒"
# 设备按键 → 系统虚拟键码的「转发表」。
# 为什么需要转发：真机对照实验（同进程双通道、带时间戳）证明——我们的进程一读设备的
# 键盘集合，macOS 就收不到这台设备的按键报文了（谁先打开谁独占），于是「确认=回车、
# 取消=退格」在前台应用里完全没反应。报文既然只到我们手里，就由我们转交给系统。
ROUTE_KEYS = {0x28: 36,    # HID 0x28 Enter       → macOS vk 36 (Return)
              0x2A: 51}    # HID 0x2A Backspace   → macOS vk 51 (Delete/退格)

# 我们认识的设备键码；其余一律当未知（只记日志，不做动作），方便分辨设备是不是发了怪码
KNOWN_KEYCODES = {KC_VOICE, KC_ESC, KC_F9, KC_F11, *ROUTE_KEYS}

MIN_AUDIO_S = 0.5       # 短于这个时长直接丢（用户要求：<0.5s 忽略，避免静音被模型脑补出字）
SILENCE_RMS = 0.002     # 音量低于此值视为「没说话」——实测 0.0008 的 0.2s 片段会被识别成「嗯」
FAULT_HOLD_S = 3.0      # 「未上屏」提示在悬浮条上留多久（用户要求：空闲就收起来）
PHASE_META = {
    PHASE_IDLE: ("circle", None, "空闲", "○"),
    PHASE_REC: ("circle.fill", (1.00, 0.30, 0.30, 1.0), "听写中", "●"),
    PHASE_PROC: ("circle.dotted", (0.30, 0.55, 1.00, 1.0), "上屏中", "◐"),
    PHASE_ERR: ("exclamationmark.circle", (1.00, 0.60, 0.10, 1.0), "未上屏", "○"),
}


def pill_wanted(st: dict, fault_age: float, auto: bool = True) -> bool:
    """悬浮条该不该出现（用户要求：空闲时不占屏幕，只在「有事要说」的时候弹出来）。

    出现的情况：听写中 / 上屏中、设备掉线或还在探测、暂停了、权限缺失、
    模型加载失败这类持续错误、以及刚上屏失败的那几秒。
    抽成纯函数是为了能直接断言这个真值表（见 tools/smoke.py），不用起整个 App。
    """
    if not auto:
        return False
    return bool(
        st["phase"] in (PHASE_REC, PHASE_PROC)
        or st["phase"] == PHASE_ERR                         # 模型加载失败、麦克风打不开
        or st["paused"]                                     # 用户主动停了监听，得让人看见
        or st["connected"] in (False, None)                 # 设备掉线 / 还在探测
        or not st["post_ok"] or not st["mic_ok"]            # 权限缺失（None = 还在查）
        or (st["injected"].startswith("未上屏") and fault_age < FAULT_HOLD_S))


def log(tag: str, msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {tag:4s}  {msg}", flush=True)


# ---------------------------------------------------------------- 权限

def mic_status() -> int:
    return AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)


def ensure_mic_permission() -> bool:
    if mic_status() == 3:
        return True
    if mic_status() == 0:  # 没问过 → 触发系统弹窗
        done = threading.Event()
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio, lambda ok: done.set())
        done.wait(30)
    return mic_status() == 3


def open_settings(anchor: str) -> None:
    url = f"x-apple.systempreferences:com.apple.preference.security?{anchor}"
    Foundation.NSWorkspace.sharedWorkspace().openURL_(Foundation.NSURL.URLWithString_(url))


# ---------------------------------------------------------------- 单实例

class SingleInstance:
    def __init__(self):
        self._fh = None

    def acquire(self) -> bool:
        try:
            self._fh = open(LOCK_FILE, "w")
        except OSError as e:          # 锁文件写不了（权限）不该拦住启动
            log("单实例", f"锁文件不可用（{e}），跳过单实例保护")
            return True
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return True


# ---------------------------------------------------------------- 设备看守

class KeySupervisor(threading.Thread):
    """看着 AU05 的键盘集合：掉线 → 标记未连接并停 reader，插回来 → 自动重启。

    「插回来」不等于「能用了」：键盘集合能打开只是拿到了句柄，实测插拔接收器之后还有约 3.6 秒
    设备一个按键报文都不发（固件/音频子系统还没起来）。所以打开之后先等 `ready_probe` 成立
    再报「已连接」——用户在提示消失的那一刻按键就必须能用（用户要求）。
    """

    READY_TIMEOUT_S = 8.0    # 等设备证明自己可用的上限；超时就先按已连接处理，别把提示挂死
    READY_SETTLE_S = 1.0     # 集合打开后至少等这么久再判就绪（纯保险）

    def __init__(self, on_key, on_conn, stop_event: threading.Event, ready_probe=None):
        super().__init__(daemon=True)
        self.on_key = on_key
        self.on_conn = on_conn
        self.stop_event = stop_event
        self.ready_probe = ready_probe        # () -> bool：设备真的能用了么（见 TrayApp._device_ready）
        self.reader: DeviceKeyReader | None = None
        self._dropped_reason: str | None = None   # 同一条掉线原因只报一次，别每 2 秒刷一遍日志

    def run(self) -> None:
        while not self.stop_event.is_set():
            if find_keyboard_path() is None:
                self._drop("设备未连接")
                self.stop_event.wait(2.0)
                continue
            if self.reader is not None and not self.reader.is_alive():
                # 设备还在（枚举得到），但读线程已经死了：多半是拔插过接收器，hidapi 句柄失效。
                err = self.reader.error
                self._drop(f"按键读取中断（{err}），等你插回来" if err else "按键读取中断，等你插回来")
            if self.reader is None:
                try:
                    self.reader = DeviceKeyReader(self.on_key).start()
                except Exception as e:
                    self.on_conn(False, f"打不开：{e}")
                    self.stop_event.wait(2.0)
                    continue
                self.on_conn(None)                # 句柄有了，但设备还没证明自己能发按键
                log("设备", "键盘集合已打开，等设备就绪…")
                t0 = time.monotonic()
                ok = self._wait_ready()
                ms = (time.monotonic() - t0) * 1000
                self._dropped_reason = None
                self.on_conn(True)
                log("设备", f"就绪（{'探测通过' if ok else '探测超时，先按就绪处理'}，用时 {ms:.0f}ms），"
                            f"语音键监听中")
            self.stop_event.wait(2.0)

    def _wait_ready(self) -> bool:
        """等到 ready_probe 说「能用」。没有探针就只等 settle。"""
        t0 = time.monotonic()
        while not self.stop_event.is_set() and time.monotonic() - t0 < self.READY_TIMEOUT_S:
            settled = (time.monotonic() - t0) >= self.READY_SETTLE_S
            if settled and (self.ready_probe is None or self.ready_probe()):
                return True
            self.stop_event.wait(0.25)
        return False

    def _drop(self, reason: str) -> None:
        had_reader = self.reader is not None
        if had_reader:
            try:
                self.reader.stop()
            except Exception:
                pass
            self.reader = None
        # 首次掉线（包括「启动时设备就没插」，这时本来就没有 reader）必须报出去：
        # 只报 had_reader 的话 connected 会一直是 None，菜单永远显示「设备探测中」。
        if had_reader or self._dropped_reason != reason:
            self.on_conn(False, reason)
            log("设备", reason)
        self._dropped_reason = reason

    def reconnect(self) -> None:
        self._drop("手动重连")


# ---------------------------------------------------------------- 菜单栏 App

class TrayApp(Foundation.NSObject):

    def initWithArgs_(self, args):
        self = objc.super(TrayApp, self).init()
        if self is None:
            return None
        self.args = args
        self.decoder = None
        self.injector = Injector(newline_mode=args.newline)   # 只走不碰剪贴板的两条路
        self.audio_device = None
        self.supervisor = None
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.current_stop = None
        self._checked_visibility = False
        self._flash = None
        self.state_lock = threading.Lock()
        self.state = {
            "phase": PHASE_IDLE, "connected": None, "reason": "", "paused": False,
            "last_text": "", "preview": "", "device_info": "设备信息读取中…",
            "mic_ok": None, "post_ok": None, "injected": "",
        }
        self.gesture_start = None
        self.utt_no = 0
        self._routed = set()
        self.pill_auto = True          # 悬浮条自动显示（空闲收起）；菜单里可以关掉
        self._audio_dirty = False      # 设备掉过线 → 下次录音前重新枚举音频设备
        self._last_unknown: tuple = ()  # 上次报过的未知键码（去重，别刷屏）
        self._woke_device = False       # 这次连接有没有给设备发过唤醒心跳
        self.min_audio_s = getattr(args, "min_audio_s", MIN_AUDIO_S)
        return self

    # ---------- 状态：后台线程写，主线程 tick 读 ----------

    @objc.python_method
    def set_state(self, **kw) -> None:
        with self.state_lock:
            self.state.update(kw)

    @objc.python_method
    def get_state(self) -> dict:
        with self.state_lock:
            return dict(self.state)

    # ---------- 按键回调（reader 线程） ----------

    @objc.python_method
    def on_key_state(self, mods: int, keys: list) -> None:
        """纯「按住说话」：按下开始录，松手就上屏。

        为什么不做「点按免手」（曾经做过，用户要求去掉）：
        AU05 的按键报文本身不稳——一次按压会拆成多条（⌘ → ⌘+语音键 → 语音键 → 全松），
        偶尔缺中间那条，还会出现 20~100ms 的极短按压。在「松开」那一刻判定意图时，
        这些极短按压很容易被判成「点按」，于是误进免手模式，用户感觉"按键不听话"。
        所以只保留一种语义，把不确定性留给日志和提示，不改变行为。
        """
        if self.args.debug_keys:
            log("报文", f"mods=0x{mods:02x} keys={[hex(k) for k in keys]}")
        unknown = tuple(k for k in keys if k not in KNOWN_KEYCODES)
        if unknown and unknown != self._last_unknown:
            # 未知键码一直记（不用 --debug-keys）：插拔接收器后出现过 0xde 这种怪码，
            # 得能从日志里分清「设备还没就绪、根本没发报文」和「发了但键码不认识」。
            log("报文", f"未知键码 {[hex(k) for k in unknown]}（mods=0x{mods:02x} "
                        f"keys={[hex(k) for k in keys]}）")
        self._last_unknown = unknown
        st = self.get_state()
        ptt = (KC_VOICE in keys
               or (KC_F9 in keys and (mods & (MOD_CTRL | MOD_OPT | MOD_CMD)) ==
                   (MOD_CTRL | MOD_OPT | MOD_CMD)))
        cancel = KC_ESC in keys or KC_F11 in keys
        # 转发不受暂停影响：设备的键盘集合被我们独占，暂停时若不再转发，
        # 设备上的「确认/取消」两键谁都收不到（Enter/退格全哑）。
        self._route_keys(keys)

        if ptt:
            if self.gesture_start is None:          # 一次物理按压只认第一条报文
                self.gesture_start = time.monotonic()
                # 暂停只挡「开始新的一次录音」——松手/取消必须照常处理，
                # 否则录到一半点暂停，松手报文被吞掉，会一直录到 60 秒上限才收摊。
                if self.current_stop is None and not st["paused"]:
                    self._start_recording()
            return

        if cancel and self.current_stop is not None:
            log("按键", "取消键 → 丢掉这次")
            self.cancel_event.set()
            self.gesture_start = None
            return

        if not keys and self.gesture_start is not None:
            hold_ms = (time.monotonic() - self.gesture_start) * 1000
            self.gesture_start = None
            self._stop_recording(f"松开（按住 {hold_ms:.0f}ms）")

    @objc.python_method
    def _reload_audio_devices(self) -> None:
        """重新枚举音频设备（Pa_Terminate + Pa_Initialize）。

        真机踩到：USB 接收器插拔之后 PortAudio 的设备表还是旧的——`sd.query_devices()` 照样
        把 AU05 报在原来的编号上，于是我们拿着一个**已经不存在的设备**去 open，报
        `-10851 (Audio Unit: Invalid Property Value)` 再 `-9986`，而新起一个进程立刻就能录
        （新进程会重新枚举）。不重新初始化就永远打不开。
        `_terminate/_initialize` 是 sounddevice 的私有 API，但它自己的 FAQ 就是这么写的，
        而且调用点是「刚打不开、手里没有任何 stream」的时候，代价只是几十毫秒。
        """
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:
            log("音频", f"重载 PortAudio 设备表失败：{e}")

    @objc.python_method
    def _resolve_audio_device(self) -> int | None:
        """每次录音前重新解析输入设备。

        真机踩过：USB 接收器插拔后 CoreAudio 会给设备换一个编号，缓存的那个编号就失效了，
        表现是「按语音键没反应」——其实按键报文正常，是麦克风打不开（PortAudio -9986）。
        注意这里只能刷新「我们缓存的那个编号」；PortAudio 自己的设备表要 `_reload_audio_devices()`。
        """
        if self._audio_dirty:
            # 设备掉线过（插拔接收器）：PortAudio 的设备表还是旧的，**必须先重新枚举再解析编号**，
            # 否则解析出来的还是旧表里的旧编号。这样插回来第一次按键就能直接录上。
            self._audio_dirty = False
            self._reload_audio_devices()
        dev = find_input_device(self.args.device)
        if dev != self.audio_device:
            try:
                name = sd.query_devices(dev)["name"] if dev is not None else "系统默认"
            except Exception:
                name = "?"
            log("音频", f"输入设备更新为 #{dev} {name}")
            self.audio_device = dev
        return dev

    @objc.python_method
    def _route_keys(self, keys: list) -> None:
        """把设备上的「确认/取消」等普通键原样转交给系统（见 ROUTE_KEYS 的说明）。"""
        now_down = {k for k in keys if k in ROUTE_KEYS}
        for code in now_down - self._routed:        # 只在「刚按下」那一刻合成一次
            vk = ROUTE_KEYS[code]
            try:
                src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
                for down in (True, False):
                    ev = Quartz.CGEventCreateKeyboardEvent(src, vk, down)
                    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                log("转发", f"设备键 0x{code:02x} → 系统虚拟键 {vk}")
            except Exception as e:
                log("转发", f"失败：{e}")
        self._routed = now_down

    @objc.python_method
    def _start_recording(self) -> None:
        if self.decoder is None:
            log("按键", "模型还没加载完，这次忽略")
            return
        self._resolve_audio_device()
        stop_ev = threading.Event()
        self.current_stop = (stop_ev,)
        self.cancel_event.clear()
        self.set_state(phase=PHASE_REC, preview="", reason="")
        self.state["t_press"] = time.monotonic()
        pid, name = frontmost_info()
        self.state["target_pid"], self.state["target_name"] = pid, name
        log("按键", "开始录音")
        threading.Thread(target=self.handle_utterance, args=(stop_ev,), daemon=True).start()

    @objc.python_method
    def on_conn(self, connected: bool, reason: str = "") -> None:
        if not connected:
            self._audio_dirty = True      # 设备掉过线，音频设备表要重新枚举（见 _resolve_audio_device）
            self._woke_device = False     # 下次连上要重新唤醒一次
        self.set_state(connected=connected, reason=reason)

    # ---------- 设备「真的能用了吗」（插回来时报「已连接」之前先过这一关） ----------

    @objc.python_method
    def _vendor_alive(self) -> bool:
        """厂商通道能应答吗——固件起来了才会回我们的加密帧。

        顺便发一帧心跳：协议里 `heartbeat` 的说明就是「试着唤醒假死的厂商口」，
        而 `getStandbyTime` 说明「待机超时后厂商口不响应」——插拔之后设备很可能就在这个状态，
        不敲一下它会一直不应答（也就没法用它判断设备到底起没起来）。
        """
        try:
            with VibeKey() as vk:
                if not self._woke_device:
                    self._woke_device = True
                    vk.send(P.build_frame(0x06, 0x01, 0x23, 0x00))
                return vk.version() is not None
        except Exception:
            return False

    @objc.python_method
    def _audio_device_present(self) -> bool:
        """AU05 的录音设备在 CoreAudio 里了吗。

        必须先重新枚举 PortAudio：插拔之后它手里那张设备表还是旧的，不重载的话这里永远返回
        「在」（旧表里就有 AU05），探测就失去意义。录音进行中不动它（重载会废掉正在录的流）。
        """
        if self.current_stop is not None:
            return True
        self._reload_audio_devices()
        return find_input_device(self.args.device) is not None

    @objc.python_method
    def _device_ready(self) -> bool:
        """设备真的能用了么。键盘集合「能打开」不算——那只是个句柄。

        实测（插拔接收器）：集合打开之后约 3.6 秒里设备一个按键报文都不发，用户正好在这段
        时间按键就是「按了没反应」。这里要求①厂商通道应答②AU05 的录音设备已经在 CoreAudio 里，
        两条都成立才报「已连接」、悬浮条才收起——用户看到提示消失就能马上用。
        """
        if not self._vendor_alive():
            return False
        return self._audio_device_present()

    # ---------- 一次说话的全流程（后台线程） ----------

    @objc.python_method
    def _stop_recording(self, why: str) -> None:
        if self.current_stop is None:
            return
        self.current_stop[0].set()
        self.state["t_release"] = time.monotonic()
        self.current_stop = None
        self.set_state(phase=PHASE_PROC, preview="")   # 松手即切「上屏中」，覆盖关流+转写+注入整段
        log("按键", f"{why} → 上屏中…")

    @objc.python_method
    def handle_utterance(self, stop_ev: threading.Event) -> None:
        cap = Recorder(self.decoder, self.audio_device,
                       on_preview=lambda t: self.set_state(preview=t, result_ts=time.monotonic()),
                       on_level=lambda rms: self.pill.set_level(rms),      # 悬浮条波形
                       is_busy=lambda: self.get_state().get("inflight", 0) > 0)
        try:
            cap.start()
        except Exception as e1:
            # 打不开基本上是插拔过接收器：PortAudio 的设备表还是旧的（见 _reload_audio_devices），
            # 先重新枚举再重解析编号，然后重试一次。
            log("音频", f"打不开（{e1}），重新枚举音频设备后重试")
            self._reload_audio_devices()
            self._resolve_audio_device()
            log("音频", f"重载后设备表 {len(sd.query_devices())} 个，AU05 → " +
                        (f"#{self.audio_device} {sd.query_devices(self.audio_device)['name']}"
                         if self.audio_device is not None else "（没找到，用系统默认）"))
            # 半开的那条流要显式关掉：sounddevice 的 Stream 没有 __del__，GC 不会替你关，
            # 一直占着输入设备会让第二次 start 更容易失败。
            try:
                cap.stop()
            except Exception:
                pass
            try:
                cap = Recorder(self.decoder, self.audio_device,
                               on_preview=lambda t: self.set_state(preview=t, result_ts=time.monotonic()),
                               on_level=lambda rms: self.pill.set_level(rms),
                               is_busy=lambda: self.get_state().get("inflight", 0) > 0)
                cap.start()
            except Exception as e2:
                # 这次录音录不成，但不代表软件坏了：回空闲 + 提示几秒，下次按键会重新枚举设备再试。
                # （以前把 phase 打成 PHASE_ERR 会一直挂着一条「上屏不可用」，用户以为是软件坏了，
                # 其实只是这次没打开麦克风）
                self.set_state(phase=PHASE_IDLE, reason=f"麦克风打不开：{e2}",
                               injected="未上屏：麦克风打不开", result_ts=time.monotonic())
                log("错误", f"麦克风打不开（重试后仍失败）：{e2}")
                self.current_stop = None
                return
        samples = cap.run_until(lambda: stop_ev.is_set() or self.cancel_event.is_set())
        t_rec_stop = time.monotonic()
        t_release = self.get_state().get("t_release") or t_rec_stop
        cancelled = self.cancel_event.is_set()
        self.cancel_event.clear()
        try:
            self._finish_utterance(cap, samples, t_release, t_rec_stop, cancelled)
        finally:
            self.set_state(inflight=max(0, self.get_state().get("inflight", 1) - 1))
            # 这一句结束了（不管是上屏、取消还是太短），把状态清干净，
            # 否则取消后 current_stop 还挂着，下一次按键会被当成"正在录音"而完全不响应。
            if self.current_stop is not None and self.current_stop[0] is stop_ev:
                self.current_stop = None

    @objc.python_method
    def _archive_audio(self, cap, samples, hold_ms: float) -> None:
        """把这次按键的音频存下来 + 索引一行（按住多久 / 录到几秒 / 有没有声音）。"""
        if not self.args.save_audio:
            return
        import json as _json
        import wave
        self.utt_no += 1
        n = len(samples)
        dur = n / SAMPLE_RATE
        rms = float(np.sqrt((samples ** 2).mean())) if n else 0.0
        peak = float(np.abs(samples).max()) if n else 0.0
        d = Path(self.args.save_audio)
        d.mkdir(parents=True, exist_ok=True)
        wav = d / f"utt{self.utt_no:03d}_{datetime.now().strftime('%H%M%S')}.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
        with open(d / "index.jsonl", "a") as f:
            f.write(_json.dumps({"n": self.utt_no, "wav": wav.name, "hold_ms": round(hold_ms),
                                 "audio_s": round(dur, 2), "rms": round(rms, 4),
                                 "peak": round(peak, 4)}, ensure_ascii=False) + "\n")
        log("存档", f"#{self.utt_no} {wav.name} 按住 {hold_ms:.0f}ms / 音频 {dur:.2f}s / RMS {rms:.4f}")

    @objc.python_method
    def _finish_utterance(self, cap, samples, t_release, t_rec_stop, cancelled: bool = False) -> None:
        # 取消状态必须在外面读、传进来：caller 已经 clear 了 cancel_event，在这里再读永远是 False
        if cancelled:
            self.set_state(phase=PHASE_IDLE, preview="")
            return
        dur = len(samples) / SAMPLE_RATE
        hold_ms = (t_release - self.get_state().get("t_press", t_release)) * 1000
        self._archive_audio(cap, samples, hold_ms)
        rms = float(np.sqrt((samples ** 2).mean())) if len(samples) else 0.0
        log("音频", f"按住 {hold_ms:.0f}ms → 录到 {dur:.2f}s，RMS {rms:.4f}")
        if dur < self.min_audio_s:            # 太短：不送模型，免得被脑补出「嗯」这类填充词
            log("忽略", f"只录到 {dur:.2f}s（< {self.min_audio_s:.2f}s），这次丢掉")
            self.set_state(phase=PHASE_IDLE, last_text="", preview="",
                           injected=f"未上屏：只录到 {dur:.2f}s（太短，已忽略）",
                           result_ts=time.monotonic())
            return
        if rms < SILENCE_RMS:                 # 有长度但基本没声音：同样不送模型
            log("忽略", f"录到 {dur:.2f}s 但基本无声（RMS {rms:.4f}），这次丢掉")
            self.set_state(phase=PHASE_IDLE, last_text="", preview="",
                           injected=f"未上屏：录到 {dur:.2f}s 但没声音（已忽略）",
                           result_ts=time.monotonic())
            return
        text = cap.decode(samples)
        t_decoded = time.monotonic()
        ms = (t_decoded - t_rec_stop) * 1000
        if not text:
            log("结果", f"（{dur:.1f}s 没听清）")
            self.set_state(phase=PHASE_IDLE, last_text="", preview="")
            return
        if self.injector.last_newlines:
            log("换行", f"识别结果含 {self.injector.last_newlines} 个换行，"
                        f"按 --newline={self.injector.newline_mode} 处理（避免在微信/Slack 里误发送）")
        seg = f"{self.decoder.last_segments} 段，" if self.decoder.last_segments > 1 else ""
        fin = f"其中 {cap.finalized_segments} 段录音期间已定稿，" if cap.finalized_segments else ""
        log("结果", f"{text}   [音频 {dur:.1f}s，{seg}{fin}松手→出字 {ms:.0f}ms]")

        # 上屏前先把「按下语音键那一刻的前台 App」拉回前台：中途切窗口的话，
        # 字会打进错误的窗口（Wispr 也是存焦点元素 + 粘贴前还原）。
        pid = self.get_state().get("target_pid")
        cur, cur_name = frontmost_info()
        if pid and cur != pid:
            if reactivate(pid):
                log("焦点", f"中途切到了「{cur_name}」，已把「{self.state.get('target_name')}」拉回前台再上屏")
            else:
                self.set_state(phase=PHASE_IDLE, last_text=text, preview="",
                               injected=f"未上屏：目标是「{self.state.get('target_name')}」，"
                                        f"但你切到了「{cur_name}」且拉不回来")
                log("输出", self.state["injected"])
                return
        try:
            injected = self.injector.inject(text)
        except Exception as e:
            injected = f"未上屏：注入异常 {e}"
        log("输出", f"{injected}   [松手→停录 {(t_rec_stop - t_release) * 1000:.0f}ms"
                    f"（关流 {cap.stream_stop_ms:.0f}+{cap.stream_close_ms:.0f}ms"
                    f"，等预览 {cap.preview_wait_ms:.0f}ms，上次预览 {cap.last_preview_ms:.0f}ms）"
                    f"+ 解码 {ms:.0f}ms"
                    f" + 上屏 {self.injector.ax_ms + self.injector.type_ms:.0f}ms"
                    f" = 松手→完成 {(time.monotonic() - t_release) * 1000:.0f}ms]")
        self.set_state(phase=PHASE_IDLE, last_text=text, preview="", injected=injected,
                       result_ts=time.monotonic())
        self.flash(*(("checkmark.circle.fill", (0.25, 0.85, 0.4, 1.0))
                     if injected.startswith("已上屏")
                     else ("doc.on.clipboard", (0.8, 0.8, 0.85, 1.0))))

    # ---------- 菜单动作（主线程） ----------

    def popup_menu(self):
        """悬浮条被点击：把菜单弹在它下面（等价于点菜单栏图标）。"""
        menu = self.item.menu()
        menu.popUpMenuPositioningItem_atLocation_inView_(None, self.pill.menu_anchor(), self.pill.view)

    def tick_(self, _timer):
        self._recheck_permissions()
        if not self._checked_visibility:
            self._checked_visibility = True
            self.self_check_visibility()

        st = self.get_state()
        phase = PHASE_ERR if (st["paused"] or st["connected"] is False) else st["phase"]
        sf, tint, label, glyph = PHASE_META[phase]
        if st["paused"]:
            label, glyph = "已暂停", "○"
        elif st["connected"] is False:
            label, glyph = "设备未连接", "○"
        elif phase == PHASE_ERR:                     # 未上屏：把原因写出来
            label = "未上屏"
        if phase == PHASE_PROC:                      # 转圈动画（转写 + 注入都算「上屏中」）
            self._spin = (getattr(self, "_spin", -1) + 1) % len(SPINNER)
            glyph = SPINNER[self._spin]

        # 菜单栏图标（本机菜单栏排满，画不出来；别的机器/有位置时会显示）
        self.set_icon(sf, tint)

        # 菜单文案
        conn = {True: "已连接", False: "未连接", None: "连接中…"}[st["connected"]]
        mic = {True: "已授权", False: "未授权（去授权）", None: "检查中"}[st["mic_ok"]]
        post = {True: "已授权", False: "未授权（现在写不进输入框）", None: "检查中"}[st["post_ok"]]
        self.mi_status.setTitle_(f"{glyph} {label} · 设备{conn} · {st['device_info']}")
        self.mi_preview.setTitle_("最近结果：" + (st["last_text"] or st["preview"] or "（无）")[:52])
        if st["injected"]:
            self.mi_preview.setTitle_(self.mi_preview.title() + "   · " + st["injected"][:30])
        self.mi_copy.setEnabled_(bool(st["last_text"]))
        self.mi_toggle.setTitle_("恢复监听" if st["paused"] else "暂停监听")
        self.mi_mic.setTitle_(f"权限：麦克风 {mic}")
        self.mi_post.setTitle_(f"权限：辅助功能（AX 直写用）{post}")
        self.mi_pill.setTitle_(f"悬浮状态条：{'自动（空闲收起）' if self.pill_auto else '已关闭'}")

        # 悬浮条：空闲时收起来不占位置（用户要求），只在「有事要说」的时候出现。
        age = time.monotonic() - st.get("result_ts", 0)
        want_pill = pill_wanted(st, age, self.pill_auto)
        fault = st["injected"].startswith("未上屏") and age < FAULT_HOLD_S
        if want_pill:
            # 引导元素（波形/呼吸）由 pill 自己画，文字里不再塞 ○●◐ 和转圈字符
            W, RED = AppKit.NSColor.whiteColor(), AppKit.NSColor.systemRedColor()
            BLUE, YELLOW = AppKit.NSColor.systemBlueColor(), AppKit.NSColor.systemYellowColor()
            ORANGE = AppKit.NSColor.systemOrangeColor()
            lead = Pill.LEAD_NONE
            pulse = False
            color, text, detail = W, f"{label}", None
            if phase == PHASE_REC:
                # 状态行只放「听写中」，预览文字走下面一行——文字变长时是条子左右张开，
                # 状态标签始终钉在正中间不动（用户要求）。
                lead, color = Pill.LEAD_WAVE, RED
                # 不在这里截断：能放多少行由 pill 按实际行高决定（放不下就显示最近的尾巴），
                # 以前这里硬切 [:60]（正好两行），长语音说到两行就再也不长了。
                detail = (st["preview"] or None)
            elif st["connected"] is None:
                # 句柄拿到了但设备还没证明自己能发按键：这时候提示不能消失，
                # 否则用户以为能用了，按下去却什么都没发生（见 _device_ready）
                text, color = "设备连接中…", YELLOW
            elif phase == PHASE_PROC:
                # 上屏中不再重复显示转写文本（用户要求）：缩小成一个小条「上屏中」就够了。
                # 呼吸是这一档唯一的动效，pulse=True 才让 pill 的 30fps 定时器开着。
                color, pulse = BLUE, True
            elif st["paused"]:
                color = YELLOW
            elif st["connected"] is False:
                color = YELLOW
            elif st["phase"] == PHASE_ERR:
                # 持续的错误态（模型没下载完就跑起来了）：reason 形如「模型加载失败：xxx」，
                # 大标题取冒号前那半句，剩下的放内容行——原来只有「未上屏」三个字，看不出原因
                head, _, tail = (st["reason"] or "出错了").partition("：")
                text, color, detail = (head or "出错了"), ORANGE, (tail.strip() or None)
            elif not st["post_ok"]:
                text, color = "缺辅助功能权限", ORANGE
            elif fault:
                text, color = st["injected"][:30], ORANGE
                detail = (st["reason"].partition("：")[2].strip() or None)   # 失败原因写全
            self.pill.set_status(text, color, lead, detail, pulse=pulse)
        # 先更新内容再显示：窗口弹出来时不会闪一下上一次的旧内容
        if want_pill != bool(self.pill.win.isVisible()):
            self.pill.set_visible(want_pill)
            log("悬浮条", "显示" if want_pill else "收起（空闲）")

    @objc.python_method
    def flash(self, symbolic: str, tint, secs: float = 1.5) -> None:
        """结果出来后短暂换菜单栏图标：上屏成功 = 对勾绿，失败 = 橙色警告。"""
        self._flash = (symbolic, time.monotonic() + secs, tint)

    @objc.python_method
    def symbol(self, name: str):
        """按名字取 SF Symbol 并设成 template（不透明处自动跟随菜单栏明暗反色）。"""
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if img is None:
            return None
        img.setTemplate_(True)
        return img

    @objc.python_method
    def set_icon(self, name: str, tint) -> None:
        img = self.symbol(name)
        if img is None:
            self.button.setTitle_("OS")     # 极端情况退回文字，至少看得见
            return
        self.button.setTitle_(self.args.label)
        self.button.setImage_(img)
        try:
            self.button.setContentTintColor_(
                AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*tint) if tint else None)
        except Exception:
            pass

    @objc.python_method
    def _recheck_permissions(self) -> None:
        """每 2 秒复查一次：用户在系统设置里勾上权限后立刻生效，不用重启应用。"""
        now = time.monotonic()
        if now - getattr(self, "_perm_checked_at", 0) < 2.0:
            return
        self._perm_checked_at = now
        post, mic = ax_trusted(), mic_status() == 3
        st = self.get_state()
        if (post, mic) != (st["post_ok"], st["mic_ok"]):
            self.set_state(post_ok=post, mic_ok=mic)
            log("权限", f"变更：麦克风 {'已授权' if mic else '未授权'}，"
                        f"辅助功能 {'已授权' if post else '未授权'}"
                        + ("（现在可以上屏了）" if post else ""))

    @objc.python_method
    def _pill_cfg(self):
        """悬浮条位置存哪：优先 Application Support，写不了就用临时目录（不影响使用）。"""
        import tempfile
        for d in (Path.home() / "Library/Application Support/VoxKey",
                  Path(tempfile.gettempdir()) / "VoxKey"):
            try:
                d.mkdir(parents=True, exist_ok=True)
                return d / "pill.json"
            except OSError:
                continue
        return None

    @objc.python_method
    def _load_pill_x(self):
        f = self._pill_cfg()
        try:
            d = json.loads(f.read_text())
            return d.get("center_x", d.get("x"))     # 兼容旧字段
        except Exception:
            return None

    @objc.python_method
    def _remember_pill_x(self, center_x: float) -> None:
        """记住的是悬浮条的「中心 x」——宽度变化时以中心为锚，才不会越用越偏。"""
        self.pill.center_x = center_x
        f = self._pill_cfg()
        if not f:
            return
        try:
            f.write_text(json.dumps({"center_x": round(center_x, 1)}))
        except OSError:
            pass

    @objc.python_method
    def self_check_visibility(self) -> None:
        """自检菜单栏图标是否真的画在屏幕里：isVisible 在 macOS 26 上不可信，看 button 窗口 frame。"""
        try:
            win = self.button.window()
            frame = win.frame() if win else None
            screen = AppKit.NSScreen.mainScreen().frame()
            inside = bool(frame and frame.origin.x + frame.size.width <= screen.origin.x + screen.size.width
                          and frame.origin.y >= screen.origin.y)
            notch_l = AppKit.NSScreen.mainScreen().auxiliaryTopLeftArea()
            notch_r = AppKit.NSScreen.mainScreen().auxiliaryTopRightArea()
            in_notch = bool(frame and notch_l and notch_r and
                            frame.origin.x < notch_r.origin.x and
                            frame.origin.x + frame.size.width > notch_l.origin.x + notch_l.size.width)
            try:
                pw = self.pill.win
                log("自检", f"悬浮条 visible={bool(pw.isVisible())} alpha={pw.alphaValue():.2f} "
                            f"level={pw.level()} frame={pw.frame()} screen={pw.screen() is not None}")
            except Exception as e:
                log("自检", f"取悬浮条状态失败：{e}")
            log("自检", f"状态项 isVisible={bool(self.item.isVisible())} frame={frame} "
                        f"屏内={inside} 落在刘海区={in_notch}"
                        f"（注意：macOS 26 的 NSSceneStatusItem 这个 frame 跟实际渲染位置不一致——"
                        f"实测 frame 报 668、截图 diff 显示真在 1095）")
        except Exception as e:
            log("自检", f"取图标位置失败：{e}")

    def copyLast_(self, _sender):
        try:
            import pyperclip
        except ImportError:
            log("剪贴板", "没装 pyperclip（pip install -e '.[tools]'），这个菜单项用不了")
            return
        text = self.get_state()["last_text"]
        if text:
            pyperclip.copy(text)
            log("剪贴板", "已复制上次结果")

    def togglePause_(self, _sender):
        paused = not self.get_state()["paused"]
        self.set_state(paused=paused)
        log("监听", "已暂停" if paused else "已恢复")

    def manualToggle_(self, _sender):
        if self.current_stop is None:
            self.on_key_state(0, [KC_VOICE])
        else:
            self.current_stop[0].set()
            self.state["t_release"] = time.monotonic()
            self.current_stop = None

    def testInject_(self, _sender):
        """不录音、不转写，直接往当前焦点输入框注入一句测试文本——用来单独验证上屏这条路。"""
        text = "VoxKey 上屏测试 ABC123"
        t0 = time.monotonic()
        injected = self.injector.inject(text)
        log("测试上屏", f"{injected}   [{(time.monotonic() - t0) * 1000:.0f}ms]")
        self.set_state(last_text=text, injected=injected, result_ts=time.monotonic())

    def togglePill_(self, _sender):
        """开关「自动显示」：关掉之后悬浮条不再自己弹出来（空闲本来就收起了，这是个逃生开关）。"""
        self.pill_auto = not self.pill_auto
        if not self.pill_auto:
            self.pill.set_visible(False)
        log("悬浮条", "自动显示已开（听写/上屏/异常时出现）" if self.pill_auto else "已关闭（不再自动出现）")

    def reconnect_(self, _sender):
        if self.supervisor:
            self.supervisor.reconnect()

    def openMic_(self, _sender):
        open_settings("Privacy_Microphone")

    def requestPost_(self, _sender):
        """调 CGRequestPostEventAccess：系统会把本程序加进「辅助功能」列表并弹框，
        用户勾上后 tick 里的轮询会自己发现，不用重启。"""
        # 官方承诺会弹框并跳设置的入口（CGRequestPostEventAccess 实测不弹）
        AS = __import__("ApplicationServices")
        AS.AXIsProcessTrustedWithOptions(
            {AS.kAXTrustedCheckOptionPrompt: True})
        log("权限", "已请求辅助功能权限（系统应弹框/跳设置），勾上后 2 秒内自动生效")
        open_settings("Privacy_Accessibility")

    def openAccessibility_(self, _sender):
        open_settings("Privacy_Accessibility")

    def quitApp_(self, _sender):
        log("退出", "拜拜")
        self.stop_event.set()
        if self.supervisor:
            self.supervisor.reconnect()
        AppKit.NSApplication.sharedApplication().terminate_(None)

    # ---------- 启动 ----------

    @objc.python_method
    def setup_ui(self) -> None:
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.button = self.item.button()
        self.button.setImage_(self.symbol("mic"))
        self.button.setImagePosition_(AppKit.NSImageLeading if hasattr(AppKit, "NSImageLeading") else 3)
        self.button.setTitle_(self.args.label)
        self.button.setToolTip_("VoxKey 语音输入：按住设备语音键说话")
        menu = AppKit.NSMenu.new()

        def add(title, action=None, enabled=True):
            mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
            if action:
                mi.setTarget_(self)
            else:
                mi.setEnabled_(False)
            menu.addItem_(mi)
            return mi

        self.mi_status = add("启动中…")
        self.mi_preview = add("最近结果：（无）")
        self.mi_copy = add("复制上次结果（手动点才用剪贴板）", "copyLast:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self.mi_toggle = add("暂停监听", "togglePause:")
        self.mi_manual = add("手动开始/结束说话", "manualToggle:")
        self.mi_reconnect = add("重新连接设备", "reconnect:")
        self.mi_testinject = add("测试上屏（往当前输入框写一行测试文本）", "testInject:")
        self.mi_pill = add("悬浮状态条：自动（空闲收起）", "togglePill:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        self.mi_mic = add("权限：麦克风 …", "openMic:")
        self.mi_post = add("权限：辅助功能（上屏用）…", "openAccessibility:")
        self.mi_request_post = add("启用上屏：请求辅助功能权限", "requestPost:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        add("退出", "quitApp:")
        self.item.setMenu_(menu)

        # 菜单栏在刘海屏上可能被排满（本机实测：系统直接不给新状态项位置），
        # 所以再挂一条悬浮状态条：不占菜单栏、不抢焦点、可拖动、单击弹同一个菜单。
        self.pill = Pill.alloc().initWithHandler_(lambda: self.popup_menu())
        self.pill.view.on_moved = self._remember_pill_x
        self.pill.place_bottom(self._load_pill_x())
        self.pill.set_status("启动中…", AppKit.NSColor.whiteColor(), Pill.LEAD_NONE)
        self.pill.show()

    def applicationDidFinishLaunching_(self, _note):
        self.setup_ui()
        if not self.args.no_device:
            self.supervisor = KeySupervisor(self.on_key_state, self.on_conn, self.stop_event,
                                            ready_probe=self._device_ready)
            self.supervisor.start()
        else:
            self.set_state(connected=False, reason="--no-device 模式")
            log("设备", "--no-device：不读按键，用菜单里「手动开始/结束说话」测试")
        Foundation.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.12, self, "tick:", None, True)
        log("就绪", "按住设备语音键说话，松手上屏；菜单栏图标里可暂停/退出。")

    @objc.python_method
    def run(self) -> None:
        # 实例必须存下来：写成 SingleInstance().acquire() 的话临时对象当场被回收、
        # 文件句柄一关 flock 就释放了，锁活不过一次函数调用，第二个实例照样能起。
        self._lock = SingleInstance()
        if not self._lock.acquire():
            print(f"已有一个 VoxKey 菜单栏实例在跑（锁文件 {LOCK_FILE}）。", flush=True)
            sys.exit(2)

        self.set_state(mic_ok=ensure_mic_permission(), post_ok=ax_trusted())
        st = self.get_state()
        log("权限", f"麦克风 {'已授权' if st['mic_ok'] else '未授权（采不到音）'}，"
                    f"辅助功能 {'已授权' if st['post_ok'] else '未授权（写不进输入框）'}")

        self.audio_device = find_input_device(self.args.device)
        try:
            name = sd.query_devices(self.audio_device)["name"]
        except Exception:
            name = "系统默认"
        print(f"麦克风   : {name}", flush=True)

        def load_model():
            self.set_state(phase=PHASE_PROC)
            try:
                self.decoder = Decoder(load_recognizer(self.args.model),
                                       max_segment_s=self.args.segment_s)
                self.set_state(phase=PHASE_IDLE)
                log("模型", "已就绪，可以开始说话")
            except Exception as e:
                self.set_state(phase=PHASE_ERR, reason=f"模型加载失败：{e}")
                log("错误", f"模型加载失败：{e}")
        threading.Thread(target=load_model, daemon=True).start()

        def read_device_info():
            try:
                with VibeKey() as vk:
                    bat = vk.battery()
                    info = f"固件 {vk.version()} / 电量 {f'{bat[0]}%' if bat else '?'}"
            except VibeKeyNotFound:
                info = "厂商通道未打开"
            self.set_state(device_info=info)
        threading.Thread(target=read_device_info, daemon=True).start()

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        app.setDelegate_(self)
        app.run()


def main() -> int:
    ap = argparse.ArgumentParser(description="VoxKey 常驻（按住说话 → 文字进光标）")
    ap.add_argument("--model", default="funasr-nano-int8")
    ap.add_argument("--device", default="AU05", help="录音设备名关键字，空串=系统默认")
    ap.add_argument("--segment-s", type=float, default=22.0, help="长语音切段长度（秒）")

    ap.add_argument("--no-device", action="store_true", help="不读按键（菜单手动开始/结束）")
    ap.add_argument("--label", default="VoxKey", help="图标旁的文字（空串=不显示，默认 VoxKey）")
    ap.add_argument("--save-audio", metavar="DIR", default=None,
                    help="每次按键的音频都存成 wav + 一行索引（排查丢音频用）")
    ap.add_argument("--debug-keys", action="store_true",
                    help="把设备发来的每一次按键报文打出来（排查连按/丢键用）")
    ap.add_argument("--min-audio-s", type=float, default=MIN_AUDIO_S,
                    help=f"短于这个时长直接忽略（默认 {MIN_AUDIO_S} 秒）")
    ap.add_argument("--newline", default="space", choices=["space", "keep"],
                    help="识别结果带换行时怎么处理：space=换成空格（默认，避免在微信/Slack 里误发送）；keep=原样发")
    args = ap.parse_args()

    app = TrayApp.alloc().initWithArgs_(args)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
