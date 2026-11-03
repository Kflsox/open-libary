"""Favoriten und Playlists der Open Libary.

Gespeichert in sammlungen.json neben dem Server (OPENLIBARY_SAMMLUNGEN lenkt Testläufe woanders hin):
  {"favoriten": [pfad, ...], "playlists": [{"name": "...", "pfade": [pfad, ...], "angelegt": "..."}],
   "ausschnitte": {pfad: {"start": s, "ende": s}}, "notizen": {pfad: "text"}}     (Paket 10)
  "verworfen": [pfad, ...]                                                           (Paket 4, wie Favoriten)
Pfade in NFC wie im Index. Jede Änderung wird sofort atomar geschrieben (erst .neu, dann umbenennen), damit ein Absturz
nie eine halbe Datei hinterlässt. Die Reihenfolge in einer Playlist ist die Reihenfolge des Hinzufügens.
"""
import json
import os
import threading
import time
import unicodedata
from pathlib import Path

DATEI = Path(os.environ.get("OPENLIBARY_SAMMLUNGEN", Path(__file__).resolve().parent / "sammlungen.json"))
_LOCK = threading.Lock()
MAX_NAME = 60
MAX_NOTIZ = 300                 # eine Zeile im Fuß
AUSSCHNITT_MIN = 0.05           # kürzer als 50 ms wird verworfen (Paket 10)
AUSSCHNITT_MAX = 6 * 3600       # Sekunden; länger ist keine Datei im Bestand
_NOTIZ_STAND = [0]              # zählt jede Änderung an Notizen (Suchzwischenspeicher im Server)
_VERWORFEN_STAND = [0]          # ebenso für Verworfen (Paket 4): Suche und Zufall lassen verworfene weg


class SammlungFehler(ValueError):
    pass


def _nfc(s):
    return unicodedata.normalize("NFC", s or "")


def _lesen():
    if not DATEI.is_file():
        return {"favoriten": [], "playlists": [], "ausschnitte": {}, "notizen": {}, "verworfen": []}
    try:
        d = json.loads(DATEI.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # kaputte Datei nicht überschreiben: beiseitelegen und leer weitermachen
        DATEI.rename(DATEI.with_name(DATEI.name + f".kaputt-{int(time.time())}"))
        return {"favoriten": [], "playlists": [], "ausschnitte": {}, "notizen": {}, "verworfen": []}
    d.setdefault("favoriten", [])
    d.setdefault("playlists", [])
    d.setdefault("ausschnitte", {})
    d.setdefault("notizen", {})
    d.setdefault("verworfen", [])
    return d


def _schreiben(d):
    tmp = DATEI.with_name(DATEI.name + ".neu")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DATEI)


def _name(s):
    n = " ".join((s or "").split())[:MAX_NAME]
    if not n:
        raise SammlungFehler("Die Playlist braucht einen Namen")
    return n


def _playlist(d, name):
    for p in d["playlists"]:
        if p["name"].casefold() == name.casefold():
            return p
    raise SammlungFehler(f"Playlist „{name}“ gibt es nicht")


def stand():
    """Für die Oberfläche: Favoriten als Menge und Playlists mit Anzahl."""
    with _LOCK:
        d = _lesen()
    return {"favoriten": d["favoriten"],
            "playlists": [{"name": p["name"], "anzahl": len(p["pfade"]), "pfade": p["pfade"]} for p in d["playlists"]],
            "ausschnitte": d["ausschnitte"], "notizen": d["notizen"], "verworfen": d["verworfen"]}


def _sekunden(v, was):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not 0 <= v <= AUSSCHNITT_MAX:
        raise SammlungFehler(f"{was} muss eine Zahl von 0 bis {AUSSCHNITT_MAX} Sekunden sein")
    return round(float(v), 6)


def notizen():
    """Notizen für die Suche: {pfad: text}"""
    with _LOCK:
        return dict(_lesen()["notizen"])


def notiz_stand():
    return _NOTIZ_STAND[0]


def verworfen():
    """Verworfene Pfade als Menge (Paket 4)"""
    with _LOCK:
        return set(_lesen()["verworfen"])


def verworfen_stand():
    return _VERWORFEN_STAND[0]


