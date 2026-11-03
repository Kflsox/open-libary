"""Manual corrections: set a wrong type, loop/one-shot, BPM, key or license right once.

Stored in korrekturen.json next to the server (OPENLIBARY_KORREKTUREN points test runs elsewhere):
  {"eintraege": {pfad: {"md5": "...", "felder": {"bpm": null, "art": "perc", ...}, "zeit": "2026-10-24 10:12"}}}
The key is the path in NFC as in the index, plus the md5 of the file: when the path is no longer in the index (renamed
or moved), the server finds the correction again through the md5. When the index loads, the correction wins over the
name, the Splice database and gehoer.

Values per field: a value sets it, null means “no value” (say a hat without BPM; also counts as a correction), an
empty string "" removes the correction and the old value comes back. Every change is written atomically right away
(first .neu, then rename), like sammlungen.py.

License per pack: the same file holds the decision per pack under "packs",
  {"packs": {packname: {"felder": {"lizenz": "ja", "lizenz_grund": "...", "beleg": "..."}, "zeit": "...",
                        "spaeter_bis": "2026-11-23"}}}
The decision applies to all files of the pack; a license correction for a single file (above) wins. “Later” is not a
decision: the pack stays open and sits at the bottom for 30 days.
"""
import json
import os
import threading
import time
import unicodedata
from pathlib import Path

from i18n import N_, tx

DATEI = Path(os.environ.get("OPENLIBARY_KORREKTUREN", Path(__file__).resolve().parent / "korrekturen.json"))
FELDER = ("art", "lo", "bpm", "tonart", "lizenz", "lizenz_grund")
LO_WERTE = ("loop", "oneshot")
LIZENZ_WERTE = ("ja", "nein", "unklar")
BPM_MIN, BPM_MAX = 40, 300
MAX_GRUND = 200
MAX_PFADE = 70000               # the library has no more rows than this
PACK_FELDER = ("lizenz", "lizenz_grund", "beleg", "spaeter")
PACK_LIZENZ = ("ja", "nein")    # per pack only ja or nein; "" removes the decision, spaeter: true postpones it
MAX_BELEG = 500
MAX_PACK = 300
SPAETER_TAGE = 30
_LOCK = threading.Lock()
_D = None                       # state in memory, read on first access
_STAND = [0]                    # counts changes


class KorrekturFehler(ValueError):
    pass


def _nfc(s):
    return unicodedata.normalize("NFC", s or "")


def _laden():
    global _D
    if _D is not None:
        return _D
    d = {"eintraege": {}, "packs": {}}
    if DATEI.is_file():
        try:
            roh = json.loads(DATEI.read_text(encoding="utf-8"))
            if not isinstance(roh, dict) or not isinstance(roh.get("eintraege", {}), dict) \
                    or not isinstance(roh.get("packs", {}), dict):
                raise ValueError("not an object")
            for p, e in roh.get("packs", {}).items():
                if isinstance(e, dict) and isinstance(e.get("felder", {}), dict):
                    felder = {f: v for f, v in e.get("felder", {}).items() if f in PACK_FELDER and f != "spaeter"}
                    if felder.get("lizenz") not in PACK_LIZENZ:
                        felder = {}
                    spaeter = str(e.get("spaeter_bis") or "")
                    if felder or spaeter:
                        d["packs"][_nfc(p)] = {"felder": felder, "zeit": str(e.get("zeit") or ""), "spaeter_bis": spaeter}
            for p, e in roh.get("eintraege", {}).items():
                if isinstance(e, dict) and isinstance(e.get("felder"), dict):
                    felder = {f: v for f, v in e["felder"].items() if f in FELDER}
                    if felder:
                        d["eintraege"][_nfc(p)] = {"md5": str(e.get("md5") or ""), "felder": felder,
                                                   "zeit": str(e.get("zeit") or "")}
        except (OSError, ValueError):
            # do not overwrite a broken file: set it aside and go on empty (like sammlungen.py)
            DATEI.rename(DATEI.with_name(DATEI.name + f".kaputt-{int(time.time())}"))
            d = {"eintraege": {}, "packs": {}}
    _D = d
    return d


def _schreiben(d):
    tmp = DATEI.with_name(DATEI.name + ".neu")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DATEI)


def stand():
    return _STAND[0]


def anzahl():
    with _LOCK:
        return len(_laden()["eintraege"])


