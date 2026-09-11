"""字错率（CER）计算。

中英混合文本的计量单位：连续中文字符逐字算，连续拉丁字母整词算。
数字按整段算一个词。忽略大小写、空白和标点。
"""

import re

_ZH = re.compile(r"[一-鿿]")
_TOKEN = re.compile(r"[一-鿿]|[a-z0-9']+")


def tokenize(text: str):
    text = text.lower()
    return _TOKEN.findall(text)


def _levenshtein(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def cer(ref: str, hyp: str):
    """返回 (错误率, 错误数, 参考单元数)。参考为空时返回 (0,0,0)。"""
    r, h = tokenize(ref), tokenize(hyp)
    if not r:
        return (0.0, 0, 0) if not h else (1.0, len(h), 0)
    err = _levenshtein(r, h)
    return err / len(r), err, len(r)
