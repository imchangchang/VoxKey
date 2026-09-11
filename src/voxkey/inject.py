"""文字注入：两条路，都不碰剪贴板；都不行就明说没上屏。

用户要求：剪贴板方案一次都不要用、不降级，开辅助功能权限就行。
调研过的同类产品分两派——Wispr Flow / Handy / VoiceInk 全是「剪贴板 + 模拟 ⌘V」
（官方文档与源码都写着），**不碰剪贴板的那一派是输入法**（微信输入法、系统听写），
它们的做法是「把文字当成你打字打进去」。所以这里按输入法那派来：

L1 **AX 直写**：把文字写进前台 App 焦点控件的选中区（`kAXSelectedTextAttribute`）。
   Apple 头文件把这个属性标成 `Writable? No.`，**写成功≠插进去了**——真机实测 VS Code
   （Electron）就是「err=0 但内容没变」，所以必须回读 `kAXValueAttribute` 校验；
   校验不过就换 L2。原生 App（TextEdit、备忘录这类）走这条最快最干净，不逐字打。
L2 **合成 Unicode 按键**：`CGEventKeyboardSetUnicodeString` + `CGEventPost`，一次最多
   20 个 UTF-16 单元，把整串文字当键盘输入打进去。不碰剪贴板，中文与 emoji 都能过。
   代价：逐段打字有耗时（约 100 字/秒），个别 App 会忽略合成事件。
   【未验证】微信输入法到底走不走这条：我们只查过它的二进制里有 CGEventPost(8 处)、
   insertText:(3 处)、AXIsProcessTrusted(18 处)，**证明不了快捷键语音模式具体用哪条**；
   更可能是「它本身是当前输入法 → 直接 insertText 交字，辅助功能权限是给监听 Fn 用的」。

其他细节：
- 查询前给前台 App 设 1 秒 AX 消息超时（`AXUIElementSetMessagingTimeout`），
  否则目标 App 卡住时 AX 调用会挂好几秒（用户反馈过「转写中很久」）；
- 先给 Electron/Chromium 设 `AXManualAccessibility=true` 再查焦点；
- 安全输入期间（密码框、终端 sudo）直接拒绝注入。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import time

import ApplicationServices as AS
import Quartz

_KAX_SELECTED_TEXT = "kAXSelectedTextAttribute"
_KAX_VALUE = "kAXValueAttribute"
_KAX_MANUAL_AX = "AXManualAccessibility"
_KAX_ROLE = "kAXRoleAttribute"

AX_NO_VALUE = -25212          # kAXErrorNoValue：App 没有聚焦的输入控件
AX_CANNOT_COMPLETE = -25204   # kAXErrorCannotComplete

AX_TIMEOUT_S = 1.0

# 已知 AX 直写注定失败、直接走合成按键的 App（省一轮 1 秒 messaging timeout）。
# 依据：本机实测（终端直接拒、VS Code 假成功、微信拿不到焦点）+ 18 号调研里 TypeWhisper
# 的终端/Electron 名单、FluidVoice 把 AX 排在按键注入之后的取舍。
AX_SKIP_IDS = {
    "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable", "com.github.wez.wezterm",
    "com.microsoft.VSCode", "com.microsoft.VSCodeInsiders", "com.visualstudio.code.oss",
    "com.tencent.xinWeChat", "com.tinyspeck.slackmacgap", "com.hnc.Discord", "com.electron.wispr-flow",
    "com.google.Chrome", "com.microsoft.edgemac", "com.brave.Browser", "org.chromium.Chromium",
}
AX_SKIP_PREFIXES = ("com.electron.", "com.jetbrains.", "com.google.Chrome.app.")
_hid = None


def _hitoolbox():
    global _hid
    if _hid is None:
        for path in (
            "/System/Library/Frameworks/Carbon.framework/Frameworks/HIToolbox.framework/HIToolbox",
            ctypes.util.find_library("HIToolbox"),
        ):
            if not path:
                continue
            try:
                lib = ctypes.cdll.LoadLibrary(path)
                lib.IsSecureEventInputEnabled.restype = ctypes.c_bool
                _hid = lib
                break
            except OSError:
                continue
        else:
            _hid = False
    return _hid or None


def secure_input_active() -> bool:
    lib = _hitoolbox()
    if lib is None:
        return False
    try:
        return bool(lib.IsSecureEventInputEnabled())
    except Exception:
        return False


def ax_trusted() -> bool:
    return bool(AS.AXIsProcessTrusted())


def post_event_ok() -> bool:
    return bool(Quartz.CGPreflightPostEventAccess())


# ---------------------------------------------------------------- L1：AX 直写

def _frontmost_app():
    from AppKit import NSWorkspace
    nsapp = NSWorkspace.sharedWorkspace().frontmostApplication()
    if nsapp is None:
        return None, None, None
    pid = nsapp.processIdentifier()
    return AS.AXUIElementCreateApplication(pid), pid, nsapp.localizedName()


def _get(el, attr):
    err, val = AS.AXUIElementCopyAttributeValue(el, attr, None)
    return (val, 0) if err == 0 else (None, err)


_manual_ax_done: set = set()


def _enable_manual_ax(app, pid) -> None:
    """Electron/Chromium 默认不暴露 AX 文本树，要先开这个开关（每个进程一次）。"""
    if pid in _manual_ax_done:
        return
    _manual_ax_done.add(pid)
    try:
        AS.AXUIElementSetAttributeValue(app, _KAX_MANUAL_AX, True)
    except Exception:
        pass


def ax_focused_value():
    """取当前焦点控件的文本值（能取到才返回），用于给「合成按键」做回读校验。"""
    app, pid, name = _frontmost_app()
    if app is None:
        return None, None
    try:
        AS.AXUIElementSetMessagingTimeout(app, AX_TIMEOUT_S)
    except Exception:
        pass
    elem, _ = _get(app, AS.kAXFocusedUIElementAttribute)
    if elem is None:
        return None, name
    val, _ = _get(elem, _KAX_VALUE)
    return (val if isinstance(val, str) else None), name


def _ax_known_hopeless(pid) -> str | None:
    """这个 App 是不是「已知 AX 直写没用」；是就返回它的 bundle id。"""
    try:
        from AppKit import NSRunningApplication
        ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        bid = ra.bundleIdentifier() if ra else None
    except Exception:
        bid = None
    if not bid:
        return None
    if bid in AX_SKIP_IDS or bid.startswith(AX_SKIP_PREFIXES):
        return bid
    return None


def ax_insert(text: str) -> tuple[bool, str]:
    """AX 直写。只有回读校验通过才返回 True。"""
    if not ax_trusted():
        return False, "无辅助功能权限"
    app, pid, name = _frontmost_app()
    if app is not None and (bid := _ax_known_hopeless(pid)):
        return False, f"{name} 属已知不支持 AX 直写的类型（{bid}），直接走合成按键"
    if app is None:
        return False, "取不到前台 App"
    try:
        AS.AXUIElementSetMessagingTimeout(app, AX_TIMEOUT_S)
    except Exception:
        pass
    _enable_manual_ax(app, pid)
    elem, err = _get(app, AS.kAXFocusedUIElementAttribute)
    if elem is None:
        if err == AX_NO_VALUE:
            return False, f"「{name}」没有聚焦的输入框"
        return False, f"取不到焦点控件（{name}，err={err}）"
    role, _ = _get(elem, _KAX_ROLE)
    before, _ = _get(elem, _KAX_VALUE)
    if AS.AXUIElementSetAttributeValue(elem, _KAX_SELECTED_TEXT, text) != 0:
        return False, f"写被拒（{name}，role={role}）"
    after, _ = _get(elem, _KAX_VALUE)
    if isinstance(before, str) and isinstance(after, str):
        if after == before:
            return False, f"写入无效果（{name} 丢弃了，role={role}）"
        if text not in after:
            return False, f"写入被改写（{name}，role={role}）"
        return True, f"{name} 已校验"
    return False, f"无法校验（{name} 不暴露文本值，role={role}）"


# ---------------------------------------------------------------- L2：合成 Unicode 按键

def _u16len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _chunks(text: str, limit: int = 16):
    """切块。三点讲究（见 19 号调研，都有出处）：
    · 长度按 UTF-16 单元算，代理对（emoji/扩展汉字）不能切开；
    · 块首不能是 \n / \t / \r——enigo #260 实测这种块整块静默失败，所以遇到就并进上一块末尾；
    · 内容不丢：把各块拼起来必须等于原文。
    """
    out: list[str] = []
    text = text.lstrip("\n\t\r")
    for ch in text:
        if out and _u16len(out[-1]) + _u16len(ch) > limit:
            if ch in "\n\t\r":
                out[-1] += ch        # 空白并到上一块末尾，保证块首不是空白
                continue
            out.append(ch)
        elif out:
            out[-1] += ch
        else:
            out.append(ch)
    return out


_warmed = False
TAP = Quartz.kCGHIDEventTap       # 若某个 App 忽略合成的字，第一个该试的开关是换成 kCGSessionEventTap
# keycode 用 Space(49) 而不是 0：App 忽略 Unicode 载荷时（远程桌面、虚拟机按 scancode 转发）
# 会吐出空格而不是一串 'a'——Chromium 与 espanso 的源码注释都写了这个选择理由。
# 载荷被正常读取时 keycode 无关紧要。
KEYCODE = 49


def _warm_up(src) -> None:
    """冷启动后第一发合成事件可能被系统吞掉（社区实测），先发一个空事件热身。"""
    global _warmed
    if _warmed:
        return
    _warmed = True
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(src, KEYCODE, down)
        Quartz.CGEventPost(TAP, ev)


def type_unicode(text: str, tap: int | None = None, settle_ms: int = 25) -> tuple[bool, str]:
    """把文字当成键盘打字打进去（不碰剪贴板）。需要「辅助功能」里的合成按键权限。

    节奏（19 号调研的实测建议）：块间 8ms；最后一发之后再等 25ms —— 不等的话尾部事件可能丢
    （enigo 的 Drop 注释就是这么写的）。分块 16 个 UTF-16 单元，吞吐约 1300 字/秒，够用。
    """
    if not post_event_ok():
        return False, "无合成按键权限"
    tap = TAP if tap is None else tap
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
    _warm_up(src)
    chunks = list(_chunks(text))
    if not chunks:
        return False, "没有可打的字符"
    for i, chunk in enumerate(chunks):
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(src, KEYCODE, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(chunk.encode("utf-16-le")) // 2, chunk)
            Quartz.CGEventPost(tap, ev)
        if i < len(chunks) - 1:
            time.sleep(0.008)
    time.sleep(settle_ms / 1000.0)
    return True, f"合成按键打了 {len(chunks)} 段"


# ---------------------------------------------------------------- 入口

def frontmost_info():
    from AppKit import NSWorkspace
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return (app.processIdentifier(), app.localizedName()) if app else (None, None)


def reactivate(pid: int, wait: float = 0.18) -> bool:
    """把之前记住的目标 App 拉回前台（Wispr 也是这么干的：松手到出字之间用户可能切窗口）。

    返回是否成功把它拉回前台；拉不回来就别注入——宁可不上屏，也别把字打进错误的窗口。
    """
    from AppKit import NSRunningApplication, NSWorkspace
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return False
    app.activateWithOptions_(1)          # NSApplicationActivateIgnoringOtherApps
    time.sleep(wait)
    cur, _ = frontmost_info()
    return cur == pid


def sanitize_newlines(text: str, mode: str = "space") -> tuple[str, int]:
    """处理识别结果里的换行。

    `\n` 塞进 Unicode 串会被 macOS 当 Return——在微信/Slack 里等于**直接发送**（openless 踩过）。
    我们的转写很少带换行，所以默认换成空格（哪都不会出事）；--keep-newlines 可保留原样。
    返回 (处理后的文本, 被替换掉的换行数)。
    """
    n = text.count("\n") + text.count("\r")
    if mode == "keep" or n == 0:
        return text, 0
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    n = text.count("\n")
    return text.replace("\n", " "), n


class Injector:
    """两条都不碰剪贴板：AX 直写（校验通过才算）→ 合成按键打字 → 明说没上屏。"""

    def __init__(self, allow_typing: bool = True, verify_typing: bool = True,
                 newline_mode: str = "space"):
        self.allow_typing = allow_typing
        self.verify_typing = verify_typing
        self.newline_mode = newline_mode
        self.last_newlines = 0
        self.last_method = ""
        self.last_ok = False
        self.ax_ms = 0.0
        self.type_ms = 0.0

    def inject(self, text: str) -> str:
        text, self.last_newlines = sanitize_newlines(text, self.newline_mode)
        if secure_input_active():
            self.last_ok = False
            self.last_method = "未上屏：当前是安全输入（密码框/终端 sudo）"
            return self.last_method

        t0 = time.monotonic()
        ok, why = ax_insert(text)
        self.ax_ms = (time.monotonic() - t0) * 1000
        if ok:
            self.last_ok = True
            self.last_method = f"AX 直写（{why}，{self.ax_ms:.0f}ms）"
            return self.last_method

        if not self.allow_typing:
            self.last_ok = False
            self.last_method = f"未上屏：{why}"
            return self.last_method

        before, app_name = ax_focused_value() if self.verify_typing else (None, None)
        t0 = time.monotonic()
        ok2, why2 = type_unicode(text)
        self.type_ms = (time.monotonic() - t0) * 1000
        if not ok2:
            self.last_ok = False
            self.last_method = f"未上屏：AX {why}；按键 {why2}"
            return self.last_method

        verdict = "已发送未校验"
        if self.verify_typing:
            after, _ = ax_focused_value()
            if isinstance(before, str) and isinstance(after, str):
                if after == before:
                    verdict = "已发送但内容没变（可能被 App 丢弃）"
                    self.last_ok = False
                elif text in after:
                    verdict = "已校验（回读到文字）"
                    self.last_ok = True
                else:
                    verdict = "已发送但回读不到原文"
                    self.last_ok = False
        else:
            self.last_ok = True
        self.last_method = f"{why2}·{verdict}（{self.type_ms:.0f}ms；AX 未成：{why}）"
        return self.last_method
