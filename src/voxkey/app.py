#!/usr/bin/env python3
"""VoxKey 常驻主程序：按住设备语音键说话 → 松手 → 文字进当前光标。

跑法（仓库根目录下）：
  PYTHONPATH=src python -m voxkey.app               # 默认模型 funasr-nano-int8
  PYTHONPATH=src python -m voxkey.app --no-device   # 不读按键，用菜单手动开始/结束

一次说话的状态机（悬浮条四态，状态靠文字 + 颜色表达，不用 emoji）：
  空闲 → 听写中（波形跟实时电平跳，下面一行显示已识别内容）→ 上屏中 → 空闲；
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

from voxkey.audio import (SPEECH_MIN_PEAK, SPEECH_MIN_SEC, Recorder, find_input_device,
                          has_speech, reload_audio_devices)
from voxkey.device import protocol as P
from voxkey.device.device import VibeKey
from voxkey.devicewatch import DeviceState, DeviceWatch
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
FAULT_HOLD_S = 3.0      # 「未上屏」提示在悬浮条上留多久（用户要求：空闲就收起来）
LINGER_S = 1.2          # 悬浮条收起之前，先把「收场状态」显示这么久（用户要求：消失前要有对应交互）
PHASE_META = {
    PHASE_IDLE: ("circle", None, "空闲", "○"),
    PHASE_REC: ("circle.fill", (1.00, 0.30, 0.30, 1.0), "听写中", "●"),
    PHASE_PROC: ("circle.dotted", (0.30, 0.55, 1.00, 1.0), "处理中", "◐"),
    PHASE_ERR: ("exclamationmark.circle", (1.00, 0.60, 0.10, 1.0), "未上屏", "○"),
}


def next_linger(want_pill: bool, last_want: bool, linger_until: float,
                now: float, hold_s: float) -> float:
    """收场倒计时：`want_pill` 由真变假的那一刻开始计时，返回新的到期时刻（0 = 没在计时）。

    抽出来是为了能直接断言这条规则（见 tools/smoke.py）：它踩过坑——之前是在状态变化那一刻
    就把到期时刻算好，结果「设备开机」这种（此时模型还在加载、浮窗本来就该显示）提示会被
    后面那些 want_pill=True 的 tick 当场清掉，用户根本看不到「已开机」。
    """
    if want_pill:
        return 0.0
    if last_want:
        return now + hold_s
    return linger_until


def pill_wanted(st: dict, fault_age: float, auto: bool = True) -> bool:
    """悬浮条该不该出现（用户要求：空闲时不占屏幕，只在「有事要说」的时候弹出来）。

    出现的情况：听写中 / 上屏中、设备掉线或还在探测、暂停了、权限缺失、
    模型加载失败这类持续错误、刚上屏失败的那几秒、以及**充电中**（用户要求：充电时一直挂着，
    这样随时能看见电量和充电状态）。
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
        or st["battery_charging"]                           # 充电中：一直显示电量
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


# ---------------------------------------------------------------- 菜单栏 App

