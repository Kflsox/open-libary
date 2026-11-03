"""Eigene Ordner (Musiker-Test 03.11.2026): Sample-Ordner in der App hinzufügen, im Hintergrund einlesen.

Vorher kam jedes Sample über die Skripte in Werkzeug/bibliothek/ in den Index, mit festen Wurzeln (Splice-Konto,
FL Studio 2025, eigene Ordner). Wer nicht Jeremias ist, hatte damit eine leere App. Hier steht die Liste der Ordner in
ordner.json, die eingelesenen Dateien in eigene.csv (Spalten wie bestand.csv, herkunft "ordner", Lizenz "unklar"). Der
Index liest eigene.csv als dritte Quelle nach bestand.csv und zusaetze.csv; was dort schon steht, wird hier nicht noch
einmal geführt. Gelesen wird nur, was ohne Rechnen geht: Länge aus dem Dateikopf (WAV, AIFF; sonst ffprobe, wenn da),
Art, Loop oder One-Shot, Tempo und Tonart aus dem Namen (netz.einordnen), md5. Messen (gehoer) und Klang-Fingerabdruck
stößt der Server danach an.

Testläufe: OPENLIBARY_ORDNER und OPENLIBARY_EIGENE lenken beide Dateien woanders hin.
"""
import csv
import hashlib
import json
import os
import re
import struct
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

import audioform
import netz
import plattform

AUDIO = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg")
SPALTEN = ["datei", "pfad", "pack", "art", "tempo", "tonart", "dauer", "samplerate", "md5", "lizenz_gefunden", "groesse",
           "geaendert", "kanaele", "bits", "format", "loop_oneshot", "art_quelle", "tempo_quelle", "tonart_quelle",
           "herkunft", "wurzel"]
MAX_DATEIEN = 200_000        # je Ordner; ein versehentlich gewählter Systemordner soll nicht stundenlang laufen
MD5_BIS = 100 << 20          # größere Dateien ohne md5 (Doppelte erkennt dann nur der Pfad)
OHNE_ORDNER = {"__MACOSX", ".Trash", ".Trashes", ".Spotlight-V100", ".fseventsd", "node_modules"}
# Ordnernamen, die eine Kategorie und kein Pack sind: dann heißt das Pack wie der hinzugefügte Ordner
KATEGORIE = re.compile(r"(?i)^(drums?|kicks?|snares?|hats?|hi[\s_-]?hats?|claps?|percs?|percussion|808s?|bass|"
                       r"cymbals?|toms?|rims?|fx|sfx|loops?|one[\s_-]?shots?|samples?|melod(y|ies|ic)|vocals?|vox|"
                       r"chords?|keys|synths?|textures?|foley|atmos?|breaks?|fills?|tops?|midi|stems?|construction kits?)$")


def nfc(s):
    return unicodedata.normalize("NFC", s)


def _riff_kopf(pfad):
    """WAV: (Dauer, Abtastrate, Kanäle, Bit) aus fmt und data, auch Float und Extensible; sonst None."""
    try:
        with open(pfad, "rb") as f:
            kopf = f.read(12)
            if len(kopf) < 12 or kopf[:4] not in (b"RIFF", b"RF64") or kopf[8:12] != b"WAVE":
                return None
            fmt = None
            while True:
                c = f.read(8)
                if len(c) < 8:
                    return None
                name, groesse = c[:4], struct.unpack("<I", c[4:])[0]
                if name == b"fmt ":
                    roh = f.read(groesse)
                    if len(roh) < 16:
                        return None
                    art, kanaele, rate, _, _, bits = struct.unpack("<HHIIHH", roh[:16])
                    if art in audioform._OGG_IN_WAV:       # FL-Werkssample: Ogg Vorbis im WAV, Länge aus dem Ogg-Strom
                        f.close()
                        dauer = audioform.ogg_in_wav_dauer(pfad, rate)
                        return (dauer, rate, kanaele, 16) if dauer else None
                    fmt = (kanaele, rate, bits)
                    if groesse % 2:
                        f.seek(1, 1)
                elif name == b"data":
                    if not fmt or not all(fmt):
                        return None
                    kanaele, rate, bits = fmt
                    if groesse == 0xFFFFFFFF:       # RF64 oder Streaming: Rest der Datei
                        groesse = os.path.getsize(pfad) - f.tell()
                    return round(groesse / (kanaele * bits / 8 * rate), 3), rate, kanaele, bits
                else:
                    f.seek(groesse + (groesse % 2), 1)
    except (OSError, struct.error, ZeroDivisionError):
        return None


