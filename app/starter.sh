#!/bin/bash
# openlibary: startet die Open Libary (Sample-Bibliothek) im Hintergrund und öffnet sie.
#   openlibary          starten oder, wenn sie schon läuft, nur öffnen
#   openlibary stopp    Server beenden
#   openlibary log      Protokoll zeigen
#   openlibary --nur-server   nur den Server starten (so ruft ihn die App auf)
# Vorlage: einrichten.sh schreibt sie nach ~/.local/bin/openlibary und setzt den Code-Ordner ein.
HIER="__HIER__"
PORT=8765
URL="http://127.0.0.1:$PORT/"
LOG="$HOME/Library/Logs/openlibary.log"
case "${1:-}" in
  stopp) pkill -f "openlibary.py --port" && echo "Open Libary beendet." || echo "Open Libary lief nicht."; exit 0 ;;
  log) tail -n 40 "$LOG"; exit 0 ;;
  --nur-server) NUR_SERVER=1 ;;
esac
if ! curl -s -o /dev/null --max-time 1 "$URL"; then
  nohup /usr/bin/python3 "$HIER/openlibary.py" --port "$PORT" --ohne-browser >> "$LOG" 2>&1 < /dev/null &
  for _ in $(seq 1 40); do curl -s -o /dev/null --max-time 1 "$URL" && break; sleep 0.25; done
fi
[ -n "${NUR_SERVER:-}" ] && exit 0
if [ -d "$HOME/Applications/Open Libary.app" ]; then open "$HOME/Applications/Open Libary.app"; else open "$URL"; fi
