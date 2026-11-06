#!/usr/bin/env python3
"""Open Libary: a local interface for your sample library, like Splice for the samples you already own.

  python3 openlibary.py [--port 8765] [--ohne-browser]

Sources (read only, none of them is changed):
  - Bibliothek/bestand.csv in the music project folder (lokal.json "musik"; all own samples plus checked Web packs)
  - the gehoer database ~/.cache/gehoer/bibliothek.sqlite (analyzed BPM, key, bars, swing, halftime, instruments,
    style, one-shot class) once it exists, and next to it openlibary_netz.sqlite (see analysis)
  - zusaetze.csv next to this script: what Open Libary itself downloaded from the Web (netz.py)
  - _herkunft.csv of a sorted sample collection (lokal.json "library_neu"): swing, gefuehl, live (via md5)

Written are only: zusaetze.csv, downloaded files under Samples/Frei (Netz)/ (with LIZENZ.txt and QUELLE.txt) and,
through gehoer bibliothek scan, Open Libary's own gehoer database openlibary_netz.sqlite: every finished Web download and
every click on "Analyze" (a separate file, no competition with the full scan). Open Libary never writes into gehoer's
main database (since the evening of 20.10.2026, see Messdienst). Also rendered versions under
Samples/Open Libary Renders/ (environment OPENLIBARY_RENDERS) with their list .renders.json.
Test runs: OPENLIBARY_NETZ_ZIEL and OPENLIBARY_ZUSAETZE send downloads to another folder, GEHOER_BIBLIOTHEK the
main database (openlibary_netz.sqlite then sits next to it, OPENLIBARY_NETZ_DB sets it on its own), OPENLIBARY_MESS_PRIO
the prefix before gehoer (default "nice -n 10").

Standard library only, runs with the Python 3.9 of macOS. The server listens only on 127.0.0.1 and only accepts
requests addressed to its own host (protection against other web pages in the browser, found in a test on 19.10.2026).
"""
import argparse
import collections
import csv
import hashlib
import html
import json
import math
import mimetypes
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import socket
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Packaged app: the code lives in the app bundle, user data under OPENLIBARY_DATEN (the app sets
# ~/Library/Application Support/Open Libary). sammlungen, verlauf and korrekturen read their path on import, so this
# comes before the imports. Without OPENLIBARY_DATEN everything stays next to the code (the author's setup, einrichten.sh).
_DATEN = os.environ.get("OPENLIBARY_DATEN")
if _DATEN:
    os.makedirs(_DATEN, exist_ok=True)
    # the packaged app stands on its own: even on the author's Mac no mixing with his library, gehoer and sound
    # database (otherwise he saw his library without favorites, and downloads went into his Frei (Netz))
    for _var, _datei in (("OPENLIBARY_SAMMLUNGEN", "sammlungen.json"), ("OPENLIBARY_VERLAUF", "verlauf.json"),
                         ("OPENLIBARY_KORREKTUREN", "korrekturen.json"), ("GEHOER_BIBLIOTHEK", "gehoer.sqlite"),
                         ("OPENLIBARY_NETZ_DB", "netz.sqlite"), ("OPENLIBARY_KLANG_DB", "klang.sqlite"),
                         ("OPENLIBARY_GEHOER", "kein-gehoer"), ("OPENLIBARY_LEICHT_DB", "leicht.sqlite")):
        os.environ.setdefault(_var, os.path.join(_DATEN, _datei))

import plattform         # Mac, Windows, Linux
import audioform         # AIFF without ffmpeg, "as WAV when dragging"
import netz
import sammlungen
import verlauf          # played and dragged
import korrekturen      # type, loop/one-shot, BPM, key, license by hand
import ordner           # your own folders in the app
import klangnaehe       # sound fingerprint for "Similar"
import leicht           # tempo and root note without gehoer
import bericht          # problem reports: crashes, freezes, snapshot
import i18n             # interface language (English source, lang/<code>.json)
from i18n import tx, N_

HIER = Path(__file__).resolve().parent
DATEN = Path(_DATEN) if _DATEN else HIER        # user data (see above)


def _lokal():
    """Your own setup, not in the repo: lokal.json next to the code, for example {"musik": "~/Musik-Projekt",
    "library_neu": "~/Samples/_herkunft.csv", "gehoer_code": "~/gehoer"}. Without the file, neutral defaults apply."""
    try:
        with open(HIER / "lokal.json", encoding="utf-8") as f:
            return {k: os.path.expanduser(v) for k, v in json.load(f).items() if isinstance(v, str) and v}
    except (OSError, ValueError, AttributeError):
        return {}


LOKAL = _lokal()
# music project folder with Bibliothek/bestand.csv (the author's grown setup); packaged apps and other users have none
MUSIK = Path(os.environ.get("OPENLIBARY_MUSIK") or (LOKAL.get("musik") if not _DATEN else "") or DATEN / "kein-musikordner")
BESTAND = MUSIK / "Bibliothek/bestand.csv"
URTEILE = MUSIK / "Bibliothek/funde-teile/netz-urteile.tsv"
GEHOER_DB = Path(os.environ.get("GEHOER_BIBLIOTHEK", Path.home() / ".cache/gehoer/bibliothek.sqlite"))
# Open Libary's own gehoer database: Web downloads and "Analyze" while a full scan is running
NETZ_DB = Path(os.environ.get("OPENLIBARY_NETZ_DB", GEHOER_DB.parent / "openlibary_netz.sqlite"))
VOLLSCAN_PID = GEHOER_DB.with_name(GEHOER_DB.stem + ".vollscan.pid")    # written by gehoer/vollscan.sh
LIBRARY_NEU = Path(os.environ.get("OPENLIBARY_LIBRARY_NEU") or LOKAL.get("library_neu") or DATEN / "kein-herkunft.csv")
MESS_PRIO = os.environ.get("OPENLIBARY_MESS_PRIO", "nice -n 10").split()
ZUSAETZE = Path(os.environ.get("OPENLIBARY_ZUSAETZE", DATEN / "zusaetze.csv"))   # test runs: another place
# your own folders, added in the app (ordner.py)
ORDNER_LISTE = Path(os.environ.get("OPENLIBARY_ORDNER", DATEN / "ordner.json"))
EIGENE = Path(os.environ.get("OPENLIBARY_EIGENE", DATEN / "eigene.csv"))
# Without a music project folder: downloads and renders under ~/Music/Open Libary
ABLAGE = Path.home() / "Music/Open Libary"
_EIGENE_ABLAGE = MUSIK.is_dir()
NETZ_ZIEL = Path(os.environ.get("OPENLIBARY_NETZ_ZIEL", MUSIK / "Samples/Frei (Netz)" if _EIGENE_ABLAGE else ABLAGE / "Netz"))
DRUM_ZIEL = Path(os.environ.get("OPENLIBARY_DRUM_ZIEL",
                                MUSIK / "Drum Kits/Frei (Netz)" if _EIGENE_ABLAGE else ABLAGE / "Netz Drums"))   # drum one-shots
GEHOER = os.environ.get("OPENLIBARY_GEHOER") or shutil.which("gehoer") or str(Path.home() / ".local/bin/gehoer")
FFMPEG = plattform.werkzeug("ffmpeg") or "ffmpeg"
GEHOER_PY = Path.home() / ".local/share/gehoer/venv/bin/python"
KLANG_DB = Path(os.environ.get("OPENLIBARY_KLANG_DB", GEHOER_DB.parent / "openlibary_klang.sqlite"))
LEICHT_SWIFT = Path(os.environ.get("OPENLIBARY_LEICHT", HIER / "app/leichtmessen"))     # Mac without numpy
LEICHT_DB = Path(os.environ.get("OPENLIBARY_LEICHT_DB", plattform.datenordner() / "leicht.sqlite"))
KLANG_AUTO_MAX = 2000        # new files of a folder that get a fingerprint on their own (0.2 s per file)
GEHOER_CODE = Path(os.environ.get("OPENLIBARY_GEHOER_CODE") or LOKAL.get("gehoer_code") or Path.home() / ".local/share/gehoer/code")
WANDEL_CACHE = Path(tempfile.gettempdir()) / "openlibary-wandel"
WANDEL_MAX_BYTES = 1024 ** 3        # the oldest conversions are dropped when the cache grows bigger
PORT = 8765
CODE_STAND = os.path.getmtime(__file__)     # packaged app: detects an old server after an update (--ersetzen)
VERSION = "1.2.1"           # the same as app/fenster/package.json (the smoke test checks it)
# Keys and logins (Freesound): not in the code folder, so that folder can be passed on
KONFIG_ORDNER = Path(os.environ.get("OPENLIBARY_KONFIG", plattform.konfigordner()))
# /favicon.ico (before: a 404 in every console): four bars of a waveform, embedded
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="2" fill="#1f2a44"/>'
           b'<g fill="#e8e4d8"><rect x="2" y="6" width="2" height="4"/><rect x="5" y="3" width="2" height="10"/>'
           b'<rect x="8" y="5" width="2" height="6"/><rect x="11" y="2" width="2" height="12"/></g></svg>')
STILE = HIER / "stile"
STIL_NAMEN = {   # order = order in Settings; texts for Settings
    "glas": ("Liquid Glass", N_("On its own all glass, otherwise header and player.")),   # short: two lines on the card
    "kobalt": (N_("Cobalt"), N_("Light, filters and player in strong cobalt blue, the list black on white.")),
    "kobalt-lila": (N_("Violet"), N_("Like Cobalt, only in violet.")),
    "partitur": (N_("Score"), N_("Light like music paper, vermilion as the only color, names in a serif font.")),
    "tracker": ("Tracker", N_("Dark bottle green, everything in monospace like an old music tracker.")),
    "werkstatt-alt": (N_("Workshop"), N_("Dark brown with amber, the first draft.")),
}
KEIN_STIL = {"grundriss", "glasschicht"}   # building blocks, not selectable styles


def stil_normal(text):
    """stil.txt since 24.10.2026: "glas" (glass only), "kobalt" (no glass) or "glas+kobalt" (color style with glass
    layer). Old values (one name) still work. Returns the cleaned value, or None when it is not usable."""
    teile = [t for t in re.split(r"[+\s,]+", str(text or "").strip().lower()) if t]
    if not teile or any(not re.match(r"^[a-z0-9-]+$", t) or t in KEIN_STIL or not (STILE / f"{t}.css").is_file() for t in teile):
        return None
    farben = [t for t in teile if t != "glas"]
    if len(farben) > 1:
        return None
    if not farben:
        return "glas"
    return ("glas+" if "glas" in teile else "") + farben[0]


def stil_aktuell():
    datei = DATEN / "stil.txt"
    return stil_normal(datei.read_text(encoding="utf-8") if datei.is_file() else "") or "kobalt"


def sprache_gespeichert():
    """Interface language from sprache.txt next to stil.txt (English when missing)."""
    datei = DATEN / "sprache.txt"
    try:
        return i18n.normal(datei.read_text(encoding="utf-8") if datei.is_file() else "")
    except OSError:
        return i18n.DEFAULT


def seite_html():
    """seite.html with the chosen style and language filled in; the translation table travels inside the page, so the
    first paint is already in the right language."""
    tabelle = json.dumps(i18n.table(i18n.current()), ensure_ascii=False).replace("<", "\\u003c")
    return ((HIER / "seite.html").read_text(encoding="utf-8").replace("__STIL__", stil_aktuell())
            .replace("__LANG__", i18n.current()).replace("/*__TRANSLATION__*/{}", tabelle))


def stile_liste():
    """All style files except grundriss and glasschicht, with the description from the header comment and the main colors."""
    namen = list(STIL_NAMEN) + sorted(p.stem for p in STILE.glob("*.css") if p.stem not in STIL_NAMEN and p.stem not in KEIN_STIL)
    aus = []
    for name in namen:
        datei = STILE / f"{name}.css"
        if not datei.is_file():
            continue
        text = datei.read_text(encoding="utf-8")
        kopf = re.match(r"/\*\s*(.*?)\*/", text, re.S)
        beschr = re.sub(r"\s+", " ", kopf.group(1)).strip() if kopf else ""
        beschr = re.sub(r"^[^:]{1,40}:\s*", "", beschr)
        farben = {}
        for var in ("grund", "leiste", "signal", "text", "fuss", "glas-probe"):   # glas-probe: capsule in the preview with glass on
            m = re.search(r"--" + var + r":\s*(#[0-9a-fA-F]{3,8})", text)
            if m:
                farben[var] = m.group(1)
        titel, text_kurz = STIL_NAMEN.get(name, (name, beschr))
        aus.append({"name": name, "titel": tx(titel), "beschreibung": tx(text_kurz), "farben": farben})
    return aus

TONARTEN = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
_GLEICH = {"Db": "C#", "D#": "Eb", "Gb": "F#", "G#": "Ab", "A#": "Bb", "Cb": "B", "Fb": "E", "E#": "F", "B#": "C"}
GRUNDTON_ARTEN = {"808", "bass"}
# Tonal one-shots show their root note as a note with octave ("G1"), only with lo "oneshot";
# loops of these types keep the key. Type from the library or gehoer's one-shot class.
GRUNDTON_ONESHOT = {"kick", "snare", "tom", "perc", "808", "bass"}


def grundton_zeile(z):
    """Does this row show and filter the root note instead of a key?"""
    return z["art"] in GRUNDTON_ARTEN or (z["lo"] == "oneshot" and (z["art"] in GRUNDTON_ONESHOT
                                                                    or z.get("klasse") in GRUNDTON_ONESHOT))


def pegel_wert(v, lo, hi):
    """Measured value only within a plausible range (gehoer has single outliers like 739 LUFS), otherwise None."""
    return round(float(v), 1) if v is not None and lo <= v <= hi else None
GENRE_WOERTER = {"drill", "trap", "grime", "ukg", "garage", "house", "techno", "dnb", "jungle", "lofi", "boombap", "afro",
                 "afrobeat", "amapiano", "reggaeton", "dancehall", "jersey", "memphis", "phonk", "rnb", "soul", "funk",
                 "jazz", "dubstep", "hiphop", "trance", "breakbeat", "footwork", "baile", "cumbia", "dembow", "hyperpop"}    # mostly carry only a root note, no major or minor
LIZENZ_ERKLAERUNG = {
    "ja": N_("License read and free to use, commercially too"),
    "unklar": N_("Source, purchase record or account holder unclear; clear it up before selling"),
    "nein": N_("Not usable according to the review (the license forbids it or the source is unlawful)"),
    "ungeprüft": N_("Downloaded yourself from the Web; the uploader entered the license, the source is not checked"),
    "unbekannt": N_("No license information found"),
}
SPLICE_GRUND = N_("The Splice license applies only to the account holder and cannot be transferred")


def nfc(s):
    return unicodedata.normalize("NFC", s)


def tonart_norm(t):
    """'F#m', 'Gbm', 'A min', 'f#min', 'C' -> (root note from TONARTEN, 'm' or '') or None."""
    if not t:
        return None
    m = re.match(r"^\s*([A-Ga-g])\s*(#|b|sharp|flat)?\s*(m|min|minor|moll|maj|major|dur)?\s*$", t.strip(), re.I)
    if not m:
        return None
    vor = (m.group(2) or "").lower()
    ton = m.group(1).upper() + ("#" if vor in ("#", "sharp") else "b" if vor in ("b", "flat") else "")
    ton = _GLEICH.get(ton, ton)
    if ton not in TONARTEN:
        return None
    return ton, "m" if (m.group(3) or "").lower() in ("m", "min", "minor", "moll") else ""


def camelot_nachbarn(ton, modus):
    """Compatible keys: the same one, the relative major or minor, a fifth up and down (Camelot ±1)."""
    pc = TONARTEN.index(ton)
    if modus == "m":
        roh = [(pc, "m"), ((pc + 7) % 12, "m"), ((pc + 5) % 12, "m"), ((pc + 3) % 12, "")]
    else:
        roh = [(pc, ""), ((pc + 7) % 12, ""), ((pc + 5) % 12, ""), ((pc + 9) % 12, "m")]
    return {TONARTEN[p] + m for p, m in roh}


def zahl(s):
    try:
        w = float(s) if s not in (None, "") else None
    except (ValueError, TypeError):
        return None
    return None if w is None or w != w or w in (float("inf"), float("-inf")) else w   # nan and inf count as empty


# ================================================================ gehoer values: swing, bars, one-shot class

SWING_KLASSEN = ["gerade", "leicht", "deutlich", "triole"]
# Left column (24.10.2026): "Swing" combines light and strong (swing=swing), in swing_nicht too
SWING_GRUPPEN = {"swing": ("leicht", "deutlich")}
# Seven fixed genres (parameters genre, genre_nicht): OR over related vibe words; the tags in the library stay
GENRE_GRUPPEN = {"grime": ("grime",), "ukg": ("ukg", "garage"), "trap": ("trap",), "drill": ("drill",),
                 "house": ("house",), "dnb": ("dnb", "jungle"), "hiphop": ("hiphop", "boombap")}
WORT_GENRE = {w: k for k, ws in GENRE_GRUPPEN.items() for w in ws}      # counting in one pass
TAKTE_CHIPS = [1, 2, 4, 8, 16]
TAKTE_GERECHNET = {1, 2, 3, 4, 6, 8, 12, 16, 24, 32}   # loop lengths that count when computed from length and tempo
# One-shot classes in the order of the filters; gehoer writes the same words (klang.KLASSEN, which also has
# fx, vocal and bass, which are no class here)
EINZELSCHLAG = ["kick", "snare", "clap", "hat", "cymbal", "perc", "tom", "rim", "808", "melodisch"]
# gehoer also writes the category from the name into oneshot_klasse, for "Kick Drum #1" e.g. kickloop (test 20.10.)
KLASSE_AUS_GEHOER = {"kickloop": "kick"}
# Kicks from the name only up to this length: longer ones are mostly 808-like kicks with a long tail, impacts or racks
# with several hits (test 20.10.: 42 "kicks" over 2 s, one of them 28 s long); analyzed ones count anyway
KICK_MAX_S = 2.0
# Halftime means "snare on beat 3": only for drum loops (breaks, tops, fills). Test 20.10.: a Rhodes loop without a
# snare was listed as halftime
GEFUEHL_ARTEN = {"break", "top", "fill", "perc", "hat"}
SONG_AB_S = 90.0            # longer: a full song, not a loop
SONG_BEHAELT_ART = {"vocal", "fx", "atmo", "foley"}     # long acapellas and field recordings are not songs
# gehoer's produzenten_begriffe come from a sound model, independent of its one-shot class. For one-shots with a
# known class, hit terms only count when they match the class (a bongo sample, analyzed as perc, carried the term
# kick and stood at the top for "kick")
BEGRIFF_KLASSE = {"kick": "kick", "snare": "snare", "clap": "clap", "hi-hat": "hat", "open hat": "hat",
                  "cymbal": "cymbal", "percussion": "perc", "shaker": "perc", "tom": "tom", "rim": "rim", "808": "808"}
KLASSE_VERWANDT = {"kick": {"808"}, "808": {"kick"}, "hat": {"cymbal"}, "cymbal": {"hat"}, "clap": {"snare"},
                   "snare": {"clap", "rim"}, "rim": {"snare", "perc"}, "tom": {"perc"}, "perc": {"tom", "rim"}}
UNSICHER = 0.5              # like gehoer: below this a "?" follows the value
BEZUG_TOLERANZ = 0.02       # swing and halftime only count at the tempo gehoer analyzed them at (2 %)
# gehoer's watchdog (bibliothek.py SCAN_FRIST_S, SCAN_FRIST_JE_S, SCAN_ZUSCHLAG_S, ALLEIN_S): per file 120 s plus 4 s
# per second of audio, the first one of a process 600 s longer (loading models); files from 300 s on the full scan runs alone
GEHOER_FRIST_S, GEHOER_FRIST_JE_S, GEHOER_ZUSCHLAG_S, GEHOER_ALLEIN_S = 120.0, 4.0, 600.0, 300.0


def swing_klasse(x):
    """Position of the off-beat eighths (gehoer swing, 0.50 straight to 0.67 triplet) -> class: gerade up to 0.55,
    leicht 0.56 to 0.58, deutlich 0.59 to 0.64, triole from 0.65 (rounded to two decimals)."""
    if x is None:
        return None
    x = round(x, 2)
    return "gerade" if x <= 0.55 else "leicht" if x <= 0.58 else "deutlich" if x <= 0.64 else "triole"


def _sicherheit(text, name):
    """'Sicherheit swing 0.86, halftime 0.85, live 0.80' (stimmung_quelle of the Library-Neu) -> 0.86 for 'swing'."""
    m = re.search(r"\b" + name + r" (\d\.\d+)", text or "")
    return float(m.group(1)) if m else None


# ================================================================ Words

_SYNONYME = [   # both sides (index and search) run through this
    (r"hi[\s_-]?hats?|\bhh\b", " hihat hat "), (r"lo[\s_-]?fi", " lofi "), (r"boom[\s_-]?bap", " boombap "),
    (r"one[\s_-]?shots?", " oneshot "), (r"\bvox\b", " vocal "), (r"\bperc\b|percussion", " perc percussion "),
    (r"shaker|conga|bongo|tambourine|cowbell|clave", r" perc \g<0> "),
    (r"\bfx\b|\bsfx\b", " fx "), (r"\bkicks\b", " kick "), (r"\bsnares\b", " snare "), (r"\bclaps\b", " clap "),
]


def woerter(text):
    t = nfc(text).casefold()
    for rx, ersatz in _SYNONYME:
        t = re.sub(rx, ersatz, t)
    return re.findall(r"[^\W\d_]+|\d+", t)


def stimmung_woerter(text):
    """Vibe words like woerter, but decades stay whole: "70s", "90's", "2000s" instead of "70", "90", "2000" (test
    20.10.: the vibes "70" and "90" looked like a tempo next to the BPM column); "1970s" becomes "70s"."""
    w, aus, i = woerter(text), [], 0
    while i < len(w):
        if i + 1 < len(w) and w[i + 1] == "s" and re.fullmatch(r"(?:19|20)?\d0", w[i]):
            aus.append((w[i][2:] if len(w[i]) == 4 and w[i].startswith("19") else w[i]) + "s")
            i += 2
        else:
            aus.append(w[i])
            i += 1
    return aus


# ================================================================ Index