def fuer_index(pfade, md5_von):
    """Corrections for the index: {pfad: felder}. pfade: all paths in the index; md5_von: {md5: [pfade]} in the index.
    If the stored path is no longer in the index, the correction applies to the files with the same md5."""
    with _LOCK:
        ein = dict(_laden()["eintraege"])
    aus = {}
    for p, e in ein.items():
        if p in pfade:
            aus[p] = dict(e["felder"])
    for p, e in ein.items():
        if p in pfade or not e["md5"]:
            continue
        for q in md5_von.get(e["md5"], ()):
            if q not in aus:             # an entry for the new path itself wins
                aus[q] = dict(e["felder"])
    return aus


def pruefen(felder, arten, tonart_norm):
    """felder from the request -> checked fields. arten: allowed types; tonart_norm: the server's function.
    Value "" removes the correction of the field, None means no value."""
    if not isinstance(felder, dict) or not felder:
        raise KorrekturFehler(tx("felder must be an object with at least one field"))
    aus = {}
    for f, v in felder.items():
        if f not in FELDER:
            raise KorrekturFehler(tx("unknown field: {name}", name=f))
        if v == "":
            aus[f] = ""
            continue
        if f == "art":
            if not isinstance(v, str) or v not in arten:
                raise KorrekturFehler(tx("Unknown type: only types that exist in the library"))
        elif f == "lo":
            if v is not None and v not in LO_WERTE:
                raise KorrekturFehler(tx("lo: loop, oneshot or null"))
        elif f == "bpm":
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v != v
                                  or not BPM_MIN <= v <= BPM_MAX):
                raise KorrekturFehler(tx("BPM: a number from {min} to {max} or null", min=BPM_MIN, max=BPM_MAX))
            v = None if v is None else round(float(v), 2)
        elif f == "tonart":
            if v is not None:
                n = tonart_norm(v) if isinstance(v, str) else None
                if not n:
                    raise KorrekturFehler(tx("Unknown key: {value}", value=v))
                v = n[0] + n[1]
        elif f == "lizenz":
            if v not in LIZENZ_WERTE:
                raise KorrekturFehler(tx("License: ja, nein or unklar"))
        elif f == "lizenz_grund":
            if not isinstance(v, str):
                raise KorrekturFehler(tx("Reason must be text"))
            v = " ".join(_nfc(v).split())
            if len(v) > MAX_GRUND:
                raise KorrekturFehler(tx("Reason: at most {n} characters", n=MAX_GRUND))
            if not v:
                v = ""
        aus[f] = v
    return aus      # a reason without its own license is dropped in setzen()


def setzen(zeilen, felder, im_index=lambda p: True):
    """zeilen: [(pfad, md5)], felder: checked (pruefen). im_index(pfad): is the path in the index? -> {pfad: new
    felder or {}} for the index. If the file had an entry under an old path (found through the md5), it moves to the
    new path so that nothing of it gets lost."""
    if len(zeilen) > MAX_PFADE:
        raise KorrekturFehler(tx("at most {n} samples at once", n=MAX_PFADE))
    jetzt = time.strftime("%Y-%m-%d %H:%M:%S")
    aus = {}
    with _LOCK:
        d = _laden()
        ein = d["eintraege"]
        alt_nach_md5 = {}
        if any(p not in ein for p, _ in zeilen):
            alt_nach_md5 = {e["md5"]: p for p, e in ein.items() if e["md5"] and not im_index(p)}
        for pfad, md5 in zeilen:
            e = ein.get(pfad)
            if e is None and md5 and md5 in alt_nach_md5 and alt_nach_md5[md5] in ein:
                e = ein.pop(alt_nach_md5[md5])          # renamed: the old entry moves
            e = e or {"md5": md5 or "", "felder": {}, "zeit": jetzt}
            for f, v in felder.items():
                if v == "":
                    e["felder"].pop(f, None)
                    if f == "lizenz":          # no own reason without an own license
                        e["felder"].pop("lizenz_grund", None)
                else:
                    e["felder"][f] = v
            if "lizenz_grund" in e["felder"] and "lizenz" not in e["felder"]:
                e["felder"].pop("lizenz_grund")
            e["zeit"], e["md5"] = jetzt, md5 or e["md5"]
            if e["felder"]:
                ein[pfad] = e
            else:
                ein.pop(pfad, None)
            aus[pfad] = dict(e["felder"])
        _schreiben(d)
        _STAND[0] += 1
    return aus


# ---------------------------------------------------------------- License per pack

