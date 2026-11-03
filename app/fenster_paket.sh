#!/bin/bash
# Fertige App für Windows (x64) oder Linux (x64) bauen (03.11.2026, „max kompi“): Electron-Hülle (app/fenster),
# eigenständiges Python 3.12 mit numpy und soundfile, Code. Ergebnis als Zip bzw. tar.gz in [Zielordner].
#   bash app/fenster_paket.sh windows|linux [Zielordner]
# Vorher einmal: bash app/pythons_holen.sh und (in app/fenster) npm install.
set -euo pipefail
HIER="$(cd "$(dirname "$0")/.." && pwd)"
SYS="${1:?windows oder linux}"
ZIEL="${2:-$HOME/Downloads}"
BAU="${OPENLIBARY_BAU:-$HOME/Library/Caches/OpenLibary-bau}"
F="$HIER/app/fenster"
RES="$F/res"
rm -rf "$RES" && mkdir -p "$RES/openlibary/stile"
cp "$HIER"/{openlibary,netz,ordner,leicht,leichtmessen,plattform,audioform,sammlungen,verlauf,korrekturen,klangnaehe}.py "$RES/openlibary/"
cp "$HIER/seite.html" "$HIER/ANLEITUNG.md" "$RES/openlibary/"
cp "$HIER"/stile/*.css "$RES/openlibary/stile/"
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
# vorkompilieren (Python startet mit -B und schreibt nichts in den Programmordner)
PYC="$( (ls -d "$HOME"/.local/share/uv/python/cpython-3.12*-macos-aarch64-none/bin/python3.12 2>/dev/null || true) | tail -1)"
[ -n "$PYC" ] || PYC="$(command -v python3.12 || true)"          # Bytecode ist je Version gleich, egal welches System
[ -n "$PYC" ] && "$PYC" -E -s -m compileall -q -j 0 "$LIB" "$RES/openlibary" >/dev/null 2>&1 || true
cd "$F"
[ -d node_modules/electron-builder ] || { echo "erst in app/fenster: npm install"; exit 1; }
# Python und Code: extraResources in package.json (res/); Ausgabe ins Protokoll, bei Fehler dessen Ende zeigen
npx electron-builder $ZIELART > "$F/bau.log" 2>&1 || { tail -40 "$F/bau.log"; exit 1; }
mkdir -p "$ZIEL"
for d in dist/*.zip dist/*.tar.gz dist/*.deb; do
  [ -f "$d" ] || continue
  neu="$ZIEL/Open Libary $(date +%Y-%m-%d) $( [ "$SYS" = windows ] && echo Windows || echo Linux ).${d##*.}"
  [ "${d##*.}" = gz ] && neu="${neu%.gz}.tar.gz"
  # .deb (Ubuntu, Mint, Debian): richtet die Chromium-Sandbox samt AppArmor-Profil ein, startet ohne --no-sandbox
  mv "$d" "$neu"; du -h "$neu" | awk '{print "Paket: " $2 " (" $1 ")"}'
done
