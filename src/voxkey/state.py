"""集中式状态容器：谁都能读，但只能通过这里写；写的时候能通知关心的人。

为什么要有它：以前状态是一个裸字典挂在 TrayApp 上，任何方法都能 `state["xx"] = ...`
直接改，出了问题不知道是谁改的；UI 想知道「状态变了没有」也只能靠 0.12 秒的 tick 去轮询。
现在读写都收口到这里：

- 读：`get(key)` / `snapshot()`（快照，拿出去随便用，不怕被改）
- 写：`update(**kw)`（加锁，一次改多个键，只有真变了才触发订阅者）
- 订阅：`subscribe(fn)` —— 状态变化时回调 `fn(snapshot, changed_keys)`，UI 据此刷新

线程约定：任意线程可调 update/get；订阅回调在**调用 update 的那个线程**里执行，
回调里不要做耗时的事（UI 的刷新走主线程的 tick 去读快照，不依赖订阅）。
"""

from __future__ import annotations

import threading
from typing import Callable


class State:
    def __init__(self, **initial):
        self._lock = threading.Lock()
        self._data = dict(initial)
        self._subs: list[Callable] = []

    def subscribe(self, fn: Callable[[dict, tuple[str, ...]], None]) -> None:
        """注册变更回调：fn(snapshot, changed_keys)。"""
        with self._lock:
            self._subs = [*self._subs, fn]

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    # 兼容旧调用方式：app 里大量 `st = get_state()` / `st["xx"]`，留着少改一轮
    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def update(self, **kw) -> tuple[str, ...]:
        """写入并返回真正变化的键。值没变就不触发订阅者（UI 不会白刷一遍）。"""
        with self._lock:
            changed = tuple(k for k, v in kw.items() if self._data.get(k) != v)
            if not changed:
                return ()
            self._data.update(kw)
            subs = list(self._subs)
        snap = dict(self._data)
        for fn in subs:
            try:
                fn(snap, changed)
            except Exception as e:          # 订阅者的错不能反过来打断写状态的一方
                from .logging import log
                log("状态", f"订阅回调出错：{type(e).__name__}: {e}")
        return changed
