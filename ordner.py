"""Your folders: add sample folders in the app and scan them in the background.

Before, every sample reached the index through the author's scripts in Werkzeug/bibliothek/, with fixed roots (Splice
account, FL Studio 2025, own folders). Anyone else got an empty app. Here the folder list lives in ordner.json and the
scanned files in eigene.csv (columns as in bestand.csv, herkunft "ordner", license "unklar"). The index reads
eigene.csv as a third source after bestand.csv and zusaetze.csv; what is already listed there is not listed again
here. Only what needs no computing is read: length from the file header (WAV, AIFF; otherwise ffprobe, if present),
type, loop or one-shot, tempo and key from the name (netz.einordnen), md5. The server starts the analysis (gehoer) and
the sound fingerprint afterwards.

Test runs: OPENLIBARY_ORDNER and OPENLIBARY_EIGENE point both files elsewhere.
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
from i18n import current, tx

AUDIO = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg")
SPALTEN = ["datei", "pfad", "pack", "art", "tempo", "tonart", "dauer", "samplerate", "md5", "lizenz_gefunden", "groesse",
           "geaendert", "kanaele", "bits", "format", "loop_oneshot", "art_quelle", "tempo_quelle", "tonart_quelle",
           "herkunft", "wurzel"]
MAX_DATEIEN = 200_000        # per folder; a system folder chosen by mistake should not run for hours
MD5_BIS = 100 << 20          # larger files get no md5 (duplicates are then found by path only)
OHNE_ORDNER = {"__MACOSX", ".Trash", ".Trashes", ".Spotlight-V100", ".fseventsd", "node_modules"}
# Folder names that are a category, not a pack: then the pack is named after the added folder
KATEGORIE = re.compile(r"(?i)^(drums?|kicks?|snares?|hats?|hi[\s_-]?hats?|claps?|percs?|percussion|808s?|bass|"
                       r"cymbals?|toms?|rims?|fx|sfx|loops?|one[\s_-]?shots?|samples?|melod(y|ies|ic)|vocals?|vox|"
                       r"chords?|keys|synths?|textures?|foley|atmos?|breaks?|fills?|tops?|midi|stems?|construction kits?)$")


def nfc(s):
    return unicodedata.normalize("NFC", s)


def _riff_kopf(pfad):
    """WAV: (duration, sample rate, channels, bits) from fmt and data, also float and extensible; otherwise None."""
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
                    if art in audioform._OGG_IN_WAV:       # FL factory sample: Ogg Vorbis inside WAV, length from the Ogg stream
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
                    if groesse == 0xFFFFFFFF:       # RF64 or streaming: rest of the file
                        groesse = os.path.getsize(pfad) - f.tell()
                    return round(groesse / (kanaele * bits / 8 * rate), 3), rate, kanaele, bits
                else:
                    f.seek(groesse + (groesse % 2), 1)
    except (OSError, struct.error, ZeroDivisionError):
        return None


def _aiff_kopf(pfad):
    return audioform.aiff_kopf(pfad)      # own reader instead of aifc (gone since Python 3.13)


def _flac_kopf(pfad):
    """FLAC: length from STREAMINFO (without ffprobe; otherwise whole songs had no length and counted as loops)."""
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
    """MP3: rough length from file size and the bitrate of the first frame (without ffprobe; approximate for VBR, enough
    to tell song from loop)."""
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
            # VBR: the Xing/Info header in the first frame gives the number of frames (otherwise the estimate was up to 50 % off)
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
    """First folder below the root, unless it is a category (“Drums”, “Loops”): then the name of the root."""
    rel = Path(pfad).relative_to(wurzel).parts
    if len(rel) >= 2 and not KATEGORIE.match(rel[0].strip()):
        return rel[0]
    return Path(wurzel).name


def pfad_pruefen(pfad):
    """-> (real path, None) or (None, reason). No root folder, no whole user folder, nothing from the system."""
    if not isinstance(pfad, str) or not pfad.strip():
        return None, tx("No folder given")
    p = os.path.realpath(os.path.expanduser(pfad.strip()))
    if not os.path.isdir(p):
        return None, tx("This folder does not exist (or it is a file)")
    grund = plattform.gesperrter_ordner(p)      # per system: root, user folder, system folders
    if grund:
        return None, grund
    if not os.access(p, os.R_OK | os.X_OK):
        return None, tx("No permission to read this folder")
    return nfc(p), None


class Ordner:
    """List of your folders and the background scan (one run at a time)."""

    def __init__(self, liste_datei, csv_datei, bekannt, nach_einlesen, ffprobe=None, ohne=()):
        self.liste_datei, self.csv_datei = Path(liste_datei), Path(csv_datei)
        self.bekannt = bekannt                 # () -> set of paths from bestand.csv and zusaetze.csv
        self.nach_einlesen = nach_einlesen     # (new paths) -> reload the index, queue analysis
        self.ffprobe = ffprobe
        self.ohne = tuple(nfc(str(x)).rstrip(os.sep) + os.sep for x in ohne)   # folders that never hold samples (renders)
        self.lock = threading.Lock()           # state, list and the run switches
        self.laeuft = False
        self.nochmal = False
        self._stand = {"laeuft": False, "aktuell": "", "gesehen": 0, "neu": 0, "fertig_um": 0.0, "fehler": "",
                       "zuletzt_neu": 0, "zuletzt_weg": 0}

    # ---------------------------------------------------------- List
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
                    return False, tx("Already inside “{name}”, which is already being scanned", name=Path(o['pfad']).name)
            # a parent folder replaces the folders inside it
            ordner = [o for o in ordner if not o["pfad"].startswith(p + os.sep)]
            ordner.append({"pfad": p, "seit": time.strftime("%Y-%m-%d %H:%M")})
            self._liste_schreiben(ordner)
        self.einlesen()
        return True, tx("Scanning “{name}”", name=Path(p).name)

    def entfernen(self, pfad):
        p = nfc(os.path.realpath(os.path.expanduser(pfad or "")))
        with self.lock:
            ordner = self.liste()
            rest = [o for o in ordner if o["pfad"] != p]
            if len(rest) == len(ordner):
                return False, tx("This folder is not in the list")
            self._liste_schreiben(rest)
        self.einlesen()
        return True, tx("“{name}” removed; the files themselves stay where they are", name=Path(p).name)

    # ---------------------------------------------------------- Scan
    def stand(self):
        with self.lock:
            d = dict(self._stand)
        zahl = {}
        for z in self._zeilen_lesen().values():
            zahl[z.get("wurzel", "")] = zahl.get(z.get("wurzel", ""), 0) + 1
        d["ordner"] = [dict(o, dateien=zahl.get(o["pfad"], 0), da=os.path.isdir(o["pfad"])) for o in self.liste()]
        return d

    def einlesen(self):
        """In the background; if one is already running, exactly one more pass follows. Check and release under the
        same lock (otherwise a folder added right at the end of a run got lost)."""
        with self.lock:
            if self.laeuft:
                self.nochmal = True
                return
            self.laeuft, self.nochmal = True, False
            self._stand.update(laeuft=True, zuletzt_neu=0, zuletzt_weg=0)    # the numbers cover the whole run
        threading.Thread(target=self._lauf, daemon=True, name="ordner").start()

    def _lauf(self):
        while True:
            try:
                self._durchgang()
            except Exception as e:      # never take the server down; the error shows in Settings
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
        if not k and endung in (".ogg", ".oga", ".opus"):     # without ffprobe (Windows), otherwise no length
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
            if not os.path.isdir(wurzel):        # drive not connected: keep the rows, do not count them as deleted
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
                    if plattform.nur_in_cloud(st):       # iCloud/OneDrive online-only: reading would download them first
                        if pfad in alt:                  # already scanned, file only offloaded: the row stays
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
                        self._stand["fehler"] = tx("“{name}”: only the first {n} files", name=Path(wurzel).name,
                                                n=f"{MAX_DATEIEN:,}".replace(",", "." if current() == "de" else ","))
                    break
            if cloud:
                with self.lock:
                    if not self._stand["fehler"]:
                        self._stand["fehler"] = tx("“{name}”: {n} files are only in the cloud and missing; “Always keep on this device” downloads them",
                                                name=Path(wurzel).name, n=f"{cloud:,}".replace(",", "." if current() == "de" else ","))
            # Root not readable (macOS asks again after the app is re-signed) or empty although it had rows (empty mount
            # point after an unclean eject): treat it like a disconnected drive, count nothing as deleted
            hatte = [p for p, r in alt.items() if r.get("wurzel") == wurzel]
            if hatte and (fehler_wurzel or gezaehlt == 0):
                for p in hatte:
                    neu_zeilen.setdefault(p, alt[p])
                with self.lock:
                    self._stand["fehler"] = (tx("“{name}” cannot be read right now", name=Path(wurzel).name) if fehler_wurzel else
                                             tx("“{name}” is empty right now", name=Path(wurzel).name)) + tx("; the existing entries stay")
                continue
        weg = len(set(alt) - set(neu_zeilen))
        if neu_zeilen != alt:
            self._zeilen_schreiben(neu_zeilen)
        with self.lock:
            self._stand["zuletzt_neu"] += len(neue_pfade)
            self._stand["zuletzt_weg"] += weg
        if neue_pfade or weg:
            self.nach_einlesen(neue_pfade)
