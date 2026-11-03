"""Verlauf der Open Libary (Paket 4): was gehört und was nach FL Studio gezogen wurde.

Gespeichert in verlauf.json neben dem Server (OPENLIBARY_VERLAUF lenkt Testläufe woanders hin):
  {"gehoert": [{"pfad": ..., "zeit": "2026-10-23 14:32:05"}, ...],      neueste zuerst, höchstens 500
   "gezogen": [{"pfad": ..., "zeit": ..., "datei": "Name_140_Fm.wav"}, ...],   datei nur bei gerechneter Fassung
   "gehoert_alle": [pfad, ...]}                                          jemals gehört, älteste zuerst, höchstens 20.000
Der gleiche Pfad rückt nach oben statt doppelt. gehoert_alle trägt das Dämpfen in der Liste und den Filter
„noch nie gehört“: sonst gälte ein Sample nach 300 anderen wieder als nie gehört. Pfade in NFC wie im Index.
Der Stand liegt im Speicher (jede Wiedergabe schreibt, die Suche liest oft) und wird bei jeder Änderung atomar
geschrieben (erst .neu, dann umbenennen).
"""
import json
import os
import threading
import time
import unicodedata
from pathlib import Path

DATEI = Path(os.environ.get("OPENLIBARY_VERLAUF", Path(__file__).resolve().parent / "verlauf.json"))
ARTEN = ("gehoert", "gezogen")
MAX_JE_ART = 500               # so viele lassen sich auf einmal ziehen (Paket 6, ZIEHEN_MAX in seite.html)
MAX_ALLE = 20000
MAX_DATEI = 300                 # Länge des Dateinamens einer gerechneten Fassung
_LOCK = threading.Lock()
_D = None                       # Stand im Speicher, beim ersten Zugriff gelesen
_STAND = [0]                    # zählt Änderungen (Schlüssel des Suchzwischenspeichers bei nie_gehoert)


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
                raise ValueError("kein Objekt")
            for a in ARTEN:
                d[a] = [e for e in roh.get(a, []) if isinstance(e, dict) and isinstance(e.get("pfad"), str)][:MAX_JE_ART]
            d["gehoert_alle"] = [p for p in roh.get("gehoert_alle", []) if isinstance(p, str)][-MAX_ALLE:]
        except (OSError, ValueError):
            # kaputte Datei nicht überschreiben: beiseitelegen und leer weitermachen (wie sammlungen.py)
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
    """k: {"art": "gehoert"|"gezogen", "pfad": ..., "datei": optional}. Gibt die Zahlen zurück."""
    art = k.get("art")
    if art not in ARTEN:
        raise VerlaufFehler("art muss gehoert oder gezogen sein")
    pfad = k.get("pfad")
    if not isinstance(pfad, str) or not pfad:
        raise VerlaufFehler("pfad fehlt")
    pfad = unicodedata.normalize("NFC", pfad)
    if not pfad_erlaubt(pfad):
        raise VerlaufFehler("Sample steht nicht in der Bibliothek")
    datei = k.get("datei")
    if datei is not None and (not isinstance(datei, str) or len(datei) > MAX_DATEI):
        raise VerlaufFehler(f"datei: Text bis {MAX_DATEI} Zeichen")
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
    """{pfad: eintrag} der Art, und die Reihenfolge {pfad: rang} (0 = neuester)."""
    with _LOCK:
        liste = list(_laden()[art])
    return {e["pfad"]: e for e in liste}, {e["pfad"]: i for i, e in enumerate(liste)}


def gehoert_menge():
    """Alle jemals gehörten Pfade (Menge; nicht verändern)."""
    with _LOCK:
        return _laden()["_alle_menge"]


def stand():
    return _STAND[0]
