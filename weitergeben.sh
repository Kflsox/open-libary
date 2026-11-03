#!/bin/bash
# Paket für Freunde (Musiker-Test 03.11.2026): nur Code, keine eigenen Daten. Favoriten, Notizen, Hörverlauf,
# Korrekturen, Downloads-Liste, eigene Ordner und Stilwahl bleiben hier. Aufruf: bash weitergeben.sh [Zielordner]
set -euo pipefail
HIER="$(cd "$(dirname "$0")" && pwd)"
ZIEL="${1:-$HOME/Downloads}"
NAME="Open Libary $(date +%Y-%m-%d)"
TMP="$(mktemp -d)"
mkdir -p "$TMP/$NAME/app" "$TMP/$NAME/stile"
cp "$HIER"/{openlibary,netz,ordner,leicht,leichtmessen,plattform,audioform,sammlungen,verlauf,korrekturen,klangnaehe,klang_vektoren,klang_dienst}.py "$TMP/$NAME/" 2>/dev/null || true
cp "$HIER"/seite.html "$HIER"/einrichten.sh "$HIER"/ANLEITUNG.md "$TMP/$NAME/"
cp "$HIER"/stile/*.css "$TMP/$NAME/stile/"
cp "$HIER"/app/{main.m,leichtmessen.swift,bauen.sh,starter.sh,AppIcon.icns} "$TMP/$NAME/app/"
for d in sammlungen.json verlauf.json korrekturen.json zusaetze.csv eigene.csv ordner.json stil.txt; do
  [ -e "$TMP/$NAME/$d" ] && { echo "Fehler: $d wäre im Paket" >&2; exit 1; }
done
mkdir -p "$ZIEL"
(cd "$TMP" && ditto -c -k --keepParent "$NAME" "$ZIEL/$NAME.zip")
rm -rf "$TMP"
echo "Paket: $ZIEL/$NAME.zip"
