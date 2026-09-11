#!/usr/bin/env python3
"""S1 模型验证主脚本：遍历候选模型 × 三组语料，输出对比表。

指标：
- CER：字错率（中英混合计量，见 metrics.py）
- 首字延迟：模拟实时喂音频，从句子开始到第一次出字的音频时间（ms）
- RTF：处理耗时 / 音频时长（<1 表示快于实时）
- 精修延迟：离线模型整句解码耗时（ms），即"松手到出字"

用法（仓库根目录）：
  PYTHONPATH=src .venv/bin/python tools/verify/run_verify.py                     # 全量
  PYTHONPATH=src .venv/bin/python tools/verify/run_verify.py --only zipformer-bilingual
  PYTHONPATH=src .venv/bin/python tools/verify/run_verify.py --groups zh mix
  PYTHONPATH=src .venv/bin/python tools/verify/run_verify.py --corpus <真人录音目录>

语料不在仓库里：默认取 tools/verify/corpus/（transcripts.tsv + 同名 wav，16k 单声道）。
"""

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

VERIFY_DIR = Path(__file__).parent
sys.path.insert(0, str(VERIFY_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))   # voxkey 包（src 布局）

from metrics import cer
from voxkey.models import MODELS, REGISTRY   # 模型注册表在软件本体里（src/voxkey/models.py）

CHUNK_S = 0.1    # 模拟实时喂流的块长，跟常驻软件的采集块一致（voxkey/audio.py 的 BLOCK）


# ---------- 音频与评测 ----------

def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, path
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def eval_online(rec, samples: np.ndarray):
    """模拟实时喂流。返回 (最终文本, 首字音频时间ms, RTF)。"""
    stream = rec.create_stream()
    chunk = int(CHUNK_S * 16000)
    first_text_ms = None
    t0 = time.perf_counter()
    for i in range(0, len(samples), chunk):
        stream.accept_waveform(16000, samples[i:i + chunk])
        while rec.is_ready(stream):
            rec.decode_stream(stream)
        if first_text_ms is None and rec.get_result(stream).strip():
            first_text_ms = (i + chunk) / 16.0
    stream.input_finished()
    while rec.is_ready(stream):
        rec.decode_stream(stream)
    text = rec.get_result(stream).strip()
    wall = time.perf_counter() - t0
    rtf = wall / (len(samples) / 16000)
    return text, first_text_ms, rtf


def eval_offline(rec, samples: np.ndarray):
    """返回 (文本, 解码耗时ms)。"""
    stream = rec.create_stream()
    stream.accept_waveform(16000, samples)
    t0 = time.perf_counter()
    rec.decode_stream(stream)
    wall_ms = (time.perf_counter() - t0) * 1000
    return stream.result.text.strip(), wall_ms


def load_corpus(corpus_dir: Path, groups):
    items = []
    for line in (corpus_dir / "transcripts.tsv").read_text().splitlines():
        name, group, ref = line.split("\t")
        if group in groups and (corpus_dir / f"{name}.wav").exists():
            items.append((name, group, ref))
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="只跑指定模型（注册表里的名字）")
    ap.add_argument("--groups", nargs="*", default=["zh", "en", "mix"])
    ap.add_argument("--corpus", default=str(VERIFY_DIR / "corpus"))
    ap.add_argument("--out", default="", help="结果 markdown 输出路径")
    args = ap.parse_args()

    corpus = load_corpus(Path(args.corpus), set(args.groups))
    if not corpus:
        sys.exit(f"语料为空，检查 {args.corpus}（先用 gen_corpus.sh 生成）")

    lines = []
    for name, kind, build, dirname in REGISTRY:
        if args.only and name not in args.only:
            continue
        if not (MODELS / dirname).is_dir():
            print(f"[跳过] {name}：模型目录不存在")
            continue
        print(f"\n===== {name} ({kind}) =====")
        rec = build()

        per_group = {g: [0, 0] for g in args.groups}  # group -> [err, units]
        first_ms_all, latency_all, rtf_all = [], [], []
        for fname, group, ref in corpus:
            samples = load_wav(Path(args.corpus) / f"{fname}.wav")
            if kind == "online":
                hyp, first_ms, rtf = eval_online(rec, samples)
                if first_ms:
                    first_ms_all.append(first_ms)
                rtf_all.append(rtf)
            else:
                hyp, wall_ms = eval_offline(rec, samples)
                latency_all.append(wall_ms)
            rate, err, units = cer(ref, hyp)
            per_group[group][0] += err
            per_group[group][1] += units
            print(f"  [{group}] {fname}  CER={rate:.1%}  ref={ref}")
            print(f"           hyp={hyp}")

        row = f"| {name} |"
        for g in ["zh", "en", "mix"]:
            if g in per_group and per_group[g][1]:
                row += f" {per_group[g][0]/per_group[g][1]:.1%} |"
            else:
                row += " - |"
        if kind == "online":
            row += f" {np.mean(first_ms_all):.0f} | {np.mean(rtf_all):.2f} | - |"
        else:
            row += f" - | - | {np.mean(latency_all):.0f} |"
        lines.append(row)

    header = ("| 模型 | 中文CER | 英文CER | 混读CER | 首字延迟ms | RTF | 精修延迟ms |\n"
              "|---|---|---|---|---|---|---|")
    print("\n===== 汇总 =====")
    print(header)
    print("\n".join(lines))
    if args.out:
        Path(args.out).write_text(
            f"# S1 模型验证结果\n\n语料：{args.corpus}\n\n{header}\n" + "\n".join(lines) + "\n")
        print(f"\n已写入 {args.out}")


if __name__ == "__main__":
    main()
