#!/usr/bin/env python3
"""AU05 设备诊断 CLI：读设备信息、监听上报帧、给按键槽写/清「哨兵键」。

用法（在 host/ 目录下）：
  .venv/bin/python tools/device_probe.py info
  .venv/bin/python tools/device_probe.py listen --secs 15      # 边听边按按键/转旋钮
  .venv/bin/python tools/device_probe.py set-key --slot 0 --combo ctrl+alt+cmd+F9
  .venv/bin/python tools/device_probe.py clear-key --slot 0
  .venv/bin/python tools/device_probe.py raw 0x0b 0x89         # 任意命令的原始响应
"""

from __future__ import annotations

import argparse
import time

from voxkey.device import protocol as P
from voxkey.device.device import VibeKey, VibeKeyNotFound

# 槽位：0=btn1 / 1=btn2 / 2=btn3 / 3=旋钮按下。
SLOT_NAMES = {0: "btn1", 1: "btn2", 2: "btn3", 3: "旋钮按下"}


def cmd_info(vk: VibeKey) -> None:
    print(f"固件版本 : {vk.version() or '（无响应）'}")
    bat = vk.battery()
    print(f"电量     : {f'{bat[0]}% / {bat[1]}mV / ' + ('充电中' if bat[2] else '电池') if bat else '（无响应）'}")
    sn = vk.serial()
    print(f"SN       : {sn or '（无响应）'}")
    hooks = vk.read_raw(0x0B, 0x89)
    audio_btn = vk.read_raw(0x06, 0x51)
    standby = vk.request(0x01, 0x2C)
    standby_s = int.from_bytes(standby[:4], "little") if standby and len(standby) >= 4 else None
    print(f"HooksMode        : {hooks}   (非 0 = 按键钩给上位机；本机固件疑似写不进去)")
    print(f"AudioButtonMode  : {audio_btn}   (同上)")
    print(f"待机超时         : {standby_s}s   (超时后厂商口不响应，转一下旋钮就回来)")
    print("按键槽位（0x50=快捷键 / 0x10=原厂固定功能，两套存储）：")
    for slot, name in SLOT_NAMES.items():
        tokens = vk.get_shortcut(slot)
        func = vk.get_fixed_function(slot)
        shown = "（读失败）" if tokens is None else (P.tokens_to_display(tokens) or "（空）")
        func_shown = "?" if func is None else (f"0x{func:02x}" if func else "无")
        print(f"  [{slot}] {name:5s} 0x50={shown:22s} 0x10={func_shown}")


def cmd_listen(vk: VibeKey, secs: float) -> None:
    print(f"被动监听 {secs:g} 秒：请按按键 / 转旋钮 / 按旋钮。")
    print("（设备只在被请求时回帧，另有约每 3 秒一次的电池广播；按键本身大概率没有任何帧——")
    print(" 这正是 OpenVibeKey 要用「哨兵键」的原因。）\n")
    t0 = time.monotonic()

    def show(plain: bytes) -> None:
        head = " ".join(f"{b:02x}" for b in plain[:16])
        print(f"  +{time.monotonic() - t0:5.2f}s  b0={plain[0]:02x}  {head}")

    vk.listen(secs, show)
    print("\n监听结束。")


def _entries_spec(tokens: list[str]) -> str:
    """把读回的 token 列表变成可写回的原始条目串（键表外条目原样带 ?）。"""
    out = []
    for t in tokens:
        if t.startswith("?"):
            out.append(t[1:])
        else:
            page, value, sign = P.KEYMAP[t]
            out.append(f"{(page & 0x7F) | (0x80 if sign else 0):02x}:{value:02x}")
    return " ".join(out)


