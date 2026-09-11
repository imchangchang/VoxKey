#!/usr/bin/env bash
# 用 macOS `say` 生成 S1 验证语料。语料清单以 tools/verify/corpus.py 为准，改那边就行。
#
# 产出 tools/verify/corpus/：
#   <name>.wav          16kHz 单声道 PCM（run_verify.py 只认这个格式，不符会 assert）
#   transcripts.tsv     每行「名字 \t 组 \t 参考文本」
#
# 这是 TTS 基线，只用来把流水线跑通——TTS 没有口音/口语/停顿，识别结果偏乐观，
# 正式验收（issue #1 的三组达标线）必须换成真人录音：把录音按同样的命名和 tsv 放进
# 任意目录，再 `run_verify.py --corpus <那个目录>`。
#
# 用法：tools/verify/gen_corpus.sh [输出目录]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/corpus}"
PY="${PYTHON:-$HERE/../../.venv/bin/python}"
[ -x "$PY" ] || PY=python3

mkdir -p "$OUT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

"$PY" - "$HERE/corpus.py" > "$TMP/list.tsv" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("corpus", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for name, text, group, voice in mod.CORPUS:
    print(f"{name}\t{group}\t{voice}\t{text}")
PY

: > "$OUT/transcripts.tsv"
while IFS=$'\t' read -r name group voice text; do
    [ -n "$name" ] || continue
    say -v "$voice" -o "$TMP/$name.aiff" "$text"
    afconvert -f WAVE -d LEI16@16000 -c 1 "$TMP/$name.aiff" "$OUT/$name.wav"
    printf '%s\t%s\t%s\n' "$name" "$group" "$text" >> "$OUT/transcripts.tsv"
    printf '  %s\n' "$name"
done < "$TMP/list.tsv"

printf '生成 %s 条 → %s\n' "$(wc -l < "$OUT/transcripts.tsv" | tr -d ' ')" "$OUT"
