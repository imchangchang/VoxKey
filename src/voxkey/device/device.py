"""AU05 厂商通道（0xFFFC / report 0x55）的 hidapi 传输层。

对应 OpenVibeKey 的 VibeKitHID：打开 → 发 63 字节密文 → 轮询等响应（b0 高位=1 且 b1/b2 回显）。
macOS 上 hidapi 用 IOKit HID，不需要额外驱动；但**不能和 Ulanzi Studio 抢会话**
（实测：Studio 开着时本进程仍能打开并读写，但两边同时写配置会互相覆盖）。
"""

from __future__ import annotations

import time

import hid

from . import protocol as P


class VibeKeyNotFound(RuntimeError):
    pass


class VibeKey:
    """按 usage page 0xFFFC 打开 AU05 的厂商集合。"""

    def __init__(self, path: bytes | None = None):
        self.path = path
        self._dev: hid.device | None = None

    # ---------------------------------------------------------- 打开/关闭

    @staticmethod
    def find() -> bytes:
        """返回厂商集合的 hidapi path；没插设备就抛 VibeKeyNotFound。"""
        for d in hid.enumerate(P.VENDOR_ID, P.PRODUCT_ID):
            if d["usage_page"] == P.USAGE_PAGE:
                return d["path"]
        raise VibeKeyNotFound(
            "没找到 AU05 的 0xFFFC 厂商接口。检查：接收器插好了吗？Ulanzi Studio 是不是独占着？")

    def open(self) -> "VibeKey":
        self._dev = hid.device()
        self._dev.open_path(self.path or self.find())
        self._dev.set_nonblocking(True)
        return self

    def close(self) -> None:
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    def __enter__(self) -> "VibeKey":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------------------------------------------------- 收发

    def send(self, frame: bytes) -> None:
        """发一帧（自动前置 report id）。"""
        assert self._dev is not None, "先 open()"
        self._dev.write(bytes([P.REPORT_ID]) + frame)

    def drain(self) -> list[bytes]:
        """把已到达的输入报文全部取走（返回解密后的明文，含 4 字节头）。"""
        assert self._dev is not None, "先 open()"
        out = []
        while True:
            raw = self._dev.read(64)
            if not raw:
                break
            out.append(P.decode_frame(bytes(raw)))
        return out

    def request(self, b1: int, b2: int, b0: int = 0x01, payload: bytes = b"",
                timeout: float = 0.8) -> bytes | None:
        """发一条 GET 并等匹配响应；返回剥掉 4 字节头的 data，超时返回 None。"""
        self.drain()
        self.send(P.read_frame(b0, b1, b2, payload))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for plain in self.drain():
                if len(plain) >= 3 and (plain[0] & 0x80) and plain[1] == b1 and plain[2] == b2:
                    return plain[4:]
            time.sleep(0.01)
        return None

    def collect(self, b1: int, b2: int, b0: int = 0x01, payload: bytes = b"",
                window: float = 0.6) -> list[bytes]:
        """多帧收集（SN/UUID 这类分片返回的命令）。"""
        self.drain()
        self.send(P.read_frame(b0, b1, b2, payload))
        time.sleep(window)
        return [p[4:] for p in self.drain()
                if len(p) >= 3 and (p[0] & 0x80) and p[1] == b1 and p[2] == b2]

    def listen(self, window: float, on_frame) -> None:
        """被动监听 window 秒：设备主动上报的帧原样回调（不解密后的语义）。"""
        self.drain()
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            for plain in self.drain():
                on_frame(plain)
            time.sleep(0.02)

    # ---------------------------------------------------------- 常用读取

    def version(self) -> str | None:
        return P.parse_version(self.request(0x04, 0x04) or b"")

    def battery(self) -> tuple[int, int, bool] | None:
        return P.parse_battery(self.request(0x01, 0x02) or b"")

    def serial(self) -> str:
        return P.assemble_sn(self.collect(0x01, 0x0B))

    def get_shortcut(self, slot: int) -> list[str] | None:
        return P.parse_shortcut(self.request(0x06, 0x50, payload=bytes([slot])) or b"")

    def set_shortcut(self, slot: int, tokens: list[str]) -> None:
        self.send(P.build_shortcut(slot, tokens))
        time.sleep(0.12)

    def clear_shortcut(self, slot: int) -> None:
        self.send(P.clear_shortcut(slot))
        time.sleep(0.12)

    def get_fixed_function(self, slot: int) -> int | None:
        """读某槽的固定功能 funcIndex（0 = 无）。与 0x50 快捷键是两套独立存储。"""
        data = self.request(0x06, 0x10, payload=bytes([0x00, slot]))
        return data[2] if data and len(data) >= 3 else None

    def set_fixed_function(self, slot: int, func_index: int) -> None:
        self.send(P.build_fixed_function(slot, func_index))
        time.sleep(0.12)

    def bind_shortcut(self, slot: int, tokens: list[str]) -> None:
        """写快捷键的推荐入口：先清掉该槽的固定功能（否则原厂语义功能仍然生效），
        中间留 120ms——不留固件会丢帧（上游真机实测）。"""
        self.set_fixed_function(slot, 0)
        time.sleep(0.12)
        self.set_shortcut(slot, tokens)

    def read_raw(self, b1: int, b2: int, b0: int = 0x01, payload: bytes = b"") -> int | None:
        """读一个单字节配置项（HooksMode / 媒体键这类）。"""
        data = self.request(b1, b2, b0=b0, payload=payload)
        return data[0] if data else None
