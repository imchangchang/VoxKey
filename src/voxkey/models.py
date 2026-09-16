"""模型注册表与加载：软件本体只认 funasr-nano-int8（离线），其余模型用于质量对比（tools/verify）。

模型目录：环境变量 `VOXKEY_MODELS_DIR` 优先，打包后是用户数据目录，源码运行时是仓库根下的
`models/`。模型文件不在仓库里（每个 1~3GB），需要单独下载——软件本体用的那个由
`voxkey.modeldl` 在首启时自动拉取，见那里的说明。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import sherpa_onnx

from .modeldl import default_models_dir

MODELS = default_models_dir()


def _pick(d: Path, *subs, avoid=("fp16",)) -> str:
    """在模型目录里找 onnx 文件，优先 int8。"""
    cands = [
        p for p in d.iterdir()
        if p.suffix == ".onnx" and any(s in p.name for s in subs)
        and not any(a in p.name for a in avoid)
    ]
    if not cands:
        raise FileNotFoundError(f"{d} 下找不到 {subs}")
    int8 = [p for p in cands if "int8" in p.name]
    return str(sorted(int8 or cands)[0])


# ---------- 模型注册表 ----------

def _zipformer_bilingual():
    d = MODELS / "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(d / "tokens.txt"),
        encoder=_pick(d, "encoder"), decoder=_pick(d, "decoder"),
        joiner=_pick(d, "joiner"),
        num_threads=4, decoding_method="greedy_search",
    )


def _paraformer_bilingual():
    d = MODELS / "sherpa-onnx-streaming-paraformer-bilingual-zh-en"
    return sherpa_onnx.OnlineRecognizer.from_paraformer(
        tokens=str(d / "tokens.txt"),
        encoder=_pick(d, "encoder"), decoder=_pick(d, "decoder"),
        num_threads=4,
    )


def _zipformer_ctc_zh():
    d = MODELS / "sherpa-onnx-streaming-zipformer-ctc-zh-int8-2025-06-30"
    return sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(
        tokens=str(d / "tokens.txt"), model=_pick(d, ".onnx", avoid=("fp16",)),
        num_threads=4,
    )


def _sense_voice():
    d = MODELS / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=_pick(d, ".onnx", avoid=("fp16",)), tokens=str(d / "tokens.txt"),
        num_threads=4, use_itn=True,
    )


def _funasr_nano():
    d = MODELS / "sherpa-onnx-funasr-nano-int8-2025-12-30"
    # 线程数可调（VOXKEY_THREADS）。实测（44s 音频 / 3 段，本机 18 核）：4 线程 1048ms 一段、
    # 6 线程 984ms、8 线程 1026ms——**加线程没用**，解码不是 CPU 瓶颈，所以保持 4。
    # 真正的省时办法是边录边把满段解掉（见 voxkey/audio.py 的 _finalize_closed）。
    return sherpa_onnx.OfflineRecognizer.from_funasr_nano(
        encoder_adaptor=_pick(d, "encoder_adaptor", "encoder.adaptor"),
        llm=_pick(d, "llm"),
        embedding=_pick(d, "embedding"),
        tokenizer=str(d / "Qwen3-0.6B"),
        num_threads=int(os.environ.get("VOXKEY_THREADS", "4")),
    )


# (名字, 流式/离线, 构建函数, 模型目录)
REGISTRY = [
    ("zipformer-bilingual", "online", _zipformer_bilingual,
     "sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"),
    ("paraformer-bilingual", "online", _paraformer_bilingual,
     "sherpa-onnx-streaming-paraformer-bilingual-zh-en"),
    ("zipformer-ctc-zh", "online", _zipformer_ctc_zh,
     "sherpa-onnx-streaming-zipformer-ctc-zh-int8-2025-06-30"),
    ("sensevoice-int8", "offline", _sense_voice,
     "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"),
    ("funasr-nano-int8", "offline", _funasr_nano,
     "sherpa-onnx-funasr-nano-int8-2025-12-30"),
]


class ModelNotAvailable(RuntimeError):
    """模型名字写错 / 没下载 / 不是离线模型。

    故意不用 `sys.exit`：常驻主程序是在后台线程里加载模型的（app.py 的 `load_model`），
    `SystemExit` 不是 `Exception`，`except Exception` 抓不到，整个进程会静默死掉、菜单还停在
    「上屏中」。抛异常让调用方决定是进错误态还是打印一句退出。
    """


def load_recognizer(name: str):
    """按名字加载离线模型；名字见 REGISTRY。首选的常驻模型是 `funasr-nano-int8`。"""
    entry = next((e for e in REGISTRY if e[0] == name), None)
    if entry is None:
        names = ", ".join(n for n, k, _, _ in REGISTRY if k == "offline")
        raise ModelNotAvailable(f"未知模型 {name}；可选：{names}")
    _, kind, builder, model_dir = entry
    if kind != "offline":
        raise ModelNotAvailable(f"{name} 不是离线模型，常驻软件需要离线模型做伪流式")
    if not (MODELS / model_dir).is_dir():
        raise ModelNotAvailable(f"模型未下载：{model_dir}（目录 {MODELS}，可用 VOXKEY_MODELS_DIR 覆盖）")
    print(f"加载模型 {name} …", flush=True)
    t0 = time.perf_counter()
    rec = builder()
    print(f"模型加载 {(time.perf_counter() - t0) * 1000:.0f}ms")
    return rec