class Index:
    """All samples in memory (around 63,000 rows), plus analysis values from gehoer. Reloading runs in the background;
    until it is done the old state answers (before, every concurrent request loaded on its own: peaks of 8.5 s)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lade_lock = threading.Lock()      # at most one reload in the background (aktuell_halten)
        self.lauf_lock = threading.Lock()      # reloads one after another, never two at once (laden)
        self.zeilen, self.nach_pfad, self.md5, self.packs = [], {}, set(), []
        self.pack_menge = set()
        self.arten = []            # types in the library (choice when editing)
        self.stempel = self.fehler = None
        self.gemessen = 0
        self.abdeckung = {}
        self.version = 0           # number of the last fully loaded state
        self.gestartet = 0         # number of the last started one (Messdienst: values are there once version is greater)
        self.naechster_versuch = 0.0
        self.unlesbar_runden = 0
        self.urteile = self._urteile_lesen()
        self.laden()

    @staticmethod
    def _urteile_lesen():
        if not URTEILE.is_file():
            return {}
        with open(URTEILE, newline="", encoding="utf-8") as f:
            return {r["pack"]: r for r in csv.DictReader(f, delimiter="\t")}

    def _stempel(self):
        def mt(p):
            try:
                return p.stat().st_mtime
            except OSError:
                return None
        # the first two count at once, the rest (gehoer databases, Library-Neu) at most once per minute
        return (mt(BESTAND), mt(ZUSAETZE), mt(EIGENE), mt(GEHOER_DB), mt(Path(str(GEHOER_DB) + "-wal")),
                mt(NETZ_DB), mt(Path(str(NETZ_DB) + "-wal")), mt(LIBRARY_NEU))

    def aktuell_halten(self):
        neu = self._stempel()
        # the rows carry texts (license reasons, warnings) in the language of the last load: reload after a switch
        # (not while a load runs: the switch in /api/sprache is loading right now, a second one would only cost time)
        sprache_alt = getattr(self, "sprache", None) != i18n.current() and not self.lauf_lock.locked()
        if (neu == self.stempel and not sprache_alt) or time.time() < self.naechster_versuch:
            return
        # When only a gehoer database changes (the full scan writes for hours) or the Library-Neu list, reload at most
        # once per minute; your own downloads (zusaetze.csv) and bestand.csv at once
        if self.stempel and neu[:3] == self.stempel[:3] and not sprache_alt \
                and time.time() - getattr(self, "geladen_um", 0) < 60:
            return
        if self.lade_lock.acquire(blocking=False):   # exactly one loads, in the background
            def lauf():
                try:
                    self.laden()
                finally:
                    self.lade_lock.release()
            threading.Thread(target=lauf, daemon=True).start()

    def laden(self):
        """Rebuild the index. One after another (lauf_lock): before, aktuell_halten, netz.py and the Messdienst could
        load at the same time, and an older run that finished last overwrote newer analysis values (check 20.10.)."""
        with self.lauf_lock:
            self.gestartet += 1
            self._laden(self.gestartet)

    def _laden(self, nr):
        stempel = self._stempel()
        sprache = i18n.current()       # the texts built into the rows follow this language (aktuell_halten)
        # gehoer databases first: if one is not readable, the old state stays (otherwise the list would stand there
        # without any analysis value, check 20.10.), at most three times in a row; after that, go on without it
        reihen, unlesbar = self._gehoer_reihen()
        if unlesbar:
            self.naechster_versuch = time.time() + 30
            self.unlesbar_runden += 1
            if self.zeilen and self.unlesbar_runden <= 3:
                print(f"Index keeps the old state, not readable: {', '.join(unlesbar)}", flush=True)
                return
        else:
            self.unlesbar_runden = 0
        zeilen, nach_pfad, md5, fehler = [], {}, set(), None
        quellen = []
        # zusaetze.csv first. Web downloads sit in folders that the library scripts read too; after the library was
        # rebuilt on 03.11. three Freesound CC0 sounds stood there with "nein" and hid source, author and record from
        # zusaetze.csv (same path: the first source wins)
        if ZUSAETZE.is_file():
            quellen.append((ZUSAETZE, "openlibary"))
        if BESTAND.is_file():
            quellen.append((BESTAND, None))
        elif BESTAND.parent.is_dir():      # without a library folder (friends) this is not an error, just empty
            fehler = tx("bestand.csv is missing ({pfad})", pfad=BESTAND)
        if EIGENE.is_file():               # your own folders from the app
            quellen.append((EIGENE, "ordner"))
        render_praefix = nfc(str(RENDER_ZIEL)) + os.sep
        for datei, herkunft in quellen:
            with open(datei, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    pfad = nfc(os.path.expanduser(r.get("pfad", "")))
                    if r.get("md5"):
                        md5.add(r["md5"])
                    if not pfad or pfad in nach_pfad:
                        continue
                    if pfad.startswith(render_praefix):    # rendered versions are not new samples
                        continue
                    z = self._aus_bestand(r, pfad, herkunft)
                    z["_eigen"] = datei == EIGENE        # ordner.py only lists what is not already in the index otherwise
                    nach_pfad[pfad] = z
                    zeilen.append(z)
        self._doppelte_markieren(zeilen)
        # A copy of a library pack in one of your own folders (backup, downloads) does not show up twice; it counts as
        # a copy of the library row (whose license applies) and is visible when duplicates are shown separately
        fremd_md5 = {}
        for z in zeilen:
            if z["md5"] and not z["_eigen"]:
                fremd_md5.setdefault(z["md5"], z)
        for z in zeilen:
            if z["_eigen"] and z["md5"] and z["md5"] in fremd_md5:
                o = fremd_md5[z["md5"]]
                if not os.path.exists(o["pfad"]):     # the library row points to nothing: your own one stays visible
                    o["ist_kopie"] = True
                    fremd_md5[z["md5"]] = z
                    continue
                z["ist_kopie"] = True
                o["kopien"] = o.get("kopien", 0) + 1
            elif z["_eigen"] and z["md5"]:      # the same file several times in your own folders (kits)
                fremd_md5[z["md5"]] = z
        gemessen = self._gehoer_einlesen(nach_pfad, reihen)
        self._leicht_einlesen(zeilen)          # only fills gaps, manual values and gehoer come first
        if unlesbar:   # without the unreadable database: leave the stamp open, try again in 30 s at the earliest
            stempel = None
        self._library_neu_einlesen(zeilen)
        self._live_aus_tags(zeilen)
        # Manual corrections win over name, Splice database and gehoer (before bars and search words)
        md5_von = collections.defaultdict(list)
        for z in zeilen:
            if z["md5"]:
                md5_von[z["md5"]].append(z["pfad"])
        einzeln = korrekturen.fuer_index(nach_pfad, md5_von)
        pack_ent = korrekturen.packs()      # decision per pack; single corrections come first
        for z in zeilen:
            felder = einzeln.get(z["pfad"])
            if felder is not None or (pack_entscheidbar(z) and korrekturen.pack_felder(z["pack"], pack_ent)):
                korrektur_anwenden(z, felder or {}, pack_ent)
        self._takte_rechnen(zeilen)
        for z in zeilen:
            z["begriffe"] = self._begriffe_zur_klasse(z)
            woerter_setzen(z)
        packs = sorted({z["pack"] for z in zeilen if z["pack"]}, key=str.casefold)
        abdeckung = self._abdeckung(zeilen)
        arten = sorted({z["art"] for z in zeilen if z["art"]} | {"song"})
        with self.lock:
            self.arten = arten
            self.zeilen, self.nach_pfad, self.md5, self.packs = zeilen, nach_pfad, md5, packs
            # pack decisions only for packs in the index, without the folders of single sounds
            self.pack_menge = {z["pack"] for z in zeilen if pack_entscheidbar(z)}
            self.stempel, self.fehler, self.gemessen, self.abdeckung = stempel, fehler, gemessen, abdeckung
            self.version = nr
            self.sprache = sprache
            self.geladen_um = time.time()
        SUCH_CACHE.clear()

    _AUSGESCHRIEBEN = re.compile(r"(?i)(?<![A-Za-z])([A-G])[ _-]?(sharp|flat)(?:[ _-]?(min|maj|minor|major|m))?(?![a-z])")
    _ONESHOT_ORDNER = re.compile(r"(?i)one[\s_-]?shots?|\bhits?\b|808s|\bkicks\b|\bsnares\b|\bhats\b|\bclaps\b|"
                                 r"\bpercs?\b|open hats|closed hats|\bdrums?\b(?!.*loop)")
    _BPM_IM_NAMEN = re.compile(r"(?:^|[\s_-])(\d{2,3})(?=[\s_-]+(?:[A-G](?:#|b)?\s?(?:m|min|maj|major|minor)?)(?:[\s_.-]|$))")

    def _aus_bestand(self, r, pfad, herkunft):
        tn = tonart_norm(r.get("tonart"))
        m = self._AUSGESCHRIEBEN.search(r.get("datei") or "")
        if m:   # "D Sharp Min" was read by bestand.csv as "D" (major); the name wins
            tn = tonart_norm(m.group(1) + m.group(2) + (m.group(3) or ""))
        art = r.get("art") or ""
        lo = r.get("loop_oneshot") or ""
        name = r.get("datei") or os.path.basename(pfad)
        dauer = zahl(r.get("dauer"))
        bpm, bpm_quelle = zahl(r.get("tempo")), r.get("tempo_quelle") or ("name" if r.get("tempo") else "")
        ordner = os.path.dirname(pfad)
        # ACID chunk: FL writes 140 into it by default, one-shots too (test 19.10.2026: 966 values, 421 times 140,
        # hat "loops" under 2 s). Only counts when the file is at least one bar long and not in a one-shot folder.
        if bpm_quelle == "acid_block" and bpm:
            takt = 240.0 / bpm
            if (dauer is not None and dauer < takt * 0.95) or (
                    self._ONESHOT_ORDNER.search(os.path.basename(ordner)) and "loop" not in (name + ordner).lower()):
                bpm, bpm_quelle, lo = None, "", "oneshot"
        if art == "808" and not lo and (dauer or 0) < 8:
            lo = "oneshot"
        # BPM before a key in the name ("1005 STARTER 140 GM", "_130-Amin"), loops only
        if bpm is None and lo != "oneshot":
            b = self._BPM_IM_NAMEN.search(os.path.splitext(name)[0])
            if b and 60 <= int(b.group(1)) <= 200:
                bpm, bpm_quelle = float(b.group(1)), "name"
        stimmung = []
        for teil in re.split(r"[;,|]", r.get("stimmung") or ""):
            for w in stimmung_woerter(teil):   # "vinyl lofi" are two vibes, "live 70s" live and 70s
                if w not in stimmung and len(w) > 1:
                    stimmung.append(w)
        # inherit the genre from pack and folder names (test 19.10.: your own drill kits carried no "drill")
        for w in woerter(" ".join([r.get("pack") or ""] + list(Path(os.path.dirname(pfad)).parts[-3:]))):
            if w in GENRE_WOERTER and w not in stimmung:
                stimmung.append(w)
        # Vocal entries whose name names an instrument ("91V_HT_145_drum_top_loop"): type from the name
        if art == "vocal" and re.search(r"(?i)guitar|piano|keys|drum|perc|synth|\bbass\b|strings", name) \
                and not re.search(r"(?i)vocal|vox|voice|chop|acapella", name):
            art = netz.einordnen(name, "", dauer)["art"] or art
        # full songs (over 90 s) are neither loop nor break: type "song", lo empty (a whole beat as MP3, 226 s, was
        # listed as a break loop). Acapellas, FX and atmos keep their type.
        if dauer is not None and dauer > SONG_AB_S:
            lo = ""
            if art not in SONG_BEHAELT_ART:
                art = "song"
        herkunft = herkunft or r.get("herkunft") or "ordner"
        lizenz = r.get("lizenz_gefunden") or "unbekannt"
        pack = r.get("pack") or ""
        if pack in self.urteile:
            grund = self.urteile[pack]["grund"]
        elif herkunft == "splice" and lizenz == "unklar":
            grund = tx(SPLICE_GRUND)
        else:
            grund = tx(LIZENZ_ERKLAERUNG.get(lizenz, ""))
            if r.get("hinweis"):   # downloaded yourself: license of the source, attribution, preview note
                grund += ". " + r["hinweis"]
        warnung = r.get("warnung") or ""
        if re.search(r"(?i)\bamen\b|funky[\s_-]?drummer|\bthink\b|apache|impeach|levee|skull[\s_-]?snaps", name) \
                and not warnung:
            warnung = tx("The name suggests a record break: listen first, check the license of the recording")
        z = {
            "pfad": pfad, "name": name, "pack": pack, "art": art, "lo": lo,
            "bpm": bpm, "bpm_quelle": bpm_quelle,
            "ton": tn[0] if tn else None, "modus": tn[1] if tn else None,
            "ton_quelle": r.get("tonart_quelle") or ("name" if tn else ""), "ton_unsicher": False,
            "dauer": dauer, "format": (r.get("format") or "").lower(),
            "lizenz": lizenz, "lizenz_grund": grund, "herkunft": herkunft,
            "stimmung": stimmung, "instrumente": [], "stil": [], "begriffe": [],
            "samplerate": r.get("samplerate") or "", "bits": r.get("bits") or "", "kanaele": r.get("kanaele") or "",
            "projekte": int(r.get("benutzt_in_projekten") or 0), "gemessen": False,
            "quelle_url": r.get("quelle_url") or "", "lizenz_url": r.get("lizenz_url") or "",
            "hinweis": r.get("hinweis") or "", "warnung": warnung,
            "gruppe": r.get("doppelt_gruppe") or "", "kopien": 0, "ist_kopie": False, "md5": r.get("md5") or "",
            # downloaded yourself (zusaetze.csv): for the Downloads tab
            "geladen_am": r.get("geladen_am") or "", "quelle": r.get("quelle") or "", "urheber": r.get("urheber") or "",
            # from gehoer and Library-Neu (laden): source "gemessen", "library-neu", "gerechnet" or "name"
            "takte": None, "takte_quelle": "", "bpm_gefuehl": None,
            "swing": None, "swing_wert": None, "swing_quelle": "", "swing_unsicher": False,
            "gefuehl": None, "gefuehl_quelle": "", "gefuehl_unsicher": False,     # "halftime", "normal" or None
            # "ja", "nein", "unsicher" or None; source "library-neu" (analyzed) or "tags" (vibe live, only ja)
            "live": None, "live_info": "", "live_quelle": "",
            # one-shot class: until analysis the existing category, when the file is a one-shot (kicks only short ones)
            "klasse": None, "klasse_quelle": "", "klasse_unsicher": False,
            # gehoer: root note with octave ("G1") and cents for tonal one-shots, loudness for matching levels
            "grundton": None, "grundton_cent": None, "lufs": None, "peak_dbfs": None,
            # date of the file (mtime from gehoer, else "geaendert" from bestand.csv, for downloads the download date)
            # for sorting; hand: fields corrected by hand (mark "h")
            "datum": (r.get("geaendert") or r.get("geladen_am") or "").replace("T", " ")[:16], "hand": [],
            # license from the decision for the whole pack (then it carries "h" too) and its record
            "lizenz_pack": False, "lizenz_beleg": "",
        }
        if z["datum"] > DATUM_GRENZE:      # file dates in the future (2177 in the library) count as unknown
            z["datum"] = ""
        if lo == "oneshot" and art in EINZELSCHLAG and not (art == "kick" and (dauer or 0) > KICK_MAX_S):
            z["klasse"], z["klasse_quelle"] = art, "name"
        # Name, pack and vibe count in full; of the path only the last two folders (before, "trap" found 1,629 snares
        # only because of the folder "Trap & Underground" further up)
        # the type counts separately (woerter_setzen), so that correcting the type also changes the search
        z["_wbasis"] = " ".join(woerter(" ".join([name, pack, " ".join(stimmung), r.get("tags") or ""]
                                                 + list(Path(ordner).parts[-2:]))))
        # separate fields for sorting by match quality (relevanz_punkte)
        z["_wname"] = " " + " ".join(woerter(os.path.splitext(name)[0])) + " "
        z["_wort"] = " " + " ".join(woerter(" ".join([pack] + list(Path(ordner).parts[-2:])))) + " "
        z["_wtag"] = " " + " ".join(woerter(" ".join(stimmung) + " " + (r.get("tags") or ""))) + " "
        return z

    @staticmethod
    def _doppelte_markieren(zeilen):
        """The same sound (doppelt_gruppe from bestand.csv, via the audio data) shows up only once: preferably with
        license ja, then your own folders, then the shortest path. The others count as copies."""
        gruppen = collections.defaultdict(list)
        for z in zeilen:
            if z["gruppe"]:
                gruppen[z["gruppe"]].append(z)
        rang = {"ja": 0, "ungeprüft": 1, "unklar": 2, "unbekannt": 3, "nein": 4}
        for g in gruppen.values():
            g.sort(key=lambda z: (rang.get(z["lizenz"], 5), z["herkunft"] != "ordner", len(z["pfad"])))
            g[0]["kopien"] = len(g) - 1
            g[0]["kopien_packs"] = sorted({z["pack"] for z in g[1:]})[:6]
            for z in g[1:]:
                z["ist_kopie"] = True

    GEHOER_SPALTEN = ["pfad", "bpm", "bpm_sicherheit", "bpm_gefuehl", "taktart", "takte", "swing", "swing_sicherheit",
                      "tonart_pc", "tonart_modus", "tonart_sicherheit", "art", "instrumente", "stil", "begriffe", "genre",
                      "kategorie", "oneshot_klasse", "oneshot_sicherheit", "grundton_pc", "grundton_midi", "grundton_cent",
                      "grundton_sicherheit", "lufs", "peak_dbfs", "name_bpm", "fehler", "analysiert_am",
                      "mtime",      # date of the file for sorting
                      "md5"]        # the analysis finds files that moved

    @staticmethod
    def _gehoer_lesen(db):
        """All rows of a gehoer database (read only): [] without a file, None when it is not readable. Where an older
        database lacks a column, None stands there."""
        if not db.is_file():
            return []
        try:
            # no mode=ro connection: it fails on a WAL database when no writer currently holds the -shm
            con = sqlite3.connect(str(db), timeout=5)
            try:
                con.execute("PRAGMA query_only=1")
                con.row_factory = sqlite3.Row
                da = {r[1] for r in con.execute("PRAGMA table_info(dateien)")}
                if "pfad" not in da:
                    return []
                spalten = [s if s in da else f"NULL AS {s}" for s in Index.GEHOER_SPALTEN]
                return con.execute(f"SELECT {', '.join(spalten)} FROM dateien").fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            print(f"gehoer database {db.name} not readable: {e}", flush=True)
            return None

    @staticmethod
    def _gehoer_reihen():
        """Rows of the gehoer main database and of our own (NETZ_DB). -> ([rows per readable database], [names of the
        unreadable ones])"""
        reihen, unlesbar = [], []
        for db in (GEHOER_DB, NETZ_DB):
            r = Index._gehoer_lesen(db)
            if r is None:
                unlesbar.append(db.name)
            else:
                reihen.append(r)
        return reihen, unlesbar

    @staticmethod
    def _gehoer_einlesen(nach_pfad, reihen_je_db):
        """Take over analysis values from the gehoer main database and our own (NETZ_DB), read only. If a file is in
        both, the row without an error wins, then the newer one. Analyzed values fill gaps; where the name already gives
        BPM or key, the name stays authoritative (the makers know it better than any analysis)."""
        beste = {}
        # Analysis without a file at the old place (FL Studio 2025 -> 2026, folder moved): via the md5 to the rows that
        # have no analysis of their own (otherwise 3,371 analyses of factory packs hung on the old path)
        ohne_ort = collections.defaultdict(list)
        md5_zeilen = collections.defaultdict(list)
        for z in nach_pfad.values():
            if z["md5"]:
                md5_zeilen[z["md5"]].append(z["pfad"])
        gemessen_pfade = set()
        for reihen in reihen_je_db:
            for g in reihen:
                p = nfc(g["pfad"] or "")
                if p in nach_pfad:
                    if not g["fehler"]:
                        gemessen_pfade.add(p)
                elif g["md5"] and g["md5"] in md5_zeilen and not g["fehler"]:
                    ohne_ort[g["md5"]].append(g)
        umgezogen = [(p, g) for m, gs in ohne_ort.items() for p in md5_zeilen[m] if p not in gemessen_pfade for g in gs]
        for reihen in list(reihen_je_db) + [[None]]:
            for g in reihen:
                if g is None:      # after all databases: the moved analyses, only where nothing of our own is
                    for p, g2 in umgezogen:
                        if p not in beste:
                            beste[p] = g2
                    continue
                p = nfc(g["pfad"] or "")
                if p not in nach_pfad:
                    continue
                alt = beste.get(p)
                if alt is None or (alt["fehler"] and not g["fehler"]) or (
                        bool(alt["fehler"]) == bool(g["fehler"]) and (g["analysiert_am"] or "") > (alt["analysiert_am"] or "")):
                    beste[p] = g
        n = 0
        for p, g in beste.items():
            if g["fehler"]:
                continue
            z = nach_pfad[p]
            n += 1
            z["gemessen"] = True
            if g["mtime"] and 0 < g["mtime"] < time.time() + 86400:    # date according to gehoer (the future stays out)
                z["datum"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(g["mtime"]))
            vorwissen = z["bpm"] is not None or g["name_bpm"] is not None   # tempo from the name or the project
            if z["bpm"] is None and g["bpm"]:
                z["bpm"] = round(g["bpm"], 1)
                z["bpm_quelle"] = "gemessen" + (" (unsicher)" if (g["bpm_sicherheit"] or 0) < UNSICHER else "")
            if z["ton"] is None and g["tonart_pc"] is not None:
                z["ton"] = TONARTEN[int(g["tonart_pc"]) % 12]
                z["modus"] = "m" if (g["tonart_modus"] or "").startswith(("moll", "m")) else ""
                z["ton_quelle"] = "gemessen"
                z["ton_unsicher"] = (g["tonart_sicherheit"] or 0) < UNSICHER
            for feld in ("instrumente", "stil", "begriffe"):
                z[feld] = Index._liste(g[feld])
            if g["genre"]:
                z["begriffe"] = z["begriffe"] + Index._liste(g["genre"])
            Index._groove_uebernehmen(z, g, vorwissen)
            Index._grundton_uebernehmen(z, g)
            # loudness for matching levels; implausible values count as missing
            z["lufs"], z["peak_dbfs"] = pegel_wert(g["lufs"], -70, 6), pegel_wert(g["peak_dbfs"], -90, 30)
        return n

    @staticmethod
    def _grundton_uebernehmen(z, g):
        """gehoer's root note for 808, bass and tonal one-shots (by the class that only _groove_uebernehmen sets).
        For one-shots the root note comes before an analyzed key (a kick in "Fm" says nothing); a note from the
        name stays, but gets the octave only when gehoer hears the same note."""
        if g["grundton_pc"] is None or not grundton_zeile(z):
            return
        pc = int(g["grundton_pc"]) % 12
        if z["ton"] is None or (z["ton_quelle"] == "gemessen" and z["lo"] == "oneshot"):
            z["ton"], z["modus"], z["ton_quelle"] = TONARTEN[pc], "", "gemessen"
            z["ton_unsicher"] = (g["grundton_sicherheit"] or 0) < UNSICHER
        if z["ton"] == TONARTEN[pc] and g["grundton_midi"] is not None and z["lo"] == "oneshot":   # loops without octave
            midi = int(g["grundton_midi"])
            z["grundton"] = TONARTEN[midi % 12] + str(midi // 12 - 1)
            z["grundton_cent"] = int(g["grundton_cent"]) if g["grundton_cent"] is not None else None

    @staticmethod
    def _leicht_einlesen(zeilen):
        """Tempo (loops without tempo) and root note (808, bass, tonal one-shots without a note) from the light
        analysis, only where gehoer did not analyze. Not the keys of loops: too unreliable (see leicht.py)."""
        if LEICHT is None:
            return
        werte = LEICHT.werte()
        if not werte:
            return
        for z in zeilen:
            if z["gemessen"]:
                continue
            w = werte.get(z["pfad"])
            if not w:
                continue
            if z["bpm"] is None and z["lo"] == "loop" and w["bpm"] and (w["bpm_sicher"] or 0) >= leicht.TEMPO_AB:
                z["bpm"], z["bpm_quelle"] = round(w["bpm"], 2), "leicht"
            # root note only for 808, bass and melodic one-shots: for kicks and snares it was unreliable, and with a
            # note the project frame filters them by key (kicks dropped from 520 to 246)
            tonal = z["art"] in ("808", "bass", "melodisch") or z.get("klasse") in ("808", "bass", "melodisch")
            if z["ton"] is None and z["lo"] == "oneshot" and tonal and w["grundton_midi"] is not None \
                    and (w["tonhaltig"] or 0) >= leicht.TONHALTIG_AB:
                midi = int(w["grundton_midi"])
                z["ton"], z["modus"], z["ton_quelle"], z["ton_unsicher"] = TONARTEN[midi % 12], "", "leicht", True
                z["grundton"], z["grundton_cent"] = TONARTEN[midi % 12] + str(midi // 12 - 1), w["grundton_cent"]

    @staticmethod
    def _begriffe_zur_klasse(z):
        """gehoer terms without hits that contradict the one-shot class (only one-shots with a class).
        Before, 513 files found "kick" only through this term, among them 35 analyzed percs and 17 toms."""
        k = z["klasse"]
        if not k or z["lo"] != "oneshot" or not z["begriffe"]:
            return z["begriffe"]
        ok = {k} | KLASSE_VERWANDT.get(k, set())
        return [b for b in z["begriffe"] if BEGRIFF_KLASSE.get(b, k) in ok]

    @staticmethod
    def _groove_uebernehmen(z, g, vorwissen):
        """Bars, swing, halftime and one-shot class from a gehoer row.
        Swing and halftime depend on the length of the beat (reference tempo): they only count when gehoer's tempo
        matches the row's tempo within 2 % (the name first). Halftime means: bpm_gefuehl is half the tempo and the
        time signature is not 6/8 (there it is the dotted quarter). Without a tempo in the name, the same half tempo
        can also be gehoer's doubt about the octave (rhythmus: rechnung:oktave, then tempo confidence at most 0.6): then "?".
        Halftime only for drum loops (GEFUEHL_ARTEN), the one-shot class only for one-shots."""
        # The library says loop, but the file is shorter than a bar and gehoer analyzes a one-shot: the tempo came from
        # the kit (Splice), like the ACID chunk above (check 20.10.: a kick with "174" in the name, 0.48 s, "kickloop")
        if (z["lo"] == "loop" and g["art"] == "oneshot" and z["bpm"] and z["dauer"] is not None
                and z["dauer"] < 0.95 * 240.0 / z["bpm"]):
            z["lo"] = "oneshot"
        if g["takte"] and g["takte"] > 0 and z["lo"] != "oneshot" and z["art"] != "song":
            z["takte"], z["takte_quelle"] = round(g["takte"], 2), "gemessen"
        gb, bezug = g["bpm"], z["bpm"]
        passt = bool(gb and bezug and abs(gb / bezug - 1) <= BEZUG_TOLERANZ)
        # swing, feel and felt tempo only for loops (gehoer finds a pulse in some one-shots too)
        loopartig = z["lo"] == "loop" or (z["lo"] != "oneshot" and g["art"] in ("loop", "ausschnitt"))
        drums = z["art"] in GEFUEHL_ARTEN
        gf = g["bpm_gefuehl"]
        halb = bool(passt and gf) and abs(2 * gf / gb - 1) <= BEZUG_TOLERANZ
        doppelt = bool(passt and gf) and abs(gf / (2 * gb) - 1) <= BEZUG_TOLERANZ    # only gehoer's doubt about the octave
        # the felt tempo counts with "half/double": the dotted quarter in 6/8 always, halftime only for drum loops
        if passt and loopartig and gf and not doppelt and (not halb or (drums and g["taktart"] != "6/8")):
            z["bpm_gefuehl"] = round(gf, 2)
        if passt and loopartig and g["swing"] is not None:
            z["swing"], z["swing_wert"], z["swing_quelle"] = swing_klasse(g["swing"]), round(g["swing"], 3), "gemessen"
            z["swing_unsicher"] = (g["swing_sicherheit"] or 0) < UNSICHER
        if passt and loopartig and drums and g["taktart"] != "6/8":
            z["gefuehl"], z["gefuehl_quelle"] = ("halftime" if halb else "normal"), "gemessen"
            z["gefuehl_unsicher"] = halb and not vorwissen and (g["bpm_sicherheit"] or 0) <= 0.6
        # one-shot class only for one-shots (check 20.10.: two loops otherwise showed up in the one-shot filter)
        klasse = (g["oneshot_klasse"] or "").lower()
        klasse = KLASSE_AUS_GEHOER.get(klasse, klasse)
        if klasse in EINZELSCHLAG and (z["lo"] == "oneshot" or (not z["lo"] and g["art"] == "oneshot")):
            z["klasse"], z["klasse_quelle"] = klasse, "gemessen"
            z["klasse_unsicher"] = (g["oneshot_sicherheit"] or 0) < UNSICHER

    @staticmethod
    def _library_neu_einlesen(zeilen):
        """swing, gefuehl, live of the Library-Neu (via md5). Analyzed there means: stimmung_quelle names the reference
        tempo ("gemessen bei 142 BPM"); then the Library-Neu value wins even over the gehoer database, an empty swing
        means "not measurable" (never straight), an empty gefuehl "no halftime". live only exists there."""
        if not LIBRARY_NEU.is_file():
            return 0
        try:
            with open(LIBRARY_NEU, newline="", encoding="utf-8") as f:
                werte = {r["md5"]: r for r in csv.DictReader(f) if r.get("md5") and r.get("stimmung_quelle")}
        except (OSError, csv.Error, KeyError, UnicodeDecodeError) as e:
            print(f"Library-Neu not readable: {e}", flush=True)
            return 0
        n = 0
        for z in zeilen:
            r = werte.get(z["md5"]) if z["md5"] else None
            if r is None:
                continue
            n += 1
            quelle = r.get("stimmung_quelle") or ""
            if re.search(r"gemessen bei [\d.]+ BPM", quelle):
                sw = (r.get("swing") or "").strip()
                z["swing"] = sw if sw in SWING_KLASSEN else None
                z["swing_wert"] = zahl(r.get("swing_anteil")) if z["swing"] else None
                z["swing_quelle"] = "library-neu"
                z["swing_unsicher"] = bool(z["swing"]) and (_sicherheit(quelle, "swing") or 1) < UNSICHER
                z["gefuehl"] = "halftime" if (r.get("gefuehl") or "").strip() == "halftime" else "normal"
                z["gefuehl_quelle"] = "library-neu"
                z["gefuehl_unsicher"] = z["gefuehl"] == "halftime" and (_sicherheit(quelle, "halftime") or 1) < UNSICHER
            lv = (r.get("live") or "").strip()
            if lv in ("ja", "nein", "unsicher"):
                z["live"], z["live_quelle"] = lv, "library-neu"
                m = re.search(r"Anschlagsstreuung [\d.]+ ms aus \d+ Paaren", quelle)
                z["live_info"] = m.group(0) if m else ""
        return n

    @staticmethod
    def _live_aus_tags(zeilen):
        """Without an analysis, the vibe "live" (Splice tags, pack description, folder and file name) counts as played
        live, for loops. Before, there were two "live": the vibe (606 samples) and the filter played live (Library-Neu
        only); "boom bap" breaks showed live, the filter found 0 (test 20.10.). An analysis always comes first."""
        for z in zeilen:
            if z["live"] is None and z["lo"] != "oneshot" and "live" in z["stimmung"]:
                z["live"], z["live_quelle"] = "ja", "tags"
                z["live_info"] = tx("according to tags or pack description (vibe live), not analyzed")

    @staticmethod
    def _takte_rechnen(zeilen):
        """Loops without analyzed bars: from length and tempo (4/4), when that gives a usual loop length within 2 % (1, 2,
        3, 4, 6, 8, 12, 16, 24, 32 bars). Shown without "g"; gehoer replaces the value once analyzed."""
        for z in zeilen:
            if z["takte"] is not None or z["lo"] != "loop" or not z["bpm"] or not z["dauer"]:
                continue
            t = z["dauer"] * z["bpm"] / 240.0
            n = int(round(t))
            if n in TAKTE_GERECHNET and abs(t - n) <= 0.02 * n:
                z["takte"], z["takte_quelle"] = float(n), "gerechnet"

    @staticmethod
    def _abdeckung(zeilen):
        """How many samples (without hidden copies) have a value: for the empty state of the filters."""
        sichtbar = [z for z in zeilen if not z["ist_kopie"]]
        return {
            "alle": len(sichtbar),
            "takte": sum(1 for z in sichtbar if z["takte"] is not None),
            "takte_gemessen": sum(1 for z in sichtbar if z["takte_quelle"] == "gemessen"),
            "swing": sum(1 for z in sichtbar if z["swing"]),
            "gefuehl": sum(1 for z in sichtbar if z["gefuehl"]),
            "live": sum(1 for z in sichtbar if z["live"] in ("ja", "nein")),
            "live_gemessen": sum(1 for z in sichtbar if z["live"] in ("ja", "nein") and z["live_quelle"] == "library-neu"),
            "klasse": sum(1 for z in sichtbar if z["klasse"]),
            "klasse_gemessen": sum(1 for z in sichtbar if z["klasse_quelle"] == "gemessen"),
            "gemessen": sum(1 for z in sichtbar if z["gemessen"]),
            "loops": sum(1 for z in sichtbar if z["lo"] == "loop"),
        }

    @staticmethod
    def _liste(wert):
        if not wert:
            return []
        try:
            v = json.loads(wert)
        except (ValueError, TypeError):
            v = re.split(r"[;,|]", str(wert))
        if isinstance(v, dict):
            v = list(v.keys())
        aus = []
        for x in v if isinstance(v, list) else [v]:
            if isinstance(x, (list, tuple)) and x:
                x = x[0]
            if isinstance(x, dict):
                x = x.get("name") or x.get("label") or ""
            x = re.sub(r"\s+\d(\.\d+)?$", "", str(x).strip())   # gehoer writes "label 0.83, label 0.41"
            if x:
                aus.append(x)
        return aus[:8]


INDEX = None


def woerter_setzen(z):
    """Search words of a row: name, pack, vibe, tags, two folders (_wbasis), the type and gehoer's instruments, style,
    terms. The type stands apart so that a correction by hand swaps it without reloading."""
    z["woerter"] = " " + " ".join([z["_wbasis"]] + woerter(z["art"])
                                  + woerter(" ".join(z["instrumente"] + z["stil"] + z["begriffe"]))).strip() + " "
    z["_wart"] = " " + " ".join(woerter(z["art"] + " " + (z["klasse"] or ""))) + " "    # match quality (relevanz_punkte)


# Fields a correction can touch; the state before the first correction lives in z["_vorher"], so that undoing it
# brings back the old value without reloading
KORR_STAND = ("art", "lo", "bpm", "bpm_quelle", "ton", "modus", "ton_quelle", "ton_unsicher", "grundton",
              "grundton_cent", "lizenz", "lizenz_grund", "klasse", "klasse_quelle", "klasse_unsicher", "takte",
              "takte_quelle", "begriffe", "lizenz_pack", "lizenz_beleg")
HAND = "hand"                 # source of values set by hand, the interface shows "h"
MESSEN_MAX = 5000             # analyzing many at once (gehoer needs seconds per file)
KOERPER_MAX = 16 << 20        # largest POST body (path list of everything selected with Cmd+A)
DATUM_GRENZE = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 86400))   # later: nonsense


def korrektur_anwenden(z, felder, pack_ent=None):
    """Lay a correction (felder from korrekturen.py, {} = none) onto an index row. First bring back the state without a
    correction, then set the fields; one-shot class, computed bars, terms and search words follow.
    Underneath lies the decision for the pack (license, reason, record); a license of the row itself comes first.
    pack_ent: korrekturen.packs() when loading (otherwise pack_felder reads it itself)."""
    z["_korr"] = dict(felder)          # remember the single correction: a new pack decision puts it back on top
    pf = korrekturen.pack_felder(z["pack"], pack_ent) if pack_entscheidbar(z) else {}
    if "_vorher" in z:
        z.update(z["_vorher"])
        z["begriffe"] = list(z["begriffe"])
    elif felder or pf:
        z["_vorher"] = {k: (list(z[k]) if isinstance(z[k], list) else z[k]) for k in KORR_STAND}
    z["hand"] = []
    if pf.get("lizenz") and "lizenz" not in felder:
        z["lizenz"], z["lizenz_pack"], z["lizenz_beleg"] = pf["lizenz"], True, pf.get("beleg") or ""
        z["lizenz_grund"] = tx("Decided for the whole pack") + (": " + pf["lizenz_grund"] if pf.get("lizenz_grund") else "") \
            + ("; " + tx("record: {beleg}", beleg=pf["beleg"]) if pf.get("beleg") else "")
        z["hand"] = ["lizenz"]
    if felder:
        if "art" in felder:
            z["art"] = felder["art"]
        if "lo" in felder:
            z["lo"] = felder["lo"] or ""
        if "bpm" in felder:
            z["bpm"], z["bpm_quelle"] = felder["bpm"], HAND
        if "tonart" in felder:
            n = tonart_norm(felder["tonart"]) if felder["tonart"] else None
            if not n or n[0] != z["ton"]:          # root note with octave only as long as the note stays the same
                z["grundton"], z["grundton_cent"] = None, None
            z["ton"], z["modus"] = (n[0], n[1]) if n else (None, None)
            z["ton_quelle"], z["ton_unsicher"] = HAND, False
        if "lizenz" in felder:
            z["lizenz"] = felder["lizenz"]
            g = felder.get("lizenz_grund") or ""
            z["lizenz_grund"] = tx("Set by hand") + (": " + g if g else "")
        if "art" in felder or "lo" in felder:     # the one-shot class follows the type for one-shots, otherwise none
            a = z["art"]
            ok = z["lo"] == "oneshot" and a in EINZELSCHLAG
            z["klasse"], z["klasse_quelle"], z["klasse_unsicher"] = (a, HAND, False) if ok else (None, "", False)
        z["hand"] = [f for f in ("art", "lo", "bpm", "tonart", "lizenz") if f in felder or (f in z["hand"])]
    # computed bars depend on tempo and loop, analyzed ones do not apply to one-shots
    if z["takte_quelle"] != "gemessen" or z["lo"] == "oneshot":
        z["takte"], z["takte_quelle"] = None, ""
        Index._takte_rechnen([z])
    z["begriffe"] = Index._begriffe_zur_klasse(z)
    woerter_setzen(z)


def korrektur_im_index(geaendert):
    """After POST /api/korrektur: replace the affected rows in memory instead of reloading. geaendert: {pfad: felder}"""
    for p, felder in geaendert.items():
        z = INDEX.nach_pfad.get(p)
        if z is not None:
            korrektur_anwenden(z, felder)
    abdeckung = Index._abdeckung(INDEX.zeilen)
    with INDEX.lock:
        INDEX.abdeckung = abdeckung
    SUCH_CACHE.clear()


# ---------------------------------------------------------------- license per pack
LIZENZ_OFFEN = ("unklar", "ungeprüft")     # not decided yet ("unbekannt" does not occur in the library)
HERKUNFT_NAME = {"splice": "Splice", "netz": N_("Web"), "openlibary": N_("Web"), "ordner": N_("Folder")}
EINZEL_TEIL = os.sep + netz.EINZEL_ORDNER + os.sep


def pack_entscheidbar(z):
    """Does a pack decision apply to this row? Not for single sounds from the Web: their "pack" is the folder by type
    or kit (netz.py: pack = folder name, e.g. "Vocals"), every file has its own license. A decision there would have
    silently taken along every future download into that folder (check 24.10.: CC BY-NC was then on ja)."""
    return bool(z["pack"]) and EINZEL_TEIL not in z["pfad"]


def pack_im_index(pack):
    """After a pack decision: re-apply all rows of the pack (their single correction stays on top)."""
    n = 0
    for z in INDEX.zeilen:
        if z["pack"] == pack:
            korrektur_anwenden(z, z.get("_korr") or {})
            n += 1
    SUCH_CACHE.clear()
    return n


def lizenz_basis(z):
    """License and reason of the row without a pack decision and without a correction by hand."""
    v = z.get("_vorher") or z
    return v["lizenz"], v["lizenz_grund"]


def lizenz_offen_zahl():
    """Counter "License unclear" on the left: counted like the library without filters (without copies and discarded)."""
    return sum(1 for z in filtern({}) if z["lizenz"] in LIZENZ_OFFEN)


def lizenz_packs():
    """License view: one row per pack with open or decided files."""
    ent, heute = korrekturen.packs(), korrekturen.heute()
    g = {}
    ohne_pack = einzeln = offen = 0
    for z in filtern({}):
        jetzt_offen = z["lizenz"] in LIZENZ_OFFEN
        offen += jetzt_offen
        if not z["pack"]:
            ohne_pack += jetzt_offen
            continue
        if not pack_entscheidbar(z):      # single sounds: license per file, no pack
            einzeln += jetzt_offen
            continue
        b, grund = lizenz_basis(z)
        e = g.get(z["pack"])
        if e is None:
            e = g[z["pack"]] = {"offen": 0, "vorher_offen": 0, "dateien": 0, "herkunft": collections.Counter(),
                                "gruende": collections.Counter()}
        e["dateien"] += 1
        e["offen"] += jetzt_offen
        e["herkunft"][HERKUNFT_NAME.get(z["herkunft"], z["herkunft"])] += 1
        if b in LIZENZ_OFFEN:
            e["vorher_offen"] += 1
            e["gruende"][grund] += 1
    aus = []
    for pack, e in g.items():
        x = ent.get(pack) or {"felder": {}, "zeit": "", "spaeter_bis": ""}
        lizenz = x["felder"].get("lizenz") or ""
        if not lizenz and not e["vorher_offen"]:
            continue
        spaeter = x["spaeter_bis"] if not lizenz and x["spaeter_bis"] >= heute else ""
        aus.append({"pack": pack, "offen": e["offen"], "vorher_offen": e["vorher_offen"], "dateien": e["dateien"],
                    "herkunft": [tx(h) for h, _ in e["herkunft"].most_common()],
                    "splice": "Splice" in e["herkunft"],
                    "grund": e["gruende"].most_common(1)[0][0] if e["gruende"] else "",
                    "lizenz": lizenz, "beleg": x["felder"].get("beleg", ""),
                    "zeit": x["zeit"] if lizenz else "", "spaeter_bis": spaeter})
    aus.sort(key=lambda r: (bool(r["lizenz"]), bool(r["spaeter_bis"]), -(r["offen"] if not r["lizenz"] else r["vorher_offen"]),
                            r["pack"].casefold()))
    return {"offen": offen, "ohne_pack": ohne_pack, "einzeln": einzeln,
            "packs_offen": sum(1 for r in aus if not r["lizenz"] and r["offen"]),
            "entschieden": sum(1 for r in aus if r["lizenz"]),
            "heute": sum(1 for r in aus if r["lizenz"] and r["zeit"].startswith(heute)),
            "packs": aus}


def lizenz_playlist(sammlung):
    """Line above a playlist: "12 ja · 5 offen · 3 nein" over the whole playlist (without the other filters)."""
    c = collections.Counter(z["lizenz"] for z in filtern({"sammlung": [sammlung]}))
    return {"ja": c["ja"], "offen": sum(c[x] for x in LIZENZ_OFFEN), "nein": c["nein"]}


def suche_aufloesen(s):
    """{parameters of the search} instead of a path list (Cmd+A over all matches) -> paths in sort order."""
    if not isinstance(s, dict):
        raise Fehleingabe(tx("suche must be an object"))
    p = {}
    for k, v in s.items():
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise Fehleingabe(tx("suche.{k} must be text or a number", k=k))
        p[k] = [str(v)]
    fremd = unbekannte_parameter(p, SUCHE_PARAMETER - {"offset", "limit"})
    if fremd:
        raise Fehleingabe(fremd["fehler"])
    return [z["pfad"] for z in filtern(p)]


def auswahl_pfade(k, grenze=korrekturen.MAX_PFADE):
    """pfade (list) or suche (object) from a POST body -> paths from the index (known ones only)."""
    if k.get("suche") is not None:
        pfade = suche_aufloesen(k["suche"])
    else:
        roh = k.get("pfade")
        if not isinstance(roh, list) or not roh or not all(isinstance(x, str) for x in roh):
            raise Fehleingabe(tx("pfade must be a list of strings (or suche an object)"))
        pfade = list(dict.fromkeys(nfc(x) for x in roh))
        if any(x not in INDEX.nach_pfad for x in pfade):
            raise Fehleingabe(tx("Sample is not in the library"))
    if len(pfade) > grenze:
        raise Fehleingabe(tx("at most {n} samples at once",
                             n=f"{grenze:,}".replace(",", "." if i18n.current() == "de" else ",")))
    return pfade


# ================================================================ Search

class Fehleingabe(ValueError):
    pass


# Allowed parameters, kept in one place (23.10.2026): a typo like "ton" instead of "tonart" used to filter out nothing
# without a word. Whoever builds in a new filter adds it here; otherwise the server answers with 400.
SUCHE_PARAMETER = frozenset([
    "q", "sammlung", "art", "herkunft", "lizenz", "stimmung", "tonart", "passend", "lo", "pack",
    "art_nicht", "herkunft_nicht", "lizenz_nicht", "stimmung_nicht",
    "swing", "gefuehl", "live", "klasse", "takte", "takte_min", "takte_max",
    "swing_nicht", "gefuehl_nicht", "live_nicht", "klasse_nicht", "takte_nicht",
    "bpm_min", "bpm_max", "halb", "dauer_min", "dauer_max", "gemessen", "doppelte", "sort", "ab", "offset", "limit",
    "bpm_toleranz", "ton_abstand", "drums_ohne_ton",      # project frame
    "oneshots_ohne_tempo",                                # the frame tempo does not filter one-shots
    "notiz",                                              # only samples with a note
    "verworfene", "nie_gehoert",                          # also show discarded ones, only never played ones
    "aehnlich", "aehnlich_bpm",                           # similar to a sample (tempo from the project)
    "genre", "genre_nicht",                               # left column: seven genres, each an OR over vibe words
])
ZUFALL_PARAMETER = frozenset(["typ", "bpm_min", "bpm_max", "halb", "lizenz", "ohne",
                              "bpm_toleranz", "tonart", "passend", "ton_abstand", "verworfene",
                              "nie_zuerst"])                # never played ones first
# typ=suche draws from the library search and takes all search parameters for that (without paging)
ZUFALL_SUCHE_PARAMETER = (SUCHE_PARAMETER - {"offset", "limit"}) | {"typ", "ohne", "nie_zuerst"}
VERLAUF_SAMMLUNGEN = ("gehoert", "gezogen")               # sammlung=gehoert, sammlung=gezogen
# Drum types without a note do not drop out with the key filter of the project frame (a snare has no key); in the
# library only with drums_ohne_ton=1 (set by the frame), in Random always. With a note (analyzed root note) it counts.
DRUM_ARTEN = {"kick", "snare", "clap", "hat", "cymbal", "perc", "tom", "rim", "brush",
              "break", "kickloop", "fill", "top"}
BPM_TOLERANZ_MAX, TON_ABSTAND_MAX = 15, 6


def tempo_bereich(p, eins):
    """bpm_min, bpm_max (swapped when reversed), widened by bpm_toleranz percent: 140 to 140 with 8 -> 129 to 151."""
    bmin, bmax = zahl(eins("bpm_min")), zahl(eins("bpm_max"))
    if bmin is not None and bmax is not None and bmin > bmax:
        bmin, bmax = bmax, bmin
    t = zahl(eins("bpm_toleranz"))
    if eins("bpm_toleranz") != "" and (t is None or not 0 <= t <= BPM_TOLERANZ_MAX):
        raise Fehleingabe(tx("bpm_toleranz: 0 to {max} (percent)", max=BPM_TOLERANZ_MAX))
    if t:   # to whole BPM inward, plus the half BPM of play from tempo_passend: 140 ±8 % shows 129 to 151
        bmin = math.ceil(bmin * (1 - t / 100) - 1e-9) if bmin is not None else None
        bmax = math.floor(bmax * (1 + t / 100) + 1e-9) if bmax is not None else None
    return bmin, bmax


def tonart_mengen(werte, passend, abstand_text):
    """-> (keys like 'F#m', root notes for 808, bass and tonal one-shots). passend: add the Camelot neighbors;
    ton_abstand n: the same key up to n semitones higher or lower, each with its relative key (Ab is distance 0 to Fm)."""
    try:
        abstand = int(abstand_text) if abstand_text != "" else 0
    except ValueError:
        abstand = -1
    if not 0 <= abstand <= TON_ABSTAND_MAX:
        raise Fehleingabe(tx("ton_abstand: whole number from 0 to {max}", max=TON_ABSTAND_MAX))
    tonarten, grund = set(), set()
    for t in werte:
        n = tonart_norm(t)
        if not n:
            raise Fehleingabe(tx("unknown key: {t}", t=t))
        basis = camelot_nachbarn(*n) if passend else {n[0] + n[1]}
        tonarten |= basis
        grund |= {x.rstrip("m") for x in basis}
        if abstand:
            pc = TONARTEN.index(n[0])
            for d in range(-abstand, abstand + 1):
                r = TONARTEN[(pc + d) % 12]
                tonarten.add(r + n[1])
                grund.add(r)
                tonarten.add(TONARTEN[(pc + d + 3) % 12] if n[1] == "m" else TONARTEN[(pc + d + 9) % 12] + "m")
    return tonarten, grund


def tonart_passt(z, tonarten, grundtoene, drums_frei):
    if z["ton"] is None:
        return drums_frei and z["art"] in DRUM_ARTEN
    nur_grundton = grundton_zeile(z) or (z["lo"] == "oneshot" and not z["modus"])
    return (z["ton"] + z["modus"]) in tonarten or (nur_grundton and z["ton"] in grundtoene)


def unbekannte_parameter(p, erlaubt):
    """-> answer for 400 or None. p: result of parse_qs."""
    fremd = sorted(k for k in p if k not in erlaubt)
    if not fremd:
        return None
    return {"fehler": tx("unknown parameter: {names}", names=", ".join(fremd)), "erlaubt": sorted(erlaubt)}


SUCH_CACHE = collections.OrderedDict()   # query without offset/limit -> sorted matches (paging without a new search)
GRUPPEN_CACHE = collections.OrderedDict()   # same key -> {bit: rows that fail only on this group}
# Bits of the facet groups in filtern(); order as in FACETTEN_GRUPPEN
GRUPPEN_BIT = {n: 1 << i for i, n in enumerate(("art", "genre", "tonart", "takte", "swing", "gefuehl", "live",
                                                  "herkunft", "lizenz"))}

# Similar to a sample (24.10.2026). Same lo; loops: same type or related group (like the Random groups), key the same
# or a Camelot neighbor, BPM with half/double within ±6 %, shared instruments and style; one-shots: same class, root
# note ±1 semitone, length ±30 %, LUFS ±4. A criterion only excludes when both values are known; a missing value gives
# no points. The same pack always stays in and comes first.
AEHNLICH_GRUPPEN = ({"break", "kickloop", "fill"}, {"top", "perc", "hat", "cymbal"}, {"melodisch", "bass"})
AEHNLICH_BPM, AEHNLICH_DAUER, AEHNLICH_LUFS = 0.06, 0.30, 4.0


def grundton_midi(text):
    """'G1' -> 31, 'C#2' -> 37, otherwise None (spelling as in _grundton_uebernehmen)."""
    m = re.match(r"^([A-G][#b]?)(-?\d+)$", text or "")
    if not m or m.group(1) not in TONARTEN:
        return None
    return TONARTEN.index(m.group(1)) + (int(m.group(2)) + 1) * 12


def aehnlich_vergleicher(p):
    """-> (reference row, info for the interface, function row -> None or (same pack, points, tier)) or None."""
    def eins(k):
        return (p.get(k) or [""])[0]
    pfad = nfc(eins("aehnlich"))
    if not pfad:
        return None
    r = INDEX.nach_pfad.get(pfad)
    if r is None:
        raise Fehleingabe(tx("aehnlich: sample not in the library"))
    projekt = eins("aehnlich_bpm") != ""
    if projekt:      # project frame on: its tempo instead of the sample's
        tempo = zahl(eins("aehnlich_bpm"))
        if tempo is None or not 20 <= tempo <= 400:
            raise Fehleingabe(tx("aehnlich_bpm: 20 to 400"))
    else:
        tempo = r["bpm"]
    einzeln = r["lo"] == "oneshot"
    gruppe = next((g for g in AEHNLICH_GRUPPEN if r["art"] in g), {r["art"]})
    instr, stil = set(r["instrumente"]), set(r["stil"])
    nachbarn = camelot_nachbarn(r["ton"], r["modus"] or "") if r["ton"] else set()
    nachbar_ton = {x.rstrip("m") for x in nachbarn}
    klasse = r["klasse"] or r["art"]
    midi = grundton_midi(r["grundton"])

    def loop(z):
        if z["art"] not in gruppe:
            return None
        pk, hart = (2.0 if z["art"] == r["art"] else 0.0), True
        if tempo and z["bpm"]:
            d = min(abs(k / tempo - 1) for k in (z["bpm"], z["bpm"] * 2, z["bpm"] / 2))
            if d <= AEHNLICH_BPM:
                pk += 3 * (1 - d / AEHNLICH_BPM)
            else:
                hart = False
        if r["ton"] and z["ton"]:
            if grundton_zeile(r) or grundton_zeile(z):      # bass, 808: only the note
                ok, gleich = z["ton"] in nachbar_ton, z["ton"] == r["ton"]
            else:
                ok = z["ton"] + (z["modus"] or "") in nachbarn
                gleich = ok and z["ton"] == r["ton"] and (z["modus"] or "") == (r["modus"] or "")
            if ok:
                pk += 3 if gleich else 2
            else:
                hart = False
        pk += min(3, len(instr.intersection(z["instrumente"]))) + min(2, len(stil.intersection(z["stil"])))
        return pk, hart

    def schlag(z):
        if (z["klasse"] or z["art"]) != klasse:
            return None
        pk, hart = (1.0 if z["art"] == r["art"] else 0.0), True
        geschaetzt = "leicht" in (r["ton_quelle"], z["ton_quelle"])     # only bonus points, never an exclusion (check 03.11.)
        if r["ton"] and z["ton"] and z["ton"] in TONARTEN and r["ton"] in TONARTEN:
            m = grundton_midi(z["grundton"])
            if midi is not None and m is not None:      # both with octave: really ±1 semitone (G0 is not close to G1)
                d = abs(m - midi)
            else:
                a, b = TONARTEN.index(r["ton"]), TONARTEN.index(z["ton"])
                d = min((a - b) % 12, (b - a) % 12)
            if d <= 1:
                pk += 3 if d == 0 else 1.5
            elif not geschaetzt:
                hart = False
        if r["dauer"] and z["dauer"]:
            q = abs(z["dauer"] / r["dauer"] - 1)
            if q <= AEHNLICH_DAUER:
                pk += 2 * (1 - q / AEHNLICH_DAUER)
            else:
                hart = False
        if r["lufs"] is not None and z["lufs"] is not None:
            d = abs(z["lufs"] - r["lufs"])
            if d <= AEHNLICH_LUFS:
                pk += 2 * (1 - d / AEHNLICH_LUFS)
            else:
                hart = False
        return pk, hart

    vergleich = schlag if einzeln else loop
    # With a sound fingerprint the sound decides the order. What does not fit musically stays mandatory (check
    # 03.11.): group or class, songs only to songs, the project tempo, the key for melodic and bass. Groove, tempo and
    # key give small bonuses; candidates without a fingerprint come last.
    klang_fehler = False
    try:
        klang_nah = KLANG.nachbarn(klang_schluessel(r)) if KLANG is not None else None
    except klangnaehe.DienstFehler:
        klang_nah, klang_fehler = None, True
    # Without gehoer, the rough fingerprint. Not with gehoer (check 03.11.: there it covers only the unanalyzed half,
    # the analyzed half would stand as a block in the second tier)
    if klang_nah is None and LEICHT is not None and not os.path.exists(GEHOER):
        grob = LEICHT.nachbarn(r["pfad"])
        if grob:
            klang_nah = {klang_schluessel(INDEX.nach_pfad[q]): w for q, w in grob.items() if q in INDEX.nach_pfad}
    swing_r, gef_r, live_r = r["swing"], r["gefuehl"], r["live"]

    def klang_punkte(z):
        if (z["art"] == "song") != (r["art"] == "song"):
            return None
        if einzeln:
            kz = z["klasse"] or z["art"]
            if kz != klasse and kz not in KLASSE_VERWANDT.get(klasse, ()):
                return None
        elif r["art"] != "song":
            if z["art"] not in gruppe:
                return None
            if projekt and z["bpm"] and min(abs(k / tempo - 1) for k in (z["bpm"], z["bpm"] * 2, z["bpm"] / 2)) > AEHNLICH_BPM:
                return None
            if r["art"] in ("melodisch", "bass") and r["ton"] and z["ton"] and "leicht" not in (r["ton_quelle"], z["ton_quelle"]):
                if z["ton"] + ("" if grundton_zeile(r) or grundton_zeile(z) else (z["modus"] or "")) not in (
                        nachbar_ton if grundton_zeile(r) or grundton_zeile(z) else nachbarn):
                    return None
        w = klang_nah.get(klang_schluessel(z))
        if w is not None and w >= 0.9995:
            return None          # the same sound (a copy under another name): not "similar"
        if w is None:
            # without a fingerprint (or not among the nearest 4,000): the old comparison with its requirements, as a
            # second tier behind all sound matches (check 03.11.: otherwise the rest stood there sorted by name only)
            e = vergleich(z)
            if e is None or not e[1]:
                return None
            return False, round(e[0], 2), False
        pk = 100.0 * w
        if not einzeln:
            if swing_r and z["swing"]:
                pk += 3 if z["swing"] == swing_r else -3
            if gef_r and z["gefuehl"]:
                pk += 2 if z["gefuehl"] == gef_r else -2
            if live_r in ("ja", "nein") and z["live"] in ("ja", "nein"):
                pk += 2 if z["live"] == live_r else -2
            if tempo and z["bpm"]:
                if abs(z["bpm"] / tempo - 1) <= AEHNLICH_BPM:
                    pk += 3
                elif min(abs(k / tempo - 1) for k in (z["bpm"] * 2, z["bpm"] / 2)) <= AEHNLICH_BPM:
                    pk += 1
            if r["ton"] and z["ton"] and z["ton"] + (z["modus"] or "") in nachbarn:
                pk += 2
        elif midi is not None and grundton_midi(z["grundton"]) is not None and abs(grundton_midi(z["grundton"]) - midi) <= 1:
            pk += 2
        return False, round(pk, 2), True

    def punkte(z):
        if z["pfad"] == r["pfad"] or z["lo"] != r["lo"]:
            return None
        if klang_nah is not None:
            return klang_punkte(z)
        e = vergleich(z)
        if e is None:
            return None
        if not e[1]:        # the same pack must also hold the criteria, it only comes first
            return None
        gleiches_pack = bool(r["pack"]) and z["pack"] == r["pack"]
        # One-shots: if the reference sample has a note, candidates without a note come after those with a matching one
        stufe = not (einzeln and r["ton"] and not z["ton"])
        return gleiches_pack, round(e[0], 2), stufe
    # almost all values missing: loops without tempo, key, instruments and style; one-shots without note and LUFS
    wenig = (r["ton"] is None and r["lufs"] is None) if einzeln else \
        (not tempo and r["ton"] is None and not instr and not stil)
    info = {"pfad": r["pfad"], "name": r["name"], "pack": r["pack"], "lo": r["lo"], "wenig": wenig,
            "laenge": einzeln and bool(r["dauer"]),
            "tempo": round(tempo, 2) if tempo and not einzeln else None,
            "tempo_projekt": projekt and not einzeln,      # one-shots have no tempo
            "klang": klang_nah is not None, "klang_moeglich": KLANG is not None and KLANG.verfuegbar(),
            "klang_fehler": klang_fehler}
    return r, info, punkte


# "808" gave 4,951 matches by name, full songs on top, because gehoer's instrument terms ("sub bass or 808") counted
# as much as the file name. Per search word the best field counts; if it hits name and type, a bonus comes on top
# (the 808 before the "808 Cymbal"). Full songs slide to the back, unless the search asks for them.
RELEVANZ_FELD = (("_wname", 10), ("_wart", 8), ("_wort", 5), ("_wtag", 4))
RELEVANZ_GANZ, RELEVANZ_NAME_UND_ART, RELEVANZ_REST = 2, 3, 1
SONG_WOERTER = {"song", "songs", "beat", "beats", "track", "tracks", "instrumental", "type"}


def relevanz_punkte(z, mit):
    art_w = z.get("_wart") or " " + " ".join(woerter(z["art"] + " " + (z["klasse"] or ""))) + " "
    punkte = 0
    for w in mit:
        if w.isdigit():
            def drin(t):
                return " " + w + " " in t
        else:
            def drin(t):
                return " " + w in t
        best = RELEVANZ_REST
        for feld, gewicht in RELEVANZ_FELD:
            if drin(art_w if feld == "_wart" else z[feld]):
                best = gewicht
                break
        if w in GENRE_WOERTER and best in (RELEVANZ_FELD[2][1], RELEVANZ_FELD[3][1]):
            best = RELEVANZ_FELD[0][1]      # "drill": the pack "VVS: Flawless Drill" counts like a name
        elif best == RELEVANZ_FELD[0][1]:
            if " " + w + " " in z["_wname"]:
                best += RELEVANZ_GANZ
            # only when gehoer analyzed the class: otherwise the type mostly comes from the same name (counted twice)
            if z["klasse_quelle"] == "gemessen" and z["klasse"] and drin(" " + z["klasse"] + " "):
                best += RELEVANZ_NAME_UND_ART
            # "808 CH.wav" is a hi-hat of the 808: if gehoer analyzed another class, it slides back
            elif (w in EINZELSCHLAG and z["klasse_quelle"] == "gemessen" and z["klasse"]
                  and z["klasse"] != w and w not in KLASSE_VERWANDT.get(z["klasse"], ())):
                best -= RELEVANZ_NAME_UND_ART
            # likewise when the type from the name is another hit ("Sn 808.wav" is a snare of the TR-808)
            elif (w in EINZELSCHLAG and z["art"] in EINZELSCHLAG and z["art"] != w
                  and w not in KLASSE_VERWANDT.get(z["art"], ())):
                best -= RELEVANZ_NAME_UND_ART
        punkte += best
    return punkte


def song_hinten(z, mit):
    """Full songs as their own tier behind everything, unless the search asks for songs (check 03.11.: "drill")."""
    return z["art"] == "song" and not SONG_WOERTER.intersection(mit)


def nach_pack_streuen(zeilen, stufe):
    """Within the same tier, one match from each pack in turn instead of a wall from one pack (check 03.11.: for
    "808", 21 of the first 50 came from a single drum kit). Packs in the order they first appear; within a pack the
    order stays."""
    aus, i, n = [], 0, len(zeilen)
    while i < n:
        s, j = stufe(zeilen[i]), i
        while j < n and stufe(zeilen[j]) == s:
            j += 1
        gruppe = zeilen[i:j]
        if len(gruppe) > 2:
            je = collections.OrderedDict()
            for z in gruppe:
                je.setdefault(z["pack"], []).append(z)
            if len(je) > 1:
                listen, gruppe = list(je.values()), []
                for k in range(max(map(len, listen))):
                    gruppe.extend(l[k] for l in listen if k < len(l))
        aus.extend(gruppe)
        i = j
    return aus


def einzelschlag_ohne_tempo(z):
    """One-shot, or short, without tempo and not listed as a loop (check 03.11.: around 5,000 snares, hats, toms and
    kicks have lo empty, e.g. the Serum drums; otherwise they kept dropping out with the frame)."""
    if z["lo"] == "oneshot":
        return True
    return z["lo"] == "" and z["bpm"] is None and z["art"] != "song" and z["dauer"] is not None and z["dauer"] < 6


def klang_schluessel(z):
    return klangnaehe.schluessel(z["md5"], z["pfad"])


def tempo_passend(b, gefuehl, bmin, bmax, halb):
    """The tempo under which a row fits into the range bmin to bmax, otherwise None. Its own tempo with 0.5 BPM of
    play (rounded values); with halb also double, half and felt (halftime, dotted quarter in 6/8), but those only
    exactly within the range (test 20.10.: breaks 88 to 94 found DnB at 175 as 87.5)."""
    if b is None:
        return None
    if (bmin is None or b >= bmin - 0.5) and (bmax is None or b <= bmax + 0.5):
        return b
    if halb:
        for k in (b * 2, b / 2) + ((gefuehl,) if gefuehl else ()):
            if (bmin is None or k >= bmin) and (bmax is None or k <= bmax):
                return k
    return None


def filtern(p, gruppen=False):
    """p: query parameters (dict of lists). Returns the matching rows in sort order.
    gruppen=True (left column, check 24.10.): -> (matches, {bit: rows}); under the bit of a facet group (GRUPPEN_BIT)
    are the rows that fail only on that one group. Matches plus these rows are exactly the matches without that
    group's selection, all in one pass (before, one search of its own per set group)."""
    # notes change without a new index, their state belongs in the key
    # discarded ones too, and the history when "never played" filters
    nie_gehoert = (p.get("nie_gehoert") or [""])[0] == "1"
    schluessel = (INDEX.version, sammlungen.notiz_stand(), sammlungen.verworfen_stand(),
                  verlauf.stand() if nie_gehoert else 0,
                  KLANG.stempel() if KLANG is not None and p.get("aehnlich") else 0,      # sound fingerprints
                  LEICHT.stempel() if LEICHT is not None and p.get("aehnlich") else 0,    # rough fingerprints
                  tuple(sorted((k, tuple(v)) for k, v in p.items() if k not in ("offset", "limit"))))
    # Favorites and playlists change without a new index: then never from the cache
    sammlung = (p.get("sammlung") or [""])[0]
    # with gruppen only when both parts come from the same pass (SUCH_CACHE is cleared in three places)
    if gruppen and not sammlung and schluessel in GRUPPEN_CACHE and GRUPPEN_CACHE[schluessel][0] is SUCH_CACHE.get(schluessel):
        GRUPPEN_CACHE.move_to_end(schluessel)
        SUCH_CACHE.move_to_end(schluessel)
        return GRUPPEN_CACHE[schluessel]
    if not gruppen and schluessel in SUCH_CACHE and not sammlung:
        SUCH_CACHE.move_to_end(schluessel)
        return SUCH_CACHE[schluessel]
    # Played and Dragged come from verlauf.json, with a time per row, newest first
    verlauf_eintraege, verlauf_rang = verlauf.eintraege(sammlung) if sammlung in VERLAUF_SAMMLUNGEN else (None, None)
    in_sammlung = set(verlauf_eintraege) if verlauf_eintraege is not None else \
        sammlungen.pfade_fuer(sammlung) if sammlung else None
    # discarded ones are missing everywhere, except in Discarded itself, in the history (Played, Dragged: there with a
    # mark, so that count and list agree and nothing dragged drops out of sight) and with verworfene=1
    weg = set() if sammlung == "verworfen" or verlauf_eintraege is not None or (p.get("verworfene") or [""])[0] == "1" \
        else sammlungen.verworfen()
    schon_gehoert = verlauf.gehoert_menge() if nie_gehoert else ()

    def eins(k, std=""):
        return (p.get(k) or [std])[0]

    def menge(k):
        return {x.strip() for x in eins(k).split(",") if x.strip()}

    roh = nfc(eins("q")).split()
    # synonyms on the whole text, not per word: "boom bap" and "lo fi" are one term (before 0 and 232 matches)
    mit = woerter(" ".join(t for t in roh if not t.startswith("-")))
    ohne_w = [w for t in roh if t.startswith("-") and len(t) > 1 for w in woerter(t[1:])]   # "-vocal" excludes

    def trifft(w, wt):   # words at the start of a word, numbers only whole ("80" should not find "808")
        return (" " + w + " " in wt) if w.isdigit() else (" " + w in wt)
    arten, herkunft, lizenzen = menge("art"), menge("herkunft"), menge("lizenz")
    stimmungen = {w for s in menge("stimmung") for w in stimmung_woerter(s)}
    ohne = {f: menge(f + "_nicht") for f in ("art", "herkunft", "lizenz", "swing", "gefuehl", "live", "klasse")}
    ohne["stimmung"] = {w for s in menge("stimmung_nicht") for w in stimmung_woerter(s)}
    # left column: genre as its own group (OR inside, AND with vibe), without genre like a minus on vibe
    genres = {w for g in menge("genre") for w in GENRE_GRUPPEN.get(g, (g,))}
    genre_ohne = {w for g in menge("genre_nicht") for w in GENRE_GRUPPEN.get(g, (g,))}
    ohne["swing"] = {w for s in ohne["swing"] for w in SWING_GRUPPEN.get(s, (s,))}
    # "without 808" with one-shots also removes everything with 808 in name, pack or folder, like "-808" in the search
    # (test 20.10.: "Kick without 808" left an "808 Kick" from the folder 808s standing)
    ohne_808 = "808" in ohne["klasse"]
    # gehoer values: only known values match, excluding only hits known ones (unknown stays in)
    swings, gefuehle, live, klassen = menge("swing"), menge("gefuehl"), menge("live"), menge("klasse")
    swings = {w for s in swings for w in SWING_GRUPPEN.get(s, (s,))}

    def takte_menge(k):
        try:
            return {int(x) for x in menge(k)}
        except ValueError:
            raise Fehleingabe(tx("{k}: whole numbers only", k=k))
    takte, takte_ohne = takte_menge("takte"), takte_menge("takte_nicht")
    tmin, tmax = zahl(eins("takte_min")), zahl(eins("takte_max"))
    if tmin is not None and tmax is not None and tmin > tmax:
        tmin, tmax = tmax, tmin

    def takte_ist(t, n):
        return t is not None and abs(t - n) <= 0.1
    # the gehoer part of the loop only runs when one of these filters is set (saves around 20 % per search)
    gehoer_filter = bool(swings or gefuehle or live or klassen or takte or takte_ohne or tmin is not None
                         or tmax is not None or any(ohne[f] for f in ("swing", "gefuehl", "live", "klasse")))
    tonarten, grundtoene = tonart_mengen(menge("tonart"), eins("passend") == "1", eins("ton_abstand"))
    drums_frei = eins("drums_ohne_ton") == "1"
    lo, pack = eins("lo"), eins("pack")
    bmin, bmax = tempo_bereich(p, eins)     # widened with bpm_toleranz
    dmax, dmin = zahl(eins("dauer_max")), zahl(eins("dauer_min"))
    if dmin is not None and dmax is not None and dmin > dmax:
        dmin, dmax = dmax, dmin
    nur_gemessen, halb, alle_kopien = eins("gemessen") == "1", eins("halb") == "1", eins("doppelte") == "1"

    tempo = bmin is not None or bmax is not None
    # With the project frame "140 · F#m", 13 of 63,354 one-shots were left, 808 and kick had 0. A one-shot has no
    # tempo; with oneshots_ohne_tempo (the page sets it while the frame sets a tempo) the tempo filters only loops and
    # songs
    oneshot_frei = eins("oneshots_ohne_tempo") == "1"
    # notes are searchable like name and tags (word starts), notiz=1 keeps only samples with a note
    nur_notiz = eins("notiz") == "1"
    notiz_text = sammlungen.notizen() if (mit or ohne_w or nur_notiz) else {}
    notiz_woerter = {k: " " + " ".join(woerter(t)) + " " for k, t in notiz_text.items()} if (mit or ohne_w) else {}
    aehnlich = aehnlich_vergleicher(p)       # Similar
    aus = []
    extra = collections.defaultdict(list)    # only with gruppen: bit -> rows that fail only on this group
    G_ART, G_GENRE, G_TON, G_TAKTE, G_SWING = (GRUPPEN_BIT[n] for n in ("art", "genre", "tonart", "takte", "swing"))
    G_GEFUEHL, G_LIVE, G_HERKUNFT, G_LIZENZ = (GRUPPEN_BIT[n] for n in ("gefuehl", "live", "herkunft", "lizenz"))
    frueh_ohne = bool(ohne["art"] or ohne["herkunft"] or ohne["lizenz"] or genre_ohne)
    for z in INDEX.zeilen:
        if in_sammlung is not None:
            if z["pfad"] not in in_sammlung:   # in favorites and playlists copies count too
                continue
        elif z["ist_kopie"] and not alle_kopien:
            continue
        if nur_notiz and z["pfad"] not in notiz_text:
            continue
        if z["pfad"] in weg or z["pfad"] in schon_gehoert:      # discarded, already played
            continue
        wt = z["woerter"]
        if notiz_woerter and z["pfad"] in notiz_woerter:
            wt = wt + notiz_woerter[z["pfad"]].lstrip()
        if mit and not all(trifft(w, wt) for w in mit):
            continue
        if ohne_w and any(trifft(w, wt) for w in ohne_w):
            continue
        if ohne["stimmung"].intersection(z["stimmung"]):
            continue
        # groups of the column: without gruppen the row fails at once, with gruppen it counts as long as only one fails
        fehl = 0
        if frueh_ohne:
            fehl = ((G_ART if z["art"] in ohne["art"] else 0) | (G_HERKUNFT if z["herkunft"] in ohne["herkunft"] else 0)
                    | (G_LIZENZ if z["lizenz"] in ohne["lizenz"] else 0)
                    | (G_GENRE if genre_ohne and genre_ohne.intersection(z["stimmung"]) else 0))
            if fehl and (not gruppen or fehl & (fehl - 1)):
                continue
        # Tempo: the matching one (with half/double the converted or the felt one, e.g. the dotted quarter in 6/8)
        k = tempo_passend(z["bpm"], z["bpm_gefuehl"], bmin, bmax, halb) if tempo else None
        if tempo and k is None and not (oneshot_frei and einzelschlag_ohne_tempo(z)):
            continue
        umgerechnet = k is not None and abs(k - z["bpm"]) > 0.01
        # bars at the matching tempo: a 4-bar loop at 70 fills eight bars at 140 (test 20.10.: "140 from 70", "4 bars")
        tk = z["takte"] * k / z["bpm"] if umgerechnet and z["takte"] is not None else z["takte"]
        if gehoer_filter:
            if z["swing"] in ohne["swing"] or (swings and z["swing"] not in swings):
                fehl |= G_SWING
            if z["gefuehl"] in ohne["gefuehl"] or (gefuehle and z["gefuehl"] not in gefuehle):
                fehl |= G_GEFUEHL
            if z["live"] in ohne["live"] or (live and z["live"] not in live):
                fehl |= G_LIVE
            if z["klasse"] in ohne["klasse"] or (ohne_808 and trifft("808", wt)) or (klassen and z["klasse"] not in klassen):
                fehl |= G_ART
            if (any(takte_ist(tk, n) for n in takte_ohne) or (takte and not any(takte_ist(tk, n) for n in takte))
                    or ((tmin is not None or tmax is not None) and (tk is None or (tmin is not None and tk < tmin - 0.05)
                                                                    or (tmax is not None and tk > tmax + 0.05)))):
                fehl |= G_TAKTE
            if fehl and (not gruppen or fehl & (fehl - 1)):
                continue
        if arten and z["art"] not in arten:
            # Hat and perc loops are mostly listed as "top" in the library (test 19.10.: 1,199 perc loops under top)
            if not (z["art"] == "top" and (("hat" in arten and " hat" in wt) or ("perc" in arten and " perc" in wt))):
                fehl |= G_ART
        if herkunft and z["herkunft"] not in herkunft:
            fehl |= G_HERKUNFT
        if lizenzen and z["lizenz"] not in lizenzen:
            fehl |= G_LIZENZ
        if genres and not genres.intersection(z["stimmung"]):
            fehl |= G_GENRE
        if fehl and (not gruppen or fehl & (fehl - 1)):
            continue
        if stimmungen and not stimmungen.intersection(z["stimmung"]):
            continue
        if tonarten and not tonart_passt(z, tonarten, grundtoene, drums_frei):
            fehl |= G_TON
            if not gruppen or fehl & (fehl - 1):
                continue
        if lo and z["lo"] != lo:
            continue
        if pack and z["pack"] != pack:
            continue
        if dmax is not None and (z["dauer"] is None or z["dauer"] > dmax):
            continue
        if dmin is not None and (z["dauer"] is None or z["dauer"] < dmin):
            continue
        if nur_gemessen and not z["gemessen"]:
            continue
        if aehnlich:
            nah = aehnlich[2](z)
            if nah is None:
                continue
            z = dict(z, aehnlich_pack=nah[0], aehnlich_punkte=nah[1], aehnlich_stufe=nah[2])
        if umgerechnet:   # pass along the converted tempo and bars (display "130 from 65")
            z = dict(z, bpm_passend=round(k, 2), bpm_passend_gefuehl=k == z["bpm_gefuehl"],
                     **({"takte_passend": round(tk, 2)} if tk is not None else {}))
        if verlauf_eintraege is not None:     # time (and for Dragged the rendered file) into the row
            e = verlauf_eintraege[z["pfad"]]
            z = dict(z, verlauf_zeit=e["zeit"], **({"verlauf_datei": e["datei"]} if e.get("datei") else {}))
        if fehl:
            extra[fehl].append(z)
        else:
            aus.append(z)

    sort, ab = eins("sort", "relevanz"), eins("ab") == "1"
    if sort == "relevanz":
        # with a search text: best matches first, on a tie the shorter name; without a search text: no songs in front,
        # then the newest file (fresh downloads and new folders on top). Several stable passes instead of one key.
        aus.sort(key=lambda z: z["name"].casefold())
        if mit:
            aus.sort(key=lambda z: len(z["name"]))
            # rank next to the row, not inside it: the rows belong to the index, concurrent searches share them
            rang = {id(z): (song_hinten(z, mit), -relevanz_punkte(z, mit)) for z in aus}
            aus.sort(key=lambda z: rang[id(z)])
            aus[:] = nach_pack_streuen(aus, lambda z: rang[id(z)])
        else:
            aus.sort(key=lambda z: z["datum"], reverse=True)
            aus.sort(key=lambda z: z["art"] == "song")
            aus[:] = nach_pack_streuen(aus, lambda z: (z["art"] == "song", z["datum"][:10]))
        if ab:
            aus.reverse()
    leer = {"bpm": lambda z: z["bpm"] is None, "tonart": lambda z: z["ton"] is None,
            "dauer": lambda z: z["dauer"] is None, "datum": lambda z: not z["datum"]}
    sortier = {
        "name": lambda z: z["name"].casefold(),
        "bpm": lambda z: (z.get("bpm_passend", z["bpm"]) or 0) if tempo else (z["bpm"] or 0),
        "tonart": lambda z: (TONARTEN.index(z["ton"]) if z["ton"] else 0, z["modus"] or ""),
        "dauer": lambda z: z["dauer"] or 0,
        "pack": lambda z: z["pack"].casefold(),
        "art": lambda z: z["art"],
        "geladen": lambda z: z.get("geladen_am") or "",
        # license from sellable to nein, then the name; date of the file (the page sends newest first)
        "lizenz": lambda z: (LIZENZ_RANG.get(z["lizenz"], 3), z["name"].casefold()),
        "datum": lambda z: (z["datum"], z["name"].casefold()),
        # Similar by closeness, the same pack first
        "naehe": lambda z: (not z.get("aehnlich_pack"), not z.get("aehnlich_stufe", True), -z.get("aehnlich_punkte", 0),
                            z["name"].casefold()),
    }.get(sort)
    if sammlung and sort == "liste":   # order as in the playlist (when added); history: newest first
        rang = verlauf_rang if verlauf_rang is not None else sammlungen.reihenfolge(sammlung)
        aus.sort(key=lambda z: rang.get(z["pfad"], 1e9), reverse=ab)
        return (aus, dict(extra)) if gruppen else aus
    if sortier:
        aus.sort(key=sortier, reverse=ab)
        if sort in leer:   # empty values always at the end
            aus.sort(key=leer[sort])
    if sammlung or (aehnlich and aehnlich[1].get("klang_fehler")):     # service error: ask again next time
        return (aus, dict(extra)) if gruppen else aus
    SUCH_CACHE[schluessel] = aus
    while len(SUCH_CACHE) > 24:
        SUCH_CACHE.popitem(last=False)
    if gruppen:
        GRUPPEN_CACHE[schluessel] = (aus, dict(extra))
        while len(GRUPPEN_CACHE) > 24:
            GRUPPEN_CACHE.popitem(last=False)
        return GRUPPEN_CACHE[schluessel]
    return aus


