#!/bin/bash
# Open Libary einrichten (Musiker-Test 03.11.2026): Startbefehl und Mac-App aus diesem Ordner bauen.
# Aufruf aus dem entpackten Ordner:  bash einrichten.sh
# Braucht nur die Command Line Tools von Apple (Python 3 und Swift). ffmpeg und rubberband sind Zusatz.
set -euo pipefail
HIER="$(cd "$(dirname "$0")" && pwd)"
echo "Open Libary einrichten, Code in: $HIER"

# 1. Command Line Tools: ohne sie gibt es weder python3 noch swiftc
if ! xcode-select -p >/dev/null 2>&1 || ! /usr/bin/python3 -c "import sys; sys.exit(sys.version_info < (3, 9))" 2>/dev/null \
   || ! command -v swiftc >/dev/null 2>&1; then
  echo
  echo "Es fehlen die Command Line Tools von Apple. macOS öffnet gleich ein Fenster: dort „Installieren“."
  echo "Danach dieses Skript noch einmal starten."
  xcode-select --install 2>/dev/null || true
  exit 1
fi

# 2. Update: Favoriten, Notizen, Verlauf, Korrekturen, Downloads-Liste, eigene Ordner und Stil aus dem bisherigen
#    Code-Ordner übernehmen (der Starter kennt ihn). Vorhandenes hier wird nie überschrieben.
ALT=""
[ -f "$HOME/.local/bin/openlibary" ] && ALT="$(sed -n 's/^HIER="\(.*\)"$/\1/p' "$HOME/.local/bin/openlibary" | head -1)"
ALT="${ALT/\$HOME/$HOME}"
if [ -n "$ALT" ] && [ "$ALT" != "$HIER" ] && [ -d "$ALT" ]; then
  for d in sammlungen.json verlauf.json korrekturen.json zusaetze.csv eigene.csv ordner.json stil.txt; do
    if [ -e "$ALT/$d" ] && [ ! -e "$HIER/$d" ]; then cp -p "$ALT/$d" "$HIER/$d" && echo "übernommen: $d"; fi
  done
fi

# 3. Startbefehl ~/.local/bin/openlibary (die App ruft ihn auf)
mkdir -p "$HOME/.local/bin"
/usr/bin/python3 - "$HIER" "$HOME/.local/bin/openlibary" <<'PY'
import sys, pathlib
hier, ziel = sys.argv[1], pathlib.Path(sys.argv[2])
vorlage = (pathlib.Path(hier) / "app/starter.sh").read_text()
ziel.write_text(vorlage.replace("__HIER__", hier))
ziel.chmod(0o755)
PY
echo "Startbefehl: $HOME/.local/bin/openlibary"

# 4. Mac-App (Drag & Drop nach FL Studio, Ableton und Co. geht nur dort)
bash "$HIER/app/bauen.sh"

# 5. Zusatzwerkzeuge: ohne sie geht die Bibliothek, aber weniger
fehlt=()
for w in ffmpeg rubberband; do
  command -v "$w" >/dev/null 2>&1 || [ -x "/opt/homebrew/bin/$w" ] || [ -x "/usr/local/bin/$w" ] || fehlt+=("$w")
done
echo
echo "Fertig. Open Libary liegt in ~/Applications. Beim ersten Start fragt macOS nach dem Zugriff auf Ordner: Erlauben."
echo "Danach unter Einstellungen „Ordner hinzufügen …“ und deine Sample-Ordner wählen."
if [ ${#fehlt[@]} -gt 0 ]; then
  echo
  echo "Empfohlen (mit Homebrew, brew.sh):  brew install ${fehlt[*]}"
  echo "  ffmpeg: Länge von MP3 und FLAC, Wiedergabe von AIFF.  rubberband: Tempo und Tonart anpassen."
fi