def _aiff_kopf(pfad):
    return audioform.aiff_kopf(pfad)      # eigener Leser statt aifc (fehlt ab Python 3.13)


def _flac_kopf(pfad):
    """FLAC: Länge aus STREAMINFO (ohne ffprobe; sonst blieben ganze Songs ohne Länge und zählten als Loops)."""
    try:
        with open(pfad, "rb") as f:
            if f.read(4) != b"fLaC":
                return None
            kopf = f.read(4)
            if len(kopf) < 4 or kopf[0] & 0x7F != 0:
                return None
            x = int.from_bytes(f.read(34)[10:18], "big")
        rate, kanaele, bits, gesamt = x >> 44, ((x >> 41) & 7) + 1, ((x >> 36) & 31) + 1, x & ((1 << 36) - 1)
        return (round(gesamt / rate, 3) if rate and gesamt else None), rate or "", kanaele, bits
    except (OSError, ValueError):
        return None


_MP3_RATE = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
_MP3_KBPS_1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MP3_KBPS_2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)


def _mp3_kopf(pfad):
    """MP3: Länge grob aus Dateigröße und Bitrate des ersten Frames (ohne ffprobe; bei VBR ungefähr, reicht für Song
    oder Loop)."""
    try:
        groesse = os.path.getsize(pfad)
        with open(pfad, "rb") as f:
            kopf = f.read(10)
            start = 0
            if kopf[:3] == b"ID3" and len(kopf) == 10:
                start = 10 + ((kopf[6] & 0x7F) << 21 | (kopf[7] & 0x7F) << 14 | (kopf[8] & 0x7F) << 7 | (kopf[9] & 0x7F))
            f.seek(start)
            daten = f.read(64 << 10)
        for i in range(len(daten) - 4):
            if daten[i] != 0xFF or daten[i + 1] & 0xE0 != 0xE0:
                continue
            version, schicht = (daten[i + 1] >> 3) & 3, (daten[i + 1] >> 1) & 3
            bi, ri = daten[i + 2] >> 4, (daten[i + 2] >> 2) & 3
            if version == 1 or schicht != 1 or bi in (0, 15) or ri == 3:
                continue
            kbps = (_MP3_KBPS_1 if version == 3 else _MP3_KBPS_2)[bi]
            rate = _MP3_RATE[version][ri]
            kanaele = 1 if (daten[i + 3] >> 6) == 3 else 2
            # VBR: der Xing-/Info-Kopf im ersten Frame nennt die Zahl der Frames (sonst lag die Schätzung bis 50 % daneben)
            seite = (32 if kanaele == 2 else 17) if version == 3 else (17 if kanaele == 2 else 9)
            x = i + 4 + seite
            if daten[x:x + 4] in (b"Xing", b"Info") and int.from_bytes(daten[x + 4:x + 8], "big") & 1:
                frames = int.from_bytes(daten[x + 8:x + 12], "big")
                return round(frames * (1152 if version == 3 else 576) / rate, 3), rate, kanaele, ""
            if daten[i + 36:i + 40] == b"VBRI":
                frames = int.from_bytes(daten[i + 50:i + 54], "big")
                return round(frames * (1152 if version == 3 else 576) / rate, 3), rate, kanaele, ""
            return round((groesse - start) * 8 / (kbps * 1000), 3), rate, kanaele, ""
    except (OSError, ValueError, IndexError):
        pass
    return None


def _md5(pfad):
    h = hashlib.md5()
    with open(pfad, "rb") as f:
        for stueck in iter(lambda: f.read(1 << 20), b""):
            h.update(stueck)
    return h.hexdigest()


