#!/bin/bash
# Set up Open Libary: build the starter command and the Mac app from this folder.
# Run it from the unpacked folder:  bash einrichten.sh
# Needs only Apple's Command Line Tools (Python 3 and Swift). ffmpeg and rubberband are optional extras.
set -euo pipefail
HIER="$(cd "$(dirname "$0")" && pwd)"
echo "Setting up Open Libary, code in: $HIER"

# 1. Command Line Tools: without them there is neither python3 nor swiftc
if ! xcode-select -p >/dev/null 2>&1 || ! /usr/bin/python3 -c "import sys; sys.exit(sys.version_info < (3, 9))" 2>/dev/null \
   || ! command -v swiftc >/dev/null 2>&1; then
  echo
  echo "Apple's Command Line Tools are missing. macOS will open a window now: click \"Install\" there."
  echo "Then run this script again."
  xcode-select --install 2>/dev/null || true
  exit 1
fi

# 2. Update: take over favorites, notes, history, corrections, the downloads list, your folders, style and language
#    from the previous code folder (the starter knows it). Nothing that already exists here is ever overwritten.
ALT=""
[ -f "$HOME/.local/bin/openlibary" ] && ALT="$(sed -n 's/^HIER="\(.*\)"$/\1/p' "$HOME/.local/bin/openlibary" | head -1)"
ALT="${ALT/\$HOME/$HOME}"
if [ -n "$ALT" ] && [ "$ALT" != "$HIER" ] && [ -d "$ALT" ]; then
  for d in sammlungen.json verlauf.json korrekturen.json zusaetze.csv eigene.csv ordner.json stil.txt sprache.txt; do
    if [ -e "$ALT/$d" ] && [ ! -e "$HIER/$d" ]; then cp -p "$ALT/$d" "$HIER/$d" && echo "taken over: $d"; fi
  done
fi

# 3. Starter command ~/.local/bin/openlibary (the app calls it)
mkdir -p "$HOME/.local/bin"
/usr/bin/python3 - "$HIER" "$HOME/.local/bin/openlibary" <<'PY'
import sys, pathlib
hier, ziel = sys.argv[1], pathlib.Path(sys.argv[2])
vorlage = (pathlib.Path(hier) / "app/starter.sh").read_text()
ziel.write_text(vorlage.replace("__HIER__", hier))
ziel.chmod(0o755)
PY
echo "Starter command: $HOME/.local/bin/openlibary"

# 4. Mac app (drag and drop into FL Studio, Ableton and others only works there)
bash "$HIER/app/bauen.sh"

# 5. Extra tools: the library works without them, but does less
fehlt=()
for w in ffmpeg rubberband; do
  command -v "$w" >/dev/null 2>&1 || [ -x "/opt/homebrew/bin/$w" ] || [ -x "/usr/local/bin/$w" ] || fehlt+=("$w")
done
echo
echo "Done. Open Libary is in ~/Applications. On first launch macOS asks for access to folders: click Allow."
echo "Then open Settings, click \"Add folder …\" and choose your sample folders."
if [ ${#fehlt[@]} -gt 0 ]; then
  echo
  echo "Recommended (with Homebrew, brew.sh):  brew install ${fehlt[*]}"
  echo "  ffmpeg: length of MP3 and FLAC, playback of AIFF.  rubberband: change tempo and key."
fi
