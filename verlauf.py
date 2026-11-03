"""History of Open Libary: what was played and what was dragged into the DAW.

Stored in verlauf.json next to the server (OPENLIBARY_VERLAUF points test runs elsewhere):
  {"gehoert": [{"pfad": ..., "zeit": "2026-10-23 14:32:05"}, ...],      newest first, at most 500
   "gezogen": [{"pfad": ..., "zeit": ..., "datei": "Name_140_Fm.wav"}, ...],   datei only for a rendered version
   "gehoert_alle": [pfad, ...]}                                          ever played, oldest first, at most 20,000
The same path moves to the top instead of appearing twice. gehoert_alle drives the dimming in the list and the filter
“never played”: otherwise a sample would count as never played again after 300 others. Paths in NFC as in the index.
The state lives in memory (every playback writes, the search reads often) and is written atomically on every change
(first .neu, then rename).
"""
import json
import os
import threading
import time
import unicodedata
from pathlib import Path

from i18n import tx

DATEI = Path(os.environ.get("OPENLIBARY_VERLAUF", Path(__file__).resolve().parent / "verlauf.json"))
ARTEN = ("gehoert", "gezogen")
MAX_JE_ART = 500               # this many can be dragged at once (ZIEHEN_MAX in seite.html)
MAX_ALLE = 20000
MAX_DATEI = 300                 # length of the file name of a rendered version
_LOCK = threading.Lock()
_D = None                       # state in memory, read on first access
_STAND = [0]                    # counts changes (key of the search cache for nie_gehoert)


class VerlaufFehler(ValueError):
    pass


def _leer():
    return {"gehoert": [], "gezogen": [], "gehoert_alle": []}


def _laden():
    global _D
    if _D is not None:
        return _D
    d = _leer()
    if DATEI.is_file():
        try:
            roh = json.loads(DATEI.read_text(encoding="utf-8"))
            if not isinstance(roh, dict):
                raise ValueError("not an object")
            for a in ARTEN:
                d[a] = [e for e in roh.get(a, []) if isinstance(e, dict) and isinstance(e.get("pfad"), str)][:MAX_JE_ART]
            d["gehoert_alle"] = [p for p in roh.get("gehoert_alle", []) if isinstance(p, str)][-MAX_ALLE:]
        except (OSError, ValueError):
            # do not overwrite a broken file: set it aside and go on empty (like sammlungen.py)
            DATEI.rename(DATEI.with_name(DATEI.name + f".kaputt-{int(time.time())}"))
            d = _leer()
    d["_alle_menge"] = set(d["gehoert_alle"])
    _D = d
    return d


def _schreiben(d):
    tmp = DATEI.with_name(DATEI.name + ".neu")
    tmp.write_text(json.dumps({k: v for k, v in d.items() if not k.startswith("_")}, ensure_ascii=False, indent=0),
                   encoding="utf-8")
    os.replace(tmp, DATEI)


def eintragen(k, pfad_erlaubt):
    """k: {"art": "gehoert"|"gezogen", "pfad": ..., "datei": optional}. Returns the counts."""
    art = k.get("art")
    if art not in ARTEN:
        raise VerlaufFehler(tx("art must be gehoert or gezogen"))
    pfad = k.get("pfad")
    if not isinstance(pfad, str) or not pfad:
        raise VerlaufFehler(tx("pfad is missing"))
    pfad = unicodedata.normalize("NFC", pfad)
    if not pfad_erlaubt(pfad):
        raise VerlaufFehler(tx("Sample is not in the library"))
    datei = k.get("datei")
    if datei is not None and (not isinstance(datei, str) or len(datei) > MAX_DATEI):
        raise VerlaufFehler(tx("datei: text of up to {n} characters", n=MAX_DATEI))
    eintrag = {"pfad": pfad, "zeit": time.strftime("%Y-%m-%d %H:%M:%S")}
    if art == "gezogen" and datei:
        eintrag["datei"] = os.path.basename(datei)
    with _LOCK:
        d = _laden()
        d[art] = [eintrag] + [e for e in d[art] if e["pfad"] != pfad][:MAX_JE_ART - 1]
        if art == "gehoert":
            if pfad in d["_alle_menge"]:
                d["gehoert_alle"].remove(pfad)
            d["gehoert_alle"].append(pfad)
            d["_alle_menge"].add(pfad)
            if len(d["gehoert_alle"]) > MAX_ALLE:
                for p in d["gehoert_alle"][:-MAX_ALLE]:
                    d["_alle_menge"].discard(p)
                d["gehoert_alle"] = d["gehoert_alle"][-MAX_ALLE:]
        _schreiben(d)
        _STAND[0] += 1
        return _zahlen(d)


def _zahlen(d):
    return {a: len(d[a]) for a in ARTEN}


def zahlen():
    with _LOCK:
        return _zahlen(_laden())


def eintraege(art):
    """{pfad: entry} of the kind, and the order {pfad: rank} (0 = newest)."""
    with _LOCK:
        liste = list(_laden()[art])
    return {e["pfad"]: e for e in liste}, {e["pfad"]: i for i, e in enumerate(liste)}


def gehoert_menge():
    """All paths ever played (a set; do not modify)."""
    with _LOCK:
        return _laden()["_alle_menge"]


def stand():
    return _STAND[0]
