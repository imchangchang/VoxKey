"""Ulanzi AU05（VibeKey）厂商 HID 通道的帧格式、TEA 加解密与命令表。

协议不是我们逆的，是移植 [OpenVibeKey](https://github.com/palaemonboy/OpenVibeKey)（MIT）
的 `VibeKitCore`（Frame.swift / Crypto.swift / Keymap.swift）。只保留 demo 用得到的命令。

链路事实（本机 ioreg 实测）：
  AU05 在 USB 上是一个 HID 复合设备，接口 3 上有 usage page 0xFFFC 的厂商集合，
  report id 0x55，输入/输出各 63 字节。官方 Ulanzi Studio 走的就是这条通道。

帧格式：
  明文 = [b0 b1 b2 b3][payload…] 补 0 到 64 字节 → 整帧按 8 字节一块做 TEA(32 轮) ECB
  → 只发前 63 字节（丢掉最后一个 block 的末字节），HID 层再前置 report id 0x55。
  读命令：b0=0x01, b3=0x01；写命令 b3=0x04。设备响应 b0 最高位为 1，b1/b2 回显请求。

TEA 密钥是全局固定的（不分设备），等于没有鉴权。
"""

from __future__ import annotations

VENDOR_ID = 0xFFF1
PRODUCT_ID = 0x00DD
USAGE_PAGE = 0xFFFC
REPORT_ID = 0x55
REPORT_SIZE = 63

_TEA_KEY = (0xCAA5BACA, 0xBC2A8A6D, 0xCA5A9EBA, 0x9BB88BCA)
_DELTA = 0x9E3779B9
_MASK = 0xFFFFFFFF


def _encrypt_block(v0: int, v1: int) -> tuple[int, int]:
    s = 0
    for _ in range(32):
        s = (s + _DELTA) & _MASK
        v0 = (v0 + ((((v1 << 4) & _MASK) + _TEA_KEY[0]) & _MASK ^ (v1 + s) & _MASK
                    ^ ((v1 >> 5) + _TEA_KEY[1]) & _MASK)) & _MASK
        v1 = (v1 + ((((v0 << 4) & _MASK) + _TEA_KEY[2]) & _MASK ^ (v0 + s) & _MASK
                    ^ ((v0 >> 5) + _TEA_KEY[3]) & _MASK)) & _MASK
    return v0, v1


def _decrypt_block(v0: int, v1: int) -> tuple[int, int]:
    s = (_DELTA * 32) & _MASK
    for _ in range(32):
        v1 = (v1 - ((((v0 << 4) & _MASK) + _TEA_KEY[2]) & _MASK ^ (v0 + s) & _MASK
                    ^ ((v0 >> 5) + _TEA_KEY[3]) & _MASK)) & _MASK
        v0 = (v0 - ((((v1 << 4) & _MASK) + _TEA_KEY[0]) & _MASK ^ (v1 + s) & _MASK
                    ^ ((v1 >> 5) + _TEA_KEY[1]) & _MASK)) & _MASK
        s = (s - _DELTA) & _MASK
    return v0, v1


def _tea(data: bytes, decrypt: bool) -> bytes:
    out = bytearray(data)
    for off in range(0, len(out) - 7, 8):
        v0 = int.from_bytes(out[off:off + 4], "little")
        v1 = int.from_bytes(out[off + 4:off + 8], "little")
        v0, v1 = (_decrypt_block(v0, v1) if decrypt else _encrypt_block(v0, v1))
        out[off:off + 4] = v0.to_bytes(4, "little")
        out[off + 4:off + 8] = v1.to_bytes(4, "little")
    return bytes(out)


def build_frame(b0: int, b1: int, b2: int, b3: int, payload: bytes = b"") -> bytes:
    """构造待发送的 63 字节密文（不含 report id）。"""
    buf = bytearray(64)
    buf[0:4] = bytes((b0, b1, b2, b3))
    buf[4:4 + len(payload)] = payload[:60]
    return _tea(bytes(buf), decrypt=False)[:REPORT_SIZE]


def read_frame(b0: int, b1: int, b2: int, payload: bytes = b"") -> bytes:
    """构造读（GET）命令帧：b3 固定 0x01。"""
    return build_frame(b0, b1, b2, 0x01, payload)