# Random loop: loops in the library on 20.10.2026: break 3,810, melodisch 2,550, top 2,435, bass 758, kickloop 300, fill 74
ZUFALL_TYPEN = {"drums": {"break", "kickloop", "fill"}, "perc": {"top", "perc", "hat", "cymbal"},
                "melodie": {"melodisch", "bass"}, "alle": None}


def zufall(p):
    """A random loop by type and tempo. ohne=numbers of the ones shown last, so that nothing comes back right away."""
    import random

    def eins(k, std=""):
        return (p.get(k) or [std])[0]
    if eins("typ") == "suche":
        return _zufall_aus_suche(p, eins)
    arten = ZUFALL_TYPEN.get(eins("typ", "alle"), None)
    bmin, bmax = tempo_bereich(p, eins)     # widened with bpm_toleranz
    halb = eins("halb") == "1"
    # Key (project frame): compare melodic loops, drums without a note stay in
    tonarten, grundtoene = tonart_mengen({x.strip() for x in eins("tonart").split(",") if x.strip()},
                                         eins("passend") == "1", eins("ton_abstand"))
    tempo = bmin is not None or bmax is not None

    def tempo_ok(z):   # the same rule as in the library (tempo_passend)
        return not tempo or tempo_passend(z["bpm"], z["bpm_gefuehl"], bmin, bmax, halb) is not None
    lizenzen = {x for x in eins("lizenz").split(",") if x}
    weg = set() if eins("verworfene") == "1" else sammlungen.verworfen()      # discarded ones
    kand = [i for i, z in enumerate(INDEX.zeilen)
            if z["lo"] == "loop" and not z["ist_kopie"] and z["pfad"] not in weg and (arten is None or z["art"] in arten)
            and (not lizenzen or z["lizenz"] in lizenzen) and tempo_ok(z)
            and (not tonarten or tonart_passt(z, tonarten, grundtoene, True))]
    if not kand:
        return {"gesamt": 0, "zeile": None}
    i, nie = _zufall_waehlen(kand, eins, lambda i: INDEX.zeilen[i]["pfad"])
    z = INDEX.zeilen[i]
    zeile = dict(oeffentlich(z), nr=i)
    k = tempo_passend(z["bpm"], z["bpm_gefuehl"], bmin, bmax, halb) if tempo else None
    if k is not None and abs(k - z["bpm"]) > 0.01:     # on the card "140 from 70" as in the list
        zeile.update(bpm_passend=round(k, 2), bpm_passend_gefuehl=k == z["bpm_gefuehl"])
    return {"gesamt": len(kand), "zeile": zeile, **nie}


