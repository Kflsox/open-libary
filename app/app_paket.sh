#!/bin/bash
# Fertige Mac-App für Freunde (03.11.2026, „max kompi“): Universal-App für Apple-Chip (ab macOS 11) und Intel (ab
# macOS 10.15). Hülle in Objective-C (app/main.m), je Architektur ein eigenständiges Python 3.12 mit numpy und
# soundfile (leichte Messung, Audio ohne ffmpeg), Code unter Resources. Kein Terminal und keine Command Line Tools
# beim Empfänger; Nutzerdaten unter ~/Library/Application Support/Open Libary.
#   bash app/app_paket.sh [Zielordner]      (Standard ~/Downloads)
# Die Pythons holt app/pythons_holen.sh nach ~/Library/Caches/OpenLibary-bau (python-build-standalone, PSF-Lizenz;
# numpy BSD, soundfile BSD mit libsndfile LGPL). Ohne Apple-Entwicklerkonto ist die App nur ad hoc signiert: beim ersten
# Öffnen einmal „Dennoch öffnen“ (steht in ANLEITUNG.md).
set -euo pipefail
HIER="$(cd "$(dirname "$0")/.." && pwd)"
ZIEL="${1:-$HOME/Downloads}"
BAU="${OPENLIBARY_BAU:-$HOME/Library/Caches/OpenLibary-bau}"
for a in aarch64 x86_64; do
  [ -x "$BAU/py-$a-apple-darwin/python/bin/python3.12" ] || { echo "Python für $a fehlt: erst bash app/pythons_holen.sh"; exit 1; }
done
TMP="$(mktemp -d)"
APP="$TMP/Open Libary.app"
R="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$R/openlibary/stile"

# Hülle: Universal-Binary
clang -fobjc-arc -O2 -target arm64-apple-macos11 -o "$TMP/ol-arm64" "$HIER/app/main.m" -framework Cocoa -framework WebKit
clang -fobjc-arc -O2 -target x86_64-apple-macos10.15 -o "$TMP/ol-x86_64" "$HIER/app/main.m" -framework Cocoa -framework WebKit
lipo -create -output "$APP/Contents/MacOS/Open Libary" "$TMP/ol-arm64" "$TMP/ol-x86_64"

# Code (ohne Nutzerdaten und ohne gehoer-Teile)
cp "$HIER"/{openlibary,netz,ordner,leicht,leichtmessen,plattform,audioform,sammlungen,verlauf,korrekturen,klangnaehe}.py "$R/openlibary/"
cp "$HIER/seite.html" "$HIER/ANLEITUNG.md" "$R/openlibary/"
cp "$HIER"/stile/*.css "$R/openlibary/stile/"
cp "$HIER/app/AppIcon.icns" "$R/AppIcon.icns"

# je Architektur ein Python, ohne Tk, Header, pip, Tests (auch die von numpy)
for paar in "aarch64:arm64" "x86_64:x86_64"; do
  quelle="$BAU/py-${paar%%:*}-apple-darwin/python"; ziel="$R/python-${paar##*:}"
  mkdir -p "$ziel/bin" "$ziel/lib"
  cp "$quelle/bin/python3.12" "$ziel/bin/python3"
  [ -f "$quelle/lib/libpython3.12.dylib" ] && cp "$quelle/lib/libpython3.12.dylib" "$ziel/lib/"
  rsync -a --exclude idlelib --exclude tkinter --exclude turtledemo --exclude ensurepip --exclude lib2to3 \
    --exclude test --exclude tests --exclude __pycache__ --exclude 'config-*' --exclude pydoc_data \
    --exclude 'pip' --exclude 'pip-*' "$quelle/lib/python3.12" "$ziel/lib/"
done
# vorkompilieren: die App startet Python mit -B (schreibt nichts ins signierte Paket), liest aber vorhandene .pyc
PYC="$( (ls -d "$HOME"/.local/share/uv/python/cpython-3.12*-macos-aarch64-none/bin/python3.12 2>/dev/null || true) | tail -1)"
[ -n "$PYC" ] || PYC="$(command -v python3.12 || true)"
if [ -n "$PYC" ]; then
  for a in arm64 x86_64; do
    "$PYC" -E -s -m compileall -q -j 0 "$R/python-$a/lib/python3.12" "$R/openlibary" >/dev/null 2>&1 || true
  done
fi

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Open Libary</string>
  <key>CFBundleDisplayName</key><string>Open Libary</string>
  <key>CFBundleIdentifier</key><string>de.jeremias.openlibary</string>
  <key>CFBundleExecutable</key><string>Open Libary</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleShortVersionString</key><string>1.1</string>
  <key>LSMinimumSystemVersion</key><string>10.15</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
codesign --force --deep --sign - "$APP" >/dev/null 2>&1
codesign --verify --deep "$APP"
mkdir -p "$ZIEL"
NAME="Open Libary $(date +%Y-%m-%d) Mac.zip"
# im Zip ein Ordner mit der App und der Anleitung daneben (öffnet sich mit Doppelklick in TextEdit)
mkdir "$TMP/Open Libary"
mv "$APP" "$TMP/Open Libary/"
cp "$HIER/ANLEITUNG.md" "$TMP/Open Libary/Anleitung.md"
(cd "$TMP" && ditto -c -k --keepParent "Open Libary" "$ZIEL/$NAME")
du -sh "$TMP/Open Libary/Open Libary.app" | awk '{print "App: " $1}'
rm -rf "$TMP"
echo "Paket: $ZIEL/$NAME"
