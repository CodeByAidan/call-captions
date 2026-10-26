#!/bin/bash
# Builds dist/CallCaptions-<version>-macOS.dmg on an Apple Silicon Mac with Python 3.12:
#   packaging/macos/build.sh 1.0.0 [python3.12]
set -euo pipefail
version="${1:-1.0.0}"
python="${2:-python3.12}"
cd "$(dirname "$0")/../.."

echo "== Creating build environment"
"$python" -m venv .venv-mac
py=.venv-mac/bin/python
"$py" -m pip install --upgrade pip --quiet
"$py" -m pip install -r requirements-macos.txt "pyinstaller>=6.11" --quiet
"$py" -m pip install --no-deps mlx-whisper==0.4.3 --quiet
# mlx ships a wheel per macOS version and pip picks the one for this Mac's version, which won't
# load on older ones. Swap in the macOS 14 wheel so the app runs everywhere it says it does.
mlx_version="$("$py" -m pip show mlx | awk '/^Version:/ {print $2}')"
rm -rf build/mlx-wheel
"$py" -m pip download "mlx==$mlx_version" --no-deps --only-binary=:all: \
    --platform macosx_14_0_arm64 --dest build/mlx-wheel --quiet
"$py" -m pip install --force-reinstall --no-deps build/mlx-wheel/*.whl --quiet

echo "== Bundling the app"
APP_VERSION="$version" "$py" -m PyInstaller packaging/CallCaptions.spec --noconfirm --clean \
    --distpath dist --workpath build
app="dist/Call Captions.app"
# Apple Silicon only runs signed code. Without an Apple Developer ID this is an ad-hoc
# signature, so people have to approve the app once (see the README).
codesign --force --deep --sign - "$app"

echo "== Self-test of the bundled app"
log="$HOME/Library/Application Support/Call Captions/captions.log"
rm -f "$log"
status=0
"$app/Contents/MacOS/CallCaptions" --selftest || status=$?
[ -f "$log" ] && cat "$log"
[ "$status" -eq 0 ] || { echo "Self-test failed (exit code $status)"; exit 1; }

echo "== Building the disk image"
staging="$(mktemp -d)"
cp -R "$app" "$staging/"
ln -s /Applications "$staging/Applications"
dmg="dist/CallCaptions-$version-macOS.dmg"
rm -f "$dmg"
hdiutil create -volname "Call Captions" -srcfolder "$staging" -ov -format UDZO "$dmg"
rm -rf "$staging"
echo "== Done: $dmg"