def _zufall_waehlen(kand, eins, pfad_von):
    """A number from kand: not among the ones shown last (ohne); with nie_zuerst never played ones first.
    -> (number, {"nie_gehoert": count of the never played ones in kand} or {})."""
    import random
    ohne = {int(x) for x in eins("ohne").split(",") if x.isdigit()}
    pool = [i for i in kand if i not in ohne] or kand
    if eins("nie_zuerst") != "1":
        return random.choice(pool), {}
    gehoert = verlauf.gehoert_menge()
    nie = [i for i in kand if pfad_von(i) not in gehoert]
    frisch = [i for i in pool if pfad_von(i) not in gehoert] or [i for i in nie if i not in ohne] or nie or pool
    return random.choice(frisch), {"nie_gehoert": len(nie)}


_POS = {"version": None, "nr": {}}


def index_nummer(pfad):
    """Position of a row in INDEX.zeilen (the number Random sends as nr), new per index state."""
    if _POS["version"] != INDEX.version:
        _POS["nr"] = {z["pfad"]: i for i, z in enumerate(INDEX.zeilen)}
        _POS["version"] = INDEX.version
    return _POS["nr"].get(pfad)


def _zufall_aus_suche(p, eins):
    """A loop from the matches of the library search (the same parameters as the list, a collection or playlist
    too). Random only plays loops; treffer counts all matches, gesamt the loops among them."""
    q = {k: v for k, v in p.items() if k in SUCHE_PARAMETER and k not in ("offset", "limit")}
    treffer = filtern(q)
    loops = {}
    for z in treffer:
        if z["lo"] == "loop":
            i = index_nummer(z["pfad"])
            if i is not None:
                loops[i] = z
    if not loops:
        return {"gesamt": 0, "treffer": len(treffer), "zeile": None}
    i, nie = _zufall_waehlen(list(loops), eins, lambda i: loops[i]["pfad"])
    zeile = dict(oeffentlich(loops[i]), nr=i)
    for k in ("aehnlich_pack", "aehnlich_punkte", "aehnlich_stufe", "verlauf_zeit", "verlauf_datei"):
        zeile.pop(k, None)
    return {"gesamt": len(loops), "treffer": len(treffer), "zeile": zeile, **nie}


