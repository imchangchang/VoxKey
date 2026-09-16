#!/usr/bin/env bash
# 打包 VoxKey.app（macOS arm64），可选签名 + 公证。
#
#   packaging/build_macos.sh                    # 只打包（未签名，自己机器上能跑）
#   packaging/build_macos.sh --sign             # 打包 + 签名
#   packaging/build_macos.sh --sign --notarize  # 打包 + 签名 + 公证 + 装订
#
# 签名/公证要的环境变量：
#   VOXKEY_SIGN_IDENTITY   "Developer ID Application: 你的名字 (TEAMID)"
#   VOXKEY_NOTARY_PROFILE  notarytool 的 keychain profile 名，先建一次：
#     xcrun notarytool store-credentials voxkey \
#       --apple-id <你的 Apple ID> --team-id <TEAMID> --password <App 专用密码>
#
# 注意：签名/公证这两条路在拿到 Apple Developer 账号之前**没跑过**，
# 脚本按官方文档写，第一次用的时候留意 notarytool 的输出。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv-build"
APP="$ROOT/packaging/dist/VoxKey.app"
ENTITLEMENTS="$ROOT/packaging/entitlements.plist"
# PyInstaller 默认把分析缓存写到 ~/Library/Application Support/pyinstaller。
# 指到仓库里：构建自包含（CI 上也能直接跑），也不去碰用户家目录。
export PYINSTALLER_CONFIG_DIR="$ROOT/packaging/.pyinstaller-cache"

DO_SIGN=0
DO_NOTARIZE=0
for arg in "$@"; do
  case "$arg" in
    --sign)     DO_SIGN=1 ;;
    --notarize) DO_NOTARIZE=1; DO_SIGN=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

if [ "$(uname -m)" != "arm64" ]; then
  echo "警告：当前是 $(uname -m)，这个配置只出 arm64 包（target_arch=arm64）" >&2
fi

# ---------- 1. 构建环境（独立一份，不碰开发用的 .venv） ----------
# 拿哪个解释器去建构建环境：优先开发用的 .venv，没有就退回系统 python3（CI 上只有 python3）
BOOTSTRAP_PY="${VOXKEY_BOOTSTRAP_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$BOOTSTRAP_PY" ] || BOOTSTRAP_PY="$(command -v python3)"
if [ ! -x "$VENV/bin/pyinstaller" ]; then
  # 变量名一定要用 ${} 包起来：后面紧跟中文全角括号时，CI 上的 C locale 会把那些字节
  # 当成变量名的一部分，报 "VENV（用: unbound variable"（本机 UTF-8 locale 下不复现）
  echo "==> 建构建环境 ${VENV}（用 ${BOOTSTRAP_PY}）"
  "$BOOTSTRAP_PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q -e '.[tools]' pyinstaller
fi

# ---------- 2. 图标 ----------
if [ ! -f "$ROOT/packaging/AppIcon.icns" ]; then
  echo "==> 生成图标"
  "$VENV/bin/python" "$ROOT/packaging/make_icon.py"
fi

# ---------- 3. 打包 ----------
echo "==> PyInstaller"
rm -rf "$ROOT/packaging/build" "$ROOT/packaging/dist"
"$VENV/bin/pyinstaller" "$ROOT/packaging/voxkey.spec" --noconfirm \
  --distpath "$ROOT/packaging/dist" --workpath "$ROOT/packaging/build"

test -d "$APP" || { echo "没打出 $APP" >&2; exit 1; }

# ---------- 4. 签名 ----------
# 由内往外签：先把包里的 .so/.dylib 逐个签掉，再签整个 bundle。
# 全靠 `--deep` 一把梭也能过，但那样每个嵌套二进制的签名参数不完全可控，
# 出问题时 notarytool 只会回一句笼统的「signature invalid」。
if [ "$DO_SIGN" = 1 ]; then
  : "${VOXKEY_SIGN_IDENTITY:?签名需要 VOXKEY_SIGN_IDENTITY}"
  echo "==> codesign：先签嵌套的动态库"
  while IFS= read -r -d '' f; do
    codesign --force --options runtime --timestamp \
             --sign "$VOXKEY_SIGN_IDENTITY" "$f" >/dev/null
  done < <(find "$APP/Contents" -type f \( -name '*.so' -o -name '*.dylib' \) -print0)

  echo "==> codesign：再签整个 bundle"
  codesign --force --deep --options runtime --timestamp \
           --entitlements "$ENTITLEMENTS" \
           --sign "$VOXKEY_SIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
fi

# ---------- 5. 公证 + 装订 ----------
# 装订（staple）是必须的：不装订的话，用户第一次打开时机器要联网去问苹果，
# 离线就还是被拦。
if [ "$DO_NOTARIZE" = 1 ]; then
  : "${VOXKEY_NOTARY_PROFILE:?公证需要 VOXKEY_NOTARY_PROFILE}"
  echo "==> 公证（要排队，几分钟）"
  ZIP="$ROOT/packaging/dist/VoxKey-notarize.zip"
  ditto -c -k --keepParent "$APP" "$ZIP"
  xcrun notarytool submit "$ZIP" --keychain-profile "$VOXKEY_NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  xcrun stapler validate "$APP"
  rm -f "$ZIP"
fi

# ---------- 6. 收尾 ----------
# 压缩一定要做，而且必须放在签名/公证**之后**：压缩包里的 .app 得带着已经装订好的票据，
# 不然用户解压出来的还是「未公证」的那份。
ZIP_OUT="$ROOT/packaging/dist/VoxKey-macos-arm64.zip"
ditto -c -k --keepParent "$APP" "$ZIP_OUT"
echo "==> 分发包：$ZIP_OUT"

echo "==> 完成：$APP"
du -sh "$APP" "$ZIP_OUT"