def pack_name(wurzel, pfad):
    """Erster Ordner unter der Wurzel, außer er ist eine Kategorie („Drums“, „Loops“): dann der Name der Wurzel."""
    rel = Path(pfad).relative_to(wurzel).parts
    if len(rel) >= 2 and not KATEGORIE.match(rel[0].strip()):
        return rel[0]
    return Path(wurzel).name


def pfad_pruefen(pfad):
    """-> (echter Pfad, None) oder (None, Grund). Kein Wurzelordner, kein ganzer Benutzerordner, nichts aus System."""
    if not isinstance(pfad, str) or not pfad.strip():
        return None, "Kein Ordner angegeben"
    p = os.path.realpath(os.path.expanduser(pfad.strip()))
    if not os.path.isdir(p):
        return None, "Den Ordner gibt es nicht (oder er ist eine Datei)"
    grund = plattform.gesperrter_ordner(p)      # je System: Wurzel, Benutzerordner, Systemordner
    if grund:
        return None, grund
    if not os.access(p, os.R_OK | os.X_OK):
        return None, "Keine Leserechte für diesen Ordner"
    return nfc(p), None


class Ordner:
    """Liste der eigenen Ordner und das Einlesen im Hintergrund (ein Lauf zugleich)."""

    def __init__(self, liste_datei, csv_datei, bekannt, nach_einlesen, ffprobe=None, ohne=()):
        self.liste_datei, self.csv_datei = Path(liste_datei), Path(csv_datei)
        self.bekannt = bekannt                 # () -> Menge der Pfade aus bestand.csv und zusaetze.csv
        self.nach_einlesen = nach_einlesen     # (neue Pfade) -> Index neu laden, messen lassen
        self.ffprobe = ffprobe
        self.ohne = tuple(nfc(str(x)).rstrip(os.sep) + os.sep for x in ohne)   # Ordner, die nie Samples sind (Renders)
        self.lock = threading.Lock()           # Stand, Liste und die Lauf-Schalter
        self.laeuft = False
        self.nochmal = False
        self._stand = {"laeuft": False, "aktuell": "", "gesehen": 0, "neu": 0, "fertig_um": 0.0, "fehler": "",
                       "zuletzt_neu": 0, "zuletzt_weg": 0}

    # ---------------------------------------------------------- Liste
    def liste(self):
        try:
            with open(self.liste_datei, encoding="utf-8") as f:
                d = json.load(f)
            return [o for o in d.get("ordner", []) if isinstance(o, dict) and isinstance(o.get("pfad"), str)]
        except (OSError, ValueError, AttributeError):
            return []

    def _liste_schreiben(self, ordner):
        self.liste_datei.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.liste_datei.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ordner": ordner}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.liste_datei)

    def hinzufuegen(self, pfad):
        p, grund = pfad_pruefen(pfad)
        if grund:
            return False, grund
        with self.lock:
            ordner = self.liste()
            for o in ordner:
                if p == o["pfad"] or p.startswith(o["pfad"] + os.sep):
                    return False, f"Liegt schon in „{Path(o['pfad']).name}“, das schon eingelesen wird"
            # ein übergeordneter Ordner ersetzt die darin liegenden
            ordner = [o for o in ordner if not o["pfad"].startswith(p + os.sep)]
            ordner.append({"pfad": p, "seit": time.strftime("%Y-%m-%d %H:%M")})
            self._liste_schreiben(ordner)
        self.einlesen()
        return True, f"„{Path(p).name}“ wird eingelesen"

    def entfernen(self, pfad):
        p = nfc(os.path.realpath(os.path.expanduser(pfad or "")))
        with self.lock:
            ordner = self.liste()
            rest = [o for o in ordner if o["pfad"] != p]
            if len(rest) == len(ordner):
                return False, "Diesen Ordner gibt es in der Liste nicht"
            self._liste_schreiben(rest)
        self.einlesen()
        return True, f"„{Path(p).name}“ ist raus; die Dateien selbst bleiben, wo sie sind"

    # ---------------------------------------------------------- Einlesen
    def stand(self):
        with self.lock:
            d = dict(self._stand)
        zahl = {}
        for z in self._zeilen_lesen().values():
            zahl[z.get("wurzel", "")] = zahl.get(z.get("wurzel", ""), 0) + 1
        d["ordner"] = [dict(o, dateien=zahl.get(o["pfad"], 0), da=os.path.isdir(o["pfad"])) for o in self.liste()]
        return d

    def einlesen(self):
        """Im Hintergrund; läuft schon einer, kommt danach genau ein weiterer Durchgang. Prüfen und Freigeben unter
        derselben Sperre (Prüfung 03.11.: sonst ging ein Hinzufügen genau am Laufende verloren)."""
        with self.lock:
            if self.laeuft:
                self.nochmal = True
                return
            self.laeuft, self.nochmal = True, False
            self._stand.update(laeuft=True, zuletzt_neu=0, zuletzt_weg=0)    # Zahlen gelten für den ganzen Lauf
        threading.Thread(target=self._lauf, daemon=True, name="ordner").start()

    def _lauf(self):
        while True:
            try:
                self._durchgang()
            except Exception as e:      # nie den Server mitreißen; der Fehler steht in den Einstellungen
                with self.lock:
                    self._stand.update(fehler=f"{type(e).__name__}: {e}"[:300])
            with self.lock:
                if self.nochmal:
                    self.nochmal = False
                    continue
                self.laeuft = False
                self._stand.update(laeuft=False, aktuell="", fertig_um=time.time())
                return

    def _zeilen_lesen(self):
        try:
            with open(self.csv_datei, newline="", encoding="utf-8") as f:
                return {r["pfad"]: r for r in csv.DictReader(f) if r.get("pfad")}
        except OSError:
            return {}

    def _zeilen_schreiben(self, zeilen):
        self.csv_datei.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.csv_datei.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SPALTEN, extrasaction="ignore")
            w.writeheader()
            for r in sorted(zeilen.values(), key=lambda r: r["pfad"]):
                w.writerow(r)
        os.replace(tmp, self.csv_datei)

    def _kopf(self, pfad, endung):
        k = _riff_kopf(pfad) if endung == ".wav" else _aiff_kopf(pfad) if endung in (".aif", ".aiff") else \
            _flac_kopf(pfad) if endung == ".flac" else None
        if not k and endung in (".ogg", ".oga", ".opus"):     # ohne ffprobe (Windows), sonst keine Länge
            o = audioform.ogg_kopf(pfad)
            k = (o[0], o[1], o[2], "") if o else None
        if k:
            return k
        if self.ffprobe and os.path.exists(self.ffprobe):
            m = netz.messen(pfad)
            return m["dauer"], m["samplerate"], m["kanaele"], ""
        if endung == ".mp3":
            return _mp3_kopf(pfad) or (None, "", "", "")
        return None, "", "", ""

    def _zeile(self, wurzel, pfad, st):
        name = os.path.basename(pfad)
        endung = os.path.splitext(name)[1].lower()
        dauer, rate, kanaele, bits = self._kopf(pfad, endung)
        ordner_rel = os.path.relpath(os.path.dirname(pfad), wurzel)
        e = netz.einordnen(name, "" if ordner_rel == "." else ordner_rel, dauer)
        try:
            md5 = _md5(pfad) if st.st_size <= MD5_BIS else ""
        except OSError:
            md5 = ""
        return {"datei": name, "pfad": pfad, "pack": pack_name(wurzel, pfad), "art": e["art"],
                "tempo": e["bpm"] or "", "tonart": e["tonart"], "dauer": dauer if dauer is not None else "",
                "samplerate": rate or "", "md5": md5, "lizenz_gefunden": "unklar", "groesse": st.st_size,
                "geaendert": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
                "kanaele": kanaele or "", "bits": bits or "", "format": endung.lstrip("."), "loop_oneshot": e["lo"],
                "art_quelle": "dateiname" if e["art"] else "", "tempo_quelle": "name" if e["bpm"] else "",
                "tonart_quelle": "name" if e["tonart"] else "", "herkunft": "ordner", "wurzel": wurzel}

    def _durchgang(self):
        with self.lock:
            self._stand.update(gesehen=0, neu=0, fehler="")
        alt = self._zeilen_lesen()
        bekannt = self.bekannt()
        neu_zeilen, neue_pfade = {}, []
        for o in self.liste():
            wurzel = o["pfad"]
            if not os.path.isdir(wurzel):        # Platte nicht dran: Zeilen behalten, nicht als gelöscht werten
                for p, r in alt.items():
                    if r.get("wurzel") == wurzel:
                        neu_zeilen[p] = r
                continue
            with self.lock:
                self._stand["aktuell"] = Path(wurzel).name
            gezaehlt, fehler_wurzel, cloud = 0, [], 0

            def lesefehler(e, wurzel=wurzel):
                if nfc(getattr(e, "filename", "") or "") == wurzel:
                    fehler_wurzel.append(e)
            for dp, dn, fn in os.walk(wurzel, onerror=lesefehler):
                dn[:] = sorted(d for d in dn if not d.startswith(".") and d not in OHNE_ORDNER
                               and not d.endswith((".app", ".bundle", ".logicx", ".band"))
                               and not nfc(os.path.join(dp, d)).startswith(self.ohne)
                               and nfc(os.path.join(dp, d)) + os.sep not in self.ohne)
                for f in fn:
                    if f.startswith(("._", ".")) or not f.lower().endswith(AUDIO):
                        continue
                    pfad = nfc(os.path.join(dp, f))
                    gezaehlt += 1
                    if gezaehlt > MAX_DATEIEN:
                        break
                    with self.lock:
                        self._stand["gesehen"] += 1
                    if pfad in bekannt or pfad in neu_zeilen:
                        continue
                    try:
                        st = os.stat(pfad)
                    except OSError:
                        continue
                    if plattform.nur_in_cloud(st):       # iCloud/OneDrive nur online: Lesen lüde sie erst herunter
                        if pfad in alt:                  # war schon eingelesen, Datei nur ausgelagert: Zeile bleibt
                            neu_zeilen[pfad] = alt[pfad]
                        cloud += 1
                        continue
                    r = alt.get(pfad)
                    if r and str(r.get("groesse")) == str(st.st_size) and r.get("geaendert") == time.strftime(
                            "%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)):
                        neu_zeilen[pfad] = dict(r, wurzel=wurzel)
                        continue
                    neu_zeilen[pfad] = self._zeile(wurzel, pfad, st)
                    if pfad not in alt:
                        neue_pfade.append(pfad)
                    with self.lock:
                        self._stand["neu"] += 1
                if gezaehlt > MAX_DATEIEN:
                    with self.lock:
                        self._stand["fehler"] = f"„{Path(wurzel).name}“: nur die ersten {MAX_DATEIEN:,} Dateien".replace(",", ".")
                    break
            if cloud:
                with self.lock:
                    if not self._stand["fehler"]:
                        self._stand["fehler"] = (f"„{Path(wurzel).name}“: {cloud:,} Dateien liegen nur in der Cloud und "
                                                 "fehlen; „Immer auf diesem Gerät behalten“ holt sie her").replace(",", ".")
            # Wurzel nicht lesbar (macOS fragt nach der App-Neusignatur neu) oder leer, obwohl dort Zeilen standen
            # (leerer Mountpunkt nach unsauberem Auswerfen): wie „Platte ab“, nichts als gelöscht werten (Prüfung 03.11.)
            hatte = [p for p, r in alt.items() if r.get("wurzel") == wurzel]
            if hatte and (fehler_wurzel or gezaehlt == 0):
                for p in hatte:
                    neu_zeilen.setdefault(p, alt[p])
                with self.lock:
                    self._stand["fehler"] = (f"„{Path(wurzel).name}“ ist gerade nicht lesbar" if fehler_wurzel else
                                             f"„{Path(wurzel).name}“ ist gerade leer") + "; die bisherigen Einträge bleiben"
                continue
        weg = len(set(alt) - set(neu_zeilen))
        if neu_zeilen != alt:
            self._zeilen_schreiben(neu_zeilen)
        with self.lock:
            self._stand["zuletzt_neu"] += len(neue_pfade)
            self._stand["zuletzt_weg"] += weg
        if neue_pfade or weg:
            self.nach_einlesen(neue_pfade)