def tonart_zahlen(zeilen, drums_frei=False):
    """Count per key chip = matches of the search for exactly this key (check 24.10.: Fm showed 271, the search
    found 1,489). Like tonart_passt(): rows that carry only the root note (one-shots without mode, bass and co.) count
    for major and minor of their root note; with drums_ohne_ton, drums without a key count for every key."""
    c = collections.Counter()
    drums = 0
    for z in zeilen:
        t = z["ton"]
        if t is None:
            if drums_frei and z["art"] in DRUM_ARTEN:
                drums += 1
            continue
        if grundton_zeile(z) or (z["lo"] == "oneshot" and not z["modus"]):
            c[t] += 1
            c[t + "m"] += 1
        else:
            c[t + z["modus"]] += 1
    if drums:
        for t in TONARTEN:
            for m in ("", "m"):
                c[t + m] += drums
    return dict(c)


def genre_zahlen(zeilen):
    genre = collections.Counter()
    for z in zeilen:
        if z["stimmung"]:
            treffer = {WORT_GENRE[w] for w in z["stimmung"] if w in WORT_GENRE}
            if treffer:
                genre.update(treffer)
    return [[k, genre.get(k, 0)] for k in GENRE_GRUPPEN]      # left column, fixed order


def gehoer_zahlen(zeilen, felder):
    """gehoer values in one pass; fixed order, also with 0 (the chips always stand the same, 0 shows the missing
    analysis)."""
    g = {f: collections.Counter() for f in ("takte", "swing", "gefuehl", "live", "klasse")}
    mit_takte = "takte" in felder
    andere = [f for f in ("swing", "gefuehl", "live", "klasse") if f in felder]
    for z in zeilen:
        if mit_takte:
            t = z.get("takte_passend", z["takte"])     # counted at the matching tempo, as in the list
            if t is not None:
                n = int(round(t))
                if abs(t - n) <= 0.1:
                    g["takte"][str(n)] += 1
        for f in andere:
            if z[f]:
                g[f][z[f]] += 1
    werte = {"takte": [str(n) for n in TAKTE_CHIPS], "swing": SWING_KLASSEN, "gefuehl": ["halftime"],
             "live": ["ja", "nein"], "klasse": EINZELSCHLAG}
    return {f: [[w, g[f].get(w, 0)] for w in werte[f]] for f in felder}


def facetten(zeilen, nur=None, drums_frei=False):
    """nur: set of facets that are needed (counting without its own selection needs only its own per group)."""
    def zaehl(feld, liste=False):
        c = collections.Counter()
        for z in zeilen:
            for w in (z[feld] if liste else [z[feld]]):
                if w:
                    c[w] += 1
        return c.most_common()
    if nur is not None:
        teil = {}
        for feld in ("art", "herkunft", "lizenz"):
            if feld in nur:
                teil[feld] = zaehl(feld)
        if "tonart" in nur:
            teil["tonart"] = tonart_zahlen(zeilen, drums_frei)
        if "genre" in nur:
            teil["genre"] = genre_zahlen(zeilen)
        gh = nur & {"takte", "swing", "gefuehl", "live", "klasse"}
        if gh:
            teil.update(gehoer_zahlen(zeilen, gh))
        return teil
    return {
        "genre": genre_zahlen(zeilen),
        "art": zaehl("art"), "herkunft": zaehl("herkunft"), "lizenz": zaehl("lizenz"),
        # the vibe live lives in the filter played live (otherwise live twice with different numbers, test 20.10.)
        "stimmung": [x for x in zaehl("stimmung", True) if x[0] != "live"][:400], "lo": zaehl("lo"),
        "tonart": tonart_zahlen(zeilen, drums_frei),
        **gehoer_zahlen(zeilen, ("takte", "swing", "gefuehl", "live", "klasse")),
        "mit_bpm": sum(1 for z in zeilen if z["bpm"] is not None),
        "mit_tonart": sum(1 for z in zeilen if z["ton"]),
        "lizenz_erklaerung": {k: tx(v) for k, v in LIZENZ_ERKLAERUNG.items()},
    }


# Left column (24.10.2026): every group counts without its own selection. With Hat selected, Snare shows how many
# snares there would be with the other filters (before 0, and with a fixed order the whole group turned pale after
# every click). Per group: (parameters that drop out; facets that come from them). Type and class are one group,
# because in one-shot mode the column filters Kick to Rim via the class and Bass, Vocal, FX via the type.
FACETTEN_GRUPPEN = [
    (("art", "art_nicht", "klasse", "klasse_nicht"), ("art", "klasse")),
    (("genre", "genre_nicht"), ("genre",)),
    (("tonart", "passend", "ton_abstand", "drums_ohne_ton"), ("tonart",)),
    (("takte", "takte_nicht", "takte_min", "takte_max"), ("takte",)),
    (("swing", "swing_nicht"), ("swing",)),
    (("gefuehl", "gefuehl_nicht"), ("gefuehl",)),
    (("live", "live_nicht"), ("live",)),
    (("herkunft", "herkunft_nicht"), ("herkunft",)),
    (("lizenz", "lizenz_nicht"), ("lizenz",)),
]


GRUPPEN_NAMEN = ("art", "genre", "tonart", "takte", "swing", "gefuehl", "live", "herkunft", "lizenz")


def gruppen_gesetzt(p):
    """Bits of the facet groups for which p has its own selection."""
    gesetzt = {k for k, v in p.items() if any(x.strip() for x in v)}
    return [(GRUPPEN_BIT[n], felder) for n, (parameter, felder) in zip(GRUPPEN_NAMEN, FACETTEN_GRUPPEN)
            if gesetzt.intersection(parameter)]


def facetten_ohne_eigene(p, f, treffer=None, extra=None):
    """f: facets of the matches. For every group with its own selection its counts are recounted without that
    selection (all other filters apply). treffer, extra: from filtern(p, gruppen=True); the rows without the selection
    of a group are matches plus extra[bit], no further search needed (check 24.10.: before, +43 ms)."""
    gesetzt = gruppen_gesetzt(p)
    if gesetzt and extra is None:
        treffer, extra = filtern(p, gruppen=True)
    drums_frei = (p.get("drums_ohne_ton") or [""])[0] == "1"
    for bit, felder in gesetzt:
        neu = facetten(treffer + extra.get(bit, []), nur=set(felder), drums_frei=drums_frei)
        for feld in felder:
            f[feld] = neu[feld]
    f["ohne_eigene"] = True
    return f


LIZENZ_RANG = {"ja": 0, "unklar": 1, "ungeprüft": 2, "unbekannt": 3, "nein": 4}    # sorting by license


def oeffentlich(z):
    # "_" in front: only for the server (search words without type, state before a correction)
    return {k: v for k, v in z.items() if k not in ("woerter", "gruppe") and not k.startswith("_")}


# ================================================================ Analysis (gehoer)

IN_ARBEIT = "Analyse nicht abgeschlossen"     # start of gehoer's note while a file is being analyzed (gehoer's wording)
_SCAN_STAND = {"zeit": 0.0, "wert": None}


def _lebt(pid):
    return plattform.lebt(pid)


def _vorfahren(pid):
    """Parent processes of a PID (ps). vollscan.sh writes the PID of its bash shell into the PID file, laeufe holds
    the one of the Python scan below it (check 20.10.: "vollscan" was never true because of that)."""
    try:
        aus = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    eltern = {}
    for zeile in aus.splitlines():
        teile = zeile.split()
        if len(teile) == 2 and teile[0].isdigit() and teile[1].isdigit():
            eltern[int(teile[0])] = int(teile[1])
    kette, p = set(), eltern.get(int(pid))
    while p and p > 1 and p not in kette:
        kette.add(p)
        p = eltern.get(p)
    return kette


def fremder_scan():
    """Is a gehoer scan running on the main database right now that Open Libary did not start?
    -> None or {"seit", "dateien", "fertig", "auftraege", "rest_s", "vollscan"}; vollscan: started by the full scan
    script (otherwise another gehoer scan). Keeps the answer for 20 s. Errors of any kind mean None: the answer hangs
    on every first search page, it must not make any search fail."""
    if time.time() - _SCAN_STAND["zeit"] < 20:
        return _SCAN_STAND["wert"]
    try:
        wert = _fremder_scan()
    except Exception as e:
        print(f"Scan state not readable: {type(e).__name__}: {e}", flush=True)
        wert = None
    _SCAN_STAND.update(zeit=time.time(), wert=wert)
    return wert


def _fremder_scan():
    wert = None
    eigene = MESSDIENST.eigene_pids() if MESSDIENST else set()
    vollscan_pid = None
    try:
        vollscan_pid = int(VOLLSCAN_PID.read_text().strip())
    except (OSError, ValueError):
        pass
    if vollscan_pid and not _lebt(vollscan_pid):
        vollscan_pid = None
    if GEHOER_DB.is_file():
        try:
            con = sqlite3.connect(str(GEHOER_DB), timeout=5)
            try:
                con.execute("PRAGMA query_only=1")
                l = [r for r in con.execute("SELECT lauf, pid, start, gesamt FROM laeufe WHERE status='laeuft' "
                                            "ORDER BY start DESC")
                     if _lebt(r[1]) and r[1] not in eigene]
                if l:
                    lauf, pid, start, gesamt = l[0]
                    jetzt = time.time()
                    fertig, offen, juengst = con.execute(
                        "SELECT sum(status='fertig'), sum(status<>'fertig'), sum(status='fertig' AND ende>=?) "
                        "FROM auftraege WHERE lauf=?", (jetzt - 900, lauf)).fetchone()
                    tempo = (juengst or 0) / min(900.0, max(60.0, jetzt - (start or jetzt)))     # jobs per second
                    wert = {"seit": start, "dateien": gesamt, "fertig": fertig or 0, "auftraege": (fertig or 0) + (offen or 0),
                            "rest_s": round((offen or 0) / tempo) if tempo > 0 and offen else None,
                            "vollscan": bool(vollscan_pid) and (vollscan_pid == pid or vollscan_pid in _vorfahren(pid))}
            finally:
                con.close()
        except sqlite3.Error:
            pass        # locked or without the table laeufe: then only the PID file counts
    if wert is None and vollscan_pid:
        wert = {"seit": None, "dateien": None, "fertig": None, "auftraege": None, "rest_s": None, "vollscan": True}
    return wert


_VOLLSCAN_FERTIG = {"zeit": 0.0, "wert": None}


