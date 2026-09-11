"""音频采集层：找设备、按 100ms 收块、把一次「按住说话」攒成一段，并在录音期间做伪流式预览。

预览解码跑在单独线程、交给共享的 Decoder 串行化，绝不阻塞采集；松手时只等它 0.15s
（真机上曾因为等满 5 秒，表现为「松开键后转写中很久」）。

长语音的延迟诀窍在 `Recorder._finalize_closed`：录音期间就把「录满一整段」的音频解掉，
松手只剩最后一段要解——实测 44 秒音频的松手后解码从 3176ms 降到 218ms。
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np
import sounddevice as sd

from voxkey.transcribe import (SAMPLE_RATE, Decoder, _join_parts, looks_degenerate,
                               split_for_model, tail_window)

BLOCK = 1600             # 100ms
PREVIEW_INTERVAL = 0.8   # 伪流式重解码间隔
MAX_UTTERANCE_S = 60     # 单次说话上限

# 「到底有没有人在说话」的判据。原来的 RMS 闸门（0.002）太松：用户按了键什么都没说，
# 底噪也能过闸，模型就会脑补出「嗯。」这类字（用户报的问题）。
# 现在先用 1/10 分位估这段音频自己的底噪，再数「明显高于底噪」的 20ms 帧总时长。
# 阈值是拿本机 100 多条按键录音标定出来的：真话最短的（一声「哎。」）像说话的时长 0.46s、
# 峰值 0.097；已知那两条幻听的时长是 0.28/0.32/0.44s、峰值 0.0198~0.0745。
SPEECH_FRAME_MS = 20     # 判定用的帧长
SPEECH_FLOOR_PCTL = 10   # 用 1/10 分位当底噪
SPEECH_FLOOR_X = 3.0     # 高于底噪这么多倍才算「有内容」
SPEECH_ABS_MIN = 0.005   # 底噪极低时的兜底门槛
SPEECH_MIN_SEC = 0.35    # 「像说话」的总时长下限
SPEECH_MIN_PEAK = 0.03   # 峰值下限（拦住短促的咔哒声那种）


def speech_stats(audio: np.ndarray, sr: int = SAMPLE_RATE) -> tuple[float, float]:
    """返回（像说话的总时长秒数, 峰值帧 RMS）。

    底噪是**自适应**的：不同的人、离麦远近、房间噪声都不一样，用固定绝对阈值必然要么漏
    （说话轻的时候）要么误收（环境吵的时候）。先估底噪，再数明显高于它的帧，就与绝对音量无关。
    """
    n = int(sr * SPEECH_FRAME_MS / 1000)
    if len(audio) < n:
        return 0.0, (float(np.abs(audio).max()) if len(audio) else 0.0)
    frames = audio[: len(audio) // n * n].reshape(-1, n).astype(np.float32)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    floor = float(np.percentile(rms, SPEECH_FLOOR_PCTL))
    thr = max(floor * SPEECH_FLOOR_X, SPEECH_ABS_MIN)
    return float((rms > thr).sum() * SPEECH_FRAME_MS / 1000), float(rms.max())


def has_speech(audio: np.ndarray, sr: int = SAMPLE_RATE) -> tuple[bool, float, float]:
    """这段音频里到底有没有人说话。返回（有吗, 像说话的时长, 峰值）。"""
    sec, peak = speech_stats(audio, sr)
    return (sec >= SPEECH_MIN_SEC and peak >= SPEECH_MIN_PEAK), sec, peak


def find_input_device(name_hint: str = "AU05") -> int | None:
    """按名字找录音设备；找不到返回 None（用系统默认）。"""
    if not name_hint:
        return None
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and name_hint.lower() in d["name"].lower():
            return i
    return None


class Recorder:
    """一次录音的采集 + 伪流式预览。预览解码跑在单独线程，绝不阻塞采集。

    每次说话用一个新实例（有自己的音频流和缓冲），解码交给共享的 Decoder 串行化，
    这样上一句还在解码时又能开始下一句，不会互相踩。
    """

    def __init__(self, decoder: Decoder, device: int | None, on_preview=None, is_busy=None,
                 on_level=None):
        self.decoder = decoder
        self.device = device
        self.on_preview = on_preview
        # on_level(rms)：每块音频回调一次，给悬浮条的波形用（跑在音频线程上，实现要够快，
        # 只做「存一个 float」这种量级的事，别在里面碰 UI）。
        self.on_level = on_level
        # is_busy()：上一句还在解码/上屏时返回 True——这时预览要让路，否则两边抢模型通道，
        # 表现就是「松开再按，第二次跟不上」（用户实测反馈）。
        self.is_busy = is_busy
        self.skipped_previews = 0
        self.q: queue.Queue = queue.Queue()
        self.chunks: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self._preview_busy = threading.Event()
        self.preview_wait_ms = 0.0
        self.last_preview_ms = 0.0
        self.stream_stop_ms = 0.0
        self.stream_close_ms = 0.0
        self.preview_text = ""
        # 「定稿」：录音期间就已经录满一整段、并且已经解完的音频。长语音的解码耗时是随长度
        # 线性涨的（本机实测每 22 秒一段约 1.0 秒），松手后一次性解完的话，说得越长等得越久
        # （38 秒的话实测 2.2 秒）。边录边把满段解掉，松手就只剩最后一段要解，延迟与长度无关。
        self.finalized_text = ""
        self.finalized_n = 0            # 定稿覆盖到的采样数
        self.finalized_segments = 0     # 定稿了几段（日志用）

    def _callback(self, indata, frames, t, status):
        block = indata[:, 0].copy()
        self.q.put(block)
        if self.on_level is not None:
            self.on_level(float(np.sqrt((block ** 2).mean())))

    def start(self) -> None:
        self.chunks = []
        self.preview_text = ""
        self.stream = sd.InputStream(device=self.device, samplerate=SAMPLE_RATE, channels=1,
                                     dtype="float32", blocksize=BLOCK, callback=self._callback)
        self.stream.start()

    def stop(self) -> np.ndarray:
        self.stream_stop_ms = self.stream_close_ms = 0.0
        if self.stream is not None:
            t = time.monotonic()
            self.stream.stop()
            self.stream_stop_ms = (time.monotonic() - t) * 1000
            t = time.monotonic()
            self.stream.close()
            self.stream_close_ms = (time.monotonic() - t) * 1000
            self.stream = None
        # 预览只是画面效果，别让松手等它：真机上曾因为这里 wait(5) 白等满 5 秒，
        # 表现为「松开键后转写中很久」。最终解码本来就要抢 Decoder 的锁，等它没意义。
        t0 = time.monotonic()
        self._preview_busy.wait(0.15)
        self.preview_wait_ms = (time.monotonic() - t0) * 1000
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)

    @property
    def samples(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)

    def drain(self) -> float:
        """把队列里的新块并进缓冲，返回当前总时长（秒）。"""
        while True:
            try:
                self.chunks.append(self.q.get_nowait())
            except queue.Empty:
                break
        return len(self.chunks) * BLOCK / SAMPLE_RATE

    def _finalize_closed(self) -> bool:
        """把「已经录满一整段」的音频先解掉；返回是否真的定稿了一段。

        切点用 `split_for_model`（挑能量最低处），和松手后的最终解码同一条规则，所以拼起来的
        结果与「整段一次性解码」一致。没录满一段就什么都不做。
        """
        rest = self.samples[self.finalized_n:]
        if len(rest) <= int(self.decoder.max_segment_s * SAMPLE_RATE):
            return False
        segs = split_for_model(rest, max_s=self.decoder.max_segment_s)
        if len(segs) < 2:               # 还切不出完整的一段，再等等
            return False
        seg = segs[0]
        text = self.decoder.decode(seg)
        self.finalized_text = _join_parts([self.finalized_text, text])
        self.finalized_n += len(seg)
        self.finalized_segments += 1
        return True

    def decode_rest(self, samples: np.ndarray) -> str:
        """松手后只解「还没定稿」的那部分。"""
        rest = samples[self.finalized_n:]
        if len(rest) == 0:
            return ""
        return self.decoder.decode(rest)

    def decode(self, samples: np.ndarray) -> str:
        return _join_parts([self.finalized_text, self.decode_rest(samples)])

    PREVIEW_WINDOW_S = 20.0  # 预览只看最近这段：模型上下文（512 token ≈ 28s）放不下更长的
    PREVIEW_RATE = 14.0      # 模型解码速度（倍实时，实测：4s 287ms / 8s 597ms / 20s 1464ms）

    def preview_interval(self) -> float:
        """下一轮预览至少隔多久再踢。

        解码耗时是**线性**于音频长度的（见 PREVIEW_RATE），而这里原来写死 0.8 秒踢一次、
        每次都要重解整个 20 秒尾巴（1.46 秒）——解码器几乎一直忙着、大量轮次被直接跳过，
        表现就是用户说的「越说越卡、预览的进度随语音时长线性变差」。按上一轮实测耗时来定间隔，
        踢了也白踢的那些轮次就没了（忙的时候本来也会被 _preview_busy 挡掉）。

        上限 3 秒是保险：机器负载高的时候同一段音频能慢好几倍（实测同一输入 0.54s vs 3.44s），
        没有上限的话一轮慢了会把后面的间隔也顶得很长，预览就彻底停住了。
        """
        return max(PREVIEW_INTERVAL, min(self.last_preview_ms / 1000.0 * 1.25, 3.0))

    def kick_preview(self) -> None:
        if self.is_busy and self.is_busy():
            self.skipped_previews += 1
            return
        if self._preview_busy.is_set():
            return
        # 长句只预览尾巴（模型上下文放不下全文），起点对齐停顿；已定稿的部分直接拼在前面。
        def work():
            t0 = time.monotonic()
            try:
                # 还没听到人说话就别喂模型：没有有效语音时它会对着底噪脑补出「嗯。」这类字，
                # 而预览是直接显示给用户看的（用户报的就是「按了没说话，听写中却有字」）。
                ok, sec, peak = has_speech(self.samples)
                if not ok:
                    return
                # 这一轮**永远**是「已定稿 + 尾巴」，不能因为刚定稿了一段就只显示已定稿部分——
                # 那样最近说的那一整段会从预览里消失（实测字数会从 72 掉回 64，用户看到的就是
                # 「没把我说的话全部显示出来」）。
                rest = self.samples[self.finalized_n:]
                part = self.decoder.decode(tail_window(rest, self.PREVIEW_WINDOW_S)) if len(rest) else ""
                text = _join_parts([self.finalized_text, part])
                if text and not looks_degenerate(text):  # 重复死循环的预览别刷屏
                    self.preview_text = text
                    if self.on_preview:
                        self.on_preview(text)
                    else:
                        print(f"\r\033[K  预览: {text}", end="", flush=True)
                # 满段定稿放在**显示之后**：它是为松手后的最终解码省时间（22 秒音频要 1.8 秒），
                # 跟这一轮显示什么无关，放前面只会把显示往后拖。
                self._finalize_closed()
            except Exception as e:  # 预览失败不影响主流程
                print(f"\r\033[K  [预览失败] {e}", flush=True)
            finally:
                # 整轮（含定稿）的耗时，用来定下一轮的间隔——见 preview_interval()
                self.last_preview_ms = (time.monotonic() - t0) * 1000
                if self.last_preview_ms > 4000:   # 慢得离谱就记一笔，方便对照系统负载排查
                    print(f"\r\033[K  [预览这一轮花了 {self.last_preview_ms:.0f}ms]", flush=True)
                self._preview_busy.clear()

        self._preview_busy.set()
        threading.Thread(target=work, daemon=True).start()

    def run_until(self, should_stop) -> np.ndarray:
        """采到 should_stop() 为真（或超时）为止，期间刷新预览。"""
        last_preview = 0.0
        t0 = time.monotonic()
        while not should_stop():
            self.drain()
            if time.monotonic() - t0 > MAX_UTTERANCE_S:
                print("\n（到 60 秒上限，自动停止）")
                break
            dur = len(self.chunks) * BLOCK / SAMPLE_RATE
            if dur > 0.5 and time.monotonic() - last_preview > self.preview_interval():
                last_preview = time.monotonic()
                self.kick_preview()
            time.sleep(0.02)
        self.drain()
        print()
        return self.stop()
