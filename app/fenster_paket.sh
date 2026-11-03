#!/bin/bash
# Build the packaged app for Windows (x64) or Linux (x64), for maximum compatibility: Electron shell (app/fenster),
# standalone Python 3.12 with numpy and soundfile, code. Result as zip or tar.gz in [target folder].
#   bash app/fenster_paket.sh windows|linux [target folder]
# Once beforehand: bash app/pythons_holen.sh and (in app/fenster) npm install.
set -euo pipefail
HIER="$(cd "$(dirname "$0")/.." && pwd)"
SYS="${1:?windows or linux}"
ZIEL="${2:-$HOME/Downloads}"
BAU="${OPENLIBARY_BAU:-$HOME/Library/Caches/OpenLibary-bau}"
F="$HIER/app/fenster"
RES="$F/res"
rm -rf "$RES" && mkdir -p "$RES/openlibary/stile"
cp "$HIER"/{openlibary,i18n,netz,ordner,leicht,leichtmessen,plattform,audioform,sammlungen,verlauf,korrekturen,klangnaehe}.py "$RES/openlibary/"
cp "$HIER/seite.html" "$HIER/GUIDE.md" "$RES/openlibary/"
cp "$HIER"/stile/*.css "$RES/openlibary/stile/"
cp -R "$HIER/lang" "$RES/openlibary/"           # translations, must sit next to i18n.py
AUS="--exclude idlelib --exclude tkinter --exclude turtledemo --exclude ensurepip --exclude lib2to3 --exclude test
     --exclude tests --exclude __pycache__ --exclude pydoc_data --exclude pip --exclude pip-*"
if [ "$SYS" = windows ]; then
  Q="$BAU/py-x86_64-pc-windows-msvc/python"
  mkdir -p "$RES/python"
  rsync -a $AUS --exclude tcl --exclude include --exclude Scripts --exclude libs --exclude '_tkinter.pyd' \
    --exclude 'tcl*.dll' --exclude 'tk*.dll' "$Q/" "$RES/python/"
  LIB="$RES/python/Lib"; ZIELART="--win zip --x64"
else
  Q="$BAU/py-x86_64-unknown-linux-gnu/python"
  mkdir -p "$RES/python/bin" "$RES/python/lib"
  cp "$Q/bin/python3.12" "$RES/python/bin/python3"
  cp -a "$Q"/lib/libpython3*.so* "$RES/python/lib/" 2>/dev/null || true
  rsync -a $AUS --exclude 'config-*' "$Q/lib/python3.12" "$RES/python/lib/"
  LIB="$RES/python/lib/python3.12"; ZIELART="--linux deb tar.gz --x64"
fi
# precompile (Python starts with -B and writes nothing into the program folder)
PYC="$( (ls -d "$HOME"/.local/share/uv/python/cpython-3.12*-macos-aarch64-none/bin/python3.12 2>/dev/null || true) | tail -1)"
[ -n "$PYC" ] || PYC="$(command -v python3.12 || true)"          # bytecode is the same per version, whatever the system
[ -n "$PYC" ] && "$PYC" -E -s -m compileall -q -j 0 "$LIB" "$RES/openlibary" >/dev/null 2>&1 || true
cd "$F"
[ -d node_modules/electron-builder ] || { echo "run npm install in app/fenster first"; exit 1; }
# Python and code: extraResources in package.json (res/); output goes to the log, on failure show its end
npx electron-builder $ZIELART > "$F/bau.log" 2>&1 || { tail -40 "$F/bau.log"; exit 1; }
mkdir -p "$ZIEL"
for d in dist/*.zip dist/*.tar.gz dist/*.deb; do
  [ -f "$d" ] || continue
  neu="$ZIEL/Open Libary $(date +%Y-%m-%d) $( [ "$SYS" = windows ] && echo Windows || echo Linux ).${d##*.}"
  [ "${d##*.}" = gz ] && neu="${neu%.gz}.tar.gz"
  # .deb (Ubuntu, Mint, Debian): sets up the Chromium sandbox with its AppArmor profile, starts without --no-sandbox
  mv "$d" "$neu"; du -h "$neu" | awk '{print "Package: " $2 " (" $1 ")"}'
done
