"""Favorites and playlists of Open Libary.

Stored in sammlungen.json next to the server (OPENLIBARY_SAMMLUNGEN points test runs elsewhere):
  {"favoriten": [pfad, ...], "playlists": [{"name": "...", "pfade": [pfad, ...], "angelegt": "..."}],
   "ausschnitte": {pfad: {"start": s, "ende": s}}, "notizen": {pfad: "text"}}
  "verworfen": [pfad, ...]                                                           (like favoriten)
Paths in NFC as in the index. Every change is written atomically right away (first .neu, then rename), so a crash
never leaves half a file. The order in a playlist is the order in which samples were added.
"""
import json
import os
import threading
import time
import unicodedata
from pathlib import Path

from i18n import tx

DATEI = Path(os.environ.get("OPENLIBARY_SAMMLUNGEN", Path(__file__).resolve().parent / "sammlungen.json"))
_LOCK = threading.Lock()
MAX_NAME = 60
MAX_NOTIZ = 300                 # one line in the player bar
AUSSCHNITT_MIN = 0.05           # shorter than 50 ms is dropped
AUSSCHNITT_MAX = 6 * 3600       # seconds; no file in the library is longer
_NOTIZ_STAND = [0]              # counts every change to notes (search cache in the server)
_VERWORFEN_STAND = [0]          # the same for discarded samples: search and Random leave them out


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
        # do not overwrite a broken file: set it aside and go on empty
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
        raise SammlungFehler(tx("The playlist needs a name"))
    return n


def _playlist(d, name):
    for p in d["playlists"]:
        if p["name"].casefold() == name.casefold():
            return p
    raise SammlungFehler(tx("Playlist “{name}” does not exist", name=name))


def stand():
    """For the interface: favorites as a set and playlists with their count."""
    with _LOCK:
        d = _lesen()
    return {"favoriten": d["favoriten"],
            "playlists": [{"name": p["name"], "anzahl": len(p["pfade"]), "pfade": p["pfade"]} for p in d["playlists"]],
            "ausschnitte": d["ausschnitte"], "notizen": d["notizen"], "verworfen": d["verworfen"]}


def _sekunden(v, was):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not 0 <= v <= AUSSCHNITT_MAX:
        raise SammlungFehler(tx("{name} must be a number from 0 to {max} seconds", name=was, max=AUSSCHNITT_MAX))
    return round(float(v), 6)


def notizen():
    """Notes for the search: {pfad: text}"""
    with _LOCK:
        return dict(_lesen()["notizen"])


def notiz_stand():
    return _NOTIZ_STAND[0]


def verworfen():
    """Discarded paths as a set"""
    with _LOCK:
        return set(_lesen()["verworfen"])


def verworfen_stand():
    return _VERWORFEN_STAND[0]


def aendern(k, pfad_erlaubt, dauer=None):
    """k: {"aktion": ..., ...}. pfad_erlaubt(pfad) checks that the path is in the index (known samples only);
    dauer(pfad) gives the length in seconds or None (limit for the region)."""
    aktion = k.get("aktion")
    roh = k.get("pfade") or ([k["pfad"]] if k.get("pfad") else [])
    if not isinstance(roh, list) or not all(isinstance(p, str) for p in roh):     # a number as path gave a 500
        raise SammlungFehler(tx("pfad must be a string, pfade a list of strings"))
    pfade = [_nfc(p) for p in roh]
    for p in pfade:
        if not pfad_erlaubt(p):
            raise SammlungFehler(tx("Sample is not in the library"))
    with _LOCK:
        d = _lesen()
        fav = d["favoriten"]
        if aktion == "favorit":                     # toggle, or set with "an": true/false
            for p in pfade:
                an = k.get("an", p not in fav)
                if not isinstance(an, bool):
                    raise SammlungFehler(tx("“an” must be true or false"))
                if an and p not in fav:
                    fav.append(p)
                elif not an and p in fav:
                    fav.remove(p)
        elif aktion == "verwerfen":                 # toggle, or set with "an": true/false
            weg = d["verworfen"]
            for p in pfade:
                an = k.get("an", p not in weg)
                if not isinstance(an, bool):
                    raise SammlungFehler(tx("“an” must be true or false"))
                if an and p not in weg:
                    weg.append(p)
                elif not an and p in weg:
                    weg.remove(p)
            _VERWORFEN_STAND[0] += 1
        elif aktion == "neu":                       # new playlist, optionally with samples right away
            name = _name(k.get("name"))
            if any(p["name"].casefold() == name.casefold() for p in d["playlists"]):
                raise SammlungFehler(tx("Playlist “{name}” already exists", name=name))
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
                raise SammlungFehler(tx("Playlist “{name}” already exists", name=neu))
            pl["name"] = neu
        elif aktion == "loeschen":
            pl = _playlist(d, _name(k.get("name")))
            d["playlists"].remove(pl)
        elif aktion == "ausschnitt":                # {pfad, start, ende}; start null removes it
            if len(pfade) != 1:
                raise SammlungFehler(tx("Region needs exactly one path"))
            if k.get("start") is None and k.get("ende") is None:
                d["ausschnitte"].pop(pfade[0], None)
            else:
                a, e = _sekunden(k.get("start"), "start"), _sekunden(k.get("ende"), "ende")
                if e - a < AUSSCHNITT_MIN:
                    raise SammlungFehler(tx("Region shorter than 50 ms"))
                d0 = dauer(pfade[0]) if dauer else None      # length from the index; 50 ms slack like /api/fassung
                if d0 and e > d0 + 0.05:
                    raise SammlungFehler(tx("Region goes past the end of the file"))
                d["ausschnitte"][pfade[0]] = {"start": a, "ende": e}
        elif aktion == "notiz":                     # {pfad, text}; empty text deletes the note
            if len(pfade) != 1:
                raise SammlungFehler(tx("Note needs exactly one path"))
            t = k.get("text")
            if t is not None and not isinstance(t, str):
                raise SammlungFehler(tx("Note must be text"))
            t = " ".join(_nfc(t).split())
            if len(t) > MAX_NOTIZ:
                raise SammlungFehler(tx("Note: at most {n} characters", n=MAX_NOTIZ))
            if t:
                d["notizen"][pfade[0]] = t
            else:
                d["notizen"].pop(pfade[0], None)
            _NOTIZ_STAND[0] += 1
        else:
            raise SammlungFehler(tx("unknown action"))
        _schreiben(d)
    return stand()


def pfade_fuer(filter_wert):
    """Library filter: "fav" for favorites, "pl:<name>" for a playlist (so a playlist can also be called "fav").
    Returns a set of paths."""
    return set(reihenfolge(filter_wert))


def reihenfolge(filter_wert):
    """Order of the playlist (for the sort "as in the playlist")."""
    with _LOCK:
        d = _lesen()
    name = (filter_wert or "")[3:] if (filter_wert or "").startswith("pl:") else None
    liste = d["favoriten"] if filter_wert == "fav" else d["verworfen"] if filter_wert == "verworfen" else [] if name is None else next(
        (p["pfade"] for p in d["playlists"] if p["name"].casefold() == _nfc(name).casefold()), [])
    return {p: i for i, p in enumerate(liste)}
