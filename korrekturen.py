"""Korrekturen von Hand (Paket 6): falsche Art, Loop/One-Shot, BPM, Tonart oder Lizenz einmal richtigstellen.

Gespeichert in korrekturen.json neben dem Server (OPENLIBARY_KORREKTUREN lenkt Testläufe woanders hin):
  {"eintraege": {pfad: {"md5": "...", "felder": {"bpm": null, "art": "perc", ...}, "zeit": "2026-10-24 10:12"}}}
Schlüssel ist der Pfad in NFC wie im Index, dazu die md5 der Datei: steht der Pfad nicht mehr im Index (umbenannt oder
verschoben), findet der Server die Korrektur über die md5 wieder. Beim Einlesen gewinnt die Korrektur vor Name,
Splice-Datenbank und gehoer.

Werte je Feld: ein Wert setzt ihn, null heißt „kein Wert“ (etwa eine Hat ohne BPM; zählt auch als Korrektur), ein
leerer Text "" hebt die Korrektur auf, der alte Wert kommt zurück. Jede Änderung wird sofort atomar geschrieben (erst
.neu, dann umbenennen), wie sammlungen.py.

Paket 7, Lizenz pro Pack: dieselbe Datei hält unter "packs" den Entscheid je Pack,
  {"packs": {packname: {"felder": {"lizenz": "ja", "lizenz_grund": "...", "beleg": "..."}, "zeit": "...",
                        "spaeter_bis": "2026-11-23"}}}
Der Entscheid gilt für alle Dateien des Packs; eine Einzelkorrektur der Lizenz (oben) geht vor. „später“ ist kein
Entscheid: der Pack bleibt offen und steht 30 Tage lang unten.
"""
import json
import os
import threading
import time
import unicodedata
from pathlib import Path

DATEI = Path(os.environ.get("OPENLIBARY_KORREKTUREN", Path(__file__).resolve().parent / "korrekturen.json"))
FELDER = ("art", "lo", "bpm", "tonart", "lizenz", "lizenz_grund")
LO_WERTE = ("loop", "oneshot")
LIZENZ_WERTE = ("ja", "nein", "unklar")
BPM_MIN, BPM_MAX = 40, 300
MAX_GRUND = 200
MAX_PFADE = 70000               # mehr Zeilen hat der Bestand nicht
PACK_FELDER = ("lizenz", "lizenz_grund", "beleg", "spaeter")     # Paket 7
PACK_LIZENZ = ("ja", "nein")    # je Pack nur ja oder nein; "" hebt den Entscheid auf, spaeter: true schiebt ihn auf
MAX_BELEG = 500
MAX_PACK = 300
SPAETER_TAGE = 30
_LOCK = threading.Lock()
_D = None                       # Stand im Speicher, beim ersten Zugriff gelesen
_STAND = [0]                    # zählt Änderungen


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
                raise ValueError("kein Objekt")
            for p, e in roh.get("packs", {}).items():       # Paket 7
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
            # kaputte Datei nicht überschreiben: beiseitelegen und leer weitermachen (wie sammlungen.py)
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
    """Korrekturen für den Index: {pfad: felder}. pfade: alle Pfade im Index; md5_von: {md5: [pfade]} im Index.
    Steht der gespeicherte Pfad nicht mehr im Index, gilt die Korrektur für die Dateien mit derselben md5."""
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
            if q not in aus:             # ein eigener Eintrag für den neuen Pfad geht vor
                aus[q] = dict(e["felder"])
    return aus


def pruefen(felder, arten, tonart_norm):
    """felder aus der Anfrage -> geprüfte Felder. arten: erlaubte Arten; tonart_norm: Funktion des Servers.
    Wert "" hebt die Korrektur des Felds auf, None heißt kein Wert."""
    if not isinstance(felder, dict) or not felder:
        raise KorrekturFehler("felder muss ein Objekt mit mindestens einem Feld sein")
    aus = {}
    for f, v in felder.items():
        if f not in FELDER:
            raise KorrekturFehler(f"unbekanntes Feld: {f}")
        if v == "":
            aus[f] = ""
            continue
        if f == "art":
            if not isinstance(v, str) or v not in arten:
                raise KorrekturFehler("Art unbekannt: nur Arten, die es im Bestand gibt")
        elif f == "lo":
            if v is not None and v not in LO_WERTE:
                raise KorrekturFehler("lo: loop, oneshot oder null")
        elif f == "bpm":
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v != v
                                  or not BPM_MIN <= v <= BPM_MAX):
                raise KorrekturFehler(f"BPM: Zahl von {BPM_MIN} bis {BPM_MAX} oder null")
            v = None if v is None else round(float(v), 2)
        elif f == "tonart":
            if v is not None:
                n = tonart_norm(v) if isinstance(v, str) else None
                if not n:
                    raise KorrekturFehler(f"Tonart unbekannt: {v}")
                v = n[0] + n[1]
        elif f == "lizenz":
            if v not in LIZENZ_WERTE:
                raise KorrekturFehler("Lizenz: ja, nein oder unklar")
        elif f == "lizenz_grund":
            if not isinstance(v, str):
                raise KorrekturFehler("Grund muss Text sein")
            v = " ".join(_nfc(v).split())
            if len(v) > MAX_GRUND:
                raise KorrekturFehler(f"Grund höchstens {MAX_GRUND} Zeichen")
            if not v:
                v = ""
        aus[f] = v
    return aus      # ein Grund ohne eigene Lizenz fällt in setzen() weg