class TrayApp(Foundation.NSObject):

    def initWithArgs_(self, args):
        self = objc.super(TrayApp, self).init()
        if self is None:
            return None
        self.args = args
        self.decoder = None
        self.injector = Injector(newline_mode=args.newline)   # 只走不碰剪贴板的两条路
        from voxkey.pipeline import SpeakingPipeline, PipelineConfig
        self.pipeline = SpeakingPipeline(
            get_decoder=lambda: self.decoder,   # 用时现取，避免和 self.decoder 两份引用不同步
            config=PipelineConfig(min_audio_s=args.min_audio_s, device_hint=args.device,
                                  newline_mode=args.newline),
            injector=self.injector,
            archive_dir=args.save_audio,
            on_archive=lambda wav, summ: log("存档", f"{wav}  {summ}"))
        self.audio_device = None
        self.supervisor = None
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.current_stop = None
        self._checked_visibility = False
        self._flash = None
        self._icon_key = None           # 上次设过的菜单栏图标（状态没变就不重建图片）
        self.state_lock = threading.Lock()
        self.state = {
            "phase": PHASE_IDLE, "connected": None, "reason": "", "paused": False,
            "last_text": "", "device_info": "设备信息读取中…",
            "mic_ok": None, "post_ok": None, "injected": "", "device_off": False,
            "fw_version": "", "battery_pct": None, "battery_mv": None,
            "battery_charging": False,          # 悬浮条角标（电量/电压/是否充电）
        }
        self.gesture_start = None
        self.utt_no = 0
        self._routed = set()
        self.pill_auto = True          # 悬浮条自动显示（空闲收起）；菜单里可以关掉
        self._audio_dirty = False      # 设备掉过线 → 下次录音前重新枚举音频设备
        self._last_unknown: tuple = ()  # 上次报过的未知键码（去重，别刷屏）
        self._woke_device = False       # 这次连接有没有给设备发过唤醒心跳
        self._standby_logged = False    # 待机阈值每次连接只记一次
        # 每 3 秒问一次厂商通道会一直「吵醒」设备，它自己那个 300 秒待机就永远触发不了。
        # 要观察待机（或单纯省电）时用 VOXKEY_NO_DEVICE_PROBE=1 关掉周期探测，
        # 这时设备状态只在启动时读一次，之后靠按键报文判断在线。
        self._probe_enabled = not os.environ.get("VOXKEY_NO_DEVICE_PROBE")
        self._linger_text = ""          # 收场提示的文字（"" = 没有）
        self._linger_color = None
        self._linger_until = 0.0        # 收场倒计时的到期时刻（0 = 还没开始计时）
        self._linger_hold = LINGER_S
        self._last_want = False         # 上一次 tick 里「有没有正经理由显示」
        self._pill_shown = False        # 我们自己的显隐标志（淡出期间 win.isVisible() 还是 true）
        self.min_audio_s = getattr(args, "min_audio_s", MIN_AUDIO_S)
        return self

    # ---------- 状态：后台线程写，主线程 tick 读 ----------

    @objc.python_method
    def _set_linger(self, text: str, color, secs: float = LINGER_S) -> None:
        """记一条「收场」提示：等浮窗真的没有别的显示理由了，先用它顶一会儿再淡出收起。

        用户要求：浮窗消失前要有一个对应的交互——「上屏中」别直接跳没了，得先显示「空闲」
        再缩掉；设备开机也别静悄悄地就没了，先显示绿色的「已开机」。
        注意这里只「记下来」，倒计时从 want_pill 由真变假那一刻才开始：不然像开机这种
        （此时模型还在加载、浮窗本来就该显示）提示会被当场清掉。
        """
        self._linger_text, self._linger_color = text, color
        self._linger_hold = secs

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
        if keys and (st["device_off"] or st["connected"] is False):
            # 收到按键 = 设备明明活着：刚才那个「关机」判定错了（多半只是进了待机，或者是我们
            # 探测时它正在打盹）。立刻翻回在线，别让用户对着「设备已关机」按半天。
            # --no-device 模式没有 supervisor，菜单手动触发不会走到这里，但防御性跳过。
            if self.supervisor is not None:
                log("设备", "收到按键报文——设备在线（此前的『待机/关机』是它在打盹或误判）")
                self.supervisor._emit(DeviceState.READY, "")   # 按键来了 = 设备活着，翻回在线
                self._set_linger("已唤醒", AppKit.NSColor.systemGreenColor())
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
            reload_audio_devices()
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
        self.set_state(phase=PHASE_REC, reason="")
        self.state["t_press"] = time.monotonic()
        pid, name = frontmost_info()
        self.state["target_pid"], self.state["target_name"] = pid, name
        log("按键", "开始录音")
        threading.Thread(target=self.handle_utterance, args=(stop_ev,), daemon=True).start()

    @objc.python_method
    def on_device_state(self, state: DeviceState, reason: str = "") -> None:
        """设备状态变化（来自 DeviceWatch 线程）：翻成 state 字典里的字段并处理副作用。"""
        prev = self.get_state()
        if state is not DeviceState.READY:
            self._audio_dirty = True      # 掉过线/还没就绪，音频设备表要重新枚举
            self._woke_device = False     # 下次连上要重新唤醒一次
            self._standby_logged = state is DeviceState.DISCONNECTED   # 只真掉线才重记
            # 设备不在线时版本/电量读不到，角标跟着空掉，别留着上一次的旧数字
            self.set_state(connected=(state is not DeviceState.DISCONNECTED),
                           reason=reason, device_off=state is DeviceState.STANDBY_OR_OFF,
                           fw_version="", battery_pct=None, battery_mv=None,
                           battery_charging=False)
            return
        # 就绪那一刻：关机→开机说「已开机」，连接中→就绪说「已连接」（用户要求消失前先说一声）
        green = AppKit.NSColor.systemGreenColor()
        if prev["device_off"]:
            self._set_linger("已开机", green)
        elif prev["connected"] is None:
            self._set_linger("已连接", green)
        self.set_state(connected=True, reason="", device_off=False)

    # ---------- 设备「真的能用了吗」（插回来时报「已连接」之前先过这一关） ----------

    @objc.python_method
    def _read_device_status(self) -> str:
        """读一次设备本体状态。返回 ""=在线；非空字符串 = 为什么问不到。

        一次会话里把版本和电量都读掉——电量顺便喂给悬浮条右上角的角标（用户要求）。
        连着第一次读时顺带把「待机秒数」也读出来记进日志：协议里写着「待机超时后厂商口不响应」，
        所以「厂商通道不应答」既可能是关机、也可能只是闲久了进待机，得先知道这个阈值。
        """
        try:
            with VibeKey() as vk:
                ver = vk.version()
                if ver is None:
                    return "打开成功但不应答"
                bat = vk.battery()
                if not self._standby_logged:
                    self._standby_logged = True
                    secs = vk.standby_seconds()
                    if secs is not None:
                        log("设备", f"待机设置 {secs}s（超时后厂商通道不再响应——所以「问不到」"
                                    f"不等于关机，见 _device_power_probe）")
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        pct = bat[0] if bat else None
        charging = bool(bat[2]) if bat else False
        info = f"固件 {ver}" + (f" · 电量 {pct}%" if pct is not None else "")
        if charging:
            info += "（充电中）"
        if info != self.get_state()["device_info"]:      # 变了才记，别每 3 秒刷一遍
            log("设备", info)
        self.set_state(fw_version=ver, battery_pct=pct, battery_charging=charging,
                       battery_mv=(bat[1] if bat else None), device_info=info)
        return ""

    @objc.python_method
    def _wake_device(self) -> None:
        """发一帧心跳（协议表里这条命令写的是「试着唤醒假死的厂商口」）。

        实话：实测**叫不醒待机中的设备**（静置 7 分钟后不应答，连发心跳仍不应答）。
        留着是因为它只花一帧，万一遇到的是「厂商口假死」这种别的毛病还能试试；判断逻辑里
        不再依赖它——待机和关机就是区分不了。
        """
        try:
            with VibeKey() as vk:
                vk.send(P.build_frame(0x06, 0x01, 0x23, 0x00))
        except Exception as e:
            log("设备", f"心跳没发出去：{e}")

    @objc.python_method
    def _device_power_probe(self) -> str:
        """周期性问一句「设备本体还在吗」。返回 ""=在线，否则返回「为什么问不到」。

        **待机和真关机区分不了**，别在这儿白费劲：实测（静置 7 分钟、期间不碰厂商通道也不碰设备）
        设备 300 秒没用就进待机，之后厂商通道一律不应答；而真关机是同样的现象，连发 heartbeat
        都叫不醒（试过）。而且待机时**第一次按键会被设备自己吞掉**（用户实测：第一次没反应、
        第二次才行），所以只能对外说「待机/关机中」，等按键报文来了再翻回在线。
        """
        if self.current_stop is not None:
            return ""                      # 录音中不打扰
        if not self._probe_enabled:
            return ""                      # VOXKEY_NO_DEVICE_PROBE=1：完全不打搅设备
        return self._read_device_status()

    @objc.python_method
    def _audio_device_present(self) -> bool:
        """AU05 的录音设备在 CoreAudio 里了吗。

        必须先重新枚举 PortAudio：插拔之后它手里那张设备表还是旧的，不重载的话这里永远返回
        「在」（旧表里就有 AU05），探测就失去意义。录音进行中不动它（重载会废掉正在录的流）。
        """
        if self.current_stop is not None:
            return True
        reload_audio_devices()
        return find_input_device(self.args.device) is not None

    @objc.python_method
    def _device_ready(self) -> bool:
        """设备真的能用了么。键盘集合「能打开」不算——那只是个句柄。

        实测（插拔接收器）：集合打开之后约 3.6 秒里设备一个按键报文都不发，用户正好在这段
        时间按键就是「按了没反应」。这里要求①厂商通道应答②AU05 的录音设备已经在 CoreAudio 里，
        两条都成立才报「已连接」、悬浮条才收起——用户看到提示消失就能马上用。
        """
        if not self._woke_device:
            self._woke_device = True
            self._wake_device()          # 先敲一下（聊胜于无，见 _wake_device 的说明）
        if self._read_device_status() != "":
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
        self.set_state(phase=PHASE_PROC)   # 松手即切「处理中」，覆盖关流+转写+注入整段
        log("按键", f"{why} → 上屏中…")

    @objc.python_method
    def handle_utterance(self, stop_ev: threading.Event) -> None:
        cap = Recorder(self.decoder, self.audio_device,
                       on_level=lambda rms: self.pill.set_level(rms))   # 悬浮条波形
        try:
            cap.start()
        except Exception as e1:
            # 打不开基本上是插拔过接收器：PortAudio 的设备表还是旧的（见 _reload_audio_devices），
            # 先重新枚举再重解析编号，然后重试一次。
            log("音频", f"打不开（{e1}），重新枚举音频设备后重试")
            reload_audio_devices()
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
                               on_level=lambda rms: self.pill.set_level(rms))
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
    @objc.python_method
    def _finish_utterance(self, cap, samples, t_release, t_rec_stop, cancelled: bool = False) -> None:
        """一次说话的收尾：四道闸 → 解码 → 上屏。业务判断都在 pipeline，这里只管状态与提示。"""
        gate = self.pipeline.gates(samples, cancelled, self.min_audio_s)
        if gate == "cancelled":               # 取消状态必须在外面读、传进来：caller 已 clear 掉事件
            self.set_state(phase=PHASE_IDLE)
            return
        dur = len(samples) / SAMPLE_RATE
        hold_ms = (t_release - self.get_state().get("t_press", t_release)) * 1000
        rms = float(np.sqrt((samples ** 2).mean())) if len(samples) else 0.0
        _, sp_sec, sp_peak = has_speech(samples)
        self.pipeline.archive(samples, hold_ms)
        log("音频", f"按住 {hold_ms:.0f}ms → 录到 {dur:.2f}s，RMS {rms:.4f}，"
                    f"像说话的时长 {sp_sec:.2f}s（峰值 {sp_peak:.4f}）")
        if gate == "too_short":               # 太短：不送模型，免得被脑补出「嗯」这类填充词
            log("忽略", f"只录到 {dur:.2f}s（< {self.min_audio_s:.2f}s），这次丢掉")
            self.set_state(phase=PHASE_IDLE, last_text="",
                           injected=f"未上屏：只录到 {dur:.2f}s（太短，已忽略）",
                           result_ts=time.monotonic())
            return
        if gate == "no_speech":
            # 没有有效语音就别送模型——它会对着底噪脑补出「嗯。」（用户报的问题）。
            # 判据见 audio.has_speech：自适应底噪 + 像说话的总时长 + 峰值。
            log("忽略", f"没检测到说话（像说话的时长 {sp_sec:.2f}s < {SPEECH_MIN_SEC}s "
                        f"或峰值 {sp_peak:.4f} < {SPEECH_MIN_PEAK}），这次丢掉")
            self.set_state(phase=PHASE_IDLE, last_text="",
                           injected=f"未上屏：没听到说话（{sp_sec:.1f}s 有效语音）",
                           result_ts=time.monotonic())
            return
        text, decode_ms = self.pipeline.transcribe(samples)
        if not text:
            log("结果", f"（{dur:.1f}s 没听清）")
            self.set_state(phase=PHASE_IDLE, last_text="")
            self._set_linger("没听清", AppKit.NSColor.systemYellowColor())
            return
        seg = f"{self.decoder.last_segments} 段，" if self.decoder.last_segments > 1 else ""
        fin = f"其中 {cap.finalized_segments} 段录音期间已定稿，" if cap.finalized_segments else ""
        log("结果", f"{text}   [音频 {dur:.1f}s，{seg}{fin}松手→出字 {decode_ms:.0f}ms]")

        # 上屏前先把「按下语音键那一刻的前台 App」拉回前台：中途切窗口的话，
        # 字会打进错误的窗口（Wispr 也是存焦点元素 + 粘贴前还原）。
        pid = self.get_state().get("target_pid")
        cur, cur_name = frontmost_info()
        if pid and cur != pid:
            if reactivate(pid):
                log("焦点", f"中途切到了「{cur_name}」，已把「{self.state.get('target_name')}」拉回前台再上屏")
            else:
                self.set_state(phase=PHASE_IDLE, last_text=text,
                               injected=f"未上屏：目标是「{self.state.get('target_name')}」，"
                                        f"但你切到了「{cur_name}」且拉不回来",
                               result_ts=time.monotonic())
                log("输出", self.state["injected"])
                return
        injected, inject_ms = self.pipeline.inject(text)
        if self.injector.last_newlines:
            log("换行", f"识别结果含 {self.injector.last_newlines} 个换行，"
                        f"按 --newline={self.injector.newline_mode} 处理（避免在微信/Slack 里误发送）")
        log("输出", f"{injected}   [松手→停录 {(t_rec_stop - t_release) * 1000:.0f}ms"
                    f"（关流 {cap.stream_stop_ms:.0f}+{cap.stream_close_ms:.0f}ms"
                    f"）"
                    f"+ 解码 {decode_ms:.0f}ms"
                    f" + 上屏 {inject_ms:.0f}ms"
                    f" = 松手→完成 {(time.monotonic() - t_release) * 1000:.0f}ms]")
        self.set_state(phase=PHASE_IDLE, last_text=text, injected=injected,
                       result_ts=time.monotonic())
        # 上屏中→空闲：别直接跳没了，先把「空闲」亮一下再淡出（用户要求）
        self._set_linger("空闲", AppKit.NSColor.whiteColor())
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
        elif st["device_off"]:
            # 厂商通道沉默。实测（静置 7 分钟、一个厂商帧都不发）：设备 300 秒不用就进待机，
            # 进待机后厂商通道完全不应答。而真关机的现象**一模一样**——HID 枚举在、键盘集合
            # 能开、CoreAudio 里 AU05 也在，连发 heartbeat 都叫不醒（实测）。所以只能说「待机/关机中」。
            label, glyph = "设备待机/关机中", "○"
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
        self.mi_preview.setTitle_("最近结果：" + (st["last_text"] or "（无）")[:52])
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
        # 收场提示：want_pill 由真变假的那一刻才开始倒计时，之前只是把「准备说什么」记着
        now = time.monotonic()
        self._linger_until = next_linger(want_pill, self._last_want, self._linger_until,
                                         now, self._linger_hold)
        self._last_want = want_pill
        ling = bool(self._linger_text) and 0.0 < self._linger_until - now
        show_pill = want_pill or ling
        ending = self._linger_text        # 日志用：这一轮收起时的收场文字
        if not show_pill and self._linger_until:      # 收场也结束了，清干净
            self._linger_until = 0.0
            self._linger_text = ""
        if show_pill:
            # 角标：左上角固件版本、右上角电量（都来自厂商通道，设备不在线时是空的）。
            # 电量后面缀上电压：设备那个百分比字段是 10% 一档的粗表（实测充电一分钟、电压
            # 涨了 100mV，百分比纹丝不动），而电压是 1mV 分辨率、会实时跟着充放电动——
            # 想看「更精确」的就看它。充电时右上角转绿，省掉「充电」两个字省宽度。
            ver, pct, mv = st["fw_version"], st["battery_pct"], st["battery_mv"]
            bat_text = ""
            if pct is not None:
                bat_text = f"{pct}% · {mv / 1000:.2f}V" if mv else f"{pct}%"
            self.pill.set_meta(f"v{ver}" if ver else "", bat_text, st["battery_charging"])
            # 引导元素（波形/呼吸）由 pill 自己画，文字里不再塞 ○●◐ 和转圈字符
            W, RED = AppKit.NSColor.whiteColor(), AppKit.NSColor.systemRedColor()
            BLUE, YELLOW = AppKit.NSColor.systemBlueColor(), AppKit.NSColor.systemYellowColor()
            ORANGE = AppKit.NSColor.systemOrangeColor()
            lead = Pill.LEAD_NONE
            pulse = False
            color, text, detail = W, f"{label}", None
            if ling and not want_pill:
                # 收场提示：已经没有正经理由显示了，但先给用户看一眼结果状态（用户要求）
                text, color = self._linger_text, self._linger_color
            elif phase == PHASE_REC:
                # 状态行只放「听写中」，预览文字走下面一行——文字变长时是条子左右张开，
                # 状态标签始终钉在正中间不动（用户要求）。
                lead, color = Pill.LEAD_WAVE, RED
                # 不在这里截断：能放多少行由 pill 按实际行高决定（放不下就显示最近的尾巴），
                # 以前这里硬切 [:60]（正好两行），长语音说到两行就再也不长了。
            elif st["connected"] is None:
                # 句柄拿到了但设备还没证明自己能发按键：这时候提示不能消失，
                # 否则用户以为能用了，按下去却什么都没发生（见 _device_ready）
                text, color = "设备连接中…", YELLOW
            elif phase == PHASE_PROC:
                # 处理中（关流 + 等预览 + 最终解码 + 上屏，整段都算）：把文字留着，别让框空着。
                # 最终文字一解出来就换成它——用户要的是「上屏之前先看见完整的那句」，
                # 而听写时的预览是每 0.8 秒刷一次的，松手那一刻最多差着 0.8 秒的内容。
                color, pulse = BLUE, True
                detail = (st["reason"].partition("：")[2].strip() or None)   # 失败原因写全
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
        # 先更新内容再显示：窗口弹出来时不会闪一下上一次的旧内容。
        # 用我们自己的 _pill_shown 判断，不用 win.isVisible()——淡出那 0.28 秒里窗口仍然是
        # visible，拿它比较会每 0.12 秒重发一次收起。
        if show_pill != self._pill_shown:
            self._pill_shown = show_pill
            self.pill.set_visible(show_pill)
            log("悬浮条", "显示" if show_pill else f"收起（{ending or '空闲'}）")

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
        # tick 每 0.12 秒调一次，而状态大多数时候没变。实测每建一次 SF Symbol 图片要 0.027ms，
        # 什么都不判就重建 = 白白吃掉一个核的 ~22%。所以状态没变就什么都不做。
        if self._icon_key == (name, tint):
            return
        self._icon_key = (name, tint)
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
        if not paused:
            self._set_linger("已恢复监听", AppKit.NSColor.systemGreenColor())

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
            self._pill_shown = False
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
        self._pill_shown = True          # 跟 tick 里的显隐标志对齐（tick 用自己这个判断）

    def applicationDidFinishLaunching_(self, _note):
        self.setup_ui()
        if not self.args.no_device:
            self.supervisor = DeviceWatch(
                on_key=self.on_key_state, on_state=self.on_device_state,
                stop_event=self.stop_event,
                ready_probe=self._device_ready,
                power_probe=self._device_power_probe)
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
            if not self._read_device_status():
                self.set_state(device_info="厂商通道没打开")
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
