"""模型推理层：把 sherpa-onnx 的离线 recognizer 包装成「能解长音频」的接口。

两个约束（都是真机踩出来的，别动）：
1. recognizer 不能并发用——上一句还在解码时下一句又进同一个 recognizer，python 直接
   `malloc: *** error for object: pointer being freed was not allocated` abort。所以加锁串行化。
2. funasr-nano 的 KV 上限 512 token，**音频约 28 秒就顶满**，超了会被截断甚至解出空。
   所以长音频按 22 秒切段解码再拼，切点挑能量最低处（尽量落在停顿上），别在字中间断开。
"""

from __future__ import annotations

import threading

import numpy as np

SAMPLE_RATE = 16000      # 模型采样率（音频层按这个率采集，见 voxkey.audio）
MAX_SEGMENT_S_DEFAULT = 22.0


class Decoder:
    """串行化对 sherpa-onnx recognizer 的访问，并按模型上下文上限切段。

    两件事：
    1. recognizer 不能并发用。真机踩过——上一句还在解码时下一句又进同一个 recognizer，
       python 直接 `malloc: *** error for object: pointer being freed was not allocated` abort。
    2. funasr-nano 的 KV 上限 512 token，**音频约 28 秒就顶满**，超了会被截断甚至解出空
       （sherpa-onnx 会打 "Context_len ... exceeds KV capacity ... max_total_len (512)"）。
       所以长音频按 22 秒切段解码再拼，切点挑能量最低处（尽量落在停顿上），别在字中间断开。
    """

    def __init__(self, recognizer, max_segment_s: float = MAX_SEGMENT_S_DEFAULT):
        self.model = recognizer
        self.max_segment_s = max_segment_s
        self._lock = threading.Lock()
        self.last_segments = 0

    def decode(self, samples: np.ndarray) -> str:
        with self._lock:  # create_stream 也要在锁里：stream 共享 recognizer 内部状态
            segs = split_for_model(samples, max_s=self.max_segment_s)
            self.last_segments = len(segs)
            parts = []
            for seg in segs:
                s = self.model.create_stream()
                s.accept_waveform(SAMPLE_RATE, seg)
                self.model.decode_stream(s)
                text = s.result.text.strip()
                if text and text != "/sil":
                    parts.append(text)
        return _join_parts(parts)


def _lowest_energy_cut(audio: np.ndarray, lo: int, hi: int, win: int) -> int:
    """在 [lo, hi) 里找 50ms 能量最低的起点（当切点用）。"""
    best, best_e = lo, None
    for p in range(lo, max(lo + win, hi - win), win):
        e = float(np.sqrt(np.mean(audio[p:p + win] ** 2)))
        if best_e is None or e < best_e:
            best, best_e = p, e
    return best


def split_for_model(audio: np.ndarray, max_s: float = MAX_SEGMENT_S_DEFAULT,
                    search_s: float = 3.0) -> list[np.ndarray]:
    """切成 ≤max_s 的段；切点在 [max_s-search_s, max_s] 里挑 50ms 能量最低处。"""
    sr = SAMPLE_RATE
    max_n, search_n, win = int(max_s * sr), int(search_s * sr), int(0.05 * sr)
    segs, start, n = [], 0, len(audio)
    while n - start > max_n:
        cut = _lowest_energy_cut(audio, start + max_n - search_n, start + max_n, win)
        segs.append(audio[start:cut])
        start = cut
    segs.append(audio[start:])
    return segs


def _join_parts(parts: list[str]) -> str:
    """中文直接拼；两侧都是拉丁字母/数字时补一个空格（英文单词别粘在一起）。"""
    out = ""
    for p in parts:
        if out and out[-1].isascii() and out[-1].isalnum() and p[:1].isascii() and p[:1].isalnum():
            out += " "
        out += p
    return out