def setzen(zeilen, felder, im_index=lambda p: True):
    """zeilen: [(pfad, md5)], felder: geprüft (pruefen). im_index(pfad): steht der Pfad im Index? -> {pfad: neue felder
    oder {}} für den Index. Galt für die Datei bisher ein Eintrag unter altem Pfad (über die md5), zieht er auf den
    neuen Pfad um, damit nichts davon verloren geht."""
    if len(zeilen) > MAX_PFADE:
        raise KorrekturFehler(f"höchstens {MAX_PFADE} Samples auf einmal")
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
                e = ein.pop(alt_nach_md5[md5])          # umbenannt: alter Eintrag zieht um
            e = e or {"md5": md5 or "", "felder": {}, "zeit": jetzt}
            for f, v in felder.items():
                if v == "":
                    e["felder"].pop(f, None)
                    if f == "lizenz":          # ohne eigene Lizenz kein eigener Grund
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


# ---------------------------------------------------------------- Paket 7: Lizenz pro Pack

def heute():
    return time.strftime("%Y-%m-%d")


def packs():
    """Alle Pack-Entscheide: {pack: {"felder": {...}, "zeit": "...", "spaeter_bis": "..."}} (Kopie)."""
    with _LOCK:
        return {p: {"felder": dict(e["felder"]), "zeit": e["zeit"], "spaeter_bis": e["spaeter_bis"]}
                for p, e in _laden()["packs"].items()}


def pack_felder(pack, alle=None):
    """Felder des Entscheids für die Dateien eines Packs ({} ohne Entscheid). alle: Ergebnis von packs() (spart Sperren)."""
    if not pack:
        return {}
    if alle is None:
        with _LOCK:
            e = _laden()["packs"].get(pack)
            return dict(e["felder"]) if e else {}
    e = alle.get(pack)
    return dict(e["felder"]) if e else {}


def pack_pruefen(pack, felder):
    """{pack: "...", felder: {lizenz: "ja"|"nein"|"", lizenz_grund, beleg, spaeter: true}} prüfen."""
    if not isinstance(pack, str) or not _nfc(pack).strip():
        raise KorrekturFehler("pack muss ein Packname sein")
    if len(pack) > MAX_PACK:
        raise KorrekturFehler(f"Packname höchstens {MAX_PACK} Zeichen")
    if not isinstance(felder, dict) or not felder:
        raise KorrekturFehler("felder muss ein Objekt mit mindestens einem Feld sein")
    aus = {}
    for f, v in felder.items():
        if f not in PACK_FELDER:
            raise KorrekturFehler(f"unbekanntes Feld für ein Pack: {f}")
        if f == "lizenz":
            if v != "" and v not in PACK_LIZENZ:
                raise KorrekturFehler("Lizenz für ein Pack: ja, nein oder \"\" (wieder offen)")
        elif f == "spaeter":
            if v is not True:
                raise KorrekturFehler("spaeter: nur true")
        else:
            grenze = MAX_GRUND if f == "lizenz_grund" else MAX_BELEG
            if not isinstance(v, str):
                raise KorrekturFehler(f"{'Grund' if f == 'lizenz_grund' else 'Beleg'} muss Text sein")
            v = " ".join(_nfc(v).split())
            if len(v) > grenze:
                raise KorrekturFehler(f"{'Grund' if f == 'lizenz_grund' else 'Beleg'} höchstens {grenze} Zeichen")
        aus[f] = v
    if "spaeter" in aus and aus.get("lizenz"):
        raise KorrekturFehler("ja oder nein und später zugleich geht nicht")
    if ("beleg" in aus or "lizenz_grund" in aus) and "lizenz" not in aus and "spaeter" not in aus:
        aus["_nur_text"] = True         # Beleg oder Grund nachtragen: braucht einen bestehenden Entscheid (setzen)
    return aus


def pack_setzen(pack, felder):
    """Entscheid speichern. lizenz ja/nein setzt ihn (und hebt „später“ auf), "" hebt ihn auf, spaeter: true hebt ihn
    auf und schiebt den Pack 30 Tage nach unten; Beleg und Grund allein ändern einen bestehenden Entscheid.
    -> neuer Eintrag oder None (keiner mehr)."""
    pack = _nfc(pack)
    jetzt = time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        d = _laden()
        alt = d["packs"].get(pack)
        e = {"felder": dict(alt["felder"]), "zeit": alt["zeit"], "spaeter_bis": alt["spaeter_bis"]} if alt \
            else {"felder": {}, "zeit": jetzt, "spaeter_bis": ""}
        if felder.get("_nur_text"):
            if not e["felder"].get("lizenz"):
                raise KorrekturFehler("Für dieses Pack ist noch nichts entschieden: erst ja oder nein")
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