def aendern(k, pfad_erlaubt, dauer=None):
    """k: {"aktion": ..., ...}. pfad_erlaubt(pfad) prüft, ob der Pfad im Index steht (nur bekannte Samples);
    dauer(pfad) gibt die Länge in Sekunden oder None (Grenze für den Ausschnitt)."""
    aktion = k.get("aktion")
    roh = k.get("pfade") or ([k["pfad"]] if k.get("pfad") else [])
    if not isinstance(roh, list) or not all(isinstance(p, str) for p in roh):     # Zahl als Pfad gab 500 (Paket 4)
        raise SammlungFehler("pfad muss Text sein, pfade eine Liste von Texten")
    pfade = [_nfc(p) for p in roh]
    for p in pfade:
        if not pfad_erlaubt(p):
            raise SammlungFehler("Sample steht nicht in der Bibliothek")
    with _LOCK:
        d = _lesen()
        fav = d["favoriten"]
        if aktion == "favorit":                     # umschalten, oder mit "an": true/false setzen
            for p in pfade:
                an = k.get("an", p not in fav)
                if not isinstance(an, bool):
                    raise SammlungFehler("„an“ muss true oder false sein")
                if an and p not in fav:
                    fav.append(p)
                elif not an and p in fav:
                    fav.remove(p)
        elif aktion == "verwerfen":                 # Paket 4: umschalten, oder mit "an": true/false setzen
            weg = d["verworfen"]
            for p in pfade:
                an = k.get("an", p not in weg)
                if not isinstance(an, bool):
                    raise SammlungFehler("„an“ muss true oder false sein")
                if an and p not in weg:
                    weg.append(p)
                elif not an and p in weg:
                    weg.remove(p)
            _VERWORFEN_STAND[0] += 1
        elif aktion == "neu":                       # neue Playlist, optional gleich mit Samples
            name = _name(k.get("name"))
            if any(p["name"].casefold() == name.casefold() for p in d["playlists"]):
                raise SammlungFehler(f"Playlist „{name}“ gibt es schon")
            d["playlists"].append({"name": name, "pfade": list(dict.fromkeys(pfade)),
                                   "angelegt": time.strftime("%Y-%m-%d %H:%M")})
        elif aktion == "hinzu":
            pl = _playlist(d, _name(k.get("name")))
            for p in pfade:
                if p not in pl["pfade"]:
                    pl["pfade"].append(p)
        elif aktion == "raus":
            pl = _playlist(d, _name(k.get("name")))
            pl["pfade"] = [p for p in pl["pfade"] if p not in pfade]
        elif aktion == "umbenennen":
            pl = _playlist(d, _name(k.get("name")))
            neu = _name(k.get("neu"))
            if neu.casefold() != pl["name"].casefold() and any(p["name"].casefold() == neu.casefold() for p in d["playlists"]):
                raise SammlungFehler(f"Playlist „{neu}“ gibt es schon")
            pl["name"] = neu
        elif aktion == "loeschen":
            pl = _playlist(d, _name(k.get("name")))
            d["playlists"].remove(pl)
        elif aktion == "ausschnitt":                # Paket 10: {pfad, start, ende}; start null hebt ihn auf
            if len(pfade) != 1:
                raise SammlungFehler("Ausschnitt braucht genau einen Pfad")
            if k.get("start") is None and k.get("ende") is None:
                d["ausschnitte"].pop(pfade[0], None)
            else:
                a, e = _sekunden(k.get("start"), "start"), _sekunden(k.get("ende"), "ende")
                if e - a < AUSSCHNITT_MIN:
                    raise SammlungFehler("Ausschnitt kürzer als 50 ms")
                d0 = dauer(pfade[0]) if dauer else None      # Länge laut Index; 50 ms Spiel wie /api/fassung
                if d0 and e > d0 + 0.05:
                    raise SammlungFehler("Ausschnitt geht über das Ende der Datei")
                d["ausschnitte"][pfade[0]] = {"start": a, "ende": e}
        elif aktion == "notiz":                     # Paket 10: {pfad, text}; leerer Text löscht die Notiz
            if len(pfade) != 1:
                raise SammlungFehler("Notiz braucht genau einen Pfad")
            t = k.get("text")
            if t is not None and not isinstance(t, str):
                raise SammlungFehler("Notiz muss Text sein")
            t = " ".join(_nfc(t).split())
            if len(t) > MAX_NOTIZ:
                raise SammlungFehler(f"Notiz höchstens {MAX_NOTIZ} Zeichen")
            if t:
                d["notizen"][pfade[0]] = t
            else:
                d["notizen"].pop(pfade[0], None)
            _NOTIZ_STAND[0] += 1
        else:
            raise SammlungFehler("unbekannte Aktion")
        _schreiben(d)
    return stand()


def pfade_fuer(filter_wert):
    """Filter der Bibliothek: "fav" für Favoriten, "pl:<Name>" für eine Playlist (so kann eine Playlist auch "fav"
    heißen). Gibt eine Menge von Pfaden zurück."""
    return set(reihenfolge(filter_wert))


def reihenfolge(filter_wert):
    """Reihenfolge der Playlist (für die Sortierung "wie in der Playlist")."""
    with _LOCK:
        d = _lesen()
    name = (filter_wert or "")[3:] if (filter_wert or "").startswith("pl:") else None
    liste = d["favoriten"] if filter_wert == "fav" else d["verworfen"] if filter_wert == "verworfen" else [] if name is None else next(
        (p["pfade"] for p in d["playlists"] if p["name"].casefold() == _nfc(name).casefold()), [])
    return {p: i for i, p in enumerate(liste)}
