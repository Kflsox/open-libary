#!/bin/bash
# Fertige Mac-App prüfen: Rauchtest mit dem Python aus dem Paket, dann die App öffnen und warten, bis sie ihren
# Server gestartet hat. Für GitHub Actions; lokal öffnet das ein Fenster.
#   bash tests/mac_app_pruefen.sh "Open Libary … Mac.zip" arm64|x86_64
set -euo pipefail
ZIP="$1"
ARCH="$2"
HIER="$(cd "$(dirname "$0")/.." && pwd)"
ZIEL="$(mktemp -d)"
ditto -x -k "$ZIP" "$ZIEL"
APP="$ZIEL/Open Libary/Open Libary.app"
R="$APP/Contents/Resources"
lipo -info "$APP/Contents/MacOS/Open Libary"
codesign --verify --deep "$APP"
PY="$R/python-$ARCH/bin/python3"
"$PY" "$HIER/tests/rauchtest.py" --python "$PY" --code "$R/openlibary"
open "$APP"
ok=""
for i in $(seq 1 90); do
  if curl -sf http://127.0.0.1:8765/api/stand | grep -q '"paket": true'; then ok=1; break; fi
  sleep 1
done
pkill -f "Open Libary.app" || true
if [ -z "$ok" ]; then
  echo "App hat keinen Server gestartet"
  tail -50 ~/Library/Logs/openlibary.log || true
  exit 1
fi
echo "App startet ihren Server ($ARCH)"