def cmd_set_key(vk: VibeKey, slot: int, combo: str | None, entries: str | None,
                keep_func: bool = False) -> None:
    before = vk.get_shortcut(slot)
    before_func = vk.get_fixed_function(slot)
    print(f"[{slot}] {SLOT_NAMES.get(slot, '?')} 原值: 0x50="
          f"{'（读失败）' if before is None else (P.tokens_to_display(before) or '（空）')}"
          f"  0x10={('?' if before_func is None else (f'0x{before_func:02x}' if before_func else '无'))}")

    if entries:
        raw = P.parse_entries(entries)
        vk.send(P.build_shortcut_entries(slot, raw))
        time.sleep(0.12)
        expect = None  # 原始条目不比 token
    else:
        tokens = [t.strip() for t in (combo or "").split("+") if t.strip()]
        tokens = [{"ctrl": "LCtrl", "alt": "LOpt", "opt": "LOpt", "cmd": "LCmd",
                   "shift": "LShift"}.get(t.lower(), t.upper() if len(t) <= 3 and t.isalpha() else t)
                  for t in tokens]
        vk.set_shortcut(slot, tokens)
        expect = tokens

    if not keep_func:
        # 不清掉固定功能的话，原厂语义功能（语音/确认/取消）仍然会生效——「按了还发 fn」就是这么来的。
        vk.set_fixed_function(slot, 0)

    after = vk.get_shortcut(slot)
    after_func = vk.get_fixed_function(slot)
    ok = after_func == 0 and (after is not None) and (expect is None or after == expect)
    print(f"[{slot}] 写入后:    0x50={P.tokens_to_display(after) if after else after}"
          f"  0x10={f'0x{after_func:02x}' if after_func else '无'}  {'读回一致' if ok else '要复核一下'}")
    if before:
        restore = (f".venv/bin/python tools/device_probe.py set-key --slot {slot} "
                   f"--entries \"{_entries_spec(before)}\" --keep-func")
        if before_func:
            restore += (f" && .venv/bin/python tools/device_probe.py set-func "
                        f"--slot {slot} --func 0x{before_func:02x}")
        print(f"（还原：{restore}）")


def cmd_set_func(vk: VibeKey, slot: int, func: int) -> None:
    before = vk.get_fixed_function(slot)
    vk.set_fixed_function(slot, func)
    after = vk.get_fixed_function(slot)
    print(f"[{slot}] {SLOT_NAMES.get(slot, '?')} 固定功能 0x10: "
          f"{('无' if not before else f'0x{before:02x}')} → {('无' if not after else f'0x{after:02x}')}")


def cmd_clear_key(vk: VibeKey, slot: int) -> None:
    vk.clear_shortcut(slot)
    after = vk.get_shortcut(slot)
    print(f"[{slot}] {SLOT_NAMES.get(slot, '?')} 清空 → 读回 {after}")


def cmd_raw(vk: VibeKey, b1: int, b2: int) -> None:
    data = vk.request(b1, b2)
    print("data =", " ".join(f"{b:02x}" for b in data) if data else "（无响应 / 超时）")


def main() -> int:
    ap = argparse.ArgumentParser(description="AU05 设备诊断")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="读固件/电量/SN/模式/按键槽")
    p_listen = sub.add_parser("listen", help="被动监听上报帧")
    p_listen.add_argument("--secs", type=float, default=15)
    p_set = sub.add_parser("set-key", help="给按键槽写快捷键（哨兵键），并清掉原厂固定功能")
    p_set.add_argument("--slot", type=int, default=0, choices=sorted(SLOT_NAMES))
    p_set.add_argument("--combo", default=None, help="如 ctrl+alt+cmd+F9")
    p_set.add_argument("--entries", default=None,
                       help="直接写原始条目，如 \"02:01\"（还原键表外原值用）")
    p_set.add_argument("--keep-func", action="store_true",
                       help="不动 0x10 固定功能槽（默认会清 0，否则原厂语义功能还会生效）")
    p_func = sub.add_parser("set-func", help="写某槽的原厂固定功能 funcIndex")
    p_func.add_argument("--slot", type=int, default=0, choices=sorted(SLOT_NAMES))
    p_func.add_argument("--func", type=lambda s: int(s, 0), default=0)
    p_clear = sub.add_parser("clear-key", help="清空按键槽")
    p_clear.add_argument("--slot", type=int, default=0, choices=sorted(SLOT_NAMES))
    p_raw = sub.add_parser("raw", help="发任意命令看原始响应")
    p_raw.add_argument("b1", type=lambda s: int(s, 0))
    p_raw.add_argument("b2", type=lambda s: int(s, 0))
    args = ap.parse_args()

    try:
        vk = VibeKey().open()
    except VibeKeyNotFound as e:
        print(f"厂商通道打不开：{e}")
        return 1
    try:
        if args.cmd == "info":
            cmd_info(vk)
        elif args.cmd == "listen":
            cmd_listen(vk, args.secs)
        elif args.cmd == "set-key":
            cmd_set_key(vk, args.slot, args.combo, args.entries, args.keep_func)
        elif args.cmd == "set-func":
            cmd_set_func(vk, args.slot, args.func)
        elif args.cmd == "clear-key":
            cmd_clear_key(vk, args.slot)
        elif args.cmd == "raw":
            cmd_raw(vk, args.b1, args.b2)
    finally:
        vk.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
