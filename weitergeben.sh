#!/bin/bash
# Package for friends: code only, no personal data. Favorites, notes, play history, corrections, the downloads list,
# your folders, style and language choice stay here. Usage: bash weitergeben.sh [target folder]
set -euo pipefail
HIER="$(cd "$(dirname "$0")" && pwd)"
ZIEL="${1:-$HOME/Downloads}"
NAME="Open Libary $(date +%Y-%m-%d)"
TMP="$(mktemp -d)"
mkdir -p "$TMP/$NAME/app" "$TMP/$NAME/stile"
cp "$HIER"/{openlibary,i18n,netz,ordner,leicht,leichtmessen,plattform,audioform,sammlungen,verlauf,korrekturen,klangnaehe,klang_vektoren,klang_dienst,bericht}.py "$TMP/$NAME/" 2>/dev/null || true
cp "$HIER"/seite.html "$HIER"/einrichten.sh "$HIER"/GUIDE.md "$TMP/$NAME/"
cp "$HIER"/stile/*.css "$TMP/$NAME/stile/"
cp -R "$HIER/lang" "$TMP/$NAME/"
cp "$HIER"/app/{main.m,leichtmessen.swift,bauen.sh,starter.sh,AppIcon.icns} "$TMP/$NAME/app/"
for d in sammlungen.json verlauf.json korrekturen.json zusaetze.csv eigene.csv ordner.json stil.txt sprache.txt; do
  [ -e "$TMP/$NAME/$d" ] && { echo "Error: $d would be in the package" >&2; exit 1; }
done
mkdir -p "$ZIEL"
(cd "$TMP" && ditto -c -k --keepParent "$NAME" "$ZIEL/$NAME.zip")
rm -rf "$TMP"
echo "Package: $ZIEL/$NAME.zip"