def vollscan_fertig():
    """Has a full scan over the library run through (run "fertig" with at least half as many files as the index)?
    -> time of the end or None. After that, an empty value means "not measurable", not "not analyzed yet" (check
    20.10.). Keeps the answer for 60 s."""
    if time.time() - _VOLLSCAN_FERTIG["zeit"] < 60:
        return _VOLLSCAN_FERTIG["wert"]
    wert = None
    try:
        if GEHOER_DB.is_file():
            con = sqlite3.connect(str(GEHOER_DB), timeout=5)
            try:
                con.execute("PRAGMA query_only=1")
                r = con.execute("SELECT max(ende) FROM laeufe WHERE status='fertig' AND gesamt>=?",
                                (max(1000, len(INDEX.zeilen) // 2),)).fetchone()
                wert = r[0] if r else None
            finally:
                con.close()
    except Exception as e:
        print(f"Full scan state not readable: {type(e).__name__}: {e}", flush=True)
    _VOLLSCAN_FERTIG.update(zeit=time.time(), wert=wert)
    return wert


def _fehler_satz(text):
    """gehoer's error text in one sentence for the download list (gehoer's own notes are German)."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if t.startswith("Zeitüberschreitung"):
        return tx("Analysis stopped: it hung for too long.")
    if t.startswith("Analyse zweimal abgebrochen"):
        return tx("Analysis crashed twice, the file is probably damaged.")
    if t.startswith(IN_ARBEIT):
        return tx("Analysis not finished (gehoer was stopped).")
    t = re.sub(r"^[A-Za-z]+(Error|Exception|Fehler): ", "", t)        # class name in front (LadeFehler: ...)
    if re.match(r"(ffprobe kann die Datei nicht lesen|ffmpeg kann die Datei nicht dekodieren|keine Tonspur)", t):
        return tx("Analysis failed: the file cannot be read (damaged or no audio).")
    t = re.sub(r":?\s*/[^:]*/[^/:]*$", "", t)                          # drop the path at the end
    return tx("Analysis failed: {grund}.", grund=t[:140].rstrip(" .:") or tx("no details"))


LANG_TEXT = N_(
    "Over 5 minutes long: gehoer analyzes the file once the running scan (usually the full scan) is done; both together needed too much memory.")


class Messdienst:
    """A background thread analyzes one file after another with gehoer (nice -n 10, OPENLIBARY_MESS_PRIO): every file
    that netz.py has finished downloading and sorting in, and every one someone presses "Analyze" for.
    The target is always NETZ_DB (a separate file, no competition with the full scan), since the evening of 20.10.2026
    also for "Analyze" on a file of the library. Before, a click went into the main database as long as no other scan
    ran there. The data stayed intact (test with two scans on a test database), but gehoer bibliothek stand and
    vollscan.sh --stand show the latest run, so a one-file run, and gehoer deletes the jobs of finished runs on the
    next run: a click after the full scan would have pushed out its summary. Also, clicks on checked Web packs landed
    in NETZ_DB via their path anyway, and a full scan that began between click and start got the one-file run after
    all (check 20.10.). The full scan analyzes the library; a click only re-analyzes.
    One gehoer call analyzes all waiting files (the models load 30 to 90 s per process), at most STAPEL at a time,
    clicks on "Analyze" before the downloads. Files from 5 min on wait while another scan runs: gehoer alone needs
    7.8 to 22 GB for them, the full scan with 3 workers 13 to 17.6 GB. The thread reads the state per file from the
    database every 2 s: waiting, analyzing, analyzed or the error in one sentence."""
    STAPEL = 25
    BEHALTEN_S = 3 * 3600          # show finished entries this long

    def __init__(self):
        self.lock = threading.Lock()
        self.warteschlange = collections.OrderedDict()   # pfad -> {"db": Path, "neu": bool}
        self.stand = {}                                  # pfad -> {"status", "text", "ziel", "zeit", "v"}
        self.wecker = threading.Event()
        self._pids = set()
        self._dauern = {}                                # pfad -> seconds (ffprobe, for files outside the index)
        threading.Thread(target=self._lauf, daemon=True, name="messen").start()

    def eigene_pids(self):
        with self.lock:
            return set(self._pids)

    def dauer(self, p):
        """Length in seconds: from the index, otherwise ffprobe (freshly downloaded files only appear there after the
        next reload); None when unknown."""
        z = INDEX.nach_pfad.get(p) if INDEX else None
        if z is not None and z["dauer"] is not None:
            return z["dauer"]
        if p not in self._dauern:
            d = None
            try:
                r = subprocess.run([netz.KONFIG.get("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                                    "format=duration", "-of", "default=nw=1:nk=1", p],
                                   capture_output=True, text=True, timeout=20, **plattform.hilfsprozess_args())
                d = zahl(r.stdout.strip())
            except (OSError, subprocess.SubprocessError):
                pass
            self._dauern[p] = d
        return self._dauern[p]

    def lang_zurueck(self, p, scan):
        """Does the file wait until the other scan is done (from 5 min on, see above)?"""
        return bool(scan) and (self.dauer(p) or 0) >= GEHOER_ALLEIN_S

    def dazu(self, pfade, klick=False):
        """Queue files. klick: from the "Analyze" button (analyze again, queue in front). -> note for the interface
        ("" or why the file is still waiting)."""
        hinweis = ""
        scan = fremder_scan() if klick else None       # outside the lock: fremder_scan asks eigene_pids
        with self.lock:
            for p in pfade:
                p = nfc(str(p))
                if self.stand.get(p, {}).get("status") == "misst":
                    continue
                if p in self.warteschlange and not klick:
                    continue
                self.warteschlange[p] = {"db": NETZ_DB, "neu": klick}
                if klick:     # a click analyzes again and comes before the downloads
                    self.warteschlange.move_to_end(p, last=False)
                self.stand[p] = {"status": "wartet", "text": "", "ziel": "netz", "zeit": time.time()}
        if klick and any(self.lang_zurueck(nfc(str(p)), scan) for p in pfade):
            hinweis = tx(LANG_TEXT)
        self.wecker.set()
        return hinweis

    def stand_fuer(self, pfade=None):
        with self.lock:
            grenze = time.time() - self.BEHALTEN_S
            for p in [p for p, s in self.stand.items() if s["status"] in ("gemessen", "fehler") and s["zeit"] < grenze]:
                del self.stand[p]
            if pfade is None:
                return {p: dict(s) for p, s in self.stand.items()}
            return {p: dict(self.stand[p]) for p in pfade if p in self.stand}

    def _setzen(self, p, status, text="", im_index=False):
        """v: the interface refreshes the row once index_version is greater than v. That is the last started load
        (INDEX.gestartet): a run that read before the analysis ended does not have the new values yet (check 20.10.:
        otherwise the row came with the old state and was then forgotten). im_index: the values are already in."""
        with self.lock:
            if p in self.stand:
                v = (INDEX.version - 1 if im_index else INDEX.gestartet) if INDEX else 0
                self.stand[p].update(status=status, text=text, zeit=time.time(), v=v)

    def _stapel(self):
        """The next files of the same kind (new or not), at most STAPEL; long files stay put while another scan runs
        (then with a note)."""
        scan = fremder_scan()        # outside the lock: fremder_scan asks eigene_pids
        with self.lock:
            offen = list(self.warteschlange.items())
        zurueck = {p for p, a in offen if self.lang_zurueck(p, scan)}     # ffprobe outside the lock
        with self.lock:
            for p in zurueck:
                if p in self.stand and self.stand[p]["status"] == "wartet":
                    self.stand[p]["text"] = tx(LANG_TEXT)
            frei = [(p, a) for p, a in self.warteschlange.items() if p not in zurueck]
            if not frei:
                return None
            erster = frei[0][1]
            pfade = [p for p, a in frei if a["db"] == erster["db"] and a["neu"] == erster["neu"]][:self.STAPEL]
            for p in pfade:
                del self.warteschlange[p]
                self.stand[p].update(status="misst", text="", zeit=time.time())
            return erster["db"], erster["neu"], pfade

    def _klick_wartet(self):
        with self.lock:
            return any(a["neu"] for a in self.warteschlange.values())

    def _lauf(self):
        while True:
            self.wecker.wait(10)
            self.wecker.clear()
            # Downloads come shortly one after another (test 20.10.: three sounds in 6 s, the first file started gehoer
            # alone, the others loaded the models a second time): collect until nothing comes for 4 s, at most 30 s;
            # a click on "Analyze" does not wait
            beginn = time.time()
            while not self._klick_wartet() and time.time() - beginn < 30 and self.wecker.wait(4):
                self.wecker.clear()
            while True:
                s = self._stapel()
                if not s:
                    break
                gerechnet = True
                try:
                    gerechnet = self._messen(*s)
                except Exception as e:     # the thread must never die
                    for p in s[2]:
                        self._setzen(p, "fehler", tx("Analysis failed: {grund}", grund=f"{type(e).__name__}: {e}")[:200])
                if gerechnet:
                    try:
                        INDEX.laden()      # new analysis values in the list at once, not only after a minute
                    except Exception as e:
                        print(f"Index not reloaded after the analysis: {e}", flush=True)

    @staticmethod
    def _zeilen(db, pfade):
        """{pfad: (analysiert_am, fehler, groesse, mtime)} of the paths in a gehoer database (read only)."""
        if not db.is_file():
            return {}
        try:
            con = sqlite3.connect(str(db), timeout=5)
            try:
                con.execute("PRAGMA query_only=1")
                aus = {}
                for i in range(0, len(pfade), 400):
                    teil = pfade[i:i + 400]
                    for r in con.execute(f"SELECT pfad, analysiert_am, fehler, groesse, mtime FROM dateien WHERE pfad IN "
                                         f"({', '.join('?' * len(teil))})", teil):
                        aus[nfc(r[0])] = tuple(r[1:])
                return aus
            finally:
                con.close()
        except sqlite3.Error:
            return {}

    def _messen(self, db, neu, pfade):
        """One gehoer call for the paths. -> True when gehoer ran (then the index reloads)."""
        # already analyzed (same size and time, without an error, in one of the two databases): done without gehoer
        if not neu:
            rest = []
            bekannt = {**self._zeilen(GEHOER_DB, pfade), **{p: r for p, r in self._zeilen(NETZ_DB, pfade).items() if not r[1]}}
            for p in pfade:
                r = bekannt.get(p)
                try:
                    st = os.stat(p)
                except OSError:
                    self._setzen(p, "fehler", tx("File is missing on disk."))
                    continue
                if r and not r[1] and r[2] == st.st_size and abs((r[3] or 0) - st.st_mtime) < 1e-3:
                    self._setzen(p, "gemessen", im_index=True)
                else:
                    rest.append(p)
            pfade = rest
        if not pfade:
            return False
        if not (os.access(GEHOER, os.X_OK) or shutil.which(GEHOER)):
            for p in pfade:
                self._setzen(p, "fehler", tx("gehoer is not installed ({pfad}).", pfad=GEHOER))
            return False
        vorher = self._zeilen(db, pfade)
        liste = tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="openlibary-messen-", delete=False, encoding="utf-8")
        with liste:
            liste.write("\n".join(pfade) + "\n")
        self.liste_jetzt = liste.name          # cleaned up when the server exits (main, beenden)
        befehl = MESS_PRIO + [GEHOER, "bibliothek", "scan", "--liste", liste.name, "--arbeiter", "1", "--db", str(db)]
        if neu:
            befehl.append("--neu")
        ausgabe = tempfile.TemporaryFile()
        # Emergency brake only after gehoer's own watchdog (per file 120 s plus 4 s per second of audio, the first one
        # 600 s longer) plus 10 min of reserve: then gehoer notes the timeout itself, and a new attempt skips the
        # file. Before, 600 + 120 s per file applied, so Open Libary always stopped first, gehoer deleted its note,
        # and the file came up again on every start (check 20.10.)
        frist = time.time() + GEHOER_ZUSCHLAG_S + 600 + sum(
            GEHOER_FRIST_S + GEHOER_FRIST_JE_S * (self.dauer(p) or 60.0) for p in pfade)
        zu_lange = False
        try:
            proc = subprocess.Popen(befehl, stdout=ausgabe, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            with self.lock:
                self._pids.add(proc.pid)
            try:
                while True:
                    try:
                        proc.wait(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    self._abgleichen(db, pfade, vorher, None)
                    if time.time() > frist:
                        zu_lange = True
                        proc.terminate()      # gehoer stops cleanly and notes the running file
                        try:
                            proc.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()       # otherwise a zombie stays, and os.kill(pid, 0) takes it for alive
                        break
            finally:
                with self.lock:
                    self._pids.discard(proc.pid)
            ausgabe.seek(0)
            text = ausgabe.read().decode("utf-8", errors="replace")
        except OSError as e:
            text = f"gehoer did not start: {e}"
        finally:
            ausgabe.close()
            try:
                os.unlink(liste.name)
            except OSError:
                pass
        if zu_lange:     # same first word as gehoer's own timeout note (matched below)
            text += "\nZeitüberschreitung: gehoer brauchte zu lange und wurde beendet"
        self._abgleichen(db, pfade, vorher, text)
        return True

    def _abgleichen(self, db, pfade, vorher, ende_text):
        """State per file from the database: a new row without an error means analyzed, with an error the error; still
        "in progress" or unchanged means analyzing. At the end (ende_text is gehoer's output, otherwise None): unchanged
        and error-free means analyzed (gehoer already had the file), without a row the reason from the output."""
        ende = ende_text is not None
        jetzt = self._zeilen(db, pfade)
        zeilen = [z.strip() for z in (ende_text or "").splitlines() if z.strip()]
        zu_lange = any(z.startswith("Zeitüberschreitung") for z in zeilen)    # Open Libary's emergency brake
        for p in pfade:
            r = jetzt.get(p)
            if r is not None and (r[1] or "").startswith(IN_ARBEIT):
                if ende:
                    self._setzen(p, "fehler", _fehler_satz("Zeitüberschreitung" if zu_lange else r[1]))
                continue
            if r is not None and r != vorher.get(p):
                self._setzen(p, "fehler" if r[1] else "gemessen", _fehler_satz(r[1]) if r[1] else "")
            elif ende:
                if r is not None and not r[1]:
                    self._setzen(p, "gemessen")
                elif r is not None:
                    self._setzen(p, "fehler", _fehler_satz(r[1]))
                else:
                    self._setzen(p, "fehler", self._ohne_zeile(p, zeilen))

    @staticmethod
    def _ohne_zeile(p, zeilen):
        """Sentence for a file gehoer wrote no row for. Before, gehoer's last output line stood there, e.g.
        "Messung gescheitert: Fertig: 0 analysiert, 0 schon erfasst, ... 1 s." (check 20.10.)."""
        if not os.path.isfile(p):
            return tx("File is missing on disk.")
        if any(z.startswith("Zeitüberschreitung") for z in zeilen):
            return _fehler_satz("Zeitüberschreitung")
        if any(z.startswith("gehoer did not start") for z in zeilen):
            return tx("Analysis failed: gehoer could not be started.")
        eigene = next((z for z in zeilen if p in z and not z.startswith(("Fertig", "Abgebrochen"))), None)
        if eigene:      # gehoer names the path when it could not read the file: the reason follows it
            vor, _, nach = eigene.partition(p)
            grund = nach.strip(" :.-") or vor.strip(" :.-")
            if grund:
                return _fehler_satz(grund)
        return tx("Analysis failed: gehoer did not record the file.")

    def nachholen(self):
        """At start: Web downloads from zusaetze.csv that are in neither database (downloaded before this existed or
        while the server was not running) go into the queue. If a row is already there, even with an error, the file
        stays put (analyzing again goes through the button)."""
        pfade = [z["pfad"] for z in INDEX.zeilen if z["herkunft"] == "openlibary" and not z["gemessen"]
                 and os.path.isfile(z["pfad"])]
        if pfade:
            bekannt = set(self._zeilen(GEHOER_DB, pfade)) | set(self._zeilen(NETZ_DB, pfade))
            pfade = [p for p in pfade if p not in bekannt]
        if pfade:
            print(f"Analysis: {len(pfade)} Web downloads without analysis put into the queue", flush=True)
            self.dazu(pfade)


MESSDIENST = None
ORDNER = None
LEICHT = None                # light analysis (leicht.py), None before the start
KLANG = None                 # sound fingerprint (klangnaehe.py), None without the gehoer environment
BERICHTE = None              # problem reports (bericht.py), None before the start
ORDNER_MESSEN_MAX = 300      # new files of a folder that gehoer analyzes on its own; more through selection and "Analyze"
ORDNER_TAKT_S = 3600         # rescan your own folders every hour (new and deleted files)


def werkzeuge():
    """What exists on this computer; for Settings (setting it up for others)."""
    return {"ffmpeg": os.path.exists(FFMPEG), "ffprobe": os.path.exists(netz.KONFIG["ffprobe"] or ""),
            "rubberband": os.path.exists(RUBBERBAND), "gehoer": os.path.exists(GEHOER),
            "klang": bool(KLANG and KLANG.verfuegbar() and KLANG.zahl()), "leicht": bool(LEICHT and LEICHT.da())}


def leicht_befehl():
    """Light analysis: the Python version (leichtmessen.py) where numpy and soundfile exist (packaged apps on all
    systems, the gehoer environment on the author's Mac), otherwise on the Mac the Swift version. -> command or None."""
    pythons = [os.environ.get("OPENLIBARY_LEICHT_PY"), sys.executable, str(GEHOER_PY)]
    for py in pythons:
        if not py or not os.path.exists(py):
            continue
        try:
            ok = subprocess.run([py, "-I", "-c", "import numpy, soundfile"], capture_output=True, timeout=60,
                                **plattform.hilfsprozess_args()).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            return [py, "-I", str(HIER / "leichtmessen.py")]
    if LEICHT_SWIFT.is_file() and os.access(LEICHT_SWIFT, os.X_OK):
        return [str(LEICHT_SWIFT)]
    return None


def leicht_kandidaten():
    """Rows without a gehoer analysis: tempo, root note and rough sound fingerprint (for Similar without gehoer)."""
    return [z["pfad"] for z in INDEX.zeilen if not z["gemessen"] and not z["ist_kopie"]]


def leicht_neu_laden():
    threading.Thread(target=INDEX.laden, daemon=True).start()


def ordner_bekannt():
    return {p for p, z in INDEX.nach_pfad.items() if not z.get("_eigen")}


def ordner_eingelesen(neue):
    """After scanning: index at once, then gehoer and sound fingerprint for what is new (only when available)."""
    INDEX.laden()
    neue = [p for p in neue if p in INDEX.nach_pfad]      # do not analyze renders and skipped files
    if neue and LEICHT:
        LEICHT.wecken()
    if neue and os.path.exists(GEHOER):
        MESSDIENST.dazu(sorted(neue)[:ORDNER_MESSEN_MAX])
    if neue and KLANG:
        KLANG.rechnen_lassen([(klang_schluessel(INDEX.nach_pfad[p]), p) for p in sorted(neue)[:KLANG_AUTO_MAX]
                              if p in INDEX.nach_pfad])


def fertig_geladen(pfade):
    """netz.py has finished sorting in files: gehoer analyzes, the sound fingerprint is added. Without gehoer (other
    computers) nothing is queued: before, every download showed "gehoer is not installed"."""
    if os.path.exists(GEHOER):
        MESSDIENST.dazu(pfade)
    elif LEICHT:
        LEICHT.wecken()
    if KLANG:
        KLANG.rechnen_lassen([(k, p) for p in pfade for k in [klangnaehe.datei_schluessel(p)] if k])


def ordner_takt():
    ORDNER.einlesen()
    t = threading.Timer(ORDNER_TAKT_S, ordner_takt)
    t.daemon = True
    t.start()


def netz_geprueft():
    """The packs checked against the original source on 17.10.2026 (netz-urteile.tsv), with a folder on disk."""
    zaehler = collections.Counter(z["pack"] for z in INDEX.zeilen)
    aus = []
    for pack, r in INDEX.urteile.items():
        ordner = next((str(o) for o in (MUSIK / "Drum Kits/Frei (Netz)" / pack, MUSIK / "Samples/Frei (Netz)" / pack)
                       if o.is_dir()), "")
        aus.append({"pack": pack, "urteil": r["urteil"], "grund": r["grund"], "namensnennung": r["namensnennung"],
                    "quelle": r["quelle_url"], "ordner": ordner, "dateien": zaehler.get(pack, 0)})
    return aus


# ================================================================ Audio

_WANDEL_LOCKS = collections.defaultdict(threading.Lock)


def gewandelt(pfad):
    """WAV copy for formats the browser does not play (AIFF, Ogg in the WAV container of the FL packs). One lock per
    file: before, concurrent requests wrote into the same .teil file (5 of 6 ended with error 500)."""
    WANDEL_CACHE.mkdir(exist_ok=True)
    st = os.stat(pfad)
    schluessel = hashlib.md5(f"{pfad}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()
    ziel = WANDEL_CACHE / (schluessel + ".wav")
    with _WANDEL_LOCKS[schluessel]:
        if ziel.is_file():
            os.utime(ziel)
            return str(ziel)
        tmp = WANDEL_CACHE / f"{schluessel}.{os.getpid()}.{threading.get_ident()}.teil.wav"
        # AIFF without ffmpeg (Windows and Linux mostly lack it; checked bit-identical with ffmpeg)
        if pfad.lower().endswith((".aif", ".aiff", ".aifc")):
            try:
                audioform.aiff_zu_wav(pfad, tmp)
                os.replace(tmp, ziel)
                _wandel_aufraeumen()
                return str(ziel)
            except (ValueError, OSError, struct_fehler):
                pass
        if not os.path.exists(FFMPEG):      # without ffmpeg: soundfile, when available (packaged apps)
            audioform.wav_aus(pfad, str(tmp))
            os.replace(tmp, ziel)
            _wandel_aufraeumen()
            return str(ziel)
        r = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", pfad, "-c:a", "pcm_s16le", str(tmp)],
                           capture_output=True, timeout=120, **plattform.hilfsprozess_args())
        if r.returncode != 0:
            # Ogg Vorbis in the WAV container (around 3,000 files of the FL packs): ffmpeg finds no decoder. audioform
            # pulls out the Ogg stream (standard library only; before via the gehoer environment, which friends lack).
            ogg = audioform.ogg_in_wav(pfad)
            if not ogg:
                raise RuntimeError(tx("Format not readable: {detail}", detail=r.stderr.decode(errors="replace")[-200:]))
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "ogg", "-i", "pipe:0", "-c:a", "pcm_s16le", str(tmp)],
                           input=ogg, check=True, timeout=120, **plattform.hilfsprozess_args())
        os.replace(tmp, ziel)
    _wandel_aufraeumen()
    return str(ziel)


import struct as _struct
struct_fehler = _struct.error


def als_wav(pfad):
    """WAV version for dragging (DAWs without OGG, FLAC or MP3: Logic, Pro Tools). Stays under the renders
    ("als WAV"), because a DAW project points to the file; same source, same state: the same file."""
    st = os.stat(pfad)
    kennung = hashlib.md5(f"{pfad}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:8]
    ordner = RENDER_ZIEL / "als WAV"
    ordner.mkdir(parents=True, exist_ok=True)
    ziel = ordner / f"{Path(pfad).stem} ({kennung}).wav"
    with _WANDEL_LOCKS["wav:" + kennung]:
        if not ziel.is_file():
            audioform.wav_aus(pfad, str(ziel), FFMPEG if os.path.exists(FFMPEG) else None)
    return str(ziel)


def _wandel_aufraeumen():
    dateien = [(p.stat().st_mtime, p.stat().st_size, p) for p in WANDEL_CACHE.glob("*.wav") if ".teil." not in p.name]
    summe = sum(g for _, g, _ in dateien)
    for _, g, p in sorted(dateien):
        if summe <= WANDEL_MAX_BYTES:
            break
        try:
            p.unlink()
            summe -= g
        except OSError:
            pass


# ================================================================ Versions: preview and drag in the project
# GET /api/fassung renders an adjusted version as WAV: tempo with rubberband (--time), pitch with rubberband
# (--pitch, with formant preservation for vocals), varispeed with ffmpeg (asetrate + aresample, tempo and pitch together
# like on a turntable). The file lands in RENDER_ZIEL and is exactly the one the app drags into FL. The same request
# again returns the existing file at once (key: path, size, modification time, factor, semitones, formant, varispeed;
# list in .renders.json in the folder). RENDER_ZIEL is excluded from the library scan (bestand_messen.py and index).
RENDER_ZIEL = Path(os.environ.get("OPENLIBARY_RENDERS", MUSIK / "Samples/Open Libary Renders" if _EIGENE_ABLAGE
                                   else ABLAGE / "Renders"))
RUBBERBAND = os.environ.get("OPENLIBARY_RUBBERBAND") or plattform.werkzeug("rubberband") or "rubberband"   # environment: to test error cases
FASSUNG_PARAMETER = frozenset(["pfad", "bpm", "ht", "formant", "vari", "seite", "nr", "start", "ende"])   # start, ende: region
RENDER_GLEICHZEITIG = 2
_RENDER_PLAETZE = threading.BoundedSemaphore(RENDER_GLEICHZEITIG)
_RENDER_LISTE_LOCK = threading.Lock()
_RENDER_NEUESTE = {}        # page -> highest request number; older requests of the same page drop out (running ones too)
_RENDER_ANGEFRAGT = {}      # (page, key) -> highest number that requested this version (arrow onto the pre-rendered
                            # row: the new request takes over the running render instead of stopping it)


class FassungVerworfen(Exception):
    pass


class FassungFehler(Exception):
    def __init__(self, code, text):
        super().__init__(text)
        self.code = code


def im_render_ziel(pfad):
    try:
        return os.path.commonpath([os.path.realpath(pfad), os.path.realpath(str(RENDER_ZIEL))]) == os.path.realpath(str(RENDER_ZIEL))
    except ValueError:
        return False


def _render_liste():
    try:
        d = json.loads((RENDER_ZIEL / ".renders.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _render_liste_schreiben(d):
    RENDER_ZIEL.mkdir(parents=True, exist_ok=True)
    tmp = RENDER_ZIEL / f".renders.{os.getpid()}.{threading.get_ident()}.json"
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, RENDER_ZIEL / ".renders.json")


def render_bekannt(pfad):
    """Only finished renders from our own list may be played via /audio."""
    pfad = nfc(pfad)
    if os.path.dirname(pfad) != nfc(str(RENDER_ZIEL)) or not os.path.isfile(pfad):
        return False
    with _RENDER_LISTE_LOCK:
        return any(e.get("datei") == os.path.basename(pfad) for e in _render_liste().values())


def _wav_rate(pfad):
    """Sample rate from the fmt chunk of a WAV (also WAVE_FORMAT_EXTENSIBLE, which wave in Python 3.9 does not read)."""
    with open(pfad, "rb") as f:
        kopf = f.read(12)
        if kopf[:4] != b"RIFF" or kopf[8:12] != b"WAVE":
            raise FassungFehler(500, tx("Intermediate file is not a WAV"))
        while True:
            c = f.read(8)
            if len(c) < 8:
                raise FassungFehler(500, tx("WAV without an fmt chunk"))
            n = int.from_bytes(c[4:8], "little")
            if c[:4] == b"fmt ":
                return int.from_bytes(f.read(n)[4:8], "little")
            f.seek(n + (n & 1), 1)


def _ton_name(ton, modus, ht):
    if not ton or ton not in TONARTEN:
        return None
    return TONARTEN[(TONARTEN.index(ton) + int(round(ht))) % 12] + (modus or "")


def _bpm_text(b):
    return str(int(round(b))) if abs(b - round(b)) < 0.05 else f"{b:.1f}"      # as in the player bar: 145.9


def _render_ziel_name(stamm, teile, schluessel, liste, endung=""):
    """<original name>_140_Fm.wav; if another version already carries this name, then first with the extension of the
    source ("… (mp3).wav", Cymatics ships the same file as WAV and MP3), otherwise (2), (3) (same name in two packs)."""
    grund = re.sub(r'[/:\x00]', "_", stamm + "".join("_" + t for t in teile))
    belegt = {e.get("datei") for k, e in liste.items() if k != schluessel}
    name, n = grund + ".wav", 2
    if name in belegt and endung and endung != "wav":
        name = f"{grund} ({endung}).wav"
    while name in belegt:
        name, n = f"{grund} ({n}).wav", n + 1
    return name


def schnitt_name(a, b, bpm):
    """Name part of a region. On bars (4/4 from the start of the file, 1 ms of play each) "T1-2" or "T3", otherwise
    the start time "0m42s"."""
    if bpm:
        takt = 240 / bpm
        ta, tb = a / takt, b / takt
        if abs(a - round(ta) * takt) <= 0.001 and abs(b - round(tb) * takt) <= 0.001 and round(tb) > round(ta):
            von, bis = int(round(ta)) + 1, int(round(tb))
            return f"T{von}" if von == bis else f"T{von}-{bis}"
    return f"{int(a // 60)}m{int(a % 60):02d}s"


def fassung_rechnen(p):
    """-> {"pfad", "url", "datei", "neu", "sekunden"}. Fehleingabe (400), FassungFehler (403, 404, 503, 500),
    FassungVerworfen (answer 200 with "verworfen": a newer request of the same page came in while this one was still waiting)."""
    def eins(k):
        return (p.get(k) or [""])[0]
    pfad = nfc(eins("pfad"))
    z = INDEX.nach_pfad.get(pfad)
    if z is None:
        raise FassungFehler(403, tx("path is not in the index"))
    if not os.path.isfile(pfad):
        raise FassungFehler(404, tx("File is missing on disk"))
    try:
        ht = int(eins("ht") or "0")
    except ValueError:
        raise Fehleingabe(tx("ht must be a whole number"))
    if not -12 <= ht <= 12:
        raise Fehleingabe(tx("ht goes from -12 to 12"))
    vari, formant = eins("vari") == "1", eins("formant") == "1"
    ziel_bpm = zahl(eins("bpm")) if eins("bpm") else None
    if eins("bpm") and ziel_bpm is None:
        raise Fehleingabe(tx("bpm must be a number"))
    if ziel_bpm is not None and not 20 <= ziel_bpm <= 400:
        raise Fehleingabe(tx("bpm goes from 20 to 400"))
    if vari and ziel_bpm is not None:
        raise Fehleingabe(tx("Varispeed sets the tempo itself, leave out bpm"))
    faktor = 1.0                                  # new length to old length
    if ziel_bpm is not None:
        if not z.get("bpm"):
            raise Fehleingabe(tx("no BPM, not stretched"))
        faktor = z["bpm"] / ziel_bpm
        if not 0.5 <= faktor <= 2.0:
            raise Fehleingabe(tx("Stretching only up to half or double the tempo"))
        if abs(faktor - 1) < 1e-4:
            faktor, ziel_bpm = 1.0, None
    if vari and ht == 0:
        vari = False
    # Region in seconds of the original file; only that part is rendered (then stretched or transposed)
    schnitt = None
    if eins("start") or eins("ende"):
        a, b = zahl(eins("start")), zahl(eins("ende"))
        if a is None or b is None:
            raise Fehleingabe(tx("start and ende must be numbers"))
        if a < 0 or b - a < 0.05:
            raise Fehleingabe(tx("Region: start from 0, at least 50 ms long"))
        if z.get("dauer") and b > z["dauer"] + 0.05:
            raise Fehleingabe(tx("Region goes past the end of the file"))
        schnitt = (a, b)
    if faktor == 1.0 and ht == 0 and schnitt is None:
        raise Fehleingabe(tx("nothing to render: tempo and pitch stay the same"))
    seite, nr = eins("seite")[:40], 0
    if eins("nr"):
        try:
            nr = int(eins("nr"))
        except ValueError:
            raise Fehleingabe(tx("nr must be a whole number"))
    st = os.stat(pfad)
    schluessel = hashlib.sha1(f"{pfad}|{st.st_size}|{st.st_mtime}|{faktor:.6f}|{ht}|{int(formant)}|{int(vari)}|1"
                              .encode() + (f"|{schnitt[0]:.6f}-{schnitt[1]:.6f}".encode() if schnitt else b"")
                              ).hexdigest()[:20]
    # Name: tempo and key the version has now; varispeed appends _vari
    rate = 2 ** (ht / 12) if vari else 1.0
    tempo = ziel_bpm if ziel_bpm is not None else (z["bpm"] * rate if z.get("bpm") else None)
    teile = []
    if tempo is not None:
        teile.append(_bpm_text(tempo))
    ton = _ton_name(z.get("ton"), z.get("modus"), ht)
    if ton:
        teile.append(ton)
    elif ht:
        teile.append(f"{ht:+d}")
    if vari:
        teile.append("vari")
    if schnitt:     # _T1-2 (on bars of the original file, 4/4 from the start of the file) or _0m42s (free)
        if faktor == 1.0 and ht == 0:
            teile = []
        teile.insert(0, schnitt_name(schnitt[0], schnitt[1], z.get("bpm")))

    def fertig(e, neu, sek):
        ziel = str(RENDER_ZIEL / e["datei"])
        return {"pfad": ziel, "url": "/audio?pfad=" + urllib.parse.quote(ziel), "datei": e["datei"], "neu": neu,
                "sekunden": round(sek, 2)}

    def vorhanden():
        with _RENDER_LISTE_LOCK:
            e = _render_liste().get(schluessel)
        if e and (RENDER_ZIEL / e["datei"]).is_file():
            return e
        return None

    e = vorhanden()
    if e:
        return fertig(e, False, 0)
    nur_schnitt = faktor == 1.0 and ht == 0          # only cutting, ffmpeg can do that alone
    if not os.path.exists(RUBBERBAND) and not (vari and faktor == 1.0) and not nur_schnitt:
        raise FassungFehler(503, tx("rubberband is missing ({pfad}), playing original", pfad=RUBBERBAND))
    if seite:
        with _RENDER_LISTE_LOCK:
            _RENDER_NEUESTE[seite] = max(nr, _RENDER_NEUESTE.get(seite, nr))
            k = (seite, schluessel)
            _RENDER_ANGEFRAGT[k] = max(nr, _RENDER_ANGEFRAGT.get(k, nr))
            if len(_RENDER_ANGEFRAGT) > 500:       # clean up what is done
                for a in [a for a, n in _RENDER_ANGEFRAGT.items() if n < _RENDER_NEUESTE.get(a[0], 0)]:
                    del _RENDER_ANGEFRAGT[a]

    def ueberholt():      # a newer request of the same page wants something else (arrow further)
        return bool(seite) and _RENDER_ANGEFRAGT.get((seite, schluessel), nr) < _RENDER_NEUESTE.get(seite, nr)

    def warten(sperre):   # wait as long as the request is not overtaken
        while not sperre.acquire(timeout=0.2):
            if ueberholt():
                raise FassungVerworfen()

    def ausfuehren(befehl, frist):
        """like subprocess.run, but an overtaken render is stopped (only the current row renders)"""
        pr = subprocess.Popen(befehl, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **plattform.hilfsprozess_args())
        ende = time.time() + frist
        try:
            while True:
                try:
                    _, err = pr.communicate(timeout=0.1)
                    return subprocess.CompletedProcess(befehl, pr.returncode, b"", err)
                except subprocess.TimeoutExpired:
                    if ueberholt():
                        raise FassungVerworfen()
                    if time.time() > ende:
                        raise
        finally:
            if pr.poll() is None:
                pr.kill()
                pr.wait()

    sperre = _WANDEL_LOCKS["fassung:" + schluessel]           # the lock per file, as with converting
    warten(sperre)
    try:
        e = vorhanden()       # an identical request has rendered in the meantime
        if e:
            return fertig(e, False, 0)
        warten(_RENDER_PLAETZE)                                # at most two renders at the same time
        t0 = time.time()
        try:
            RENDER_ZIEL.mkdir(parents=True, exist_ok=True)
            arbeit = Path(tempfile.mkdtemp(prefix="fassung-", dir=str(RENDER_ZIEL)))
            try:
                ein = arbeit / "ein.wav"
                r = ausfuehren([FFMPEG, "-v", "error", "-y", "-i", pfad, "-c:a", "pcm_s24le", str(ein)], 180)
                if r.returncode != 0:                  # Ogg in the WAV container and similar: via the conversion
                    ein = Path(gewandelt(pfad))
                aus = arbeit / "aus.wav"
                if schnitt:     # cut sample-accurately (length from the difference, so that 2 bars are 2 bars)
                    sr = _wav_rate(str(ein))
                    a = int(round(schnitt[0] * sr))
                    b = a + int(round((schnitt[1] - schnitt[0]) * sr))
                    teil = arbeit / "teil.wav"
                    r = ausfuehren([FFMPEG, "-v", "error", "-y", "-i", str(ein), "-af",
                                    f"atrim=start_sample={a}:end_sample={b},asetpts=N/SR/TB", "-c:a", "pcm_s24le",
                                    str(teil if not nur_schnitt else aus)], 180)
                    if r.returncode != 0:
                        raise FassungFehler(500, tx("ffmpeg stopped while cutting: {detail}",
                                                    detail=r.stderr.decode(errors="replace").strip()[-200:]
                                                    or tx("exit code {code}", code=r.returncode)))
                    ein = teil
                if nur_schnitt:
                    wer = "ffmpeg"
                elif vari and faktor == 1.0:
                    sr = _wav_rate(str(ein))
                    r = ausfuehren([FFMPEG, "-v", "error", "-y", "-i", str(ein), "-af",
                                    f"asetrate={sr * rate:.4f},aresample={sr}", "-c:a", "pcm_s24le", str(aus)], 300)
                    wer = "ffmpeg"
                else:
                    befehl = [RUBBERBAND, "-q", "-3"]
                    if faktor != 1.0:
                        befehl += ["-t", f"{faktor:.8f}"]
                    if ht:
                        befehl += ["-p", str(ht)]
                        if formant:
                            befehl.append("-F")
                    r = ausfuehren(befehl + [str(ein), str(aus)], 300)
                    wer = "rubberband"
                if r.returncode != 0 or not aus.is_file():
                    raise FassungFehler(500, tx("{wer} stopped: {detail}, playing original", wer=wer,
                                                detail=r.stderr.decode(errors="replace").strip()[-200:]
                                                or tx("exit code {code}", code=r.returncode)))
                with _RENDER_LISTE_LOCK:
                    liste = _render_liste()
                    stamm, endung = os.path.splitext(z["name"])
                    name = _render_ziel_name(stamm, teile, schluessel, liste, endung.lstrip(".").lower())
                    os.replace(aus, RENDER_ZIEL / name)
                    liste[schluessel] = {"datei": name, "quelle": pfad, "faktor": round(faktor, 6), "ht": ht,
                                         "formant": formant, "vari": vari, "gerechnet": time.strftime("%Y-%m-%d %H:%M:%S"),
                                         "sekunden": round(time.time() - t0, 2),
                                         **({"start": schnitt[0], "ende": schnitt[1]} if schnitt else {})}
                    _render_liste_schreiben(liste)
                return fertig(liste[schluessel], True, time.time() - t0)
            except subprocess.TimeoutExpired:
                raise FassungFehler(500, tx("Rendering took over 5 minutes, playing original"))
            finally:
                shutil.rmtree(arbeit, ignore_errors=True)
        finally:
            _RENDER_PLAETZE.release()
    finally:
        sperre.release()


# ================================================================ HTTP

class Server(ThreadingHTTPServer):
    request_queue_size = 128   # default 5: with 20 concurrent searches 55 to 75 % failed
    daemon_threads = True
    # Windows: SO_REUSEADDR there means "share the port with a running server"; two Open Libary would then both listen
    # on 8765 and the page would talk to the old one at random. Bind exclusively there, so a second start fails cleanly.
    allow_reuse_address = not plattform.WINDOWS

    def server_bind(self):
        if plattform.WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # The browser aborts (cancel button, leaving the page): not an error, not into the log
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def ganzzahl(p, k, std, lo, hi):
    w = (p.get(k) or [str(std)])[0]
    try:
        return max(lo, min(hi, int(w)))
    except ValueError:
        raise Fehleingabe(tx("{k} must be a number", k=k))


class Handler(BaseHTTPRequestHandler):
    server_version = "Open Libary"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _fehler(self, code, text):
        self._json({"fehler": text}, code)

    def _roh(self, b, typ):
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(b)

    def _host_ok(self):
        """Own address only: otherwise any web page in the browser could trigger downloads (DNS rebinding, CSRF)."""
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _origin_ok(self):
        o = self.headers.get("Origin")
        return o in (None, "null", f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def _koerper(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > KOERPER_MAX:
            while n > 0:            # read to the end first, otherwise the caller sees a dropped connection instead of the 400
                stueck = self.rfile.read(min(n, 1 << 20))
                if not stueck:
                    break
                n -= len(stueck)
            raise Fehleingabe(tx("Request too large"))
        try:
            # 16 MB: Cmd+A sends the held paths, the whole library needs around 10 MB
            k = json.loads(self.rfile.read(min(n, KOERPER_MAX)) or b"{}")
        except ValueError:
            raise Fehleingabe(tx("Body is not JSON"))
        if not isinstance(k, dict):
            raise Fehleingabe(tx("Body must be a JSON object"))
        return k

    def _konten(self):
        return {"freesound": dict(netz.freesound_stand(), rueckruf=f"http://127.0.0.1:{PORT}/api/freesound/rueckruf"),
                "looperman": netz.WAECHTER.bericht()}

    def _html_seite(self, titel, text):
        """Small page for callbacks from the browser (Freesound sign-in), in the app's look."""
        b = ("<!doctype html><meta charset=utf-8><title>Open Libary</title><body style='background:#161512;color:#e9e4d8;"
             "font:15px -apple-system,sans-serif;padding:48px'><b>" + html.escape(titel) + "</b><p>" + html.escape(text)
             + "</p></body>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _pfad_erlaubt(self, pfad):
        return nfc(pfad) in INDEX.nach_pfad

    def do_GET(self):
        with BERICHTE.anfrage(self.path):        # a request that takes too long counts as a freeze
            return self._get()

    def _get(self):
        if not self._host_ok():
            return self._fehler(403, tx("foreign host"))
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                b = seite_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(b)
            if re.match(r"^/stile/[a-z0-9-]+\.css$", u.path):
                datei = HIER / u.path.lstrip("/")
                return self._datei(str(datei), "text/css; charset=utf-8") if datei.is_file() else self._fehler(404, tx("Style is missing"))
            if u.path == "/favicon.ico":
                return self._roh(FAVICON, "image/svg+xml")
            if u.path in ("/api/suche", "/api/zufall"):
                fremd = unbekannte_parameter(p, SUCHE_PARAMETER if u.path == "/api/suche" else
                                             ZUFALL_SUCHE_PARAMETER if (p.get("typ") or [""])[0] == "suche"
                                             else ZUFALL_PARAMETER)
                if fremd:
                    return self._json(fremd, 400)
            if u.path == "/api/suche":
                INDEX.aktuell_halten()
                ab, n = ganzzahl(p, "offset", 0, 0, 10 ** 7), ganzzahl(p, "limit", 200, 1, 500)
                # first page with groups set: matches and counts without their own selection in one pass
                treffer, extra = filtern(p, gruppen=True) if ab == 0 and gruppen_gesetzt(p) else (filtern(p), None)
                drums_frei = (p.get("drums_ohne_ton") or [""])[0] == "1"
                gehoert = verlauf.gehoert_menge()     # the list dims rows that were already played
                sammlung = (p.get("sammlung") or [""])[0]
                tage = collections.Counter(z["verlauf_zeit"][:10] for z in treffer) \
                    if sammlung == "gezogen" and ab == 0 else None      # Dragged by day, with counts
                return self._json({"gesamt": len(treffer), "alle": len(INDEX.zeilen),
                                   "zeilen": [dict(oeffentlich(z), **({"gehoert": True} if z["pfad"] in gehoert else {}))
                                              for z in treffer[ab:ab + n]],
                                   "verlauf_tage": tage,
                                   "aehnlich": aehnlich_vergleicher(p)[1] if (p.get("aehnlich") or [""])[0] else None,
                                   "facetten": facetten_ohne_eigene(p, facetten(treffer, drums_frei=drums_frei), treffer, extra)
                                   if ab == 0 else None,
                                   "abdeckung": INDEX.abdeckung if ab == 0 else None,
                                   "arten_alle": INDEX.arten if ab == 0 else None,     # choice when editing
                                   "vollscan": fremder_scan() if ab == 0 else None,
                                   "vollscan_fertig": vollscan_fertig() if ab == 0 else None,
                                   # license line above an opened playlist
                                   "lizenz_playlist": lizenz_playlist(sammlung)
                                   if ab == 0 and sammlung.startswith("pl:") else None})
            if u.path == "/api/stand":
                INDEX.aktuell_halten()
                ladungen = netz.stand()
                messungen = MESSDIENST.stand_fuer()
                for x in ladungen.values():     # per downloaded file: waiting, analyzing, analyzed or the error
                    x["messung"] = [dict(messungen.get(nfc(q), {"status": "", "text": ""}), name=os.path.basename(q),
                                         pfad=q) for q in x.pop("pfade", [])]
                return self._json({"alle": len(INDEX.zeilen), "gemessen": INDEX.gemessen, "fehler": INDEX.fehler,
                                   "code": str(HIER), "code_stand": CODE_STAND, "pid": os.getpid(),
                                   "paket": bool(os.environ.get("OPENLIBARY_PAKET")), "system": plattform.SYSTEM,
                                   "gehoer_db": GEHOER_DB.is_file(), "gehoer_da": os.path.exists(GEHOER),
                                   "messungen": messungen, "ladungen": ladungen,
                                   "index_version": INDEX.version, "vollscan": fremder_scan()})
            if u.path == "/api/zeilen":
                # current values of single rows (after an analysis), only paths from the index
                pfade = [nfc(x) for x in p.get("pfad", [])][:200]
                return self._json({"zeilen": [oeffentlich(INDEX.nach_pfad[x]) for x in pfade if x in INDEX.nach_pfad],
                                   "index_version": INDEX.version})
            if u.path == "/api/stile":
                return self._json({"stile": stile_liste(), "aktiv": stil_aktuell()})
            if u.path == "/api/sprache":
                return self._json({"sprache": i18n.current(), "sprachen": i18n.LANGUAGES})
            if u.path == "/api/vorfaelle":      # crashes and freezes the page should ask about (bericht.py)
                return self._json({"offen": [{"id": v["id"], "art": v["art"], "zeit": v["zeit"],
                                              "was": (v.get("details") or {}).get("was", "")} for v in BERICHTE.offen()],
                                   "fragen": BERICHTE.fragen(), "senden": bool(bericht.URL), "version": VERSION})
            if u.path == "/api/test/haengen" and os.environ.get("OPENLIBARY_TEST") == "1":    # smoke test only
                time.sleep(min(120.0, float((p.get("s") or ["1"])[0])))
                return self._json({"ok": True})
            if u.path == "/api/sammlungen":
                return self._json(dict(sammlungen.stand(), verlauf=verlauf.zahlen(),     # counts on the left
                                       lizenz_offen=lizenz_offen_zahl()))                # counter on the left
            if u.path == "/api/lizenzen":     # license view, one row per pack
                fremd = unbekannte_parameter(p, frozenset())
                if fremd:
                    return self._json(fremd, 400)
                INDEX.aktuell_halten()
                return self._json(lizenz_packs())
            if u.path == "/api/downloads":
                # everything Open Libary downloaded from the Web, newest first
                INDEX.aktuell_halten()
                zeilen = sorted((z for z in INDEX.zeilen if z["herkunft"] == "openlibary"),
                                key=lambda z: (z["geladen_am"], z["name"].casefold()), reverse=True)
                return self._json({"gesamt": len(zeilen), "zeilen": [dict(oeffentlich(z), ordner=os.path.dirname(z["pfad"]))
                                                                     for z in zeilen]})
            if u.path == "/api/zufall":
                INDEX.aktuell_halten()
                return self._json(zufall(p))
            if u.path == "/api/packs":
                return self._json({"packs": INDEX.packs})
            if u.path == "/api/fassung":     # render an adjusted version (or return the one already rendered)
                fremd = unbekannte_parameter(p, FASSUNG_PARAMETER)
                if fremd:
                    return self._json(fremd, 400)
                try:
                    return self._json(fassung_rechnen(p))
                except FassungVerworfen:    # not an error (200): otherwise the console reports every dropped request in red
                    return self._json({"verworfen": True, "grund": tx("overtaken by a newer request")})
                except FassungFehler as e:
                    if e.code == 403:       # protection (path not in the index) stays a real error
                        return self._fehler(e.code, str(e))
                    # Rendering did not work (rubberband missing 503, stopped 500, file missing 404): a state the
                    # player bar shows in red, not a page error; 200, so the console does not turn red for every sample
                    return self._json({"fehler": str(e), "code": e.code})
            if u.path == "/audio":
                pfad = nfc((p.get("pfad") or [""])[0])
                if render_bekannt(pfad):      # rendered version from the render folder
                    return self._datei(pfad, "audio/wav")
                if not self._pfad_erlaubt(pfad):
                    return self._fehler(403, tx("path is not in the index"))
                if not os.path.isfile(pfad):
                    return self._fehler(404, tx("File is missing on disk"))
                # No browser plays AIFF and FL factory samples (Ogg in WAV): send WAV at once, no failed try by the page
                if (p.get("wandeln") or [""])[0] == "1" or pfad.lower().endswith((".aif", ".aiff")) \
                        or audioform.ist_ogg_in_wav(pfad):
                    pfad = gewandelt(pfad)
                return self._datei(pfad, mimetypes.guess_type(pfad)[0] or "application/octet-stream")
            if u.path == "/api/ordner":          # your own folders, tools
                return self._json(dict(ORDNER.stand(), werkzeuge=werkzeuge(), netz_ziel=str(NETZ_ZIEL), leicht=LEICHT.bericht(),
                                       messen_max=ORDNER_MESSEN_MAX, samples=len(INDEX.zeilen), heim=str(Path.home())))
            if u.path == "/api/als_wav":         # as WAV when dragging (Logic, Pro Tools)
                pfad = nfc((p.get("pfad") or [""])[0])
                if not (self._pfad_erlaubt(pfad) or render_bekannt(pfad)) or not os.path.isfile(pfad):
                    return self._fehler(403, tx("path is not in the index"))
                try:
                    return self._json({"pfad": als_wav(pfad)})
                except (RuntimeError, OSError) as e:
                    return self._json({"fehler": str(e)})
            if u.path == "/api/konten":          # accounts: Freesound and Looperman at a glance
                return self._json(self._konten())
            if u.path == "/api/netz/uebernommen":     # accounts: last taken over from Looperman
                aus = []
                for e in netz.uebernommen((p.get("quelle") or ["looperman"])[0]):
                    z = INDEX.nach_pfad.get(nfc(e["pfad"]))
                    aus.append(dict(e, name=z["name"] if z else e["datei"], bpm=z["bpm"] if z else None,
                                    ton=(z["ton"] or "") + (z["modus"] or "") if z and z["ton"] else "",
                                    art=z["art"] if z else "", dauer=z["dauer"] if z else None,
                                    im_index=z is not None))
                return self._json({"dateien": aus})
            if u.path == "/api/netz/pfad":       # accounts: an already downloaded Web sound -> path (dragging from Web)
                pf = netz.pfad_von_seite((p.get("seite") or [""])[0])
                return self._json({"pfad": pf if pf and self._pfad_erlaubt(pf) else None})
            if u.path == "/api/klang":           # sound fingerprint, state
                return self._json(dict(KLANG.stand(), samples=len(INDEX.zeilen)))
            if u.path == "/api/freesound":       # Freesound directly, without secrets
                return self._json(dict(netz.freesound_stand(),
                                       rueckruf=f"http://127.0.0.1:{PORT}/api/freesound/rueckruf"))
            if u.path == "/api/freesound/login":
                ziel = netz.freesound_login_url()
                if not ziel:
                    return self._html_seite("Freesound", tx("First enter the client ID and client secret in Settings."))
                self.send_response(302)
                self.send_header("Location", ziel)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if u.path == "/api/freesound/rueckruf":
                if (p.get("error") or [""])[0]:
                    return self._html_seite("Freesound", tx("Sign-in canceled. Start it again in Open Libary under Settings."))
                ok, text = netz.freesound_code_tauschen((p.get("code") or [""])[0], (p.get("state") or [""])[0])
                return self._html_seite(tx("Freesound connected") if ok else "Freesound",
                                        text + (tx(". You can close this window and continue in Open Libary.") if ok else ""))
            if u.path == "/api/netz/suche":
                quelle = (p.get("quelle") or ["archive"])[0]
                q, lizenz = (p.get("q") or [""])[0], (p.get("lizenz") or ["cc0"])[0]
                seite = ganzzahl(p, "seite", 1, 1, 100)
                quelle = {"openverse": "freesound"}.get(quelle, quelle)
                if quelle in netz.EINZEL_QUELLEN:
                    return self._json(netz.EINZEL_QUELLEN[quelle](q, lizenz, seite))
                if quelle != "archive":
                    return self._fehler(400, tx("unknown source"))
                return self._json(netz.archive_suche(q, lizenz, seite, (p.get("alles") or [""])[0] != "1"))
            if u.path == "/api/netz/vorschau":
                # preview through the server where the source blocks outside requests (ccMixter: requires a Referer)
                t = netz._EINZEL.get((p.get("s") or [""])[0])
                if not t:
                    return self._fehler(404, tx("Sound is not from one of the last searches"))
                url = t.get("hoer_url") or t["url"]
                if not url.startswith(("https://", "http://ccmixter.org/")):
                    return self._fehler(400, tx("https only"))
                kopf = dict(netz.UA, **netz.quell_kopf(url))
                if self.headers.get("Range"):
                    kopf["Range"] = self.headers["Range"]
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers=kopf), timeout=30) as r:
                        self.send_response(r.status)
                        for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                            if r.headers.get(h):
                                self.send_header(h, r.headers[h])
                        self.end_headers()
                        try:
                            shutil.copyfileobj(r, self.wfile, 1 << 16)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    return
                except urllib.error.HTTPError as e:
                    return self._fehler(e.code, tx("source reports {code}", code=e.code))
            if u.path == "/api/netz/geprueft":
                return self._json({"packs": netz_geprueft()})
            if u.path == "/api/netz/dateien":
                return self._json(netz.archive_dateien((p.get("id") or [""])[0]))
            return self._fehler(404, tx("unknown"))
        except Fehleingabe as e:
            return self._fehler(400, str(e))
        except netz.NichtGefunden as e:
            return self._fehler(404, str(e))
        except netz.QuelleFehler as e:
            return self._fehler(e.code, str(e))
        except (TimeoutError, OSError) as e:
            if "timed out" in str(e):
                return self._fehler(504, tx("The source is not answering right now (timeout); try again in a moment"))
            return self._fehler(502, tx("Source not reachable: {grund}", grund=getattr(e, "reason", e)))
        except Exception as e:
            return self._fehler(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        with BERICHTE.anfrage(self.path):
            return self._post()

    def _post(self):
        if not (self._host_ok() and self._origin_ok()) or \
                not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._fehler(403, tx("only from Open Libary itself"))
        u = urllib.parse.urlparse(self.path)
        try:
            k = self._koerper()
            pfad = nfc(k["pfad"] if isinstance(k.get("pfad"), str) else "")     # a number as path gave 500
            if u.path == "/api/finder":
                echt = os.path.realpath(pfad)
                erlaubter_ordner = os.path.isdir(echt) and (echt.startswith(str(MUSIK) + os.sep)
                                                            or echt.startswith(str(NETZ_ZIEL))
                                                            or echt.startswith(str(DRUM_ZIEL))
                                                            or any(echt == o["pfad"] or echt.startswith(o["pfad"] + os.sep)
                                                                   for o in ORDNER.liste()))
                if not self._pfad_erlaubt(pfad) and not erlaubter_ordner:
                    return self._fehler(403, tx("path is not in the index"))
                if not os.path.exists(pfad):
                    return self._fehler(404, tx("File is missing on disk"))
                if k.get("zeigen", True):   # the app shows it itself (zeigen: false), the browser needs the server
                    plattform.im_ordner_zeigen(echt if erlaubter_ordner else pfad)
                return self._json({"ok": True})
            if u.path == "/api/messen" and ("pfade" in k or "suche" in k):    # multiple selection
                pfade = [x for x in auswahl_pfade(k, MESSEN_MAX) if os.path.isfile(x)]
                if not pfade:
                    return self._fehler(404, tx("None of the files is on disk."))
                hinweis = MESSDIENST.dazu(pfade, klick=True)
                return self._json({"ok": True, "ziel": "netz", "hinweis": hinweis, "anzahl": len(pfade)})
            if u.path == "/api/korrektur" and "pack" in k:     # {pack: "...", felder: {lizenz, lizenz_grund, beleg}}
                if "pfade" in k or "suche" in k:
                    return self._fehler(400, tx("pack not together with pfade or suche"))
                try:
                    felder = korrekturen.pack_pruefen(k.get("pack"), k.get("felder"))
                    pack = nfc(k["pack"])
                    if pack not in INDEX.pack_menge:
                        return self._fehler(400, tx("Single sounds from the Web are not a pack, license per file (e)")
                                            if pack in INDEX.packs else tx("Pack is not in the library"))
                    e = korrekturen.pack_setzen(pack, felder)
                except korrekturen.KorrekturFehler as e:
                    return self._fehler(400, str(e))
                n = pack_im_index(pack)
                return self._json({"ok": True, "pack": pack, "anzahl": n, "entscheid": e,
                                   "lizenz_offen": lizenz_offen_zahl()})
            if u.path == "/api/korrektur":     # {pfade: [...] | suche: {...}, felder: {...}}
                try:
                    felder = korrekturen.pruefen(k.get("felder"), set(INDEX.arten), tonart_norm)
                    pfade = auswahl_pfade(k)
                    geaendert = korrekturen.setzen([(x, INDEX.nach_pfad[x]["md5"]) for x in pfade], felder,
                                                   lambda x: x in INDEX.nach_pfad)
                except korrekturen.KorrekturFehler as e:
                    return self._fehler(400, str(e))
                korrektur_im_index(geaendert)
                return self._json({"ok": True, "anzahl": len(geaendert), "lizenz_offen": lizenz_offen_zahl(),
                                   "zeilen": [oeffentlich(INDEX.nach_pfad[x]) for x in pfade[:500] if x in INDEX.nach_pfad]})
            if u.path == "/api/auswahl":       # paths of all matches of a search (Cmd+A: copy, drag)
                pfade = auswahl_pfade({"suche": k.get("suche")})
                return self._json({"pfade": pfade, "gesamt": len(pfade)})
            if u.path == "/api/messen":
                if not self._pfad_erlaubt(pfad):
                    return self._fehler(403, tx("path is not in the index"))
                if not os.path.isfile(pfad):    # otherwise the list reported "analyzed" although nothing ran (check 20.10.)
                    return self._fehler(404, tx("File is missing on disk."))
                hinweis = MESSDIENST.dazu([pfad], klick=True)
                return self._json({"ok": True, "ziel": "netz", "hinweis": hinweis})
            if u.path == "/api/netz/laden":
                ids = k.get("ids") or []
                if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                    return self._fehler(400, tx("ids must be a list"))
                code, antwort = netz.laden_starten(k.get("quelle") or "archive", str(k.get("id") or ""),
                                                   set(k.get("dateien") or []), bool(k.get("bestaetigt")),
                                                   ids=ids, ordner=str(k.get("ordner") or ""))
                return self._json(antwort, code)
            if u.path == "/api/sammlungen":
                if k.get("suche") is not None:      # Cmd+A over all matches, the server resolves the search
                    k = dict(k, pfade=auswahl_pfade({"suche": k.pop("suche")}))
                    k.pop("pfad", None)
                try:
                    return self._json(dict(sammlungen.aendern(k, self._pfad_erlaubt,
                                                              lambda p: (INDEX.nach_pfad.get(p) or {}).get("dauer")),
                                           verlauf=verlauf.zahlen(), lizenz_offen=lizenz_offen_zahl()))
                except sammlungen.SammlungFehler as e:
                    return self._fehler(400, str(e))
            if u.path == "/api/verlauf":      # {art: "gehoert"|"gezogen", pfad, datei?}
                try:
                    return self._json({"ok": True, "verlauf": verlauf.eintragen(k, self._pfad_erlaubt)})
                except verlauf.VerlaufFehler as e:
                    return self._fehler(400, str(e))
            if u.path == "/api/vorfall":          # from the app shells: {art: crash|freeze, was: "..."}
                was = k.get("was") if isinstance(k.get("was"), str) else ""
                v = BERICHTE.vorfall(k.get("art") if k.get("art") in ("crash", "freeze") else "freeze",
                                     {"was": was[:500] or "The window stopped responding", "quelle": "app window"})
                return self._json({"ok": True, "id": v["id"]})
            if u.path == "/api/vorfaelle/erledigt":      # {ids: [...]}: asked about, do not ask again
                ids = k.get("ids") if isinstance(k.get("ids"), list) else []
                BERICHTE.erledigen({x for x in ids if isinstance(x, str)})
                return self._json({"ok": True})
            if u.path == "/api/bericht/fragen":           # {an: bool}: ask after crashes and freezes
                return self._json({"fragen": BERICHTE.fragen(bool(k.get("an")))})
            if u.path == "/api/bericht/vorschau":         # {ids, mit_dateinamen}: the snapshot exactly as it would go
                ids = [x for x in (k.get("ids") or []) if isinstance(x, str)] if isinstance(k.get("ids"), list) else []
                return self._json({"schnappschuss": BERICHTE.schnappschuss(ids, bool(k.get("mit_dateinamen")))})
            if u.path == "/api/bericht/senden":
                # {art, text, kontakt, ids, schnappschuss}: schnappschuss is the text the form showed, or null
                text = k.get("text") if isinstance(k.get("text"), str) else ""
                if not text.strip():
                    raise Fehleingabe(tx("Please describe what happened."))
                schnapp = k.get("schnappschuss") if isinstance(k.get("schnappschuss"), str) else None
                ids = [x for x in (k.get("ids") or []) if isinstance(x, str)] if isinstance(k.get("ids"), list) else []
                ok, meldung = BERICHTE.senden(k.get("art") or "manual", text.strip(),
                                              k.get("kontakt") if isinstance(k.get("kontakt"), str) else "", schnapp, ids)
                return self._json({"ok": ok, "text": tx(meldung)}, 200 if ok else 502)
            if u.path == "/api/sprache":         # {sprache: "en"|"de"}; the page reloads afterwards
                code = str(k.get("sprache") or "")
                if code not in i18n.LANGUAGES:
                    return self._fehler(400, tx("unknown language"))
                (DATEN / "sprache.txt").write_text(code + "\n", encoding="utf-8")
                code = i18n.set_language(code)
                # rows carry texts built at load time (license reasons, warnings): reload before answering, so the
                # page that reloads next already gets them in the new language (a few seconds for a big library)
                netz.suche_vergessen()             # cached web results carry translated reasons too
                if INDEX and getattr(INDEX, "sprache", None) != code:
                    INDEX.laden()
                return self._json({"ok": True, "sprache": code})
            if u.path == "/api/stil":
                name = stil_normal(k.get("name"))   # "glas", "kobalt" or "glas+kobalt"
                if not name:
                    return self._fehler(400, tx("unknown style"))
                (DATEN / "stil.txt").write_text(name + "\n", encoding="utf-8")
                return self._json({"ok": True, "aktiv": name})
            if u.path == "/api/als_wav":         # dragging: {pfade, muster} -> per path the WAV version, where needed
                endungen = (".ogg", ".oga", ".flac", ".opus", ".m4a") + ((".mp3",) if k.get("muster") == "immer" else ())
                aus, zahl, fehler, zu_viele = [], 0, "", 0
                for x in [nfc(x) for x in (k.get("pfade") or []) if isinstance(x, str)][:2000]:
                    noetig = (self._pfad_erlaubt(x) or render_bekannt(x)) and os.path.isfile(x) and (
                        x.lower().endswith(endungen) or audioform.ist_ogg_in_wav(x))   # only FL reads FL factory samples
                    if noetig and zahl >= 30:
                        zu_viele += 1
                    elif noetig:
                        try:
                            x = als_wav(x)
                            zahl += 1
                        except (RuntimeError, OSError) as e:
                            fehler = str(e)
                    aus.append(x)
                hinweis = (tx("{n} more files would need converting; they go as the original", n=zu_viele) if zu_viele else
                           tx("{fehler}; the original is dragged", fehler=fehler) if fehler else "")
                return self._json({"pfade": aus, "hinweis": hinweis})
            if u.path == "/api/ordner":          # {pfad} add | {pfad, entfernen: true} | {neu_einlesen: true}
                if k.get("waehlen"):             # in the browser: the macOS folder dialog (the app has its own)
                    try:
                        gewaehlt = plattform.ordner_waehlen()
                    except (OSError, subprocess.TimeoutExpired):
                        return self._json(dict(ORDNER.stand(), ok=False,
                                               text=tx("The folder dialog did not open; paste the path into the field")))
                    if not gewaehlt:
                        return self._json(dict(ORDNER.stand(), ok=False, abgebrochen=True, text=tx("No folder chosen")))
                    pfad = nfc(gewaehlt)
                if k.get("neu_einlesen"):
                    ORDNER.einlesen()
                    ok, text = True, tx("Rescanning")
                elif k.get("entfernen"):
                    ok, text = ORDNER.entfernen(pfad)
                else:
                    ok, text = ORDNER.hinzufuegen(pfad)
                return self._json(dict(ORDNER.stand(), ok=ok, text=text))      # 200: a refusal is not a page error
            if u.path == "/api/konten":          # {looperman: true|false}
                if "looperman" in k:
                    netz.looperman_setzen(bool(k["looperman"]))
                return self._json(dict(self._konten(), ok=True))
            if u.path == "/api/klang":           # {alle: true} computes everything (hours), {anhalten: true}
                if k.get("anhalten"):
                    ok, text = KLANG.anhalten(), tx("Stopped; what is finished stays")
                else:
                    ok, text = KLANG.alle_rechnen()
                return self._json(dict(KLANG.stand(), samples=len(INDEX.zeilen), ok=ok, text=text))
            if u.path == "/api/freesound":       # {client_id, client_secret} | {code} | {trennen: true}
                if k.get("trennen"):
                    netz.freesound_trennen()
                    return self._json(dict(netz.freesound_stand(), ok=True))
                if isinstance(k.get("code"), str):
                    ok, text = netz.freesound_code_tauschen(k["code"])
                else:
                    ok, text = netz.freesound_schluessel_setzen(str(k.get("client_id") or ""), str(k.get("client_secret") or ""))
                netz._SUCHE_CACHE.clear()      # matches carry "preview" or "original": ask again
                return self._json(dict(netz.freesound_stand(), ok=ok, text=text))
            if u.path == "/api/netz/abbrechen":
                return self._json({"ok": netz.abbrechen(k.get("schluessel") or "")})
            return self._fehler(404, tx("unknown"))
        except Fehleingabe as e:
            return self._fehler(400, str(e))
        except netz.QuelleFehler as e:
            return self._fehler(e.code, str(e))
        except netz.NichtGefunden as e:
            return self._fehler(404, str(e))
        except urllib.error.URLError as e:
            return self._fehler(502, tx("Source not reachable: {grund}", grund=getattr(e, "reason", e)))
        except Exception as e:
            return self._fehler(500, f"{type(e).__name__}: {e}")

    def _datei(self, pfad, typ):
        groesse = os.path.getsize(pfad)
        bereich = self.headers.get("Range")
        start, ende = 0, groesse - 1
        m = re.match(r"^bytes=(\d*)-(\d*)$", bereich or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                ende = min(int(m.group(2)), groesse - 1) if m.group(2) else groesse - 1
            else:
                start = max(0, groesse - int(m.group(2)))
            if start >= groesse or start > ende or (not m.group(1) and int(m.group(2)) == 0):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{groesse}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{ende}/{groesse}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(ende - start + 1))
        self.end_headers()
        with open(pfad, "rb") as f:
            f.seek(start)
            rest = ende - start + 1
            try:
                while rest > 0:
                    stueck = f.read(min(1 << 16, rest))
                    if not stueck:
                        break
                    self.wfile.write(stueck)
                    rest -= len(stueck)
            except (BrokenPipeError, ConnectionResetError):
                pass


def alten_server_ersetzen(port):
    """Packaged app after an update: if a server from an app bundle whose code is older or no longer exists runs on
    the port, it is stopped and this one starts. A server outside an app bundle (the author's setup) is always left
    alone. -> server or None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stand", timeout=3) as r:
            d = json.loads(r.read())
    except (OSError, ValueError):
        return None
    alt = d.get("code") or ""
    if not (d.get("paket") or ".app/" in alt + "/") or not isinstance(d.get("pid"), int):
        return None
    if alt == str(HIER) and d.get("code_stand") == CODE_STAND:
        return None                                   # the same state is already running
    try:
        os.kill(d["pid"], signal.SIGTERM)
    except OSError:
        return None
    for _ in range(50):
        time.sleep(0.1)
        try:
            return Server(("127.0.0.1", port), Handler)
        except OSError:
            continue
    return None


def berichtszustand():
    """Counts for the snapshot of a problem report: no names, no paths."""
    zeilen = INDEX.zeilen if INDEX else []
    formate = collections.Counter(z.get("format") or "?" for z in zeilen)
    arten = collections.Counter(z.get("lo") or "?" for z in zeilen)
    leicht_stand = LEICHT.bericht() if LEICHT else {}
    return {
        "Language, style": f"{i18n.current()}, {stil_aktuell()}",
        "Library": f"{len(ORDNER.liste()) if ORDNER else 0} folder(s), {len(zeilen)} files "
                   f"({', '.join(f'{k} {n}' for k, n in formate.most_common(8))}), "
                   f"{arten.get('loop', 0)} loops, {arten.get('oneshot', 0)} one-shots",
        "Analysis": f"gehoer {'yes' if os.path.exists(GEHOER) else 'no'}, light analysis "
                    f"{'running' if leicht_stand.get('laeuft') else 'idle'}, {len(LEICHT.werte()) if LEICHT else 0} values"
                    + (f", last error: {leicht_stand['fehler']}" if leicht_stand.get("fehler") else ""),
        "Freesound connected": "yes" if netz.freesound_stand().get("login") else "no",
        "Tools": ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in (("ffmpeg", netz.KONFIG.get("ffmpeg")),
                                                                     ("rubberband", shutil.which("rubberband")))),
    }


def bericht_ordner():
    """Folders whose names stay out of a report: the user's sample folders and where downloads go."""
    return [o["pfad"] for o in (ORDNER.liste() if ORDNER else [])] + [str(MUSIK), str(ABLAGE), str(NETZ_ZIEL), str(DRUM_ZIEL)]


def main():
    global INDEX, PORT, MESSDIENST, ORDNER, KLANG, LEICHT, BERICHTE
    ap = argparse.ArgumentParser(description="Open Libary: search and play your sample library, download Web packs")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ohne-browser", action="store_true")
    ap.add_argument("--ersetzen", action="store_true", help="stop the old server of the same app after an update")
    a = ap.parse_args()
    PORT = a.port
    i18n.set_language(sprache_gespeichert())
    url = f"http://127.0.0.1:{a.port}/"
    try:
        srv = Server(("127.0.0.1", a.port), Handler)
    except OSError:
        srv = alten_server_ersetzen(a.port) if a.ersetzen else None
    if srv is None:
        print(f"Open Libary is already running: {url}")
        if not a.ohne_browser:
            webbrowser.open(url)
        return
    t0 = time.time()
    BERICHTE = bericht.Berichte(DATEN, VERSION, berichtszustand, bericht_ordner,
                                [os.environ.get("OPENLIBARY_LOG") or plattform.protokoll(), DATEN / "openlibary.log",
                                 plattform.protokoll()],
                                {str(HIER): "<app>", sys.base_prefix: "<python>", sys.prefix: "<python>"})
    BERICHTE.starten(a.port)                     # notices whether the last run crashed; from here on it watches

    def frueh_beenden(signum, frame):            # until the full handler below is set (loading the index takes a while)
        BERICHTE.beenden()
        os._exit(0)
    signal.signal(signal.SIGTERM, frueh_beenden)
    # the terminal of a source start closed: a clean end too. Not where it is ignored (nohup in the Mac app)
    sighup = hasattr(signal, "SIGHUP") and signal.getsignal(signal.SIGHUP) != signal.SIG_IGN
    if sighup:
        signal.signal(signal.SIGHUP, frueh_beenden)
    LEICHT = leicht.Leicht(leicht_befehl(), LEICHT_DB, None, leicht_kandidaten, leicht_neu_laden)
    INDEX = Index()
    MESSDIENST = Messdienst()

    def beenden(signum, frame):
        # "openlibary stop" (pkill): stop running gehoer analyses too, otherwise they keep running as orphans;
        # gehoer stops cleanly on SIGTERM and deletes its "in progress" note
        for pid in list(MESSDIENST._pids):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if MESSDIENST._pids and getattr(MESSDIENST, "liste_jetzt", None):
            time.sleep(1)                       # gehoer read the list at start; after that it is left over
            try:
                os.unlink(MESSDIENST.liste_jetzt)
            except OSError:
                pass
        BERICHTE.beenden()                      # clean end: no crash report at the next start
        os._exit(0)
    signal.signal(signal.SIGTERM, beenden)
    if sighup:
        signal.signal(signal.SIGHUP, beenden)
    netz.KONFIG.update(ziel=NETZ_ZIEL, ziel_drums=DRUM_ZIEL, zusaetze=ZUSAETZE, md5_bestand=lambda: INDEX.md5, nach_laden=INDEX.laden,
                       konfig_ordner=KONFIG_ORDNER,
                       geprueft_urls=lambda: {r.get("quelle_url") for r in INDEX.urteile.values()},
                       fertig_datei=fertig_geladen)
    KLANG = klangnaehe.Klang(GEHOER_PY, GEHOER_CODE, KLANG_DB, MESS_PRIO, {
        "OPENLIBARY_KLANG_DB": str(KLANG_DB), "OPENLIBARY_BESTAND": str(BESTAND), "OPENLIBARY_ZUSAETZE": str(ZUSAETZE),
        "OPENLIBARY_EIGENE": str(EIGENE), "OPENLIBARY_GEHOER_CODE": str(GEHOER_CODE)})
    # Looperman downloads from the browser (never under "Frei (Netz)", see netz.LOOPERMAN_LIZENZ)
    netz.KONFIG["looperman_ziel"] = Path(os.environ.get("OPENLIBARY_LOOPERMAN_ZIEL", MUSIK / "Samples/Looperman"
                                                        if _EIGENE_ABLAGE else ABLAGE / "Looperman"))
    netz.WAECHTER.starten()
    ORDNER = ordner.Ordner(ORDNER_LISTE, EIGENE, ordner_bekannt, ordner_eingelesen, netz.KONFIG["ffprobe"],
                           ohne=[nfc(str(RENDER_ZIEL))])     # rendered versions are not samples
    LEICHT.starten()                             # analyzes in the background, only gaps, one process
    takt = threading.Timer(90, ordner_takt)      # scan only after the start, then every hour
    takt.daemon = True
    takt.start()
    print(f"Open Libary: {len(INDEX.zeilen)} samples, {INDEX.gemessen} analyzed, loaded in {time.time() - t0:.1f} s",
          flush=True)
    nachholen = threading.Timer(60, MESSDIENST.nachholen)     # only after the start, when the Mac has room to breathe
    nachholen.daemon = True
    nachholen.start()
    print(f"Open at {url} (Ctrl+C quits)", flush=True)
    if not a.ohne_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        BERICHTE.beenden()


if __name__ == "__main__":
    main()
