# Agent Instructions（VoxKey）

本仓库 = **VoxKey 软件本体**（macOS 常驻 + 设备层）。硬件与固件在另一个仓库
[c3ng-dev/OneSay](https://github.com/c3ng-dev/OneSay)——那边的上位机代码已冻结为快照，不要再改。

## 任务管理

**唯一事实源 = 本仓库的 GitHub Issues**（`imchangchang/VoxKey`）。一条目一 issue，状态 = label。

- 状态四态：`b:open`（待认领）/ `b:doing`（开发中）/ `b:done`（开发完成，待验收）/ `b:closed`（验证通过并关闭）
- 优先级：`p0` 当前必须 / `p1` 下一步 / `p2` 正常 / `p3` 未来 / `p4` 不排期或待拍板
- 改状态 = `gh issue edit <n> --add-label/--remove-label` **+ 追加一条 comment**（变更记录），两件事一起做
- 引用一律 `#N`；正文里出现的 OneSay 编号会标「迁移自 c3ng-dev/OneSay#N」
- 会话开始先 `gh issue list --label b:open`，有 `前置：#N` 未闭合的不要抢跑
- 用户报的 bug / 新需求 → `gh issue create`，不留在对话里

## 跑起来 / 验证

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[tools]'
PYTHONPATH=src .venv/bin/python -m voxkey.app            # 常驻
PYTHONPATH=src .venv/bin/python tools/smoke.py          # 改完代码先跑这个（不连设备、不要权限）
packaging/build_macos.sh                                # 出 .app + 分发包（用独立的 .venv-build）
```

- 模型不进仓库：源码运行默认放 `models/`，或 `VOXKEY_MODELS_DIR` 指到别处；
  **打包后由首启自动下载**（`src/voxkey/modeldl.py`）到 `~/Library/Application Support/VoxKey/models`
- 要写盘的东西（模型、日志）一律走 `src/voxkey/paths.py`：打包后 `__file__` 在只读的 .app 包体里，
  不能再拿它推目录
- `tools/smoke.py` 覆盖：包导入、悬浮条四态几何断言（内容在裁剪层内且居中）、模型加载、
  首启下载全流程、发版资产名一致性、四个 CLI 入口 `--help`。改动 UI/布局/依赖后必须跑，
  **别用「看起来差不多」验收数值问题**
- 涉及 UI 的改动，验证要落到数值或像素：断言层 frame、或截图 + numpy 量像素，而不是肉眼
- 打包/发版见 README 的「打包与发布」；发版靠打 tag（`v*`）触发 Actions，**资产文件名不许带版本号**
  （静态页用的是 `releases/latest/download/<固定名>`）

## 代码约定

- `src` 布局，包名 `voxkey`；不在文件里插 `sys.path` hack，靠 `PYTHONPATH=src` 或安装
- 分层不要跨：`app`（主程序/状态机）→ `pill`（UI）/ `inject`（上屏）/ `audio`（采集）/
  `transcribe`（推理）/ `models`（模型加载）；设备相关都放 `device/`
- 中文注释/文档字符串，讲「为什么」；踩过的坑写进注释（例如独占键盘集合、裁剪层与中心锚点）
- 不碰剪贴板：上屏只走 AX 直写 → 合成 Unicode 按键，两条都不成就明说「未上屏」
- 一次改动的提交信息要能让人还原「为什么改」，写完跑 `tools/smoke.py`

## 非交互命令

`cp`/`mv`/`rm` 一律带 `-f`（`rm -rf`），避免交互式确认把会话挂住。
