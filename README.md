# VoxKey

**按住键盘上的语音键说话，松手文字直接进当前光标。** macOS 常驻（贴底边的悬浮条 + 菜单栏），不碰剪贴板。

```
按住语音键 ──► 悬浮条弹开：波形 + 「听写中」，下面一行实时预览
     松手 ──► 「上屏中」──► 文字打进前台应用的光标处 ──► 回到「空闲」
```

目前在 **Ulanzi AU05（VibeKey）** 上跑通全链路；自研语音键盘到手后只换设备层（`src/voxkey/device/`），
上面的软件部分不用动。硬件与固件在另一个仓库：[c3ng-dev/OneSay](https://github.com/c3ng-dev/OneSay)。

## 特性

- **不碰剪贴板**：优先 AX 直写焦点控件（写完回读校验），失败就合成 Unicode 按键逐字打进光标；
  两条都不成就明说「未上屏」，绝不降级到剪贴板（剪贴板方案会覆盖用户内容、也和输入法纠缠）。
- **不和输入法冲突**：合成按键走的是 Unicode 载荷，中文/emoji/中英混排都过，拼音输入法激活时也不干扰。
- **触发走设备自己的 HID 报文**：不需要全局热键、不依赖厂商 Studio 软件；代价是读设备要考虑独占（见下）。
- **长语音不吃亏**：模型上下文装不下长句就按 22 秒切段，但**录音期间就把录满的段解掉**，
  松手只剩下最后一段要解（实测 44 秒音频：松手后解码从 3176ms 降到 218ms）。
- **悬浮条动效**：固定尺寸透明窗口 + Core Animation 弹簧动画 + 裁剪层（照开源刘海应用 boring.notch /
  DynamicNotchKit / NotchDrop 的做法），动画在系统渲染服务里跑，Python 主线程不参与动画帧。
  为什么这么做、踩过哪些坑，都写在 `src/voxkey/pill.py` 的模块注释里。

## 装依赖

```bash
# 推荐 uv（系统自带的 python 是 3.9，跑不了本项目）
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[tools]'

# 或者你有 python3.12 的话：
python3.12 -m venv .venv && .venv/bin/pip install -e '.[tools]'
```

**别让 venv 的基础解释器指向仓库外**：uv 默认把 Python 装在 `~/.local/share/uv/python`，
仓库搬走/那个目录被清掉，`.venv/bin/python` 就成了死链接、整个环境废掉（真踩过：
旧环境的基础解释器在另一个目录里，那个目录一没，程序连重启都起不来）。
把基础解释器也放进仓库，就再也不会有这个问题：

```bash
export UV_PYTHON_INSTALL_DIR="$PWD/.uv-python" UV_CACHE_DIR="$PWD/.uv-cache"   # 两个目录都已 gitignore
```

本机没装 `python3.12`（系统只有 3.9），现有的 `.venv` 是用 uv 建的——uv 建的 venv 不带 pip，
要补装包先 `.venv/bin/python -m ensurepip`。新机器上装个 uv 直接 `uv venv --python 3.12 .venv` 更省事。

模型不进仓库（每个 1~3GB），下载到 `models/`（默认目录，已被 gitignore），或用环境变量
`VOXKEY_MODELS_DIR` 指到别处。常驻软件只认 `funasr-nano-int8`：

```bash
curl -L -o /tmp/funasr.tar.bz2 \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2
tar -xjf /tmp/funasr.tar.bz2 -C models/     # 解出来约 972MB，tokenizer（Qwen3-0.6B）在里面
```

`tools/verify/run_verify.py` 做模型质量对比还需要 sense-voice / zipformer / paraformer 那几个，
名字见 `src/voxkey/models.py` 的 `REGISTRY`；软件本体用不到。

## 跑起来

```bash
PYTHONPATH=src .venv/bin/python -m voxkey.app            # 常驻（默认模型 funasr-nano-int8）
PYTHONPATH=src .venv/bin/python -m voxkey.app --no-device # 不读按键，用菜单手动开始/结束
```

**权限**（系统设置 → 隐私与安全性）：

| 权限 | 用途 | 没有会怎样 |
|---|---|---|
| 辅助功能 | 合成按键上屏、AX 直写、读前台窗口与焦点控件 | 写不进输入框（会明说「未上屏」） |
| 输入监控 | 读设备（AU05）自己的 HID 按键报文 | 按语音键没反应 |
| 麦克风 | 采设备音频 | 录不到声音 |

开发期直接跑脚本时，权限记在**启动它的终端**头上；打包成 `.app` 之后才记在自己头上。

## 代码结构

```
src/voxkey/
  app.py            常驻主程序：托盘菜单、状态机（按住/松手/取消）、按键转发、注入调度
  pill.py           悬浮状态条：固定窗口 + CALayer 弹簧 + 裁剪层，两行布局（状态行 / 内容行）
  inject.py         上屏层：AX 直写 → 合成 Unicode 按键，回读校验，失败明说
  audio.py          采集层：找设备、100ms 收块、伪流式预览、录音期间定稿满段
  transcribe.py     推理层：recognizer 串行化 + 长音频分段/尾窗切分（模型上下文上限 512 token ≈ 28s）
  models.py         模型注册表与加载（默认取仓库根 models/，可用 VOXKEY_MODELS_DIR 覆盖）
  device/           设备层：HID 报文读取(keyreader)、厂商通道协议(protocol)、设备对象(device)、监视脚本(monitor)
tools/
  ptt_demo.py       命令行 demo：不装托盘也能跑通「按住说话 → 剪贴板上屏」（要装 .[tools]），验证硬件链路用
  device_probe.py   设备探针：读按键、改键位映射、诊断（改完要拔插接收器才生效）
  verify/           模型质量对比：候选模型 × 语料，输出 CER / 首字延迟 / RTF
```

设备协议（AU05 的 HID 集合、厂商通道帧格式与 TEA、命令表、键位表、待机行为、以及实测踩过的坑）
整理成了 `docs/au05-protocol.html`，浏览器直接打开看。

## 任务与 issue

任务唯一事实源是**本仓库的 Issues**（状态用 label：`b:open` / `b:doing` / `b:done` / `b:closed`）。这些条目是从硬件仓库 c3ng-dev/OneSay 迁过来的，编号有变化：

| OneSay | VoxKey | OneSay | VoxKey |
|---|---|---|---|
| #2 | #1 | #25 | #10 |
| #5 | #2 | #32 | #11 |
| #6 | #3 | #33 | #12 |
| #13 | #4 | #34 | #13 |
| #14 | #5 | #35 | #14 |
| #15 | #6 | #36 | #15 |
| #20 | #7 | #39 | #16 |
| #21 | #8 | #40 | #17 |
| #22 | #9 | | |

每条正文顶部标了来源；正文里出现的 `#N` 是 OneSay 的编号，不是本仓库的。硬件、固件、分离麦、
远程控制、定价等条目仍留在 OneSay。

## 自检

改完代码先跑冒烟（不连设备、不需要权限，几秒钟）：

```bash
PYTHONPATH=src .venv/bin/python tools/smoke.py            # 全部
PYTHONPATH=src .venv/bin/python tools/smoke.py --no-model # 没下模型时
```

覆盖：包导入、悬浮条四态几何断言（文字/波形都在裁剪层内且居中，是数值不是肉眼）、模型加载、
四个命令行入口的 `--help`。

## 已知限制

- 我们的进程一打开设备的键盘集合，**macOS 就收不到该设备的按键报文**（谁先打开谁独占）。所以设备上
  的「确认 = 回车、取消 = 退格」由主程序转发（`app.py` 的 `ROUTE_KEYS`）——实测按一次只触发一次，不重复。
- 远程桌面 / 虚拟机（RDP、VNC、Parallels、VMware）按 scancode 转发、丢掉 Unicode 载荷，会打出一串
  `a`；软件层无解。
- 密码框、`sudo` 期间系统开了 Secure Input，注入会被吞，此时直接拒绝并提示。
- 还没打包 `.app`（权限归属、开机自启待做），也没做代码签名。