def write_frame(b0: int, b1: int, b2: int, payload: bytes = b"") -> bytes:
    """构造写（SET）命令帧：b3 固定 0x04（个别命令例外，见命令表）。"""
    return build_frame(b0, b1, b2, 0x04, payload)


def decode_frame(report: bytes) -> bytes:
    """解密设备返回的一帧（可含前置 report id，自动剥掉）。"""
    body = report[1:] if report and report[0] == REPORT_ID else report
    return _tea(bytes(body), decrypt=True)


# ---------------------------------------------------------------- 命令表

# name -> (b0, b1, b2, b3=读方向, 说明)。写方向把最后一个字节换成 0x04。
COMMANDS = {
    "getVersion":        (0x01, 0x04, 0x04, 0x01, "固件版本，响应 data[6..8] = 主.次.修订"),
    "getBattery":        (0x01, 0x01, 0x02, 0x01, "电量：电压 u16le@0(mV) / 百分比@2 / 充电@6"),
    "getSN":             (0x01, 0x01, 0x0b, 0x01, "设备 SN，分片返回，需 collect 拼接"),
    "getUUID":           (0x01, 0x01, 0x0c, 0x01, "设备 UUID，分片返回"),
    "getMicEnable":      (0x01, 0x01, 0x2a, 0x01, "麦克风开关"),
    "getShortcut":       (0x01, 0x06, 0x50, 0x01, "读某槽快捷键，payload=[index]"),
    "getButtonFunc":     (0x01, 0x06, 0x10, 0x01, "读某槽固定功能（媒体键），payload=[0x00, index]"),
    "getHooksMode":      (0x01, 0x0b, 0x89, 0x01, "按键钩子模式（本机固件写不进去，见 README）"),
    "getAudioBtnMode":   (0x01, 0x06, 0x51, 0x01, "语音键系统模式（同上，写不进去）"),
    "getStandbyTime":    (0x01, 0x01, 0x2c, 0x01, "待机秒数 u32le；超时后厂商口不响应"),
    "getIndicatorRaw":   (0x01, 0x0b, 0x88, 0x01, "指示灯参数原始字节"),
    "heartbeat":         (0x06, 0x01, 0x23, 0x00, "心跳帧（b3=0x00，试着唤醒假死的厂商口）"),
}

# 单条快捷键上限 4 个条目（官方 App 同款约束：至多 3 修饰 + 1 主键）。
MAX_SHORTCUT_ENTRIES = 4

# token -> (page, value, sign)。page 3 = 修饰键（sign 1 左 / 0 右），page 2 = 普通键。
# 只收 demo 会用到的：修饰键 + F 键 + 数字 + 字母。设备键表到 F12 为止（没有 F13+）。
KEYMAP: dict[str, tuple[int, int, int]] = {
    "LCtrl": (3, 0x01, 1), "LShift": (3, 0x02, 1), "LOpt": (3, 0x04, 1), "LCmd": (3, 0x08, 1),
    "RCtrl": (3, 0x10, 0), "RShift": (3, 0x20, 0), "ROpt": (3, 0x40, 0), "RCmd": (3, 0x80, 0),
}
for _i, _name in enumerate(["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M",
                            "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z"]):
    KEYMAP[_name] = (2, 0x04 + _i, 0)
