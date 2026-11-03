#!/bin/bash
# openlibary: starts Open Libary (sample library) in the background and opens it.
#   openlibary          start it, or only open it when it is already running
#   openlibary stop     stop the server (German alias: stopp)
#   openlibary log      show the log
#   openlibary --server-only   start only the server (the app calls it as --nur-server, which keeps working)
# Template: einrichten.sh writes it to ~/.local/bin/openlibary and fills in the code folder.
HIER="__HIER__"
PORT=8765
URL="http://127.0.0.1:$PORT/"
LOG="$HOME/Library/Logs/openlibary.log"
case "${1:-}" in
  stop|stopp) pkill -f "openlibary.py --port" && echo "Open Libary stopped." || echo "Open Libary was not running."; exit 0 ;;
  log) tail -n 40 "$LOG"; exit 0 ;;
  --server-only|--nur-server) NUR_SERVER=1 ;;
esac
if ! curl -s -o /dev/null --max-time 1 "$URL"; then
  nohup /usr/bin/python3 "$HIER/openlibary.py" --port "$PORT" --ohne-browser >> "$LOG" 2>&1 < /dev/null &
  for _ in $(seq 1 40); do curl -s -o /dev/null --max-time 1 "$URL" && break; sleep 0.25; done
fi
[ -n "${NUR_SERVER:-}" ] && exit 0
if [ -d "$HOME/Applications/Open Libary.app" ]; then open "$HOME/Applications/Open Libary.app"; else open "$URL"; fi
