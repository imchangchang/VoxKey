"""说话流程：把「按住说话」的整段业务收成一个类，与 UI 彻底分开。

一次说话的生命周期（都在 utterance 线程里跑，不碰任何 UI）：

    start() ──→ 采集（100ms 块，边录边判「有没有人在说话」）
    stop()  ──→ 停流 → 依次过四道闸 → 解码 → 注入
    返回 UtteranceResult：文字、上屏方式、被哪道闸拦下、各环节耗时

四道闸（按顺序，命中即丢弃，不再往后走）：
    cancelled   用户按了取消键
    too_short   时长 < min_audio_s（防按一下的咔哒声）
    no_speech   没检测到有效语音（防底噪被模型脑补出「嗯。」）
    empty_text  模型解出来是空的（说了但没听清）

为什么要独立成类：以前这段流程长在 TrayApp 里，和 UI 刷新、状态字典搅在一起，
想测「松手之后到底发生了什么」只能起整个 App。现在给它一个假注入器和真解码器
就能单测每一道闸（见 tools/smoke.py 的 pipeline_cases）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np
import sounddevice as sd

from .audio import (Recorder, find_input_device, has_speech, reload_audio_devices,
                    SAMPLE_RATE)
from .logging import log


@dataclass
class UtteranceResult:
    """一次说话的结局。text 为空 = 没上屏；reason 说明为什么。"""
    text: str = ""
    injected: str = ""          # 上屏方式的描述（来自 Injector.inject），或拒绝原因
    gate: str = ""              # 被哪道闸拦下（""=没被拦）：cancelled/too_short/no_speech/empty
    audio_s: float = 0.0
    speech_s: float = 0.0       # 有效语音时长（说话检测的度量）
    peak: float = 0.0
    decode_ms: float = 0.0
    inject_ms: float = 0.0
    audio_device: int | None = None


@dataclass
class PipelineConfig:
    min_audio_s: float = 0.5            # 短于这个时长直接丢
    device_hint: str = "AU05"
    newline_mode: str = "space"


class SpeakingPipeline:
    """一次「按住说话」的完整业务。与 UI 解耦：进度靠回调，结果靠返回值。"""

    def __init__(self, decoder, config: PipelineConfig, injector,
                 on_level=None, device_hint: str = "AU05",
                 archive_dir: str | None = None, on_archive=None):
        """
        decoder      共享的 Decoder（内部有锁，串行）
        injector     voxkey.inject.Injector
        on_level     每个音频块的实时 RMS 回调（喂悬浮条波形），在音频线程里调
        archive_dir  非 None 时把每句话存成 wav（调 ASR 用），on_archive(wav路径, 摘要dict)
        """
        self.decoder = decoder
        self.cfg = config
        self.injector = injector
        self.on_level = on_level
        self.device_hint = device_hint
        self.archive_dir = archive_dir
        self.on_archive = on_archive
        self.audio_device: int | None = None
        self.cancelled = threading.Event()

    # ---------------------------------------------------------------- 生命周期

    def start_recording(self) -> tuple[Recorder, Exception | None]:
        """起一条录音流。先按缓存编号开，打不开就重枚举音频设备再试一次。

        返回 (Recorder, None)；打不开返回 (Recorder, 异常)——调用方决定怎么提示。
        半开的那条流会在这里显式关掉：sounddevice 的 Stream 没有 __del__，
        GC 不会替你关，一直占着输入设备会让第二次 start 更容易失败。
        """
        cap = Recorder(self.decoder, self.audio_device, on_level=self.on_level)
        err = self._open(cap)
        if err is None:
            return cap, None
        # 打不开基本上是插拔过接收器：PortAudio 的设备表还是旧的，重新枚举再试一次
        self.audio_device = find_input_device(self.device_hint)
        log("音频", f"打不开（{err}），重新枚举音频设备后重试：AU05 → "
                    f"#{self.audio_device} " + (sd.query_devices(self.audio_device)["name"]
                                                if self.audio_device is not None else "（没找到，用系统默认）"))
        try:
            cap.stop()
        except Exception:
            pass
        cap2 = Recorder(self.decoder, self.audio_device, on_level=self.on_level)
        err2 = self._open(cap2)
        return cap2, err2

    def _open(self, cap: Recorder):
        try:
            cap.start()
            return None
        except Exception as e:
            return e

    def stop_recording(self, cap: Recorder) -> np.ndarray:
        return cap.stop()

    # ---------------------------------------------------------------- 四道闸与解码

    def gates(self, samples: np.ndarray, cancelled: bool, min_audio_s: float) -> str:
        """按顺序过闸，返回被拦下的闸名（"" = 都过了）。"""
        if cancelled:
            return "cancelled"
        dur = len(samples) / SAMPLE_RATE
        if dur < min_audio_s:
            return "too_short"
        spoken, _, _ = has_speech(samples)
        if not spoken:
            return "no_speech"
        return ""

    def transcribe(self, samples: np.ndarray) -> tuple[str, float]:
        t0 = time.perf_counter()
        text = self.decoder.decode(samples)
        return text, (time.perf_counter() - t0) * 1000

    def inject(self, text: str) -> tuple[str, float]:
        t0 = time.perf_counter()
        how = self.injector.inject(text)
        return how, (time.perf_counter() - t0) * 1000

    # ---------------------------------------------------------------- 音频存档

    def archive(self, samples: np.ndarray, hold_ms: float) -> tuple[str, dict] | None:
        """把这次按键的音频存成 wav + 追加一行索引。返回 (wav路径, 摘要)；未开存档返回 None。"""
        if not self.archive_dir:
            return None
        import wave
        from datetime import datetime
        from pathlib import Path
        n = len(samples)
        dur = n / SAMPLE_RATE
        rms = float(np.sqrt((samples ** 2).mean())) if n else 0.0
        peak = float(np.abs(samples).max()) if n else 0.0
        d = Path(self.archive_dir)
        d.mkdir(parents=True, exist_ok=True)
        name = f"utt_{datetime.now().strftime('%H%M%S')}_{dur:.1f}s.wav"
        wav = d / name
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
        summary = {"wav": name, "hold_ms": round(hold_ms), "audio_s": round(dur, 2),
                   "rms": round(rms, 4), "peak": round(peak, 4)}
        with open(d / "index.jsonl", "a") as f:
            f.write(json_dumps(summary) + "\n")
        return str(wav), summary


def json_dumps(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