for _i, _name in enumerate(["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]):
    KEYMAP[_name] = (2, 0x1E + _i, 0)
for _i, _name in enumerate(["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8",
                            "F9", "F10", "F11", "F12"]):
    KEYMAP[_name] = (2, 0x3A + _i, 0)
KEYMAP.update({"Enter": (2, 0x28, 0), "Esc": (2, 0x29, 0), "Tab": (2, 0x2B, 0),
               "Space": (2, 0x2C, 0), "Up": (2, 0x52, 0), "Down": (2, 0x51, 0),
               "Left": (2, 0x50, 0), "Right": (2, 0x4F, 0)})

# 反向表：(page, value, sign) -> token，读回配置时用。
_REVERSE_KEYMAP = {v: k for k, v in KEYMAP.items()}

# 展示用短名（跟 OpenVibeKey 的界面标签一致，macOS 习惯）。
KEY_LABELS = {"LCtrl": "⌃", "LShift": "⇧", "LOpt": "⌥", "LCmd": "⌘",
              "RCtrl": "右⌃", "RShift": "右⇧", "ROpt": "右⌥", "RCmd": "右⌘",
              "Space": "空格", "Enter": "⏎", "Tab": "⇥", "Up": "↑", "Down": "↓",
              "Left": "←", "Right": "→"}


def tokens_to_display(tokens: list[str]) -> str:
    return "+".join(KEY_LABELS.get(t, t) for t in tokens)


def build_shortcut(slot: int, tokens: list[str]) -> bytes:
    """0x50 写快捷键：payload = [slot, 0x01, num, (pageSign, value)×num]。"""
    if len(tokens) > MAX_SHORTCUT_ENTRIES:
        raise ValueError(f"单条快捷键最多 {MAX_SHORTCUT_ENTRIES} 个条目，给了 {len(tokens)}")
    entries = []
    for t in tokens:
        if t not in KEYMAP:
            raise ValueError(f"设备键表里没有 {t}")
        page, value, sign = KEYMAP[t]
        entries.append(((page & 0x7F) | (0x80 if sign else 0), value))
    return build_shortcut_entries(slot, entries)


def build_shortcut_entries(slot: int, entries: list[tuple[int, int]]) -> bytes:
    """直接按帧内字节写（(pageSign, value) 对），用于还原键表外的原值（如 02:01）。"""
    if len(entries) > MAX_SHORTCUT_ENTRIES:
        raise ValueError(f"单条快捷键最多 {MAX_SHORTCUT_ENTRIES} 个条目，给了 {len(entries)}")
    payload = bytes([slot, 0x01, len(entries)])
    for page_sign, value in entries:
        payload += bytes([page_sign, value])
    return write_frame(0x01, 0x06, 0x50, payload)


def parse_entries(spec: str) -> list[tuple[int, int]]:
    """解析 "02:01,03:08" 这种原始条目串。"""
    out = []
    for part in spec.replace(",", " ").split():
        page_sign, _, value = part.partition(":")
        out.append((int(page_sign, 16), int(value, 16)))
    return out


def clear_shortcut(slot: int) -> bytes:
    """清空某槽的快捷键（num=0）。"""
    return write_frame(0x01, 0x06, 0x50, bytes([slot, 0x01, 0x00]))


def build_fixed_function(slot: int, func_index: int) -> bytes:
    """0x10 写「固定功能」（原厂语义功能，如 语音/确认/取消）：payload = [0x00, slot, funcIndex]。"""
    return write_frame(0x01, 0x06, 0x10, bytes([0x00, slot, func_index]))


def parse_shortcut(data: bytes) -> list[str] | None:
    """解析 0x50 读响应：data = [slot, ?, num, (pageSign, value)×num]。

    键表外的条目（如 btn1 上的 fn/🌐，设备侧是个专有位）不猜，原样写成 "?page:value"。
    """
    if len(data) < 3:
        return None
    num = data[2]
    if num == 0:
        return []
    tokens = []
    for i in range(num):
        off = 3 + 2 * i
        if off + 1 >= len(data):
            return None
        page_sign, value = data[off], data[off + 1]
        token = _REVERSE_KEYMAP.get((page_sign & 0x7F, value, 1 if page_sign & 0x80 else 0))
        tokens.append(token or f"?{page_sign:02x}:{value:02x}")
    return tokens


def parse_version(data: bytes) -> str | None:
    if len(data) < 9:
        return None
    return f"{data[6]}.{data[7]}.{data[8]}"


def parse_battery(data: bytes) -> tuple[int, int, bool] | None:
    """(百分比, 电压 mV, 是否充电)。"""
    if len(data) < 3:
        return None
    voltage = data[0] | (data[1] << 8)
    percent = max(0, min(100, data[2]))
    charging = len(data) > 6 and data[6] != 0
    return percent, voltage, charging


def assemble_sn(chunks: list[bytes]) -> str:
    """SN/UUID 分片拼接：每片 data = [len, seg, ascii…]。"""
    segs: dict[int, str] = {}
    for d in chunks:
        if len(d) < 2:
            continue
        length, seg = d[0], d[1]
        raw = d[2:2 + length]
        if raw and all(0x20 <= b < 0x7F for b in raw):
            segs.setdefault(seg, raw.decode("ascii"))
    return "".join(segs[k] for k in sorted(segs))
