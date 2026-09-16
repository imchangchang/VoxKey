"""模型首启下载：安装包里不带模型，第一次启动时自己拉下来。

为什么模型不进安装包：解出来 972MB，打进包体就是 1GB 的下载；而且模型和程序版本无关，
每次升级都要重下一遍。所以安装包只带程序，模型单独下到用户数据目录。

为什么要固定 URL + sha256：这是从网络拉下来的二进制，校验不过就等于给任意内容开门。
sha256 拿的是 sherpa-onnx 官方 release 资产上的 digest（GitHub API 的 `digest` 字段），
换模型版本时三个常量（NAME / SHA256 / SIZE）一起改。

镜像：国内直连 GitHub 可能很慢，`VOXKEY_MODEL_MIRROR` 可以整个替换下载前缀
（默认 `https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/`），
校验用的 sha256 不变，所以镜像内容不对会被当场发现。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------- 常量

DIR_NAME = "sherpa-onnx-funasr-nano-int8-2025-12-30"
TARBALL = f"{DIR_NAME}.tar.bz2"
SHA256 = "eb43d7ccc2e86b243f6a03b7df361033dda66db9523d1a92bf6aca2b50c9476b"
SIZE = 841_730_611

DEFAULT_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"

# 判断「装好了没」的依据。用必需文件而不是 marker 文件：用户手工解压到 models/ 的老安装
# 没有 marker，用 marker 判断会让他们重下 800MB。
REQUIRED = ("encoder_adaptor.int8.onnx", "llm.int8.onnx", "embedding.int8.onnx", "Qwen3-0.6B")

Progress = Callable[[str, int, int], None]
# stage: "download" / "verify" / "extract"，后两个参数是 已完成 / 总量（0 = 未知）


class ModelDownloadError(RuntimeError):
    """下载/校验/解压失败。文案直接面向用户，会显示在悬浮条和菜单里。"""


# ---------------------------------------------------------------- 目录

def app_data_dir() -> Path:
    """用户数据目录（模型放这儿，不能放进 .app 包里——包是只读的、签名后更不许改）。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VoxKey"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "VoxKey"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "VoxKey"


def default_models_dir() -> Path:
    """模型目录：环境变量优先 > 打包后用户数据目录 > 源码仓库根下的 models/。

    打包后必须换地方：PyInstaller 把 `voxkey` 解到 .app 内部，`__file__` 往上两级也在包体内，
    往那儿写模型既占包体又会在签名校验时出问题（Gatekeeper 认签名，包内容一变就废）。
    """
    env = os.environ.get("VOXKEY_MODELS_DIR")
    if env:
        return Path(env).expanduser()
    if getattr(sys, "frozen", False):
        return app_data_dir() / "models"
    return Path(__file__).resolve().parents[2] / "models"


def model_path(models_dir: Path | None = None) -> Path:
    return (models_dir or default_models_dir()) / DIR_NAME


def is_installed(models_dir: Path | None = None) -> bool:
    """必需文件都在才算装好（下了一半、解压中断都会在这里被识破）。"""
    d = model_path(models_dir)
    return d.is_dir() and all((d / f).exists() for f in REQUIRED)


def download_url() -> str:
    base = os.environ.get("VOXKEY_MODEL_MIRROR") or DEFAULT_BASE
    return base.rstrip("/") + "/" + TARBALL


# ---------------------------------------------------------------- 下载

def _progress_throttled(cb: Progress | None) -> Progress:
    """进度回调限流：状态每 0.12 秒被 UI 读一次，800MB 下载按块回调会把线程全耗在这上面。"""
    state = {"t": 0.0}

    def emit(stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        if done < total and now - state["t"] < 0.25:
            return
        state["t"] = now
        if cb is not None:
            cb(stage, done, total)

    return emit


def download(url: str, dest: Path, sha256: str, total: int,
             on_progress: Progress | None = None) -> None:
    """下载到 dest，边下边算 sha256，最后校验。`.part` 存在则带 Range 续传。

    校验放在下载过程里而不是下完再算：800MB 的文件多读一遍盘要好几秒，没有意义。
    """
    emit = _progress_throttled(on_progress)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(dest) + ".part")
    done = part.stat().st_size if part.exists() else 0
    if done >= total:                       # 下完了但没改名（上次在这中间断了）
        done = 0
        part.unlink(missing_ok=True)

    req = urllib.request.Request(url, headers={"User-Agent": "VoxKey"})
    if done:
        req.add_header("Range", f"bytes={done}-")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.URLError as e:
        raise ModelDownloadError(f"连不上下载地址：{e}") from e

    # 服务器答 206 才是真续传；答 200 说明它不理 Range，只能从头下
    resumed = done > 0 and getattr(resp, "status", 0) == 206
    if not resumed:
        done = 0
    h = hashlib.sha256()
    if resumed:
        # 续传时前缀没法重算，只能读磁盘上已有的部分再接着算——否则校验值必然不对。
        with open(part, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
    else:
        part.unlink(missing_ok=True)

    mode = "ab" if resumed else "wb"
    emit("download", done, total)
    with open(part, mode) as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            emit("download", done, total)
    resp.close()

    got = h.hexdigest()
    if got != sha256:
        part.unlink(missing_ok=True)        # 内容不对就别留着，免得下次当续传起点
        raise ModelDownloadError(
            f"下载内容校验失败（期望 {sha256[:12]}…，实得 {got[:12]}…）。"
            f"若用了 VOXKEY_MODEL_MIRROR，检查镜像是不是这个模型")
    part.replace(dest)


def extract(tarball: Path, dest_dir: Path, on_progress: Progress | None = None) -> None:
    """解压到 dest_dir。用 `filter="data"` 挡掉绝对路径和指向外部的符号链接。"""
    emit = _progress_throttled(on_progress)
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    with tarfile.open(tarball, "r:bz2") as tf:
        for member in tf:
            tf.extract(member, dest_dir, filter="data")
            n += 1
            emit("extract", n, 0)
    emit("extract", n, 0)


def ensure(models_dir: Path | None = None, allow_download: bool = True,
           on_progress: Progress | None = None) -> Path:
    """保证模型可用，返回模型目录。已经装好就什么都不做。

    失败一律抛 ModelDownloadError，文案能直接给用户看。
    """
    d = model_path(models_dir)
    if is_installed(models_dir):
        return d

    if not allow_download:
        raise ModelDownloadError(
            f"模型没下载：{d}（已用 --no-model-download 关掉自动下载；"
            f"手动放到这里，或用 VOXKEY_MODELS_DIR 指到别处）")

    tarball = (models_dir or default_models_dir()) / TARBALL
    if tarball.exists() and tarball.stat().st_size == SIZE:
        # 之前下好了、解压那步失败的：直接复用，省一次 800MB
        pass
    else:
        download(download_url(), tarball, SHA256, SIZE, on_progress)

    if on_progress is not None:
        on_progress("extract", 0, 0)
    # 解压前先清掉半个目录：断在中间会留下不完整文件，而 REQUIRED 只检查存在性，
    # 万一凑巧凑齐了会被当成装好了。
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    extract(tarball, models_dir or default_models_dir(), on_progress)

    if not is_installed(models_dir):
        missing = [f for f in REQUIRED if not (d / f).exists()]
        raise ModelDownloadError(f"解压完还是缺文件：{missing}（目录 {d}）")

    tarball.unlink(missing_ok=True)         # 800MB 的中间产物，装好就删
    return d
