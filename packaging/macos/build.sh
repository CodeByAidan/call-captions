#!/bin/bash
# Builds dist/CallCaptions-<version>-macOS.dmg on an Apple Silicon Mac with Python 3.12:
#   packaging/macos/build.sh 1.0.0 [python3.12]
set -euo pipefail
version="${1:-1.0.0}"
python="${2:-python3.12}"
min_macos=14.2  # LSMinimumSystemVersion in packaging/CallCaptions.spec
cd "$(dirname "$0")/../.."

echo "== Creating build environment"
"$python" -m venv .venv-mac
py=.venv-mac/bin/python
"$py" -m pip install --upgrade pip --quiet
"$py" -m pip install -r requirements-macos.txt "pyinstaller>=6.11" --quiet
"$py" -m pip install --no-deps mlx-whisper==0.4.3 --quiet

echo "== Bundling the app"
APP_VERSION="$version" "$py" -m PyInstaller packaging/CallCaptions.spec --noconfirm --clean \
    --distpath dist --workpath build
app="dist/Call Captions.app"
# Apple Silicon only runs signed code. Without an Apple Developer ID this is an ad-hoc
# signature, so people have to approve the app once (see the README).
codesign --force --deep --sign - "$app"

echo "== Checking every binary runs on macOS $min_macos"
too_new=0
while IFS= read -r -d '' f; do
    file -b "$f" | grep -q Mach-O || continue
    # LC_BUILD_VERSION lists "minos", the older LC_VERSION_MIN_MACOSX lists "version".
    for v in $(otool -l "$f" | awk '/LC_BUILD_VERSION/ {b=1} b && $1 == "minos" {print $2; b=0}
                                    /LC_VERSION_MIN_MACOSX/ {m=1} m && $1 == "version" {print $2; m=0}'); do
        if [ "$(printf '%s\n' "$v" "$min_macos" | sort -V | tail -n1)" != "$min_macos" ]; then
            echo "Needs macOS $v: ${f#"$app/"}"
            too_new=1
        fi
    done
done < <(find "$app" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0)
[ "$too_new" -eq 0 ] || { echo "Some binaries need a newer macOS than $min_macos"; exit 1; }

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