def heute():
    return time.strftime("%Y-%m-%d")


def packs():
    """All pack decisions: {pack: {"felder": {...}, "zeit": "...", "spaeter_bis": "..."}} (a copy)."""
    with _LOCK:
        return {p: {"felder": dict(e["felder"]), "zeit": e["zeit"], "spaeter_bis": e["spaeter_bis"]}
                for p, e in _laden()["packs"].items()}


def pack_felder(pack, alle=None):
    """Fields of the decision for the files of a pack ({} without a decision). alle: result of packs() (saves locking)."""
    if not pack:
        return {}
    if alle is None:
        with _LOCK:
            e = _laden()["packs"].get(pack)
            return dict(e["felder"]) if e else {}
    e = alle.get(pack)
    return dict(e["felder"]) if e else {}


def pack_pruefen(pack, felder):
    """Check {pack: "...", felder: {lizenz: "ja"|"nein"|"", lizenz_grund, beleg, spaeter: true}}."""
    if not isinstance(pack, str) or not _nfc(pack).strip():
        raise KorrekturFehler(tx("pack must be a pack name"))
    if len(pack) > MAX_PACK:
        raise KorrekturFehler(tx("Pack name: at most {n} characters", n=MAX_PACK))
    if not isinstance(felder, dict) or not felder:
        raise KorrekturFehler(tx("felder must be an object with at least one field"))
    aus = {}
    for f, v in felder.items():
        if f not in PACK_FELDER:
            raise KorrekturFehler(tx("unknown field for a pack: {name}", name=f))
        if f == "lizenz":
            if v != "" and v not in PACK_LIZENZ:
                raise KorrekturFehler(tx('License for a pack: ja, nein or "" (open again)'))
        elif f == "spaeter":
            if v is not True:
                raise KorrekturFehler(tx("spaeter: only true"))
        else:
            grenze = MAX_GRUND if f == "lizenz_grund" else MAX_BELEG
            if not isinstance(v, str):
                raise KorrekturFehler(tx(N_("Reason must be text") if f == "lizenz_grund" else N_("License record must be text")))
            v = " ".join(_nfc(v).split())
            if len(v) > grenze:
                raise KorrekturFehler(tx(N_("Reason: at most {n} characters") if f == "lizenz_grund"
                                         else N_("License record: at most {n} characters"), n=grenze))
        aus[f] = v
    if "spaeter" in aus and aus.get("lizenz"):
        raise KorrekturFehler(tx("ja or nein cannot be combined with spaeter"))
    if ("beleg" in aus or "lizenz_grund" in aus) and "lizenz" not in aus and "spaeter" not in aus:
        aus["_nur_text"] = True         # adding a record or reason alone: needs an existing decision (setzen)
    return aus


def pack_setzen(pack, felder):
    """Store the decision. lizenz ja/nein sets it (and cancels “later”), "" removes it, spaeter: true removes it and
    moves the pack to the bottom for 30 days; a record or reason alone changes an existing decision.
    -> new entry or None (none left)."""
    pack = _nfc(pack)
    jetzt = time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        d = _laden()
        alt = d["packs"].get(pack)
        e = {"felder": dict(alt["felder"]), "zeit": alt["zeit"], "spaeter_bis": alt["spaeter_bis"]} if alt \
            else {"felder": {}, "zeit": jetzt, "spaeter_bis": ""}
        if felder.get("_nur_text"):
            if not e["felder"].get("lizenz"):
                raise KorrekturFehler(tx("Nothing is decided for this pack yet: choose yes or no first"))
        if felder.get("spaeter"):
            e["felder"] = {}
            e["spaeter_bis"] = time.strftime("%Y-%m-%d", time.localtime(time.time() + SPAETER_TAGE * 86400))
        elif "lizenz" in felder:
            if felder["lizenz"]:
                e["felder"]["lizenz"] = felder["lizenz"]
                e["spaeter_bis"] = ""
            else:
                e["felder"] = {}
        for f in ("lizenz_grund", "beleg"):
            if f in felder and e["felder"].get("lizenz"):
                if felder[f]:
                    e["felder"][f] = felder[f]
                else:
                    e["felder"].pop(f, None)
        e["zeit"] = jetzt
        if e["felder"] or e["spaeter_bis"]:
            d["packs"][pack] = e
        else:
            d["packs"].pop(pack, None)
            e = None
        _schreiben(d)
        _STAND[0] += 1
    return e
