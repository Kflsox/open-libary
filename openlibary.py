#!/usr/bin/env python3
"""Open Libary: lokale Oberfläche über die Sample-Bibliothek, wie Splice für den eigenen Bestand.

  python3 openlibary.py [--port 8765] [--ohne-browser]

Quellen (nur gelesen, nichts davon wird verändert):
  - Bibliothek/bestand.csv im Musik-Projektordner (lokal.json „musik“; alle eigenen Samples plus geprüfte Netz-Packs)
  - die gehoer-Datenbank ~/.cache/gehoer/bibliothek.sqlite (gemessene BPM, Tonart, Takte, Swing, Halftime, Instrumente,
    Stil, Einzelschlag-Klasse), sobald es sie gibt, und daneben openlibary_netz.sqlite (siehe Messen)
  - zusaetze.csv neben diesem Skript: was die Open Libary selbst aus dem Netz geladen hat (netz.py)
  - _herkunft.csv einer sortierten Sample-Sammlung (lokal.json „library_neu“): swing, gefuehl, live (über md5)

Geschrieben wird nur: zusaetze.csv, geladene Dateien unter Samples/Frei (Netz)/ (mit LIZENZ.txt und QUELLE.txt) und,
über gehoer bibliothek scan, die eigene gehoer-Datenbank openlibary_netz.sqlite: jede fertig geladene Netz-Datei und
jeder Klick auf "Messen" (eigene Datei, keine Konkurrenz mit dem Vollscan). In gehoers Hauptdatenbank schreibt die Open
Libary nie (seit 20.10.2026 abends, siehe Messdienst). Dazu (Paket 3) gerechnete Fassungen unter
Samples/Open Libary Renders/ (Umgebung OPENLIBARY_RENDERS) mit ihrer Liste .renders.json.
Testläufe: OPENLIBARY_NETZ_ZIEL und OPENLIBARY_ZUSAETZE lenken Downloads in einen anderen Ordner, GEHOER_BIBLIOTHEK die
Hauptdatenbank (openlibary_netz.sqlite liegt dann daneben, OPENLIBARY_NETZ_DB setzt sie einzeln), OPENLIBARY_MESS_PRIO
den Vorsatz vor gehoer (Standard "nice -n 10").

Nur Standardbibliothek, läuft mit dem Python 3.9 von macOS. Der Server hört nur auf 127.0.0.1 und nimmt nur Anfragen
mit eigenem Host an (Schutz gegen fremde Webseiten im Browser, gefunden im Test vom 19.10.2026).
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

# Fertige App (03.11.2026): der Code liegt im Programmpaket, die Nutzerdaten unter OPENLIBARY_DATEN (die App setzt
# ~/Library/Application Support/Open Libary). sammlungen, verlauf und korrekturen lesen ihren Pfad beim Import, darum
# steht das vor den Imports. Ohne OPENLIBARY_DATEN bleibt alles neben dem Code (Jeremias' Einrichtung, einrichten.sh).
_DATEN = os.environ.get("OPENLIBARY_DATEN")
if _DATEN:
    os.makedirs(_DATEN, exist_ok=True)
    # die fertige App ist für sich: auch auf Jeremias' Mac keine Mischung mit seiner Bibliothek, gehoer und Klang-
    # Datenbank (Prüfung 03.11.: sonst sah er seinen Bestand ohne Favoriten, Downloads gingen in sein Frei (Netz))
    for _var, _datei in (("OPENLIBARY_SAMMLUNGEN", "sammlungen.json"), ("OPENLIBARY_VERLAUF", "verlauf.json"),
                         ("OPENLIBARY_KORREKTUREN", "korrekturen.json"), ("GEHOER_BIBLIOTHEK", "gehoer.sqlite"),
                         ("OPENLIBARY_NETZ_DB", "netz.sqlite"), ("OPENLIBARY_KLANG_DB", "klang.sqlite"),
                         ("OPENLIBARY_GEHOER", "kein-gehoer"), ("OPENLIBARY_LEICHT_DB", "leicht.sqlite")):
        os.environ.setdefault(_var, os.path.join(_DATEN, _datei))

import plattform         # 03.11.2026: Mac, Windows, Linux
import audioform         # 03.11.2026: AIFF ohne ffmpeg, „beim Ziehen als WAV“
import netz
import sammlungen
import verlauf          # Paket 4: gehört und gezogen
import korrekturen      # Paket 6: Art, Loop/One-Shot, BPM, Tonart, Lizenz von Hand
import ordner           # Musiker-Test 03.11.2026: eigene Ordner in der App
import klangnaehe       # Musiker-Test 03.11.2026: Klang-Fingerabdruck für „Ähnliche“
import leicht           # Nutzertest 2, 03.11.2026: Tempo und Grundton ohne gehoer

HIER = Path(__file__).resolve().parent
DATEN = Path(_DATEN) if _DATEN else HIER        # Nutzerdaten (siehe oben)


def _lokal():
    """Eigene Einrichtung, nicht im Repo: lokal.json neben dem Code, etwa {"musik": "~/Musik-Projekt",
    "library_neu": "~/Samples/_herkunft.csv", "gehoer_code": "~/gehoer"}. Ohne die Datei gelten neutrale Vorgaben."""
    try:
        with open(HIER / "lokal.json", encoding="utf-8") as f:
            return {k: os.path.expanduser(v) for k, v in json.load(f).items() if isinstance(v, str) and v}
    except (OSError, ValueError, AttributeError):
        return {}


LOKAL = _lokal()
# Musik-Projektordner mit Bibliothek/bestand.csv (gewachsene Einrichtung); fertige Apps und Fremde haben keinen
MUSIK = Path(os.environ.get("OPENLIBARY_MUSIK") or (LOKAL.get("musik") if not _DATEN else "") or DATEN / "kein-musikordner")
BESTAND = MUSIK / "Bibliothek/bestand.csv"
URTEILE = MUSIK / "Bibliothek/funde-teile/netz-urteile.tsv"
GEHOER_DB = Path(os.environ.get("GEHOER_BIBLIOTHEK", Path.home() / ".cache/gehoer/bibliothek.sqlite"))
# eigene gehoer-Datenbank der Open Libary: Netz-Downloads und "Messen" während eines laufenden Vollscans
NETZ_DB = Path(os.environ.get("OPENLIBARY_NETZ_DB", GEHOER_DB.parent / "openlibary_netz.sqlite"))
VOLLSCAN_PID = GEHOER_DB.with_name(GEHOER_DB.stem + ".vollscan.pid")    # schreibt gehoer/vollscan.sh
LIBRARY_NEU = Path(os.environ.get("OPENLIBARY_LIBRARY_NEU") or LOKAL.get("library_neu") or DATEN / "kein-herkunft.csv")
MESS_PRIO = os.environ.get("OPENLIBARY_MESS_PRIO", "nice -n 10").split()
ZUSAETZE = Path(os.environ.get("OPENLIBARY_ZUSAETZE", DATEN / "zusaetze.csv"))   # Testläufe: anderer Ort
# Musiker-Test 03.11.2026: eigene Ordner, in der App hinzugefügt (ordner.py)
ORDNER_LISTE = Path(os.environ.get("OPENLIBARY_ORDNER", DATEN / "ordner.json"))
EIGENE = Path(os.environ.get("OPENLIBARY_EIGENE", DATEN / "eigene.csv"))
# Ohne Musik-Projektordner: Downloads und Renders unter ~/Music/Open Libary
ABLAGE = Path.home() / "Music/Open Libary"
_EIGENE_ABLAGE = MUSIK.is_dir()
NETZ_ZIEL = Path(os.environ.get("OPENLIBARY_NETZ_ZIEL", MUSIK / "Samples/Frei (Netz)" if _EIGENE_ABLAGE else ABLAGE / "Netz"))
DRUM_ZIEL = Path(os.environ.get("OPENLIBARY_DRUM_ZIEL",
                                MUSIK / "Drum Kits/Frei (Netz)" if _EIGENE_ABLAGE else ABLAGE / "Netz Drums"))   # Drum-One-Shots
GEHOER = os.environ.get("OPENLIBARY_GEHOER") or shutil.which("gehoer") or str(Path.home() / ".local/bin/gehoer")
FFMPEG = plattform.werkzeug("ffmpeg") or "ffmpeg"
GEHOER_PY = Path.home() / ".local/share/gehoer/venv/bin/python"
KLANG_DB = Path(os.environ.get("OPENLIBARY_KLANG_DB", GEHOER_DB.parent / "openlibary_klang.sqlite"))
LEICHT_SWIFT = Path(os.environ.get("OPENLIBARY_LEICHT", HIER / "app/leichtmessen"))     # Mac ohne numpy
LEICHT_DB = Path(os.environ.get("OPENLIBARY_LEICHT_DB", plattform.datenordner() / "leicht.sqlite"))
KLANG_AUTO_MAX = 2000        # neue Dateien eines Ordners, die von selbst einen Fingerabdruck bekommen (0,2 s je Datei)
GEHOER_CODE = Path(os.environ.get("OPENLIBARY_GEHOER_CODE") or LOKAL.get("gehoer_code") or Path.home() / ".local/share/gehoer/code")
WANDEL_CACHE = Path(tempfile.gettempdir()) / "openlibary-wandel"
WANDEL_MAX_BYTES = 1024 ** 3        # älteste Wandlungen fliegen raus, wenn der Zwischenspeicher größer wird
PORT = 8765
CODE_STAND = os.path.getmtime(__file__)     # fertige App: erkennt einen alten Server nach einem Update (--ersetzen)
# Schlüssel und Logins (Freesound): nicht im Code-Ordner, damit der weitergegeben werden kann
KONFIG_ORDNER = Path(os.environ.get("OPENLIBARY_KONFIG", plattform.konfigordner()))
# /favicon.ico (vorher 404 in jeder Konsole): vier Balken einer Wellenform, eingebettet
FAVICON = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="2" fill="#1f2a44"/>'
           b'<g fill="#e8e4d8"><rect x="2" y="6" width="2" height="4"/><rect x="5" y="3" width="2" height="10"/>'
           b'<rect x="8" y="5" width="2" height="6"/><rect x="11" y="2" width="2" height="12"/></g></svg>')
STILE = HIER / "stile"
STIL_NAMEN = {   # Reihenfolge = Reihenfolge in den Einstellungen; Text für die Einstellungen
    "glas": ("Liquid Glass", "Allein alles aus Glas, sonst Kopf und Player."),   # kurz: zwei Zeilen auf der Karte
    "kobalt": ("Kobalt", "Hell, Filter und Player in kräftigem Kobaltblau, die Liste schwarz auf Weiß."),
    "kobalt-lila": ("Lila", "Wie Kobalt, nur in Violett."),
    "partitur": ("Partitur", "Hell wie Notenpapier, Zinnoberrot als einzige Farbe, Namen in Serifenschrift."),
    "tracker": ("Tracker", "Dunkles Flaschengrün, alles in Monospace wie ein alter Musik-Tracker."),
    "werkstatt-alt": ("Werkstatt", "Dunkles Braun mit Bernstein, der erste Entwurf."),
}
KEIN_STIL = {"grundriss", "glasschicht"}   # Bausteine, keine wählbaren Stile


def stil_normal(text):
    """stil.txt seit 24.10.2026: "glas" (Glas pur), "kobalt" (ohne Glas) oder "glas+kobalt" (Farbstil mit Glasschicht).
    Alte Werte (ein Name) gelten weiter. Gibt den bereinigten Wert zurück oder None, wenn er nicht taugt."""
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


def stile_liste():
    """Alle Stildateien außer Grundriss und Glasschicht, mit Beschreibung aus dem Kopfkommentar und den Hauptfarben."""
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
        for var in ("grund", "leiste", "signal", "text", "fuss", "glas-probe"):   # glas-probe: Kapsel in der Vorschau bei an Glas
            m = re.search(r"--" + var + r":\s*(#[0-9a-fA-F]{3,8})", text)
            if m:
                farben[var] = m.group(1)
        titel, text_kurz = STIL_NAMEN.get(name, (name, beschr))
        aus.append({"name": name, "titel": titel, "beschreibung": text_kurz, "farben": farben})
    return aus

TONARTEN = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
_GLEICH = {"Db": "C#", "D#": "Eb", "Gb": "F#", "G#": "Ab", "A#": "Bb", "Cb": "B", "Fb": "E", "E#": "F", "B#": "C"}
GRUNDTON_ARTEN = {"808", "bass"}
# Paket 2 (23.10.2026): tonale One-Shots zeigen ihren Grundton als Ton mit Oktave ("G1"), nur bei lo "oneshot";
# Loops dieser Arten behalten die Tonart. Art aus dem Bestand oder gehoers Einzelschlag-Klasse.
GRUNDTON_ONESHOT = {"kick", "snare", "tom", "perc", "808", "bass"}


def grundton_zeile(z):
    """Zeigt und filtert diese Zeile den Grundton statt einer Tonart?"""
    return z["art"] in GRUNDTON_ARTEN or (z["lo"] == "oneshot" and (z["art"] in GRUNDTON_ONESHOT
                                                                    or z.get("klasse") in GRUNDTON_ONESHOT))


def pegel_wert(v, lo, hi):
    """Messwert nur im plausiblen Bereich (gehoer hat einzelne Ausreißer wie 739 LUFS), sonst None."""
    return round(float(v), 1) if v is not None and lo <= v <= hi else None
GENRE_WOERTER = {"drill", "trap", "grime", "ukg", "garage", "house", "techno", "dnb", "jungle", "lofi", "boombap", "afro",
                 "afrobeat", "amapiano", "reggaeton", "dancehall", "jersey", "memphis", "phonk", "rnb", "soul", "funk",
                 "jazz", "dubstep", "hiphop", "trance", "breakbeat", "footwork", "baile", "cumbia", "dembow", "hyperpop"}    # tragen meist nur einen Grundton, kein Dur oder Moll
LIZENZ_ERKLAERUNG = {
    "ja": "Lizenz gelesen und frei nutzbar, auch kommerziell",
    "unklar": "Herkunft, Kaufbeleg oder Kontoinhaber ungeklärt; vor Verkauf klären",
    "nein": "laut Auswertung nicht nutzbar (Lizenz verbietet es oder Herkunft unrechtmäßig)",
    "ungeprüft": "selbst aus dem Netz geladen; die Lizenz hat der Hochladende eingetragen, Herkunft nicht geprüft",
    "unbekannt": "keine Lizenzangabe gefunden",
}
SPLICE_GRUND = "Splice-Lizenz gilt nur für den Kontoinhaber und ist nicht übertragbar"


def nfc(s):
    return unicodedata.normalize("NFC", s)


def tonart_norm(t):
    """'F#m', 'Gbm', 'A min', 'f#min', 'C' -> (Grundton aus TONARTEN, 'm' oder '') oder None."""
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
    """Verträgliche Tonarten: gleiche, parallele Dur/Moll, eine Quinte rauf und runter (Camelot ±1)."""
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
    return None if w is None or w != w or w in (float("inf"), float("-inf")) else w   # nan und inf wie leer


# ================================================================ gehoer-Werte: Swing, Takte, Einzelschlag

SWING_KLASSEN = ["gerade", "leicht", "deutlich", "triole"]
# Linke Spalte (24.10.2026): „Swing“ fasst leicht und deutlich zusammen (swing=swing), auch in swing_nicht
SWING_GRUPPEN = {"swing": ("leicht", "deutlich")}
# Sieben feste Genres (Parameter genre, genre_nicht): ODER über verwandte Vibe-Wörter, die Tags im Bestand bleiben
GENRE_GRUPPEN = {"grime": ("grime",), "ukg": ("ukg", "garage"), "trap": ("trap",), "drill": ("drill",),
                 "house": ("house",), "dnb": ("dnb", "jungle"), "hiphop": ("hiphop", "boombap")}
WORT_GENRE = {w: k for k, ws in GENRE_GRUPPEN.items() for w in ws}      # Zählen in einem Durchgang
TAKTE_CHIPS = [1, 2, 4, 8, 16]
TAKTE_GERECHNET = {1, 2, 3, 4, 6, 8, 12, 16, 24, 32}   # Loop-Längen, die aus Länge und Tempo zählen
# Einzelschlag-Klassen in der Reihenfolge der Filter; gehoer schreibt dieselben Wörter (klang.KLASSEN, dort zusätzlich
# fx, vocal und bass, die hier keine Klasse sind)
EINZELSCHLAG = ["kick", "snare", "clap", "hat", "cymbal", "perc", "tom", "rim", "808", "melodisch"]
# gehoer schreibt in oneshot_klasse auch die Kategorie aus dem Namen, bei "Kick Drum #1" etwa kickloop (Test 20.10.)
KLASSE_AUS_GEHOER = {"kickloop": "kick"}
# Kicks aus dem Namen nur bis zu dieser Länge: längere sind meist 808-artige Kicks mit langer Fahne, Impacts oder Racks
# mit mehreren Schlägen (Test 20.10.: 42 "Kicks" über 2 s, eine davon 28 s lang); gemessene zählen trotzdem
KICK_MAX_S = 2.0
# Halftime heißt "Snare auf Schlag 3": nur bei Drum-Loops (STIMMUNG-REGELN.md: Breaks, Tops, Fills). Test 20.10.: ein
# Rhodes-Loop ohne Snare stand als Halftime
GEFUEHL_ARTEN = {"break", "top", "fill", "perc", "hat"}
SONG_AB_S = 90.0            # länger: ganzer Song, kein Loop (Paket 0)
SONG_BEHAELT_ART = {"vocal", "fx", "atmo", "foley"}     # lange Acapellas und Field Recordings sind keine Songs
# gehoers produzenten_begriffe kommen aus einem Klangmodell, unabhängig von seiner Einzelschlag-Klasse. Bei One-Shots
# mit bekannter Klasse zählen Schlag-Begriffe nur, wenn sie zur Klasse passen (Paket 0: ein Bongo-Sample,
# gemessen perc, trug den Begriff kick und stand bei "kick" ganz oben)
BEGRIFF_KLASSE = {"kick": "kick", "snare": "snare", "clap": "clap", "hi-hat": "hat", "open hat": "hat",
                  "cymbal": "cymbal", "percussion": "perc", "shaker": "perc", "tom": "tom", "rim": "rim", "808": "808"}
KLASSE_VERWANDT = {"kick": {"808"}, "808": {"kick"}, "hat": {"cymbal"}, "cymbal": {"hat"}, "clap": {"snare"},
                   "snare": {"clap", "rim"}, "rim": {"snare", "perc"}, "tom": {"perc"}, "perc": {"tom", "rim"}}
UNSICHER = 0.5              # wie gehoer: darunter steht hinter dem Wert ein "?"
BEZUG_TOLERANZ = 0.02       # Swing und Halftime gelten nur beim Tempo, bei dem gehoer sie gemessen hat (2 %)
# gehoers Wächter (bibliothek.py SCAN_FRIST_S, SCAN_FRIST_JE_S, SCAN_ZUSCHLAG_S, ALLEIN_S): je Datei 120 s plus 4 s je
# Sekunde Audio, die erste eines Prozesses 600 s länger (Modelle laden); Dateien ab 300 s rechnet der Vollscan allein
GEHOER_FRIST_S, GEHOER_FRIST_JE_S, GEHOER_ZUSCHLAG_S, GEHOER_ALLEIN_S = 120.0, 4.0, 600.0, 300.0


def swing_klasse(x):
    """Lage der Off-Beat-Achtel (gehoer swing, 0.50 gerade bis 0.67 Triole) -> Klasse nach STIMMUNG-REGELN.md:
    gerade bis 0.55, leicht 0.56 bis 0.58, deutlich 0.59 bis 0.64, triole ab 0.65 (auf zwei Stellen gerundet)."""
    if x is None:
        return None
    x = round(x, 2)
    return "gerade" if x <= 0.55 else "leicht" if x <= 0.58 else "deutlich" if x <= 0.64 else "triole"


def _sicherheit(text, name):
    """'Sicherheit swing 0.86, halftime 0.85, live 0.80' (stimmung_quelle der Library-Neu) -> 0.86 für 'swing'."""
    m = re.search(r"\b" + name + r" (\d\.\d+)", text or "")
    return float(m.group(1)) if m else None


# ================================================================ Wörter

_SYNONYME = [   # beide Seiten (Index und Suche) laufen hier durch
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
    """Vibe-Wörter wie woerter, aber Jahrzehnte bleiben ganz: "70s", "90's", "2000s" statt "70", "90", "2000" (Test
    20.10.: die Vibes "70" und "90" sahen neben der BPM-Spalte wie ein Tempo aus); "1970s" wird "70s"."""
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
    """Alle Samples im Speicher (rund 63.000 Zeilen), dazu Messwerte aus gehoer. Neu geladen wird im Hintergrund,
    bis dahin antwortet der alte Stand (vorher lud jede gleichzeitige Anfrage selbst: Spitzen von 8.5 s)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.lade_lock = threading.Lock()      # höchstens ein Neuladen im Hintergrund (aktuell_halten)
        self.lauf_lock = threading.Lock()      # Neuladen nacheinander, nie zwei zugleich (laden)
        self.zeilen, self.nach_pfad, self.md5, self.packs = [], {}, set(), []
        self.pack_menge = set()
        self.arten = []            # Paket 6: Arten im Bestand (Auswahl beim Bearbeiten)
        self.stempel = self.fehler = None
        self.gemessen = 0
        self.abdeckung = {}
        self.version = 0           # Nummer des zuletzt fertig geladenen Stands
        self.gestartet = 0         # Nummer des zuletzt begonnenen (Messdienst: Werte sind da, sobald version größer ist)
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
        # die ersten zwei zählen sofort, der Rest (gehoer-Datenbanken, Library-Neu) höchstens einmal je Minute
        return (mt(BESTAND), mt(ZUSAETZE), mt(EIGENE), mt(GEHOER_DB), mt(Path(str(GEHOER_DB) + "-wal")),
                mt(NETZ_DB), mt(Path(str(NETZ_DB) + "-wal")), mt(LIBRARY_NEU))

    def aktuell_halten(self):
        neu = self._stempel()
        if neu == self.stempel or time.time() < self.naechster_versuch:
            return
        # Ändert sich nur eine gehoer-Datenbank (Vollscan schreibt stundenlang) oder die Library-Neu-Liste, höchstens
        # einmal je Minute neu laden; eigene Downloads (zusaetze.csv) und bestand.csv sofort
        if self.stempel and neu[:3] == self.stempel[:3] and time.time() - getattr(self, "geladen_um", 0) < 60:
            return
        if self.lade_lock.acquire(blocking=False):   # genau einer lädt, im Hintergrund
            def lauf():
                try:
                    self.laden()
                finally:
                    self.lade_lock.release()
            threading.Thread(target=lauf, daemon=True).start()

    def laden(self):
        """Index neu aufbauen. Nacheinander (lauf_lock): vorher konnten aktuell_halten, netz.py und der Messdienst
        zugleich laden, und ein älterer Lauf, der zuletzt fertig wurde, überschrieb neuere Messwerte (Prüfung 20.10.)."""
        with self.lauf_lock:
            self.gestartet += 1
            self._laden(self.gestartet)

    def _laden(self, nr):
        stempel = self._stempel()
        # gehoer-Datenbanken zuerst: ist eine nicht lesbar, bleibt der alte Stand stehen (sonst stünde die Liste ohne
        # jeden Messwert da, Prüfung 20.10.), höchstens dreimal hintereinander; danach ohne die unlesbare weiter
        reihen, unlesbar = self._gehoer_reihen()
        if unlesbar:
            self.naechster_versuch = time.time() + 30
            self.unlesbar_runden += 1
            if self.zeilen and self.unlesbar_runden <= 3:
                print(f"Index bleibt beim alten Stand, nicht lesbar: {', '.join(unlesbar)}", flush=True)
                return
        else:
            self.unlesbar_runden = 0
        zeilen, nach_pfad, md5, fehler = [], {}, set(), None
        quellen = []
        # Prüfung 03.11.2026: zusaetze.csv zuerst. Netz-Downloads liegen in Ordnern, die auch die Bibliotheks-Skripte
        # lesen; nach dem Neubau des Bestands am 03.11. standen drei Freesound-CC0-Sounds dort mit „nein“ und
        # verdeckten Quelle, Urheber und Beleg aus zusaetze.csv (gleicher Pfad: die erste Quelle gewinnt)
        if ZUSAETZE.is_file():
            quellen.append((ZUSAETZE, "openlibary"))
        if BESTAND.is_file():
            quellen.append((BESTAND, None))
        elif BESTAND.parent.is_dir():      # ohne Bibliotheksordner (Freunde) ist das kein Fehler, nur leer
            fehler = f"bestand.csv fehlt ({BESTAND})"
        if EIGENE.is_file():               # Musiker-Test 03.11.: eigene Ordner aus der App
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
                    if pfad.startswith(render_praefix):    # Paket 3: gerechnete Fassungen sind keine neuen Samples
                        continue
                    z = self._aus_bestand(r, pfad, herkunft)
                    z["_eigen"] = datei == EIGENE        # ordner.py führt nur, was nicht schon anders im Index steht
                    nach_pfad[pfad] = z
                    zeilen.append(z)
        self._doppelte_markieren(zeilen)
        # Prüfung 03.11.: Kopie eines Packs aus dem Bestand in einem eigenen Ordner (Backup, Downloads) erscheint nicht
        # doppelt; sie zählt als Kopie der Bestandszeile (deren Lizenz gilt), sichtbar mit „Doppelte einzeln zeigen“
        fremd_md5 = {}
        for z in zeilen:
            if z["md5"] and not z["_eigen"]:
                fremd_md5.setdefault(z["md5"], z)
        for z in zeilen:
            if z["_eigen"] and z["md5"] and z["md5"] in fremd_md5:
                o = fremd_md5[z["md5"]]
                if not os.path.exists(o["pfad"]):     # die Bestandszeile zeigt ins Leere: die eigene bleibt sichtbar
                    o["ist_kopie"] = True
                    fremd_md5[z["md5"]] = z
                    continue
                z["ist_kopie"] = True
                o["kopien"] = o.get("kopien", 0) + 1
            elif z["_eigen"] and z["md5"]:      # Nutzertest 2: dieselbe Datei mehrfach in eigenen Ordnern (Kits)
                fremd_md5[z["md5"]] = z
        gemessen = self._gehoer_einlesen(nach_pfad, reihen)
        self._leicht_einlesen(zeilen)          # Nutzertest 2: füllt nur Lücken, Hand und gehoer gehen vor
        if unlesbar:   # ohne die unlesbare Datenbank: Stempel offen lassen, frühestens in 30 s nochmal
            stempel = None
        self._library_neu_einlesen(zeilen)
        self._live_aus_tags(zeilen)
        # Paket 6: Korrekturen von Hand gewinnen vor Name, Splice-Datenbank und gehoer (vor Takten und Suchwörtern)
        md5_von = collections.defaultdict(list)
        for z in zeilen:
            if z["md5"]:
                md5_von[z["md5"]].append(z["pfad"])
        einzeln = korrekturen.fuer_index(nach_pfad, md5_von)
        pack_ent = korrekturen.packs()      # Paket 7: Entscheid je Pack, Einzelkorrekturen gehen vor
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
            # Paket 7: Pack-Entscheide nur für Packs im Index, ohne die Ordner der Einzelsounds
            self.pack_menge = {z["pack"] for z in zeilen if pack_entscheidbar(z)}
            self.stempel, self.fehler, self.gemessen, self.abdeckung = stempel, fehler, gemessen, abdeckung
            self.version = nr
            self.geladen_um = time.time()
        SUCH_CACHE.clear()

    _AUSGESCHRIEBEN = re.compile(r"(?i)(?<![A-Za-z])([A-G])[ _-]?(sharp|flat)(?:[ _-]?(min|maj|minor|major|m))?(?![a-z])")
    _ONESHOT_ORDNER = re.compile(r"(?i)one[\s_-]?shots?|\bhits?\b|808s|\bkicks\b|\bsnares\b|\bhats\b|\bclaps\b|"
                                 r"\bpercs?\b|open hats|closed hats|\bdrums?\b(?!.*loop)")
    _BPM_IM_NAMEN = re.compile(r"(?:^|[\s_-])(\d{2,3})(?=[\s_-]+(?:[A-G](?:#|b)?\s?(?:m|min|maj|major|minor)?)(?:[\s_.-]|$))")

    def _aus_bestand(self, r, pfad, herkunft):
        tn = tonart_norm(r.get("tonart"))
        m = self._AUSGESCHRIEBEN.search(r.get("datei") or "")
        if m:   # "D Sharp Min" las bestand.csv als "D" (Dur); Name gewinnt
            tn = tonart_norm(m.group(1) + m.group(2) + (m.group(3) or ""))
        art = r.get("art") or ""
        lo = r.get("loop_oneshot") or ""
        name = r.get("datei") or os.path.basename(pfad)
        dauer = zahl(r.get("dauer"))
        bpm, bpm_quelle = zahl(r.get("tempo")), r.get("tempo_quelle") or ("name" if r.get("tempo") else "")
        ordner = os.path.dirname(pfad)
        # ACID-Block: FL schreibt 140 als Standard hinein, auch in One-Shots (Test 19.10.2026: 966 Werte, 421-mal 140,
        # Hat-"Loops" unter 2 s). Gilt nur, wenn die Datei mindestens einen Takt lang ist und kein One-Shot-Ordner.
        if bpm_quelle == "acid_block" and bpm:
            takt = 240.0 / bpm
            if (dauer is not None and dauer < takt * 0.95) or (
                    self._ONESHOT_ORDNER.search(os.path.basename(ordner)) and "loop" not in (name + ordner).lower()):
                bpm, bpm_quelle, lo = None, "", "oneshot"
        if art == "808" and not lo and (dauer or 0) < 8:
            lo = "oneshot"
        # BPM vor einer Tonart im Namen ("1005 STARTER 140 GM", "_130-Amin"), nur bei Loops
        if bpm is None and lo != "oneshot":
            b = self._BPM_IM_NAMEN.search(os.path.splitext(name)[0])
            if b and 60 <= int(b.group(1)) <= 200:
                bpm, bpm_quelle = float(b.group(1)), "name"
        stimmung = []
        for teil in re.split(r"[;,|]", r.get("stimmung") or ""):
            for w in stimmung_woerter(teil):   # "vinyl lofi" sind zwei Stimmungen, "live 70s" live und 70s
                if w not in stimmung and len(w) > 1:
                    stimmung.append(w)
        # Genre aus Pack- und Ordnernamen erben (Test 19.10.: eigene Drill-Kits trugen kein "drill")
        for w in woerter(" ".join([r.get("pack") or ""] + list(Path(os.path.dirname(pfad)).parts[-3:]))):
            if w in GENRE_WOERTER and w not in stimmung:
                stimmung.append(w)
        # Vocal-Einträge, deren Name ein Instrument nennt ("91V_HT_145_drum_top_loop"): Art aus dem Namen
        if art == "vocal" and re.search(r"(?i)guitar|piano|keys|drum|perc|synth|\bbass\b|strings", name) \
                and not re.search(r"(?i)vocal|vox|voice|chop|acapella", name):
            art = netz.einordnen(name, "", dauer)["art"] or art
        # ganze Songs (über 90 s) sind weder Loop noch Break: Art "song", lo leer (Paket 0, 23.10.: ein ganzer Beat
        # als MP3, 226 s, stand als Break-Loop). Acapellas, FX und Atmos behalten ihre Art.
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
            grund = SPLICE_GRUND
        else:
            grund = LIZENZ_ERKLAERUNG.get(lizenz, "")
            if r.get("hinweis"):   # selbst geladen: Lizenz der Quelle, Namensnennung, Vorschau-Hinweis
                grund += ". " + r["hinweis"]
        warnung = r.get("warnung") or ""
        if re.search(r"(?i)\bamen\b|funky[\s_-]?drummer|\bthink\b|apache|impeach|levee|skull[\s_-]?snaps", name) \
                and not warnung:
            warnung = "Name deutet auf einen Plattenbreak: vorher anhören, Lizenz der Aufnahme prüfen"
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
            # selbst geladen (zusaetze.csv): für den Reiter Downloads
            "geladen_am": r.get("geladen_am") or "", "quelle": r.get("quelle") or "", "urheber": r.get("urheber") or "",
            # aus gehoer und Library-Neu (laden): Quelle "gemessen", "library-neu", "gerechnet" oder "name"
            "takte": None, "takte_quelle": "", "bpm_gefuehl": None,
            "swing": None, "swing_wert": None, "swing_quelle": "", "swing_unsicher": False,
            "gefuehl": None, "gefuehl_quelle": "", "gefuehl_unsicher": False,     # "halftime", "normal" oder None
            # "ja", "nein", "unsicher" oder None; Quelle "library-neu" (gemessen) oder "tags" (Vibe live, nur ja)
            "live": None, "live_info": "", "live_quelle": "",
            # Einzelschlag: bis zur Messung die bisherige Kategorie, wenn die Datei ein One-Shot ist (Kicks nur kurze)
            "klasse": None, "klasse_quelle": "", "klasse_unsicher": False,
            # gehoer (Paket 2): Grundton mit Oktave ("G1") und Cent bei tonalen One-Shots, Pegel für "Pegel gleich"
            "grundton": None, "grundton_cent": None, "lufs": None, "peak_dbfs": None,
            # Paket 6: Datum der Datei (mtime aus gehoer, sonst „geaendert“ aus bestand.csv, bei Downloads das Ladedatum)
            # für die Sortierung; hand: Felder, die von Hand korrigiert sind (Zeichen „h“)
            "datum": (r.get("geaendert") or r.get("geladen_am") or "").replace("T", " ")[:16], "hand": [],
            # Paket 7: Lizenz aus dem Entscheid für das ganze Pack (dann trägt sie auch „h“) und der Beleg dazu
            "lizenz_pack": False, "lizenz_beleg": "",
        }
        if z["datum"] > DATUM_GRENZE:      # Paket 6: Dateidaten in der Zukunft (2177 im Bestand) zählen als unbekannt
            z["datum"] = ""
        if lo == "oneshot" and art in EINZELSCHLAG and not (art == "kick" and (dauer or 0) > KICK_MAX_S):
            z["klasse"], z["klasse_quelle"] = art, "name"
        # Name, Pack und Stimmung zählen voll; vom Pfad nur die zwei letzten Ordner (vorher fand "trap" 1.629 Snares
        # nur wegen des Ordners "Trap & Underground" weiter oben)
        # Paket 6: die Art zählt getrennt (woerter_setzen), damit eine Korrektur der Art auch die Suche umstellt
        z["_wbasis"] = " ".join(woerter(" ".join([name, pack, " ".join(stimmung), r.get("tags") or ""]
                                                 + list(Path(ordner).parts[-2:]))))
        # Musiker-Test 03.11.2026: Felder getrennt für die Sortierung nach Treffergüte (relevanz_punkte)
        z["_wname"] = " " + " ".join(woerter(os.path.splitext(name)[0])) + " "
        z["_wort"] = " " + " ".join(woerter(" ".join([pack] + list(Path(ordner).parts[-2:])))) + " "
        z["_wtag"] = " " + " ".join(woerter(" ".join(stimmung) + " " + (r.get("tags") or ""))) + " "
        return z

    @staticmethod
    def _doppelte_markieren(zeilen):
        """Gleicher Klang (doppelt_gruppe aus bestand.csv, über die Audiodaten) erscheint nur einmal: bevorzugt mit
        Lizenz ja, dann eigene Ordner, dann kürzester Pfad. Die übrigen zählen als Kopien."""
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
                      "mtime",      # Paket 6: Datum der Datei für die Sortierung
                      "md5"]        # Musiker-Test 03.11.: Messung findet umgezogene Dateien

    @staticmethod
    def _gehoer_lesen(db):
        """Alle Zeilen einer gehoer-Datenbank (nur lesend): [] ohne Datei, None, wenn sie nicht lesbar ist. Fehlt einer
        älteren Datenbank eine Spalte, steht dort None."""
        if not db.is_file():
            return []
        try:
            # keine mode=ro-Verbindung: die scheitert an einer WAL-Datenbank, wenn gerade kein Schreiber die -shm hält
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
            print(f"gehoer-Datenbank {db.name} nicht lesbar: {e}", flush=True)
            return None

    @staticmethod
    def _gehoer_reihen():
        """Zeilen der gehoer-Hauptdatenbank und der eigenen (NETZ_DB). -> ([Zeilen je lesbarer Datenbank], [Namen der
        nicht lesbaren])"""
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
        """Messwerte aus der gehoer-Hauptdatenbank und der eigenen (NETZ_DB) übernehmen, nur lesend. Steht eine Datei in
        beiden, gilt die Zeile ohne Fehler, dann die jüngere. Gemessene Werte füllen Lücken; wo der Name schon BPM oder
        Tonart nennt, bleibt der Name maßgeblich (die Hersteller wissen es genauer als jede Messung)."""
        beste = {}
        # Messung ohne Datei am alten Ort (FL Studio 2025 -> 2026, Ordner verschoben): über die md5 zu den Zeilen, die
        # keine eigene Messung haben (Musiker-Test 03.11.2026: 3.371 Werkspack-Messungen hingen sonst am alten Pfad)
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
                if g is None:      # nach allen Datenbanken: die umgezogenen Messungen, nur wo nichts Eigenes steht
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
            if g["mtime"] and 0 < g["mtime"] < time.time() + 86400:    # Paket 6: Datum laut gehoer (Zukunft bleibt weg)
                z["datum"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(g["mtime"]))
            vorwissen = z["bpm"] is not None or g["name_bpm"] is not None   # Tempo aus Name oder Projekt
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
            # Pegel für "Pegel gleich" (Paket 2); unplausible Werte zählen als fehlend
            z["lufs"], z["peak_dbfs"] = pegel_wert(g["lufs"], -70, 6), pegel_wert(g["peak_dbfs"], -90, 30)
        return n

    @staticmethod
    def _grundton_uebernehmen(z, g):
        """Grundton gehoers für 808, Bass und tonale One-Shots (nach der Klasse, die erst _groove_uebernehmen setzt).
        Bei One-Shots geht der Grundton vor einer gemessenen Tonart (eine Kick in "Fm" sagt nichts); ein Ton aus dem
        Namen bleibt, bekommt die Oktave aber nur, wenn gehoer denselben Ton hört."""
        if g["grundton_pc"] is None or not grundton_zeile(z):
            return
        pc = int(g["grundton_pc"]) % 12
        if z["ton"] is None or (z["ton_quelle"] == "gemessen" and z["lo"] == "oneshot"):
            z["ton"], z["modus"], z["ton_quelle"] = TONARTEN[pc], "", "gemessen"
            z["ton_unsicher"] = (g["grundton_sicherheit"] or 0) < UNSICHER
        if z["ton"] == TONARTEN[pc] and g["grundton_midi"] is not None and z["lo"] == "oneshot":   # Loops ohne Oktave
            midi = int(g["grundton_midi"])
            z["grundton"] = TONARTEN[midi % 12] + str(midi // 12 - 1)
            z["grundton_cent"] = int(g["grundton_cent"]) if g["grundton_cent"] is not None else None

    @staticmethod
    def _leicht_einlesen(zeilen):
        """Tempo (Loops ohne Tempo) und Grundton (808, Bass, tonale One-Shots ohne Ton) aus der leichten Messung, nur wo
        gehoer nicht gemessen hat. Tonarten von Loops nicht: zu unsicher (siehe leicht.py)."""
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
            # Grundton nur für 808, Bass und melodische One-Shots: bei Kicks und Snares war er unzuverlässig, und mit Ton
            # filtert der Projektrahmen sie nach Tonart (Nutzertest 2: Kick fiel von 520 auf 246)
            tonal = z["art"] in ("808", "bass", "melodisch") or z.get("klasse") in ("808", "bass", "melodisch")
            if z["ton"] is None and z["lo"] == "oneshot" and tonal and w["grundton_midi"] is not None \
                    and (w["tonhaltig"] or 0) >= leicht.TONHALTIG_AB:
                midi = int(w["grundton_midi"])
                z["ton"], z["modus"], z["ton_quelle"], z["ton_unsicher"] = TONARTEN[midi % 12], "", "leicht", True
                z["grundton"], z["grundton_cent"] = TONARTEN[midi % 12] + str(midi // 12 - 1), w["grundton_cent"]

    @staticmethod
    def _begriffe_zur_klasse(z):
        """gehoer-Begriffe ohne Schläge, die der Einzelschlag-Klasse widersprechen (nur One-Shots mit Klasse).
        Vorher fanden 513 Dateien "kick" nur über diesen Begriff, darunter 35 gemessene Percs und 17 Toms."""
        k = z["klasse"]
        if not k or z["lo"] != "oneshot" or not z["begriffe"]:
            return z["begriffe"]
        ok = {k} | KLASSE_VERWANDT.get(k, set())
        return [b for b in z["begriffe"] if BEGRIFF_KLASSE.get(b, k) in ok]

    @staticmethod
    def _groove_uebernehmen(z, g, vorwissen):
        """Takte, Swing, Halftime und Einzelschlag-Klasse aus einer gehoer-Zeile.
        Swing und Halftime hängen an der Länge des Schlags (STIMMUNG-REGELN.md, Bezugstempo): sie zählen nur, wenn
        gehoers Tempo auf 2 % zum Tempo der Zeile passt (Name zuerst). Halftime heißt: bpm_gefuehl ist das halbe Tempo
        und die Taktart nicht 6/8 (dort steht die punktierte Viertel). Ohne Tempo im Namen kann dasselbe halbe Tempo
        auch gehoers Zweifel an der Oktave sein (rhythmus: rechnung:oktave, dann Tempo-Sicherheit höchstens 0,6): dann "?".
        Halftime nur bei Drum-Loops (GEFUEHL_ARTEN), die Einzelschlag-Klasse nur bei One-Shots."""
        # Bestand sagt Loop, die Datei ist aber kürzer als ein Takt und gehoer misst einen One-Shot: das Tempo kam aus
        # dem Kit (Splice), wie beim ACID-Block oben (Prüfung 20.10.: eine Kick mit „174“ im Namen, 0,48 s, „kickloop“)
        if (z["lo"] == "loop" and g["art"] == "oneshot" and z["bpm"] and z["dauer"] is not None
                and z["dauer"] < 0.95 * 240.0 / z["bpm"]):
            z["lo"] = "oneshot"
        if g["takte"] and g["takte"] > 0 and z["lo"] != "oneshot" and z["art"] != "song":
            z["takte"], z["takte_quelle"] = round(g["takte"], 2), "gemessen"
        gb, bezug = g["bpm"], z["bpm"]
        passt = bool(gb and bezug and abs(gb / bezug - 1) <= BEZUG_TOLERANZ)
        # Swing, Gefühl und gefühltes Tempo nur bei Loops (gehoer findet auch in manchen One-Shots einen Puls)
        loopartig = z["lo"] == "loop" or (z["lo"] != "oneshot" and g["art"] in ("loop", "ausschnitt"))
        drums = z["art"] in GEFUEHL_ARTEN
        gf = g["bpm_gefuehl"]
        halb = bool(passt and gf) and abs(2 * gf / gb - 1) <= BEZUG_TOLERANZ
        doppelt = bool(passt and gf) and abs(gf / (2 * gb) - 1) <= BEZUG_TOLERANZ    # nur gehoers Oktav-Zweifel
        # gefühltes Tempo zählt bei "halb/doppelt" mit: punktierte Viertel bei 6/8 immer, Halftime nur bei Drum-Loops
        if passt and loopartig and gf and not doppelt and (not halb or (drums and g["taktart"] != "6/8")):
            z["bpm_gefuehl"] = round(gf, 2)
        if passt and loopartig and g["swing"] is not None:
            z["swing"], z["swing_wert"], z["swing_quelle"] = swing_klasse(g["swing"]), round(g["swing"], 3), "gemessen"
            z["swing_unsicher"] = (g["swing_sicherheit"] or 0) < UNSICHER
        if passt and loopartig and drums and g["taktart"] != "6/8":
            z["gefuehl"], z["gefuehl_quelle"] = ("halftime" if halb else "normal"), "gemessen"
            z["gefuehl_unsicher"] = halb and not vorwissen and (g["bpm_sicherheit"] or 0) <= 0.6
        # Einzelschlag nur bei One-Shots (Prüfung 20.10.: zwei Loops standen sonst im Filter Einzelschlag)
        klasse = (g["oneshot_klasse"] or "").lower()
        klasse = KLASSE_AUS_GEHOER.get(klasse, klasse)
        if klasse in EINZELSCHLAG and (z["lo"] == "oneshot" or (not z["lo"] and g["art"] == "oneshot")):
            z["klasse"], z["klasse_quelle"] = klasse, "gemessen"
            z["klasse_unsicher"] = (g["oneshot_sicherheit"] or 0) < UNSICHER

    @staticmethod
    def _library_neu_einlesen(zeilen):
        """swing, gefuehl, live der Library-Neu (über md5). Dort gemessen heißt: stimmung_quelle nennt das Bezugstempo
        ("gemessen bei 142 BPM"); dann gilt der Wert der Library-Neu auch vor der gehoer-Datenbank, ein leerer swing
        heißt "nicht messbar" (nie gerade), ein leeres gefuehl "kein Halftime". live gibt es nur dort."""
        if not LIBRARY_NEU.is_file():
            return 0
        try:
            with open(LIBRARY_NEU, newline="", encoding="utf-8") as f:
                werte = {r["md5"]: r for r in csv.DictReader(f) if r.get("md5") and r.get("stimmung_quelle")}
        except (OSError, csv.Error, KeyError, UnicodeDecodeError) as e:
            print(f"Library-Neu nicht lesbar: {e}", flush=True)
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
        """Ohne Messung zählt der Vibe "live" (Splice-Tags, Packbeschreibung, Ordner- und Dateiname) als live gespielt,
        bei Loops. Vorher gab es zweimal "live": den Vibe (606 Samples) und den Filter Live gespielt (nur Library-Neu);
        "boom bap"-Breaks zeigten live, der Filter fand 0 (Test 20.10.). Eine Messung geht immer vor."""
        for z in zeilen:
            if z["live"] is None and z["lo"] != "oneshot" and "live" in z["stimmung"]:
                z["live"], z["live_quelle"] = "ja", "tags"
                z["live_info"] = "laut Tags oder Packbeschreibung (Vibe live), nicht gemessen"

    @staticmethod
    def _takte_rechnen(zeilen):
        """Loops ohne gemessene Takte: aus Länge und Tempo (4/4), wenn das auf 2 % eine übliche Loop-Länge ergibt (1, 2,
        3, 4, 6, 8, 12, 16, 24, 32 Takte). Steht ohne "g" da; gehoer ersetzt den Wert, sobald gemessen."""
        for z in zeilen:
            if z["takte"] is not None or z["lo"] != "loop" or not z["bpm"] or not z["dauer"]:
                continue
            t = z["dauer"] * z["bpm"] / 240.0
            n = int(round(t))
            if n in TAKTE_GERECHNET and abs(t - n) <= 0.02 * n:
                z["takte"], z["takte_quelle"] = float(n), "gerechnet"

    @staticmethod
    def _abdeckung(zeilen):
        """Wie viele Samples (ohne ausgeblendete Kopien) einen Wert haben: für den Leerzustand der Filter."""
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
            x = re.sub(r"\s+\d(\.\d+)?$", "", str(x).strip())   # gehoer schreibt "label 0.83, label 0.41"
            if x:
                aus.append(x)
        return aus[:8]


INDEX = None


def woerter_setzen(z):
    """Suchwörter einer Zeile: Name, Pack, Vibe, Tags, zwei Ordner (_wbasis), die Art und gehoers Instrumente, Stil,
    Begriffe. Die Art steht getrennt, damit eine Korrektur von Hand (Paket 6) sie ohne Neuladen tauscht."""
    z["woerter"] = " " + " ".join([z["_wbasis"]] + woerter(z["art"])
                                  + woerter(" ".join(z["instrumente"] + z["stil"] + z["begriffe"]))).strip() + " "
    z["_wart"] = " " + " ".join(woerter(z["art"] + " " + (z["klasse"] or ""))) + " "    # Treffergüte (relevanz_punkte)


# Paket 6: Felder, die eine Korrektur berühren kann; der Stand vor der ersten Korrektur liegt in z["_vorher"], damit
# ein Aufheben den alten Wert ohne Neuladen zurückbringt
KORR_STAND = ("art", "lo", "bpm", "bpm_quelle", "ton", "modus", "ton_quelle", "ton_unsicher", "grundton",
              "grundton_cent", "lizenz", "lizenz_grund", "klasse", "klasse_quelle", "klasse_unsicher", "takte",
              "takte_quelle", "begriffe", "lizenz_pack", "lizenz_beleg")
HAND = "hand"                 # Quelle von Hand gesetzter Werte, die Oberfläche zeigt „h“
MESSEN_MAX = 5000             # Messen für viele auf einmal (gehoer braucht je Datei Sekunden)
KOERPER_MAX = 16 << 20        # größter POST-Körper (Pfadliste aller markierten bei Cmd+A)
DATUM_GRENZE = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 86400))   # später: Unsinn


def korrektur_anwenden(z, felder, pack_ent=None):
    """Korrektur (felder aus korrekturen.py, {} = keine) auf eine Indexzeile legen. Erst den Stand ohne Korrektur
    zurückholen, dann die Felder setzen; Einzelschlag-Klasse, gerechnete Takte, Begriffe und Suchwörter folgen.
    Paket 7: darunter liegt der Entscheid für das Pack (Lizenz, Grund, Beleg); eine eigene Lizenz der Zeile geht vor.
    pack_ent: korrekturen.packs() beim Laden (sonst liest pack_felder selbst)."""
    z["_korr"] = dict(felder)          # Einzelkorrektur merken: ein neuer Pack-Entscheid legt sie wieder obenauf
    pf = korrekturen.pack_felder(z["pack"], pack_ent) if pack_entscheidbar(z) else {}
    if "_vorher" in z:
        z.update(z["_vorher"])
        z["begriffe"] = list(z["begriffe"])
    elif felder or pf:
        z["_vorher"] = {k: (list(z[k]) if isinstance(z[k], list) else z[k]) for k in KORR_STAND}
    z["hand"] = []
    if pf.get("lizenz") and "lizenz" not in felder:
        z["lizenz"], z["lizenz_pack"], z["lizenz_beleg"] = pf["lizenz"], True, pf.get("beleg") or ""
        z["lizenz_grund"] = "Für das ganze Pack entschieden" + (": " + pf["lizenz_grund"] if pf.get("lizenz_grund") else "") \
            + ("; Beleg: " + pf["beleg"] if pf.get("beleg") else "")
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
            if not n or n[0] != z["ton"]:          # Grundton mit Oktave nur, solange der Ton derselbe bleibt
                z["grundton"], z["grundton_cent"] = None, None
            z["ton"], z["modus"] = (n[0], n[1]) if n else (None, None)
            z["ton_quelle"], z["ton_unsicher"] = HAND, False
        if "lizenz" in felder:
            z["lizenz"] = felder["lizenz"]
            g = felder.get("lizenz_grund") or ""
            z["lizenz_grund"] = "Von Hand gesetzt" + (": " + g if g else "")
        if "art" in felder or "lo" in felder:     # Einzelschlag folgt der Art bei One-Shots, sonst keiner
            a = z["art"]
            ok = z["lo"] == "oneshot" and a in EINZELSCHLAG
            z["klasse"], z["klasse_quelle"], z["klasse_unsicher"] = (a, HAND, False) if ok else (None, "", False)
        z["hand"] = [f for f in ("art", "lo", "bpm", "tonart", "lizenz") if f in felder or (f in z["hand"])]
    # gerechnete Takte hängen an Tempo und Loop, gemessene gelten nicht für One-Shots
    if z["takte_quelle"] != "gemessen" or z["lo"] == "oneshot":
        z["takte"], z["takte_quelle"] = None, ""
        Index._takte_rechnen([z])
    z["begriffe"] = Index._begriffe_zur_klasse(z)
    woerter_setzen(z)


def korrektur_im_index(geaendert):
    """Nach POST /api/korrektur: betroffene Zeilen im Speicher ersetzen statt neu zu laden. geaendert: {pfad: felder}"""
    for p, felder in geaendert.items():
        z = INDEX.nach_pfad.get(p)
        if z is not None:
            korrektur_anwenden(z, felder)
    abdeckung = Index._abdeckung(INDEX.zeilen)
    with INDEX.lock:
        INDEX.abdeckung = abdeckung
    SUCH_CACHE.clear()


# ---------------------------------------------------------------- Paket 7: Lizenz pro Pack
LIZENZ_OFFEN = ("unklar", "ungeprüft")     # noch nicht entschieden („unbekannt“ kommt im Bestand nicht vor)
HERKUNFT_NAME = {"splice": "Splice", "netz": "Netz", "openlibary": "Netz", "ordner": "Ordner"}
EINZEL_TEIL = os.sep + netz.EINZEL_ORDNER + os.sep


def pack_entscheidbar(z):
    """Gilt ein Pack-Entscheid für diese Zeile? Nicht für Einzelsounds aus dem Netz: ihr „Pack“ ist der Ordner nach Art
    oder Kit (netz.py: pack = Ordnername, etwa „Vocals“), jede Datei hat ihre eigene Lizenz. Ein Entscheid dort hätte
    jeden künftigen Download in diesen Ordner still mitgenommen (Prüfung 24.10.: CC BY-NC stand danach auf ja)."""
    return bool(z["pack"]) and EINZEL_TEIL not in z["pfad"]


def pack_im_index(pack):
    """Nach einem Pack-Entscheid: alle Zeilen des Packs neu belegen (ihre Einzelkorrektur bleibt obenauf)."""
    n = 0
    for z in INDEX.zeilen:
        if z["pack"] == pack:
            korrektur_anwenden(z, z.get("_korr") or {})
            n += 1
    SUCH_CACHE.clear()
    return n


def lizenz_basis(z):
    """Lizenz und Grund der Zeile ohne Pack-Entscheid und ohne Korrektur von Hand."""
    v = z.get("_vorher") or z
    return v["lizenz"], v["lizenz_grund"]


def lizenz_offen_zahl():
    """Zähler links „Lizenz offen“: wie die Bibliothek ohne Filter (ohne Kopien und verworfene) gezählt."""
    return sum(1 for z in filtern({}) if z["lizenz"] in LIZENZ_OFFEN)


def lizenz_packs():
    """Lizenzansicht: eine Zeile je Pack mit offenen oder entschiedenen Dateien."""
    ent, heute = korrekturen.packs(), korrekturen.heute()
    g = {}
    ohne_pack = einzeln = offen = 0
    for z in filtern({}):
        jetzt_offen = z["lizenz"] in LIZENZ_OFFEN
        offen += jetzt_offen
        if not z["pack"]:
            ohne_pack += jetzt_offen
            continue
        if not pack_entscheidbar(z):      # Einzelsounds: Lizenz je Datei, kein Pack
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
                    "herkunft": [h for h, _ in e["herkunft"].most_common()],
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
    """Zeile über einer Playlist: „12 ja · 5 offen · 3 nein“ über die ganze Playlist (ohne die übrigen Filter)."""
    c = collections.Counter(z["lizenz"] for z in filtern({"sammlung": [sammlung]}))
    return {"ja": c["ja"], "offen": sum(c[x] for x in LIZENZ_OFFEN), "nein": c["nein"]}


def suche_aufloesen(s):
    """Paket 6: {Parameter der Suche} statt einer Pfadliste (Cmd+A über alle Treffer) -> Pfade in Sortierung."""
    if not isinstance(s, dict):
        raise Fehleingabe("suche muss ein Objekt sein")
    p = {}
    for k, v in s.items():
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise Fehleingabe(f"suche.{k} muss Text oder Zahl sein")
        p[k] = [str(v)]
    fremd = unbekannte_parameter(p, SUCHE_PARAMETER - {"offset", "limit"})
    if fremd:
        raise Fehleingabe(fremd["fehler"])
    return [z["pfad"] for z in filtern(p)]


def auswahl_pfade(k, grenze=korrekturen.MAX_PFADE):
    """pfade (Liste) oder suche (Objekt) aus einem POST-Körper -> Pfade aus dem Index (nur bekannte)."""
    if k.get("suche") is not None:
        pfade = suche_aufloesen(k["suche"])
    else:
        roh = k.get("pfade")
        if not isinstance(roh, list) or not roh or not all(isinstance(x, str) for x in roh):
            raise Fehleingabe("pfade muss eine Liste von Texten sein (oder suche ein Objekt)")
        pfade = list(dict.fromkeys(nfc(x) for x in roh))
        if any(x not in INDEX.nach_pfad for x in pfade):
            raise Fehleingabe("Sample steht nicht in der Bibliothek")
    if len(pfade) > grenze:
        raise Fehleingabe(f"höchstens {grenze:,} Samples auf einmal".replace(",", "."))
    return pfade


# ================================================================ Suche

class Fehleingabe(ValueError):
    pass


# Erlaubte Parameter, einmal hier (Paket 0, 23.10.2026): ein Tippfehler wie "ton" statt "tonart" filterte vorher still
# nichts weg. Wer einen neuen Filter einbaut, trägt ihn hier ein; sonst antwortet der Server mit 400.
SUCHE_PARAMETER = frozenset([
    "q", "sammlung", "art", "herkunft", "lizenz", "stimmung", "tonart", "passend", "lo", "pack",
    "art_nicht", "herkunft_nicht", "lizenz_nicht", "stimmung_nicht",
    "swing", "gefuehl", "live", "klasse", "takte", "takte_min", "takte_max",
    "swing_nicht", "gefuehl_nicht", "live_nicht", "klasse_nicht", "takte_nicht",
    "bpm_min", "bpm_max", "halb", "dauer_min", "dauer_max", "gemessen", "doppelte", "sort", "ab", "offset", "limit",
    "bpm_toleranz", "ton_abstand", "drums_ohne_ton",      # Paket 1 (Projektrahmen)
    "oneshots_ohne_tempo",                                # Musiker-Test 03.11.: Rahmentempo filtert keine One-Shots
    "notiz",                                              # Paket 10: nur Samples mit Notiz
    "verworfene", "nie_gehoert",                          # Paket 4: verworfene mitzeigen, nur nie gehörte
    "aehnlich", "aehnlich_bpm",                           # Paket 5: Ähnliche zu einem Sample (Tempo vom Projekt)
    "genre", "genre_nicht",                               # linke Spalte 24.10.: sieben Genres, je ODER über Vibe-Wörter
])
ZUFALL_PARAMETER = frozenset(["typ", "bpm_min", "bpm_max", "halb", "lizenz", "ohne",
                              "bpm_toleranz", "tonart", "passend", "ton_abstand", "verworfene",
                              "nie_zuerst"])                # Paket 5: noch nie Gehörtes zuerst
# Paket 5: typ=suche zieht aus der Bibliothekssuche und nimmt dafür alle Parameter der Suche (ohne Blättern)
ZUFALL_SUCHE_PARAMETER = (SUCHE_PARAMETER - {"offset", "limit"}) | {"typ", "ohne", "nie_zuerst"}
VERLAUF_SAMMLUNGEN = ("gehoert", "gezogen")               # Paket 4: sammlung=gehoert, sammlung=gezogen
# Paket 1: Drum-Arten ohne Ton fallen beim Tonartfilter des Projektrahmens nicht heraus (eine Snare hat keine Tonart);
# in der Bibliothek nur mit drums_ohne_ton=1 (setzt der Rahmen), im Zufall immer. Mit Ton (gemessener Grundton) zählt er.
DRUM_ARTEN = {"kick", "snare", "clap", "hat", "cymbal", "perc", "tom", "rim", "brush",
              "break", "kickloop", "fill", "top"}
BPM_TOLERANZ_MAX, TON_ABSTAND_MAX = 15, 6


def tempo_bereich(p, eins):
    """bpm_min, bpm_max (vertauscht getauscht), um bpm_toleranz Prozent geweitet: 140 bis 140 mit 8 -> 129 bis 151."""
    bmin, bmax = zahl(eins("bpm_min")), zahl(eins("bpm_max"))
    if bmin is not None and bmax is not None and bmin > bmax:
        bmin, bmax = bmax, bmin
    t = zahl(eins("bpm_toleranz"))
    if eins("bpm_toleranz") != "" and (t is None or not 0 <= t <= BPM_TOLERANZ_MAX):
        raise Fehleingabe(f"bpm_toleranz: 0 bis {BPM_TOLERANZ_MAX} (Prozent)")
    if t:   # auf ganze BPM nach innen, dazu kommt das halbe BPM Spiel aus tempo_passend: 140 ±8 % zeigt 129 bis 151
        bmin = math.ceil(bmin * (1 - t / 100) - 1e-9) if bmin is not None else None
        bmax = math.floor(bmax * (1 + t / 100) + 1e-9) if bmax is not None else None
    return bmin, bmax


def tonart_mengen(werte, passend, abstand_text):
    """-> (Tonarten wie 'F#m', Grundtöne für 808, Bass und tonale One-Shots). passend: Camelot-Nachbarn dazu;
    ton_abstand n: dieselbe Tonart bis n Halbtöne höher oder tiefer, je mit ihrer Parallele (Ab ist zu Fm Abstand 0)."""
    try:
        abstand = int(abstand_text) if abstand_text != "" else 0
    except ValueError:
        abstand = -1
    if not 0 <= abstand <= TON_ABSTAND_MAX:
        raise Fehleingabe(f"ton_abstand: ganze Zahl 0 bis {TON_ABSTAND_MAX}")
    tonarten, grund = set(), set()
    for t in werte:
        n = tonart_norm(t)
        if not n:
            raise Fehleingabe(f"Tonart {t} unbekannt")
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
    """-> Antwort für 400 oder None. p: parse_qs-Ergebnis."""
    fremd = sorted(k for k in p if k not in erlaubt)
    if not fremd:
        return None
    return {"fehler": "unbekannter Parameter: " + ", ".join(fremd), "erlaubt": sorted(erlaubt)}


SUCH_CACHE = collections.OrderedDict()   # Abfrage ohne offset/limit -> sortierte Treffer (Blättern ohne Neusuche)
GRUPPEN_CACHE = collections.OrderedDict()   # gleicher Schlüssel -> {Bit: Zeilen, die nur an dieser Gruppe scheitern}
# Bits der Facettengruppen in filtern(); Reihenfolge wie FACETTEN_GRUPPEN
GRUPPEN_BIT = {n: 1 << i for i, n in enumerate(("art", "genre", "tonart", "takte", "swing", "gefuehl", "live",
                                                  "herkunft", "lizenz"))}

# Paket 5 (24.10.2026): Ähnliche zu einem Sample. Gleiche lo; Loops: gleiche Art oder verwandte Gruppe (wie die
# Zufall-Gruppen), Tonart gleich oder Camelot-Nachbar, BPM mit halb/doppelt in ±6 %, gemeinsame Instrumente und Stil;
# One-Shots: gleiche Klasse, Grundton ±1 Halbton, Länge ±30 %, LUFS ±4. Ein Kriterium schließt nur aus, wenn beide
# Werte bekannt sind; ein fehlender Wert gibt keine Punkte. Gleiches Pack bleibt immer drin und steht vorn.
AEHNLICH_GRUPPEN = ({"break", "kickloop", "fill"}, {"top", "perc", "hat", "cymbal"}, {"melodisch", "bass"})
AEHNLICH_BPM, AEHNLICH_DAUER, AEHNLICH_LUFS = 0.06, 0.30, 4.0


def grundton_midi(text):
    """'G1' -> 31, 'C#2' -> 37, sonst None (Schreibweise wie _grundton_uebernehmen)."""
    m = re.match(r"^([A-G][#b]?)(-?\d+)$", text or "")
    if not m or m.group(1) not in TONARTEN:
        return None
    return TONARTEN.index(m.group(1)) + (int(m.group(2)) + 1) * 12


def aehnlich_vergleicher(p):
    """-> (Bezugszeile, Info für die Oberfläche, Funktion zeile -> None oder (gleiches Pack, Punkte, Stufe)) oder None."""
    def eins(k):
        return (p.get(k) or [""])[0]
    pfad = nfc(eins("aehnlich"))
    if not pfad:
        return None
    r = INDEX.nach_pfad.get(pfad)
    if r is None:
        raise Fehleingabe("aehnlich: Sample nicht in der Bibliothek")
    projekt = eins("aehnlich_bpm") != ""
    if projekt:      # Projektrahmen an: sein Tempo statt dem des Samples
        tempo = zahl(eins("aehnlich_bpm"))
        if tempo is None or not 20 <= tempo <= 400:
            raise Fehleingabe("aehnlich_bpm: 20 bis 400")
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
            if grundton_zeile(r) or grundton_zeile(z):      # Bass, 808: nur der Ton
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
        geschaetzt = "leicht" in (r["ton_quelle"], z["ton_quelle"])     # nur Pluspunkte, nie Ausschluss (Prüfung 03.11.)
        if r["ton"] and z["ton"] and z["ton"] in TONARTEN and r["ton"] in TONARTEN:
            m = grundton_midi(z["grundton"])
            if midi is not None and m is not None:      # beide mit Oktave: ±1 Halbton echt (G0 ist zu G1 nicht nah)
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
    # Musiker-Test 03.11.2026: mit Klang-Fingerabdruck entscheidet der Klang über die Reihenfolge. Pflicht bleibt, was
    # musikalisch nicht passt (Prüfung 03.11.): Gruppe oder Klasse, Songs nur zu Songs, das Projekttempo, die Tonart
    # bei melodisch und Bass. Groove, Tempo und Tonart geben kleine Zuschläge; Kandidaten ohne Fingerabdruck stehen hinten.
    klang_fehler = False
    try:
        klang_nah = KLANG.nachbarn(klang_schluessel(r)) if KLANG is not None else None
    except klangnaehe.DienstFehler:
        klang_nah, klang_fehler = None, True
    # Nutzertest 2: ohne gehoer der grobe Fingerabdruck. Mit gehoer nicht (Prüfung 03.11.: er deckt dort nur die
    # ungemessene Hälfte ab, die gemessene stünde geschlossen in der zweiten Stufe)
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
            return None          # derselbe Klang (Kopie mit anderem Namen): kein „ähnlich“ (Nutzertest 2)
        if w is None:
            # ohne Fingerabdruck (oder nicht unter den nächsten 4.000): der alte Vergleich samt seiner Pflicht, als
            # zweite Stufe hinter allen Klang-Treffern (Prüfung 03.11.: sonst stand der Rest nur nach Name da)
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
        if not e[1]:        # auch das gleiche Pack muss die Kriterien halten, es steht nur zuerst
            return None
        gleiches_pack = bool(r["pack"]) and z["pack"] == r["pack"]
        # One-Shots: hat das Bezugssample einen Ton, kommen Kandidaten ohne Ton hinter die mit passendem Ton
        stufe = not (einzeln and r["ton"] and not z["ton"])
        return gleiches_pack, round(e[0], 2), stufe
    # fast alle Werte fehlen: Loops ohne Tempo, Tonart, Instrumente und Stil; One-Shots ohne Ton und LUFS
    wenig = (r["ton"] is None and r["lufs"] is None) if einzeln else \
        (not tempo and r["ton"] is None and not instr and not stil)
    info = {"pfad": r["pfad"], "name": r["name"], "pack": r["pack"], "lo": r["lo"], "wenig": wenig,
            "laenge": einzeln and bool(r["dauer"]),
            "tempo": round(tempo, 2) if tempo and not einzeln else None,
            "tempo_projekt": projekt and not einzeln,      # One-Shots haben kein Tempo
            "klang": klang_nah is not None, "klang_moeglich": KLANG is not None and KLANG.verfuegbar(),
            "klang_fehler": klang_fehler}
    return r, info, punkte


# Musiker-Test 03.11.2026: „808“ lieferte 4.951 Treffer nach Name, oben ganze Songs, weil gehoers Instrument-Begriffe
# („sub bass or 808“) so viel zählten wie der Dateiname. Je Suchwort zählt das beste Feld; trifft es Name und Art, kommt
# ein Zuschlag dazu (die 808 vor dem „808 Cymbal“). Ganze Songs rutschen nach hinten, außer die Suche will sie.
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
            best = RELEVANZ_FELD[0][1]      # „drill“: das Pack „VVS: Flawless Drill“ zählt wie ein Name
        elif best == RELEVANZ_FELD[0][1]:
            if " " + w + " " in z["_wname"]:
                best += RELEVANZ_GANZ
            # nur wenn gehoer die Klasse gemessen hat: sonst stammt die Art meist aus demselben Namen (doppelt gezählt)
            if z["klasse_quelle"] == "gemessen" and z["klasse"] and drin(" " + z["klasse"] + " "):
                best += RELEVANZ_NAME_UND_ART
            # „808 CH.wav“ ist eine Hi-Hat der 808: hat gehoer eine andere Klasse gemessen, rutscht sie nach hinten
            elif (w in EINZELSCHLAG and z["klasse_quelle"] == "gemessen" and z["klasse"]
                  and z["klasse"] != w and w not in KLASSE_VERWANDT.get(z["klasse"], ())):
                best -= RELEVANZ_NAME_UND_ART
            # ebenso, wenn die Art aus dem Namen ein anderer Schlag ist („Sn 808.wav“ ist eine Snare der TR-808)
            elif (w in EINZELSCHLAG and z["art"] in EINZELSCHLAG and z["art"] != w
                  and w not in KLASSE_VERWANDT.get(z["art"], ())):
                best -= RELEVANZ_NAME_UND_ART
        punkte += best
    return punkte


def song_hinten(z, mit):
    """Ganze Songs als eigene Stufe hinter allem, außer die Suche will Songs (Prüfung 03.11.: „drill“)."""
    return z["art"] == "song" and not SONG_WOERTER.intersection(mit)


def nach_pack_streuen(zeilen, stufe):
    """Innerhalb gleicher Stufe abwechselnd je ein Treffer aus jedem Pack, statt einer Wand aus einem Pack (Prüfung
    03.11.: zu „808“ standen 21 der ersten 50 aus einem einzigen Drumkit). Packs in der Reihenfolge ihres ersten
    Auftretens, innerhalb des Packs bleibt die Reihenfolge."""
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
    """One-Shot, oder kurz, ohne Tempo und nicht als Loop geführt (Prüfung 03.11.: rund 5.000 Snares, Hats, Toms und
    Kicks haben lo leer, etwa die Serum-Drums; sie fielen beim Rahmen sonst weiter heraus)."""
    if z["lo"] == "oneshot":
        return True
    return z["lo"] == "" and z["bpm"] is None and z["art"] != "song" and z["dauer"] is not None and z["dauer"] < 6


def klang_schluessel(z):
    return klangnaehe.schluessel(z["md5"], z["pfad"])


def tempo_passend(b, gefuehl, bmin, bmax, halb):
    """Das Tempo, unter dem eine Zeile in den Bereich bmin bis bmax passt, sonst None. Das eigene Tempo mit 0,5 BPM
    Spiel (gerundete Angaben); mit halb auch das doppelte, halbe und gefühlte (Halftime, punktierte Viertel bei 6/8),
    die aber nur genau im Bereich (Test 20.10.: Breaks 88 bis 94 fanden DnB mit 175 als 87,5)."""
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
    """p: Abfrage-Parameter (dict von Listen). Gibt die passenden Zeilen in Sortierung zurück.
    gruppen=True (linke Spalte, Prüfung 24.10.): -> (Treffer, {Bit: Zeilen}); unter dem Bit einer Facettengruppe
    (GRUPPEN_BIT) stehen die Zeilen, die nur an dieser einen Gruppe scheitern. Treffer plus diese Zeilen sind genau die
    Treffer ohne die Auswahl der Gruppe, alles in einem Durchgang (vorher je gesetzter Gruppe eine eigene Suche)."""
    # Paket 10: Notizen ändern sich ohne neuen Index, ihr Stand gehört in den Schlüssel
    # Paket 4: Verworfen ebenso, und der Verlauf, wenn „noch nie gehört“ filtert
    nie_gehoert = (p.get("nie_gehoert") or [""])[0] == "1"
    schluessel = (INDEX.version, sammlungen.notiz_stand(), sammlungen.verworfen_stand(),
                  verlauf.stand() if nie_gehoert else 0,
                  KLANG.stempel() if KLANG is not None and p.get("aehnlich") else 0,      # Musiker-Test 03.11.
                  LEICHT.stempel() if LEICHT is not None and p.get("aehnlich") else 0,    # Nutzertest 2
                  tuple(sorted((k, tuple(v)) for k, v in p.items() if k not in ("offset", "limit"))))
    # Favoriten und Playlists ändern sich ohne neuen Index: dann nie aus dem Zwischenspeicher
    sammlung = (p.get("sammlung") or [""])[0]
    # mit gruppen nur, wenn beide Teile aus demselben Durchgang stammen (SUCH_CACHE wird an drei Stellen geleert)
    if gruppen and not sammlung and schluessel in GRUPPEN_CACHE and GRUPPEN_CACHE[schluessel][0] is SUCH_CACHE.get(schluessel):
        GRUPPEN_CACHE.move_to_end(schluessel)
        SUCH_CACHE.move_to_end(schluessel)
        return GRUPPEN_CACHE[schluessel]
    if not gruppen and schluessel in SUCH_CACHE and not sammlung:
        SUCH_CACHE.move_to_end(schluessel)
        return SUCH_CACHE[schluessel]
    # Paket 4: Gehört und Gezogen kommen aus verlauf.json, mit Zeit je Zeile, neueste zuerst
    verlauf_eintraege, verlauf_rang = verlauf.eintraege(sammlung) if sammlung in VERLAUF_SAMMLUNGEN else (None, None)
    in_sammlung = set(verlauf_eintraege) if verlauf_eintraege is not None else \
        sammlungen.pfade_fuer(sammlung) if sammlung else None
    # Paket 4: verworfene fehlen überall, außer in Verworfen selbst, im Verlauf (Gehört, Gezogen: dort mit Zeichen,
    # damit Zahl und Liste übereinstimmen und nichts Gezogenes aus dem Blick fällt) und mit verworfene=1
    weg = set() if sammlung == "verworfen" or verlauf_eintraege is not None or (p.get("verworfene") or [""])[0] == "1" \
        else sammlungen.verworfen()
    schon_gehoert = verlauf.gehoert_menge() if nie_gehoert else ()

    def eins(k, std=""):
        return (p.get(k) or [std])[0]

    def menge(k):
        return {x.strip() for x in eins(k).split(",") if x.strip()}

    roh = nfc(eins("q")).split()
    # Synonyme auf den ganzen Text, nicht je Wort: "boom bap" und "lo fi" sind ein Begriff (vorher 0 bzw. 232 Treffer)
    mit = woerter(" ".join(t for t in roh if not t.startswith("-")))
    ohne_w = [w for t in roh if t.startswith("-") and len(t) > 1 for w in woerter(t[1:])]   # "-vocal" schließt aus

    def trifft(w, wt):   # Wörter am Wortanfang, Zahlen nur ganz ("80" soll nicht "808" finden)
        return (" " + w + " " in wt) if w.isdigit() else (" " + w in wt)
    arten, herkunft, lizenzen = menge("art"), menge("herkunft"), menge("lizenz")
    stimmungen = {w for s in menge("stimmung") for w in stimmung_woerter(s)}
    ohne = {f: menge(f + "_nicht") for f in ("art", "herkunft", "lizenz", "swing", "gefuehl", "live", "klasse")}
    ohne["stimmung"] = {w for s in menge("stimmung_nicht") for w in stimmung_woerter(s)}
    # linke Spalte 24.10.: Genre als eigene Gruppe (ODER innen, UND mit Vibe), ohne Genre wie ein Minus bei Vibe
    genres = {w for g in menge("genre") for w in GENRE_GRUPPEN.get(g, (g,))}
    genre_ohne = {w for g in menge("genre_nicht") for w in GENRE_GRUPPEN.get(g, (g,))}
    ohne["swing"] = {w for s in ohne["swing"] for w in SWING_GRUPPEN.get(s, (s,))}
    # "ohne 808" bei Einzelschlag nimmt auch alles mit 808 im Namen, Pack oder Ordner raus, wie "-808" in der Suche
    # (Test 20.10.: "Kick ohne 808" ließ eine „808 Kick“ aus dem Ordner 808s stehen)
    ohne_808 = "808" in ohne["klasse"]
    # gehoer-Werte: nur bekannte Werte passen, ausschließen trifft nur bekannte (Unbekanntes bleibt drin)
    swings, gefuehle, live, klassen = menge("swing"), menge("gefuehl"), menge("live"), menge("klasse")
    swings = {w for s in swings for w in SWING_GRUPPEN.get(s, (s,))}

    def takte_menge(k):
        try:
            return {int(x) for x in menge(k)}
        except ValueError:
            raise Fehleingabe(f"{k}: nur ganze Zahlen")
    takte, takte_ohne = takte_menge("takte"), takte_menge("takte_nicht")
    tmin, tmax = zahl(eins("takte_min")), zahl(eins("takte_max"))
    if tmin is not None and tmax is not None and tmin > tmax:
        tmin, tmax = tmax, tmin

    def takte_ist(t, n):
        return t is not None and abs(t - n) <= 0.1
    # der gehoer-Teil der Schleife läuft nur, wenn einer dieser Filter gesetzt ist (spart rund 20 % je Suche)
    gehoer_filter = bool(swings or gefuehle or live or klassen or takte or takte_ohne or tmin is not None
                         or tmax is not None or any(ohne[f] for f in ("swing", "gefuehl", "live", "klasse")))
    tonarten, grundtoene = tonart_mengen(menge("tonart"), eins("passend") == "1", eins("ton_abstand"))
    drums_frei = eins("drums_ohne_ton") == "1"
    lo, pack = eins("lo"), eins("pack")
    bmin, bmax = tempo_bereich(p, eins)     # mit bpm_toleranz geweitet (Paket 1)
    dmax, dmin = zahl(eins("dauer_max")), zahl(eins("dauer_min"))
    if dmin is not None and dmax is not None and dmin > dmax:
        dmin, dmax = dmax, dmin
    nur_gemessen, halb, alle_kopien = eins("gemessen") == "1", eins("halb") == "1", eins("doppelte") == "1"

    tempo = bmin is not None or bmax is not None
    # Musiker-Test 03.11.2026: beim Projektrahmen „140 · F#m“ blieben 13 von 63.354 One-Shots, 808 und Kick hatten 0.
    # Ein Einzelschlag hat kein Tempo; mit oneshots_ohne_tempo (setzt die Seite, solange der Rahmen ein Tempo stellt)
    # filtert das Tempo nur Loops und Songs
    oneshot_frei = eins("oneshots_ohne_tempo") == "1"
    # Paket 10: Notizen sind durchsuchbar wie Name und Tags (Wortanfänge), notiz=1 lässt nur Samples mit Notiz
    nur_notiz = eins("notiz") == "1"
    notiz_text = sammlungen.notizen() if (mit or ohne_w or nur_notiz) else {}
    notiz_woerter = {k: " " + " ".join(woerter(t)) + " " for k, t in notiz_text.items()} if (mit or ohne_w) else {}
    aehnlich = aehnlich_vergleicher(p)       # Paket 5
    aus = []
    extra = collections.defaultdict(list)    # nur mit gruppen: Bit -> Zeilen, die nur an dieser Gruppe scheitern
    G_ART, G_GENRE, G_TON, G_TAKTE, G_SWING = (GRUPPEN_BIT[n] for n in ("art", "genre", "tonart", "takte", "swing"))
    G_GEFUEHL, G_LIVE, G_HERKUNFT, G_LIZENZ = (GRUPPEN_BIT[n] for n in ("gefuehl", "live", "herkunft", "lizenz"))
    frueh_ohne = bool(ohne["art"] or ohne["herkunft"] or ohne["lizenz"] or genre_ohne)
    for z in INDEX.zeilen:
        if in_sammlung is not None:
            if z["pfad"] not in in_sammlung:   # in Favoriten und Playlists zählen auch Kopien
                continue
        elif z["ist_kopie"] and not alle_kopien:
            continue
        if nur_notiz and z["pfad"] not in notiz_text:
            continue
        if z["pfad"] in weg or z["pfad"] in schon_gehoert:      # Paket 4
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
        # Gruppen der Spalte: ohne gruppen scheitert die Zeile sofort, mit gruppen zählt sie, solange nur eine scheitert
        fehl = 0
        if frueh_ohne:
            fehl = ((G_ART if z["art"] in ohne["art"] else 0) | (G_HERKUNFT if z["herkunft"] in ohne["herkunft"] else 0)
                    | (G_LIZENZ if z["lizenz"] in ohne["lizenz"] else 0)
                    | (G_GENRE if genre_ohne and genre_ohne.intersection(z["stimmung"]) else 0))
            if fehl and (not gruppen or fehl & (fehl - 1)):
                continue
        # Tempo: das passende (bei halb/doppelt das umgerechnete oder das gefühlte, etwa die punktierte Viertel bei 6/8)
        k = tempo_passend(z["bpm"], z["bpm_gefuehl"], bmin, bmax, halb) if tempo else None
        if tempo and k is None and not (oneshot_frei and einzelschlag_ohne_tempo(z)):
            continue
        umgerechnet = k is not None and abs(k - z["bpm"]) > 0.01
        # Takte beim passenden Tempo: ein 4-Takter in 70 füllt bei 140 acht Takte (Test 20.10.: "140 aus 70", "4 Takte")
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
            # Hat- und Perc-Loops stehen im Bestand meist als "top" (Test 19.10.: 1.199 Perc-Loops unter top)
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
        if umgerechnet:   # umgerechnetes Tempo und Takte dabei mitgeben (Anzeige "130 aus 65")
            z = dict(z, bpm_passend=round(k, 2), bpm_passend_gefuehl=k == z["bpm_gefuehl"],
                     **({"takte_passend": round(tk, 2)} if tk is not None else {}))
        if verlauf_eintraege is not None:     # Paket 4: Zeit (und bei Gezogen die gerechnete Datei) in die Zeile
            e = verlauf_eintraege[z["pfad"]]
            z = dict(z, verlauf_zeit=e["zeit"], **({"verlauf_datei": e["datei"]} if e.get("datei") else {}))
        if fehl:
            extra[fehl].append(z)
        else:
            aus.append(z)

    sort, ab = eins("sort", "relevanz"), eins("ab") == "1"
    if sort == "relevanz":
        # mit Suchtext: beste Treffer zuerst, bei Gleichstand der kürzere Name; ohne Suchtext: keine Songs vorn, dann die
        # neueste Datei (frisch Geladenes und neue Ordner oben). Mehrere stabile Durchgänge statt eines Schlüssels.
        aus.sort(key=lambda z: z["name"].casefold())
        if mit:
            aus.sort(key=lambda z: len(z["name"]))
            # Rang neben der Zeile, nicht in ihr: die Zeilen gehören dem Index, gleichzeitige Suchen teilen sie
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
        # Paket 6: Lizenz von verkaufbar bis nein, dann der Name; Datum der Datei (die Seite schickt neueste zuerst)
        "lizenz": lambda z: (LIZENZ_RANG.get(z["lizenz"], 3), z["name"].casefold()),
        "datum": lambda z: (z["datum"], z["name"].casefold()),
        # Paket 5: Ähnliche nach Nähe, gleiches Pack zuerst
        "naehe": lambda z: (not z.get("aehnlich_pack"), not z.get("aehnlich_stufe", True), -z.get("aehnlich_punkte", 0),
                            z["name"].casefold()),
    }.get(sort)
    if sammlung and sort == "liste":   # Reihenfolge wie in der Playlist (beim Hinzufügen); Verlauf: neueste zuerst
        rang = verlauf_rang if verlauf_rang is not None else sammlungen.reihenfolge(sammlung)
        aus.sort(key=lambda z: rang.get(z["pfad"], 1e9), reverse=ab)
        return (aus, dict(extra)) if gruppen else aus
    if sortier:
        aus.sort(key=sortier, reverse=ab)
        if sort in leer:   # leere Werte immer ans Ende
            aus.sort(key=leer[sort])
    if sammlung or (aehnlich and aehnlich[1].get("klang_fehler")):     # Dienstfehler: nächstes Mal neu fragen
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


# Zufalls-Loop: Loops im Bestand am 20.10.2026: break 3.810, melodisch 2.550, top 2.435, bass 758, kickloop 300, fill 74
ZUFALL_TYPEN = {"drums": {"break", "kickloop", "fill"}, "perc": {"top", "perc", "hat", "cymbal"},
                "melodie": {"melodisch", "bass"}, "alle": None}


def zufall(p):
    """Ein zufälliger Loop nach Typ und Tempo. ohne=Nummern der zuletzt gezeigten, damit nichts gleich wiederkommt."""
    import random

    def eins(k, std=""):
        return (p.get(k) or [std])[0]
    if eins("typ") == "suche":
        return _zufall_aus_suche(p, eins)
    arten = ZUFALL_TYPEN.get(eins("typ", "alle"), None)
    bmin, bmax = tempo_bereich(p, eins)     # mit bpm_toleranz geweitet (Paket 1)
    halb = eins("halb") == "1"
    # Tonart (Paket 1, Projektrahmen): melodische Loops vergleichen, Drums ohne Ton bleiben drin
    tonarten, grundtoene = tonart_mengen({x.strip() for x in eins("tonart").split(",") if x.strip()},
                                         eins("passend") == "1", eins("ton_abstand"))
    tempo = bmin is not None or bmax is not None

    def tempo_ok(z):   # dieselbe Regel wie in der Bibliothek (tempo_passend)
        return not tempo or tempo_passend(z["bpm"], z["bpm_gefuehl"], bmin, bmax, halb) is not None
    lizenzen = {x for x in eins("lizenz").split(",") if x}
    weg = set() if eins("verworfene") == "1" else sammlungen.verworfen()      # Paket 4
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
    if k is not None and abs(k - z["bpm"]) > 0.01:     # auf der Karte "140 aus 70" wie in der Liste
        zeile.update(bpm_passend=round(k, 2), bpm_passend_gefuehl=k == z["bpm_gefuehl"])
    return {"gesamt": len(kand), "zeile": zeile, **nie}


def _zufall_waehlen(kand, eins, pfad_von):
    """Eine Nummer aus kand: nicht unter den zuletzt gezeigten (ohne); mit nie_zuerst zuerst noch nie gehörte
    (Paket 5). -> (Nummer, {"nie_gehoert": Anzahl der nie gehörten unter kand} oder {})."""
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
    """Stelle einer Zeile in INDEX.zeilen (die Nummer, die der Zufall als nr schickt), neu je Index-Stand."""
    if _POS["version"] != INDEX.version:
        _POS["nr"] = {z["pfad"]: i for i, z in enumerate(INDEX.zeilen)}
        _POS["version"] = INDEX.version
    return _POS["nr"].get(pfad)


def _zufall_aus_suche(p, eins):
    """Paket 5: ein Loop aus den Treffern der Bibliothekssuche (dieselben Parameter wie die Liste, auch Sammlung
    oder Playlist). Der Zufall spielt nur Loops; treffer zählt alle Treffer, gesamt die Loops darunter."""
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
    """Zahl je Tonart-Chip = Treffer der Suche nach genau dieser Tonart (Prüfung 24.10.: Fm zeigte 271, die Suche
    fand 1.489). Wie tonart_passt(): Zeilen, die nur den Grundton tragen (One-Shots ohne Modus, Bass und Co.), zählen
    bei Dur und Moll ihres Grundtons; mit drums_ohne_ton zählen Drums ohne Tonart bei jeder Tonart mit."""
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
    return [[k, genre.get(k, 0)] for k in GENRE_GRUPPEN]      # linke Spalte 24.10., feste Reihenfolge


def gehoer_zahlen(zeilen, felder):
    """gehoer-Werte in einem Durchgang; feste Reihenfolge, auch mit 0 (die Chips stehen immer gleich da, 0 zeigt die
    fehlende Messung)."""
    g = {f: collections.Counter() for f in ("takte", "swing", "gefuehl", "live", "klasse")}
    mit_takte = "takte" in felder
    andere = [f for f in ("swing", "gefuehl", "live", "klasse") if f in felder]
    for z in zeilen:
        if mit_takte:
            t = z.get("takte_passend", z["takte"])     # beim passenden Tempo gezählt, wie in der Liste
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
    """nur: Menge der Facetten, die gebraucht werden (die Zählung ohne eigene Auswahl braucht je Gruppe nur ihre)."""
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
        # der Vibe live steckt im Filter Live gespielt (sonst zweimal live mit verschiedenen Zahlen, Test 20.10.)
        "stimmung": [x for x in zaehl("stimmung", True) if x[0] != "live"][:400], "lo": zaehl("lo"),
        "tonart": tonart_zahlen(zeilen, drums_frei),
        **gehoer_zahlen(zeilen, ("takte", "swing", "gefuehl", "live", "klasse")),
        "mit_bpm": sum(1 for z in zeilen if z["bpm"] is not None),
        "mit_tonart": sum(1 for z in zeilen if z["ton"]),
        "lizenz_erklaerung": LIZENZ_ERKLAERUNG,
    }


# Linke Spalte (24.10.2026): jede Gruppe zählt ohne ihre eigene Auswahl. Ist Hat gewählt, zeigt Snare, wie viele Snares
# es mit den übrigen Filtern gäbe (vorher 0, und bei fester Reihenfolge wurde nach jedem Klick die ganze Gruppe blass).
# Je Gruppe: (Parameter, die wegfallen; Facetten, die daraus kommen). Art und Klasse sind eine Gruppe, weil die Spalte im
# Modus One-Shots Kick bis Rim über die Klasse und Bass, Vocal, FX über die Art filtert.
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
    """Bits der Facettengruppen, für die p eine eigene Auswahl hat."""
    gesetzt = {k for k, v in p.items() if any(x.strip() for x in v)}
    return [(GRUPPEN_BIT[n], felder) for n, (parameter, felder) in zip(GRUPPEN_NAMEN, FACETTEN_GRUPPEN)
            if gesetzt.intersection(parameter)]


def facetten_ohne_eigene(p, f, treffer=None, extra=None):
    """f: Facetten der Treffer. Für jede Gruppe mit eigener Auswahl werden ihre Zahlen ohne diese Auswahl neu gezählt
    (alle übrigen Filter gelten). treffer, extra: aus filtern(p, gruppen=True); die Zeilen ohne die Auswahl einer
    Gruppe sind Treffer plus extra[Bit], es braucht keine weitere Suche (Prüfung 24.10.: vorher +43 ms)."""
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


LIZENZ_RANG = {"ja": 0, "unklar": 1, "ungeprüft": 2, "unbekannt": 3, "nein": 4}    # Sortierung Lizenz (Paket 6)


def oeffentlich(z):
    # "_" davor: nur für den Server (Suchwörter ohne Art, Stand vor einer Korrektur)
    return {k: v for k, v in z.items() if k not in ("woerter", "gruppe") and not k.startswith("_")}


# ================================================================ Messen (gehoer)

IN_ARBEIT = "Analyse nicht abgeschlossen"     # Anfang von gehoers Vermerk, solange eine Datei rechnet
_SCAN_STAND = {"zeit": 0.0, "wert": None}


def _lebt(pid):
    return plattform.lebt(pid)


def _vorfahren(pid):
    """Elternprozesse einer PID (ps). vollscan.sh schreibt die PID seiner bash-Hülle in die PID-Datei, in laeufe steht
    die des Python-Scans darunter (Prüfung 20.10.: "vollscan" war deshalb nie wahr)."""
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
    """Läuft auf der Hauptdatenbank gerade ein gehoer-Scan, den nicht die Open Libary gestartet hat?
    -> None oder {"seit", "dateien", "fertig", "auftraege", "rest_s", "vollscan"}; vollscan: vom Vollscan-Skript
    gestartet (sonst ein anderer gehoer-Scan). Hält die Antwort 20 s. Fehler jeder Art heißen None: die Antwort hängt
    an jeder ersten Suchseite, sie darf keine Suche scheitern lassen."""
    if time.time() - _SCAN_STAND["zeit"] < 20:
        return _SCAN_STAND["wert"]
    try:
        wert = _fremder_scan()
    except Exception as e:
        print(f"Scan-Stand nicht lesbar: {type(e).__name__}: {e}", flush=True)
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
                    tempo = (juengst or 0) / min(900.0, max(60.0, jetzt - (start or jetzt)))     # Aufträge je Sekunde
                    wert = {"seit": start, "dateien": gesamt, "fertig": fertig or 0, "auftraege": (fertig or 0) + (offen or 0),
                            "rest_s": round((offen or 0) / tempo) if tempo > 0 and offen else None,
                            "vollscan": bool(vollscan_pid) and (vollscan_pid == pid or vollscan_pid in _vorfahren(pid))}
            finally:
                con.close()
        except sqlite3.Error:
            pass        # gesperrt oder ohne Tabelle laeufe: dann zählt nur die PID-Datei
    if wert is None and vollscan_pid:
        wert = {"seit": None, "dateien": None, "fertig": None, "auftraege": None, "rest_s": None, "vollscan": True}
    return wert


_VOLLSCAN_FERTIG = {"zeit": 0.0, "wert": None}


def vollscan_fertig():
    """Ist ein Vollscan über den Bestand durchgelaufen (Lauf "fertig" mit mindestens halb so vielen Dateien wie der
    Index)? -> Zeitpunkt des Endes oder None. Danach heißt ein leerer Wert "nicht messbar", nicht "noch nicht
    gemessen" (Prüfung 20.10.). Hält die Antwort 60 s."""
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
        print(f"Vollscan-Stand nicht lesbar: {type(e).__name__}: {e}", flush=True)
    _VOLLSCAN_FERTIG.update(zeit=time.time(), wert=wert)
    return wert


def _fehler_satz(text):
    """gehoers Fehlertext in einem Satz für die Ladeliste."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if t.startswith("Zeitüberschreitung"):
        return "Messung abgebrochen: die Analyse hing zu lange."
    if t.startswith("Analyse zweimal abgebrochen"):
        return "Messung zweimal abgestürzt, die Datei ist vermutlich beschädigt."
    if t.startswith(IN_ARBEIT):
        return "Messung nicht abgeschlossen (gehoer wurde beendet)."
    t = re.sub(r"^[A-Za-z]+(Error|Exception|Fehler): ", "", t)        # Klassenname vorn (LadeFehler: ...)
    if re.match(r"(ffprobe kann die Datei nicht lesen|ffmpeg kann die Datei nicht dekodieren|keine Tonspur)", t):
        return "Messung gescheitert: die Datei lässt sich nicht lesen (beschädigt oder kein Audio)."
    t = re.sub(r":?\s*/[^:]*/[^/:]*$", "", t)                          # Pfad am Ende weglassen
    return "Messung gescheitert: " + (t[:140].rstrip(" .:") or "ohne Angabe") + "."


LANG_TEXT = ("Über 5 Minuten lang: gehoer misst die Datei, sobald der laufende Scan (meist der Vollscan) durch ist; "
             "beide zusammen brauchten zu viel Speicher.")


class Messdienst:
    """Ein Hintergrund-Faden misst nacheinander mit gehoer (nice -n 10, OPENLIBARY_MESS_PRIO): jede Datei, die
    netz.py fertig geladen und einsortiert hat, und jede, für die jemand "Messen" drückt.
    Ziel ist immer NETZ_DB (eigene Datei, keine Konkurrenz mit dem Vollscan), seit 20.10.2026 abends auch für "Messen"
    auf eine Datei des Bestands. Vorher ging ein Klick in die Hauptdatenbank, solange dort kein anderer Scan lief. Die
    Daten blieben heil (Test mit zwei Scans auf einer Test-Datenbank), aber gehoer bibliothek stand und vollscan.sh
    --stand zeigen den jüngsten Lauf, also einen Ein-Datei-Lauf, und gehoer löscht beim nächsten Lauf die Aufträge
    fertiger Läufe: ein Klick nach dem Vollscan hätte dessen Bilanz verdrängt. Dazu landeten Klicks auf geprüfte
    Netz-Packs über den Pfad ohnehin in NETZ_DB, und ein Vollscan, der zwischen Klick und Start begann, bekam doch den
    Ein-Datei-Lauf (Prüfung 20.10.). Den Bestand misst der Vollscan; ein Klick misst nur neu nach.
    Ein gehoer-Aufruf misst alle wartenden Dateien (die Modelle laden je Prozess 30 bis 90 s), höchstens STAPEL
    zugleich, Klicks auf "Messen" vor den Downloads. Dateien ab 5 min warten, solange ein anderer Scan läuft: gehoer
    braucht dafür allein 7,8 bis 22 GB, der Vollscan mit 3 Arbeitern 13 bis 17,6 GB. Den Stand je Datei liest der Faden
    alle 2 s aus der Datenbank: wartet, misst, gemessen oder der Fehler in einem Satz."""
    STAPEL = 25
    BEHALTEN_S = 3 * 3600          # fertige Einträge so lange zeigen

    def __init__(self):
        self.lock = threading.Lock()
        self.warteschlange = collections.OrderedDict()   # pfad -> {"db": Path, "neu": bool}
        self.stand = {}                                  # pfad -> {"status", "text", "ziel", "zeit", "v"}
        self.wecker = threading.Event()
        self._pids = set()
        self._dauern = {}                                # pfad -> Sekunden (ffprobe, für Dateien außerhalb des Index)
        threading.Thread(target=self._lauf, daemon=True, name="messen").start()

    def eigene_pids(self):
        with self.lock:
            return set(self._pids)

    def dauer(self, p):
        """Länge in Sekunden: aus dem Index, sonst ffprobe (frisch geladene Dateien stehen dort erst nach dem nächsten
        Neuladen); None, wenn unbekannt."""
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
        """Wartet die Datei, bis der fremde Scan durch ist (ab 5 min, siehe oben)?"""
        return bool(scan) and (self.dauer(p) or 0) >= GEHOER_ALLEIN_S

    def dazu(self, pfade, klick=False):
        """Dateien einreihen. klick: vom Knopf "Messen" (neu messen, vorne einreihen). -> Hinweis für die Oberfläche
        ("" oder warum die Datei noch wartet)."""
        hinweis = ""
        scan = fremder_scan() if klick else None       # außerhalb der Sperre: fremder_scan fragt eigene_pids
        with self.lock:
            for p in pfade:
                p = nfc(str(p))
                if self.stand.get(p, {}).get("status") == "misst":
                    continue
                if p in self.warteschlange and not klick:
                    continue
                self.warteschlange[p] = {"db": NETZ_DB, "neu": klick}
                if klick:     # ein Klick misst neu und kommt vor die Downloads
                    self.warteschlange.move_to_end(p, last=False)
                self.stand[p] = {"status": "wartet", "text": "", "ziel": "netz", "zeit": time.time()}
        if klick and any(self.lang_zurueck(nfc(str(p)), scan) for p in pfade):
            hinweis = LANG_TEXT
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
        """v: die Oberfläche frischt die Zeile auf, sobald index_version größer ist als v. Das ist der zuletzt
        begonnene Ladelauf (INDEX.gestartet): ein Lauf, der schon vor dem Ende der Messung las, hat die neuen Werte noch
        nicht (Prüfung 20.10.: sonst kam die Zeile mit altem Stand und wurde danach vergessen). im_index: Werte sind
        schon drin."""
        with self.lock:
            if p in self.stand:
                v = (INDEX.version - 1 if im_index else INDEX.gestartet) if INDEX else 0
                self.stand[p].update(status=status, text=text, zeit=time.time(), v=v)

    def _stapel(self):
        """Die nächsten Dateien mit gleicher Art (neu oder nicht), höchstens STAPEL; lange Dateien bleiben liegen,
        solange ein anderer Scan läuft (dann mit Hinweis)."""
        scan = fremder_scan()        # außerhalb der Sperre: fremder_scan fragt eigene_pids
        with self.lock:
            offen = list(self.warteschlange.items())
        zurueck = {p for p, a in offen if self.lang_zurueck(p, scan)}     # ffprobe außerhalb der Sperre
        with self.lock:
            for p in zurueck:
                if p in self.stand and self.stand[p]["status"] == "wartet":
                    self.stand[p]["text"] = LANG_TEXT
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
            # Downloads kommen kurz nacheinander (Test 20.10.: drei Sounds in 6 s, die erste Datei startete gehoer
            # allein, die übrigen luden die Modelle ein zweites Mal): sammeln, bis 4 s nichts kommt, höchstens 30 s;
            # ein Klick auf "Messen" wartet nicht
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
                except Exception as e:     # der Faden darf nie sterben
                    for p in s[2]:
                        self._setzen(p, "fehler", f"Messung gescheitert: {type(e).__name__}: {e}"[:200])
                if gerechnet:
                    try:
                        INDEX.laden()      # neue Messwerte sofort in der Liste, nicht erst nach einer Minute
                    except Exception as e:
                        print(f"Index nach dem Messen nicht neu geladen: {e}", flush=True)

    @staticmethod
    def _zeilen(db, pfade):
        """{pfad: (analysiert_am, fehler, groesse, mtime)} der Pfade in einer gehoer-Datenbank (lesend)."""
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
        """Ein gehoer-Aufruf für die Pfade. -> True, wenn gehoer lief (dann lädt der Index neu)."""
        # schon gemessen (gleiche Größe und Zeit, ohne Fehler, in einer der beiden Datenbanken): ohne gehoer fertig
        if not neu:
            rest = []
            bekannt = {**self._zeilen(GEHOER_DB, pfade), **{p: r for p, r in self._zeilen(NETZ_DB, pfade).items() if not r[1]}}
            for p in pfade:
                r = bekannt.get(p)
                try:
                    st = os.stat(p)
                except OSError:
                    self._setzen(p, "fehler", "Datei fehlt auf der Platte.")
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
                self._setzen(p, "fehler", f"gehoer ist nicht installiert ({GEHOER}).")
            return False
        vorher = self._zeilen(db, pfade)
        liste = tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="openlibary-messen-", delete=False, encoding="utf-8")
        with liste:
            liste.write("\n".join(pfade) + "\n")
        self.liste_jetzt = liste.name          # räumt beim Beenden des Servers mit auf (main, beenden)
        befehl = MESS_PRIO + [GEHOER, "bibliothek", "scan", "--liste", liste.name, "--arbeiter", "1", "--db", str(db)]
        if neu:
            befehl.append("--neu")
        ausgabe = tempfile.TemporaryFile()
        # Notbremse erst nach gehoers eigenem Wächter (je Datei 120 s plus 4 s je Sekunde Audio, die erste 600 s länger)
        # plus 10 min Reserve: dann vermerkt gehoer die Zeitüberschreitung selbst, und ein neuer Versuch überspringt die
        # Datei. Vorher galt 600 + 120 s je Datei, die Open Libary brach also immer zuerst ab, gehoer löschte seinen
        # Vermerk, und die Datei kam bei jedem Start wieder dran (Prüfung 20.10.)
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
                        proc.terminate()      # gehoer hält sauber an und vermerkt die laufende Datei
                        try:
                            proc.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()       # sonst bleibt ein Zombie, und os.kill(pid, 0) hält ihn für lebendig
                        break
            finally:
                with self.lock:
                    self._pids.discard(proc.pid)
            ausgabe.seek(0)
            text = ausgabe.read().decode("utf-8", errors="replace")
        except OSError as e:
            text = f"gehoer startet nicht: {e}"
        finally:
            ausgabe.close()
            try:
                os.unlink(liste.name)
            except OSError:
                pass
        if zu_lange:
            text += "\nZeitüberschreitung: gehoer brauchte zu lange und wurde beendet"
        self._abgleichen(db, pfade, vorher, text)
        return True

    def _abgleichen(self, db, pfade, vorher, ende_text):
        """Stand je Datei aus der Datenbank: eine neue Zeile ohne Fehler heißt gemessen, mit Fehler den Fehler; noch
        "in Arbeit" oder unverändert heißt misst. Am Ende (ende_text ist gehoers Ausgabe, sonst None): unverändert und
        fehlerfrei heißt gemessen (gehoer hatte die Datei schon), ohne Zeile der Grund aus der Ausgabe."""
        ende = ende_text is not None
        jetzt = self._zeilen(db, pfade)
        zeilen = [z.strip() for z in (ende_text or "").splitlines() if z.strip()]
        zu_lange = any(z.startswith("Zeitüberschreitung") for z in zeilen)    # Notbremse der Open Libary
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
        """Satz für eine Datei, zu der gehoer keine Zeile schrieb. Vorher stand dort gehoers letzte Ausgabezeile,
        etwa "Messung gescheitert: Fertig: 0 analysiert, 0 schon erfasst, ... 1 s." (Prüfung 20.10.)."""
        if not os.path.isfile(p):
            return "Datei fehlt auf der Platte."
        if any(z.startswith("Zeitüberschreitung") for z in zeilen):
            return _fehler_satz("Zeitüberschreitung")
        if any(z.startswith("gehoer startet nicht") for z in zeilen):
            return "Messung gescheitert: gehoer ließ sich nicht starten."
        eigene = next((z for z in zeilen if p in z and not z.startswith(("Fertig", "Abgebrochen"))), None)
        if eigene:      # gehoer nennt den Pfad, wenn er die Datei nicht lesen konnte: der Grund steht dahinter
            vor, _, nach = eigene.partition(p)
            grund = nach.strip(" :.-") or vor.strip(" :.-")
            if grund:
                return _fehler_satz(grund)
        return "Messung gescheitert: gehoer hat die Datei nicht erfasst."

    def nachholen(self):
        """Beim Start: Netz-Downloads aus zusaetze.csv, die in keiner der beiden Datenbanken stehen (geladen vor dem
        Ausbau oder während der Server nicht lief), kommen in die Warteschlange. Steht dort schon eine Zeile, auch mit
        Fehler, bleibt die Datei liegen (neu messen geht über den Knopf)."""
        pfade = [z["pfad"] for z in INDEX.zeilen if z["herkunft"] == "openlibary" and not z["gemessen"]
                 and os.path.isfile(z["pfad"])]
        if pfade:
            bekannt = set(self._zeilen(GEHOER_DB, pfade)) | set(self._zeilen(NETZ_DB, pfade))
            pfade = [p for p in pfade if p not in bekannt]
        if pfade:
            print(f"Messen: {len(pfade)} Netz-Downloads ohne Messung in die Warteschlange", flush=True)
            self.dazu(pfade)


MESSDIENST = None
ORDNER = None
LEICHT = None                # leichte Messung (leicht.py), None vor dem Start
KLANG = None                 # Klang-Fingerabdruck (klangnaehe.py), None ohne gehoer-Umgebung
ORDNER_MESSEN_MAX = 300      # neue Dateien eines Ordners, die gehoer von selbst misst; mehr über Auswahl und „Messen“
ORDNER_TAKT_S = 3600         # eigene Ordner stündlich neu einlesen (neue und gelöschte Dateien)


def werkzeuge():
    """Was auf diesem Rechner da ist; für die Einstellungen (Einrichten für andere)."""
    return {"ffmpeg": os.path.exists(FFMPEG), "ffprobe": os.path.exists(netz.KONFIG["ffprobe"] or ""),
            "rubberband": os.path.exists(RUBBERBAND), "gehoer": os.path.exists(GEHOER),
            "klang": bool(KLANG and KLANG.verfuegbar() and KLANG.zahl()), "leicht": bool(LEICHT and LEICHT.da())}


def leicht_befehl():
    """Leichte Messung: die Python-Fassung (leichtmessen.py), wo numpy und soundfile da sind (fertige Apps auf allen
    Systemen, gehoer-Umgebung auf Jeremias' Mac), sonst auf dem Mac die Swift-Fassung. -> Befehl oder None."""
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
    """Zeilen ohne gehoer-Messung: Tempo, Grundton und grober Klang-Fingerabdruck (für Ähnliche ohne gehoer)."""
    return [z["pfad"] for z in INDEX.zeilen if not z["gemessen"] and not z["ist_kopie"]]


def leicht_neu_laden():
    threading.Thread(target=INDEX.laden, daemon=True).start()


def ordner_bekannt():
    return {p for p, z in INDEX.nach_pfad.items() if not z.get("_eigen")}


def ordner_eingelesen(neue):
    """Nach dem Einlesen: Index sofort neu, dann gehoer und Klang-Fingerabdruck für das Neue (nur wenn da)."""
    INDEX.laden()
    neue = [p for p in neue if p in INDEX.nach_pfad]      # Renders und Ausgelassenes nicht messen
    if neue and LEICHT:
        LEICHT.wecken()
    if neue and os.path.exists(GEHOER):
        MESSDIENST.dazu(sorted(neue)[:ORDNER_MESSEN_MAX])
    if neue and KLANG:
        KLANG.rechnen_lassen([(klang_schluessel(INDEX.nach_pfad[p]), p) for p in sorted(neue)[:KLANG_AUTO_MAX]
                              if p in INDEX.nach_pfad])


def fertig_geladen(pfade):
    """netz.py hat Dateien fertig einsortiert: gehoer misst, der Klang-Fingerabdruck kommt dazu. Ohne gehoer (andere
    Rechner) wird nichts eingereiht: vorher stand bei jedem Download „gehoer ist nicht installiert“ (Nutzertest 2)."""
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
    """Die am 17.10.2026 an der Originalquelle gegengeprüften Packs (netz-urteile.tsv), mit Ordner auf der Platte."""
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
    """WAV-Kopie für Formate, die der Browser nicht spielt (AIFF, Ogg im WAV-Container der FL-Packs). Eine Sperre je
    Datei: vorher schrieben gleichzeitige Abrufe in dieselbe .teil-Datei (5 von 6 endeten mit Fehler 500)."""
    WANDEL_CACHE.mkdir(exist_ok=True)
    st = os.stat(pfad)
    schluessel = hashlib.md5(f"{pfad}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()
    ziel = WANDEL_CACHE / (schluessel + ".wav")
    with _WANDEL_LOCKS[schluessel]:
        if ziel.is_file():
            os.utime(ziel)
            return str(ziel)
        tmp = WANDEL_CACHE / f"{schluessel}.{os.getpid()}.{threading.get_ident()}.teil.wav"
        # 03.11.2026: AIFF ohne ffmpeg (Windows und Linux haben es meist nicht; bitgleich mit ffmpeg geprüft)
        if pfad.lower().endswith((".aif", ".aiff", ".aifc")):
            try:
                audioform.aiff_zu_wav(pfad, tmp)
                os.replace(tmp, ziel)
                _wandel_aufraeumen()
                return str(ziel)
            except (ValueError, OSError, struct_fehler):
                pass
        if not os.path.exists(FFMPEG):      # ohne ffmpeg: soundfile, wenn da (fertige Apps)
            audioform.wav_aus(pfad, str(tmp))
            os.replace(tmp, ziel)
            _wandel_aufraeumen()
            return str(ziel)
        r = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", pfad, "-c:a", "pcm_s16le", str(tmp)],
                           capture_output=True, timeout=120, **plattform.hilfsprozess_args())
        if r.returncode != 0:
            # Ogg Vorbis im WAV-Container (rund 3.000 Dateien der FL-Packs): ffmpeg findet keinen Decoder. Den Ogg-Strom
            # löst audioform heraus (nur Standardbibliothek; vorher über die gehoer-Umgebung, die Freunde nicht haben).
            ogg = audioform.ogg_in_wav(pfad)
            if not ogg:
                raise RuntimeError("Format nicht lesbar: " + r.stderr.decode(errors="replace")[-200:])
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "ogg", "-i", "pipe:0", "-c:a", "pcm_s16le", str(tmp)],
                           input=ogg, check=True, timeout=120, **plattform.hilfsprozess_args())
        os.replace(tmp, ziel)
    _wandel_aufraeumen()
    return str(ziel)


import struct as _struct
struct_fehler = _struct.error


def als_wav(pfad):
    """WAV-Fassung zum Ziehen (DAWs ohne OGG, FLAC oder MP3: Logic, Pro Tools). Liegt dauerhaft unter den Renders
    („als WAV“), weil ein DAW-Projekt auf die Datei zeigt; gleiche Quelle, gleicher Stand: dieselbe Datei."""
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


# ================================================================ Fassungen (Paket 3): im Projekt vorhören und ziehen
# GET /api/fassung rechnet eine angepasste Fassung als WAV: Tempo mit rubberband (--time), Tonhöhe mit rubberband
# (--pitch, bei Vocals mit Formanterhalt), Varispeed mit ffmpeg (asetrate + aresample, Tempo und Tonhöhe zusammen wie am
# Plattenspieler). Die Datei landet in RENDER_ZIEL und ist genau die, die die App nach FL zieht. Gleiche Anfrage noch
# einmal liefert die vorhandene Datei sofort (Schlüssel: Pfad, Größe, Änderungszeit, Faktor, Halbtöne, Formant, Varispeed;
# Liste in .renders.json im Ordner). RENDER_ZIEL ist beim Bibliotheksscan ausgenommen (bestand_messen.py und Index).
RENDER_ZIEL = Path(os.environ.get("OPENLIBARY_RENDERS", MUSIK / "Samples/Open Libary Renders" if _EIGENE_ABLAGE
                                   else ABLAGE / "Renders"))
RUBBERBAND = os.environ.get("OPENLIBARY_RUBBERBAND") or plattform.werkzeug("rubberband") or "rubberband"   # Umgebung: Fehlerfälle prüfen
FASSUNG_PARAMETER = frozenset(["pfad", "bpm", "ht", "formant", "vari", "seite", "nr", "start", "ende"])   # start, ende: Paket 10
RENDER_GLEICHZEITIG = 2
_RENDER_PLAETZE = threading.BoundedSemaphore(RENDER_GLEICHZEITIG)
_RENDER_LISTE_LOCK = threading.Lock()
_RENDER_NEUESTE = {}        # Seite -> höchste Anfragenummer; ältere Anfragen derselben Seite fallen weg (auch laufende)
_RENDER_ANGEFRAGT = {}      # (Seite, Schlüssel) -> höchste Nummer, die diese Fassung angefragt hat (Pfeil auf die
                            # vorausgerechnete Zeile: die neue Anfrage übernimmt den laufenden Render, statt ihn abzubrechen)


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
    """Nur fertige Renders aus der eigenen Liste dürfen über /audio abgespielt werden."""
    pfad = nfc(pfad)
    if os.path.dirname(pfad) != nfc(str(RENDER_ZIEL)) or not os.path.isfile(pfad):
        return False
    with _RENDER_LISTE_LOCK:
        return any(e.get("datei") == os.path.basename(pfad) for e in _render_liste().values())


def _wav_rate(pfad):
    """Abtastrate aus dem fmt-Block einer WAV (auch WAVE_FORMAT_EXTENSIBLE, das wave in Python 3.9 nicht liest)."""
    with open(pfad, "rb") as f:
        kopf = f.read(12)
        if kopf[:4] != b"RIFF" or kopf[8:12] != b"WAVE":
            raise FassungFehler(500, "Zwischendatei ist keine WAV")
        while True:
            c = f.read(8)
            if len(c) < 8:
                raise FassungFehler(500, "WAV ohne fmt-Block")
            n = int.from_bytes(c[4:8], "little")
            if c[:4] == b"fmt ":
                return int.from_bytes(f.read(n)[4:8], "little")
            f.seek(n + (n & 1), 1)


def _ton_name(ton, modus, ht):
    if not ton or ton not in TONARTEN:
        return None
    return TONARTEN[(TONARTEN.index(ton) + int(round(ht))) % 12] + (modus or "")


def _bpm_text(b):
    return str(int(round(b))) if abs(b - round(b)) < 0.05 else f"{b:.1f}"      # wie im Fuß: 145.9


def _render_ziel_name(stamm, teile, schluessel, liste, endung=""):
    """<Originalname>_140_Fm.wav; trägt eine andere Fassung schon diesen Namen, dann zuerst mit der Endung der Quelle
    ("… (mp3).wav", Cymatics legt dieselbe Datei als WAV und MP3 ab), sonst (2), (3) (gleicher Name in zwei Packs)."""
    grund = re.sub(r'[/:\x00]', "_", stamm + "".join("_" + t for t in teile))
    belegt = {e.get("datei") for k, e in liste.items() if k != schluessel}
    name, n = grund + ".wav", 2
    if name in belegt and endung and endung != "wav":
        name = f"{grund} ({endung}).wav"
    while name in belegt:
        name, n = f"{grund} ({n}).wav", n + 1
    return name


def schnitt_name(a, b, bpm):
    """Paket 10: Namensteil eines Ausschnitts. Auf Takten (4/4 ab Dateianfang, je 1 ms Spiel) "T1-2" bzw. "T3",
    sonst die Anfangszeit "0m42s"."""
    if bpm:
        takt = 240 / bpm
        ta, tb = a / takt, b / takt
        if abs(a - round(ta) * takt) <= 0.001 and abs(b - round(tb) * takt) <= 0.001 and round(tb) > round(ta):
            von, bis = int(round(ta)) + 1, int(round(tb))
            return f"T{von}" if von == bis else f"T{von}-{bis}"
    return f"{int(a // 60)}m{int(a % 60):02d}s"


def fassung_rechnen(p):
    """-> {"pfad", "url", "datei", "neu", "sekunden"}. Fehleingabe (400), FassungFehler (403, 404, 503, 500),
    FassungVerworfen (Antwort 200 mit "verworfen": eine neuere Anfrage derselben Seite kam dazu, solange diese noch wartete)."""
    def eins(k):
        return (p.get(k) or [""])[0]
    pfad = nfc(eins("pfad"))
    z = INDEX.nach_pfad.get(pfad)
    if z is None:
        raise FassungFehler(403, "Pfad steht nicht im Index")
    if not os.path.isfile(pfad):
        raise FassungFehler(404, "Datei fehlt auf der Platte")
    try:
        ht = int(eins("ht") or "0")
    except ValueError:
        raise Fehleingabe("ht muss eine ganze Zahl sein")
    if not -12 <= ht <= 12:
        raise Fehleingabe("ht geht von -12 bis 12")
    vari, formant = eins("vari") == "1", eins("formant") == "1"
    ziel_bpm = zahl(eins("bpm")) if eins("bpm") else None
    if eins("bpm") and ziel_bpm is None:
        raise Fehleingabe("bpm muss eine Zahl sein")
    if ziel_bpm is not None and not 20 <= ziel_bpm <= 400:
        raise Fehleingabe("bpm geht von 20 bis 400")
    if vari and ziel_bpm is not None:
        raise Fehleingabe("Varispeed bestimmt das Tempo selbst, bpm weglassen")
    faktor = 1.0                                  # Dauer neu zu Dauer alt
    if ziel_bpm is not None:
        if not z.get("bpm"):
            raise Fehleingabe("ohne BPM, nicht gestreckt")
        faktor = z["bpm"] / ziel_bpm
        if not 0.5 <= faktor <= 2.0:
            raise Fehleingabe("Strecken nur bis zum halben oder doppelten Tempo")
        if abs(faktor - 1) < 1e-4:
            faktor, ziel_bpm = 1.0, None
    if vari and ht == 0:
        vari = False
    # Paket 10: Ausschnitt in Sekunden der Originaldatei; gerechnet wird nur er (danach gestreckt oder transponiert)
    schnitt = None
    if eins("start") or eins("ende"):
        a, b = zahl(eins("start")), zahl(eins("ende"))
        if a is None or b is None:
            raise Fehleingabe("start und ende müssen Zahlen sein")
        if a < 0 or b - a < 0.05:
            raise Fehleingabe("Ausschnitt: start ab 0, mindestens 50 ms lang")
        if z.get("dauer") and b > z["dauer"] + 0.05:
            raise Fehleingabe("Ausschnitt geht über das Ende der Datei")
        schnitt = (a, b)
    if faktor == 1.0 and ht == 0 and schnitt is None:
        raise Fehleingabe("nichts zu rechnen: Tempo und Tonhöhe bleiben")
    seite, nr = eins("seite")[:40], 0
    if eins("nr"):
        try:
            nr = int(eins("nr"))
        except ValueError:
            raise Fehleingabe("nr muss eine ganze Zahl sein")
    st = os.stat(pfad)
    schluessel = hashlib.sha1(f"{pfad}|{st.st_size}|{st.st_mtime}|{faktor:.6f}|{ht}|{int(formant)}|{int(vari)}|1"
                              .encode() + (f"|{schnitt[0]:.6f}-{schnitt[1]:.6f}".encode() if schnitt else b"")
                              ).hexdigest()[:20]
    # Name: Tempo und Tonart, die die Fassung jetzt hat; Varispeed hängt _vari an
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
    if schnitt:     # Paket 10: _T1-2 (auf Takten der Originaldatei, 4/4 ab Dateianfang) oder _0m42s (frei)
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
    nur_schnitt = faktor == 1.0 and ht == 0          # Paket 10: nur ausschneiden, das kann ffmpeg allein
    if not os.path.exists(RUBBERBAND) and not (vari and faktor == 1.0) and not nur_schnitt:
        raise FassungFehler(503, f"rubberband fehlt ({RUBBERBAND}), spielt Original")
    if seite:
        with _RENDER_LISTE_LOCK:
            _RENDER_NEUESTE[seite] = max(nr, _RENDER_NEUESTE.get(seite, nr))
            k = (seite, schluessel)
            _RENDER_ANGEFRAGT[k] = max(nr, _RENDER_ANGEFRAGT.get(k, nr))
            if len(_RENDER_ANGEFRAGT) > 500:       # Erledigtes aufräumen
                for a in [a for a, n in _RENDER_ANGEFRAGT.items() if n < _RENDER_NEUESTE.get(a[0], 0)]:
                    del _RENDER_ANGEFRAGT[a]

    def ueberholt():      # eine neuere Anfrage derselben Seite will etwas anderes (Pfeil weiter)
        return bool(seite) and _RENDER_ANGEFRAGT.get((seite, schluessel), nr) < _RENDER_NEUESTE.get(seite, nr)

    def warten(sperre):   # warten, solange die Anfrage nicht überholt ist
        while not sperre.acquire(timeout=0.2):
            if ueberholt():
                raise FassungVerworfen()

    def ausfuehren(befehl, frist):
        """wie subprocess.run, aber ein überholter Render wird abgebrochen (nur die aktuelle Zeile rechnet)"""
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

    sperre = _WANDEL_LOCKS["fassung:" + schluessel]           # die Sperre je Datei wie beim Wandeln
    warten(sperre)
    try:
        e = vorhanden()       # eine gleiche Anfrage hat inzwischen gerechnet
        if e:
            return fertig(e, False, 0)
        warten(_RENDER_PLAETZE)                                # höchstens zwei Renders gleichzeitig
        t0 = time.time()
        try:
            RENDER_ZIEL.mkdir(parents=True, exist_ok=True)
            arbeit = Path(tempfile.mkdtemp(prefix="fassung-", dir=str(RENDER_ZIEL)))
            try:
                ein = arbeit / "ein.wav"
                r = ausfuehren([FFMPEG, "-v", "error", "-y", "-i", pfad, "-c:a", "pcm_s24le", str(ein)], 180)
                if r.returncode != 0:                  # Ogg im WAV-Container und Ähnliches: über die Wandlung
                    ein = Path(gewandelt(pfad))
                aus = arbeit / "aus.wav"
                if schnitt:     # Paket 10: sample-genau ausschneiden (Länge aus der Differenz, damit 2 Takte 2 Takte sind)
                    sr = _wav_rate(str(ein))
                    a = int(round(schnitt[0] * sr))
                    b = a + int(round((schnitt[1] - schnitt[0]) * sr))
                    teil = arbeit / "teil.wav"
                    r = ausfuehren([FFMPEG, "-v", "error", "-y", "-i", str(ein), "-af",
                                    f"atrim=start_sample={a}:end_sample={b},asetpts=N/SR/TB", "-c:a", "pcm_s24le",
                                    str(teil if not nur_schnitt else aus)], 180)
                    if r.returncode != 0:
                        raise FassungFehler(500, "ffmpeg brach beim Ausschneiden ab: " +
                                            (r.stderr.decode(errors="replace").strip()[-200:] or f"Rückgabe {r.returncode}"))
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
                    raise FassungFehler(500, f"{wer} brach ab: " + (r.stderr.decode(errors="replace").strip()[-200:]
                                                                     or f"Rückgabe {r.returncode}") + ", spielt Original")
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
                raise FassungFehler(500, "Rechnen dauerte über 5 Minuten, spielt Original")
            finally:
                shutil.rmtree(arbeit, ignore_errors=True)
        finally:
            _RENDER_PLAETZE.release()
    finally:
        sperre.release()


# ================================================================ HTTP

class Server(ThreadingHTTPServer):
    request_queue_size = 128   # Standard 5: bei 20 gleichzeitigen Suchen scheiterten 55 bis 75 %
    daemon_threads = True
    # Windows: SO_REUSEADDR heißt dort „Port mit einem laufenden Server teilen“; zwei Open Libary hörten dann beide auf
    # 8765 und die Seite spräche zufällig mit dem alten. Dort exklusiv binden, damit der zweite Start sauber scheitert.
    allow_reuse_address = not plattform.WINDOWS

    def server_bind(self):
        if plattform.WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # Browser bricht ab (Abbrechen-Knopf, Seitenwechsel): kein Fehler, nicht ins Protokoll
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def ganzzahl(p, k, std, lo, hi):
    w = (p.get(k) or [str(std)])[0]
    try:
        return max(lo, min(hi, int(w)))
    except ValueError:
        raise Fehleingabe(f"{k} muss eine Zahl sein")


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
        """Nur eigene Adresse: sonst könnte jede Webseite im Browser Downloads anstoßen (DNS-Rebinding, CSRF)."""
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _origin_ok(self):
        o = self.headers.get("Origin")
        return o in (None, "null", f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")

    def _koerper(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > KOERPER_MAX:
            while n > 0:            # erst zu Ende lesen, sonst sieht der Aufrufer statt der 400 einen Verbindungsabbruch
                stueck = self.rfile.read(min(n, 1 << 20))
                if not stueck:
                    break
                n -= len(stueck)
            raise Fehleingabe("Anfrage zu groß")
        try:
            # 16 MB: Cmd+A schickt die festgehaltenen Pfade (Paket 6), der ganze Bestand braucht rund 10 MB
            k = json.loads(self.rfile.read(min(n, KOERPER_MAX)) or b"{}")
        except ValueError:
            raise Fehleingabe("Körper ist kein JSON")
        if not isinstance(k, dict):
            raise Fehleingabe("Körper muss ein JSON-Objekt sein")
        return k

    def _konten(self):
        return {"freesound": dict(netz.freesound_stand(), rueckruf=f"http://127.0.0.1:{PORT}/api/freesound/rueckruf"),
                "looperman": netz.WAECHTER.bericht()}

    def _html_seite(self, titel, text):
        """Kleine Seite für Rückrufe aus dem Browser (Freesound-Login), im Ton der App."""
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
        if not self._host_ok():
            return self._fehler(403, "fremder Host")
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                stil = stil_aktuell()
                b = (HIER / "seite.html").read_text(encoding="utf-8").replace("__STIL__", stil).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(b)
            if re.match(r"^/stile/[a-z0-9-]+\.css$", u.path):
                datei = HIER / u.path.lstrip("/")
                return self._datei(str(datei), "text/css; charset=utf-8") if datei.is_file() else self._fehler(404, "Stil fehlt")
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
                # erste Seite mit gesetzten Gruppen: Treffer und Zählung ohne eigene Auswahl in einem Durchgang
                treffer, extra = filtern(p, gruppen=True) if ab == 0 and gruppen_gesetzt(p) else (filtern(p), None)
                drums_frei = (p.get("drums_ohne_ton") or [""])[0] == "1"
                gehoert = verlauf.gehoert_menge()     # Paket 4: schon gehörte Zeilen dämpft die Liste
                sammlung = (p.get("sammlung") or [""])[0]
                tage = collections.Counter(z["verlauf_zeit"][:10] for z in treffer) \
                    if sammlung == "gezogen" and ab == 0 else None      # Paket 4: Gezogen nach Tagen, mit Anzahl
                return self._json({"gesamt": len(treffer), "alle": len(INDEX.zeilen),
                                   "zeilen": [dict(oeffentlich(z), **({"gehoert": True} if z["pfad"] in gehoert else {}))
                                              for z in treffer[ab:ab + n]],
                                   "verlauf_tage": tage,
                                   "aehnlich": aehnlich_vergleicher(p)[1] if (p.get("aehnlich") or [""])[0] else None,
                                   "facetten": facetten_ohne_eigene(p, facetten(treffer, drums_frei=drums_frei), treffer, extra)
                                   if ab == 0 else None,
                                   "abdeckung": INDEX.abdeckung if ab == 0 else None,
                                   "arten_alle": INDEX.arten if ab == 0 else None,     # Paket 6: Auswahl beim Bearbeiten
                                   "vollscan": fremder_scan() if ab == 0 else None,
                                   "vollscan_fertig": vollscan_fertig() if ab == 0 else None,
                                   # Paket 7: Lizenzzeile über einer geöffneten Playlist
                                   "lizenz_playlist": lizenz_playlist(sammlung)
                                   if ab == 0 and sammlung.startswith("pl:") else None})
            if u.path == "/api/stand":
                INDEX.aktuell_halten()
                ladungen = netz.stand()
                messungen = MESSDIENST.stand_fuer()
                for x in ladungen.values():     # je geladene Datei: wartet, misst, gemessen oder der Fehler
                    x["messung"] = [dict(messungen.get(nfc(q), {"status": "", "text": ""}), name=os.path.basename(q),
                                         pfad=q) for q in x.pop("pfade", [])]
                return self._json({"alle": len(INDEX.zeilen), "gemessen": INDEX.gemessen, "fehler": INDEX.fehler,
                                   "code": str(HIER), "code_stand": CODE_STAND, "pid": os.getpid(),
                                   "paket": bool(os.environ.get("OPENLIBARY_PAKET")), "system": plattform.SYSTEM,
                                   "gehoer_db": GEHOER_DB.is_file(), "gehoer_da": os.path.exists(GEHOER),
                                   "messungen": messungen, "ladungen": ladungen,
                                   "index_version": INDEX.version, "vollscan": fremder_scan()})
            if u.path == "/api/zeilen":
                # aktuelle Werte einzelner Zeilen (nach einer Messung), nur Pfade aus dem Index
                pfade = [nfc(x) for x in p.get("pfad", [])][:200]
                return self._json({"zeilen": [oeffentlich(INDEX.nach_pfad[x]) for x in pfade if x in INDEX.nach_pfad],
                                   "index_version": INDEX.version})
            if u.path == "/api/stile":
                return self._json({"stile": stile_liste(), "aktiv": stil_aktuell()})
            if u.path == "/api/sammlungen":
                return self._json(dict(sammlungen.stand(), verlauf=verlauf.zahlen(),     # Paket 4: Zahlen links
                                       lizenz_offen=lizenz_offen_zahl()))                # Paket 7: Zähler links
            if u.path == "/api/lizenzen":     # Paket 7: Lizenzansicht, eine Zeile je Pack
                fremd = unbekannte_parameter(p, frozenset())
                if fremd:
                    return self._json(fremd, 400)
                INDEX.aktuell_halten()
                return self._json(lizenz_packs())
            if u.path == "/api/downloads":
                # alles, was die Open Libary aus dem Netz geladen hat, neueste zuerst
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
            if u.path == "/api/fassung":     # Paket 3: angepasste Fassung rechnen (oder die schon gerechnete)
                fremd = unbekannte_parameter(p, FASSUNG_PARAMETER)
                if fremd:
                    return self._json(fremd, 400)
                try:
                    return self._json(fassung_rechnen(p))
                except FassungVerworfen:    # kein Fehler (200): sonst meldet die Konsole jede verworfene Anfrage rot
                    return self._json({"verworfen": True, "grund": "von einer neueren Anfrage überholt"})
                except FassungFehler as e:
                    if e.code == 403:       # Schutz (Pfad nicht im Index) bleibt ein echter Fehler
                        return self._fehler(e.code, str(e))
                    # Rechnen ging nicht (rubberband fehlt 503, bricht ab 500, Datei fehlt 404): ein Zustand, den der
                    # Fuß rot anzeigt, kein Seitenfehler; 200, damit die Konsole nicht bei jedem Sample rot wird
                    return self._json({"fehler": str(e), "code": e.code})
            if u.path == "/audio":
                pfad = nfc((p.get("pfad") or [""])[0])
                if render_bekannt(pfad):      # Paket 3: gerechnete Fassung aus dem Render-Ordner
                    return self._datei(pfad, "audio/wav")
                if not self._pfad_erlaubt(pfad):
                    return self._fehler(403, "Pfad steht nicht im Index")
                if not os.path.isfile(pfad):
                    return self._fehler(404, "Datei fehlt auf der Platte")
                # AIFF und FL-Werkssamples (Ogg im WAV) spielt kein Browser: gleich als WAV, ohne Fehlversuch der Seite
                if (p.get("wandeln") or [""])[0] == "1" or pfad.lower().endswith((".aif", ".aiff")) \
                        or audioform.ist_ogg_in_wav(pfad):
                    pfad = gewandelt(pfad)
                return self._datei(pfad, mimetypes.guess_type(pfad)[0] or "application/octet-stream")
            if u.path == "/api/ordner":          # Musiker-Test 03.11.: eigene Ordner, Werkzeuge
                return self._json(dict(ORDNER.stand(), werkzeuge=werkzeuge(), netz_ziel=str(NETZ_ZIEL), leicht=LEICHT.bericht(),
                                       messen_max=ORDNER_MESSEN_MAX, samples=len(INDEX.zeilen), heim=str(Path.home())))
            if u.path == "/api/als_wav":         # 03.11.: beim Ziehen als WAV (Logic, Pro Tools)
                pfad = nfc((p.get("pfad") or [""])[0])
                if not (self._pfad_erlaubt(pfad) or render_bekannt(pfad)) or not os.path.isfile(pfad):
                    return self._fehler(403, "Pfad steht nicht im Index")
                try:
                    return self._json({"pfad": als_wav(pfad)})
                except (RuntimeError, OSError) as e:
                    return self._json({"fehler": str(e)})
            if u.path == "/api/konten":          # Konten 03.11.: Freesound und Looperman auf einen Blick
                return self._json(self._konten())
            if u.path == "/api/netz/uebernommen":     # Konten 03.11.: zuletzt aus Looperman übernommen
                aus = []
                for e in netz.uebernommen((p.get("quelle") or ["looperman"])[0]):
                    z = INDEX.nach_pfad.get(nfc(e["pfad"]))
                    aus.append(dict(e, name=z["name"] if z else e["datei"], bpm=z["bpm"] if z else None,
                                    ton=(z["ton"] or "") + (z["modus"] or "") if z and z["ton"] else "",
                                    art=z["art"] if z else "", dauer=z["dauer"] if z else None,
                                    im_index=z is not None))
                return self._json({"dateien": aus})
            if u.path == "/api/netz/pfad":       # Konten 03.11.: schon geladener Netz-Sound -> Pfad (Ziehen aus Netz)
                pf = netz.pfad_von_seite((p.get("seite") or [""])[0])
                return self._json({"pfad": pf if pf and self._pfad_erlaubt(pf) else None})
            if u.path == "/api/klang":           # Musiker-Test 03.11.: Klang-Fingerabdruck, Stand
                return self._json(dict(KLANG.stand(), samples=len(INDEX.zeilen)))
            if u.path == "/api/freesound":       # Musiker-Test 03.11.: Freesound direkt, ohne Geheimnisse
                return self._json(dict(netz.freesound_stand(),
                                       rueckruf=f"http://127.0.0.1:{PORT}/api/freesound/rueckruf"))
            if u.path == "/api/freesound/login":
                ziel = netz.freesound_login_url()
                if not ziel:
                    return self._html_seite("Freesound", "Erst Client-ID und Client-Secret in den Einstellungen eintragen.")
                self.send_response(302)
                self.send_header("Location", ziel)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if u.path == "/api/freesound/rueckruf":
                if (p.get("error") or [""])[0]:
                    return self._html_seite("Freesound", "Login abgebrochen. In Open Libary unter Einstellungen neu starten.")
                ok, text = netz.freesound_code_tauschen((p.get("code") or [""])[0], (p.get("state") or [""])[0])
                return self._html_seite("Freesound verbunden" if ok else "Freesound",
                                        text + (". Dieses Fenster kann zu, weiter geht es in Open Libary." if ok else ""))
            if u.path == "/api/netz/suche":
                quelle = (p.get("quelle") or ["archive"])[0]
                q, lizenz = (p.get("q") or [""])[0], (p.get("lizenz") or ["cc0"])[0]
                seite = ganzzahl(p, "seite", 1, 1, 100)
                quelle = {"openverse": "freesound"}.get(quelle, quelle)
                if quelle in netz.EINZEL_QUELLEN:
                    return self._json(netz.EINZEL_QUELLEN[quelle](q, lizenz, seite))
                if quelle != "archive":
                    return self._fehler(400, "unbekannte Quelle")
                return self._json(netz.archive_suche(q, lizenz, seite, (p.get("alles") or [""])[0] != "1"))
            if u.path == "/api/netz/vorschau":
                # Vorhören über den Server, wo die Quelle fremde Abrufe sperrt (ccMixter: Referer-Pflicht)
                t = netz._EINZEL.get((p.get("s") or [""])[0])
                if not t:
                    return self._fehler(404, "Sound nicht aus einer der letzten Suchen")
                url = t.get("hoer_url") or t["url"]
                if not url.startswith(("https://", "http://ccmixter.org/")):
                    return self._fehler(400, "nur https")
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
                    return self._fehler(e.code, f"Quelle meldet {e.code}")
            if u.path == "/api/netz/geprueft":
                return self._json({"packs": netz_geprueft()})
            if u.path == "/api/netz/dateien":
                return self._json(netz.archive_dateien((p.get("id") or [""])[0]))
            return self._fehler(404, "unbekannt")
        except Fehleingabe as e:
            return self._fehler(400, str(e))
        except netz.NichtGefunden as e:
            return self._fehler(404, str(e))
        except netz.QuelleFehler as e:
            return self._fehler(e.code, str(e))
        except (TimeoutError, OSError) as e:
            if "timed out" in str(e):
                return self._fehler(504, "Die Quelle antwortet gerade nicht (Zeitüberschreitung); gleich nochmal versuchen")
            return self._fehler(502, f"Quelle nicht erreichbar: {getattr(e, 'reason', e)}")
        except Exception as e:
            return self._fehler(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        if not (self._host_ok() and self._origin_ok()) or \
                not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._fehler(403, "nur von der Open Libary selbst")
        u = urllib.parse.urlparse(self.path)
        try:
            k = self._koerper()
            pfad = nfc(k["pfad"] if isinstance(k.get("pfad"), str) else "")     # Zahl als Pfad gab 500 (Paket 4)
            if u.path == "/api/finder":
                echt = os.path.realpath(pfad)
                erlaubter_ordner = os.path.isdir(echt) and (echt.startswith(str(MUSIK) + os.sep)
                                                            or echt.startswith(str(NETZ_ZIEL))
                                                            or echt.startswith(str(DRUM_ZIEL))
                                                            or any(echt == o["pfad"] or echt.startswith(o["pfad"] + os.sep)
                                                                   for o in ORDNER.liste()))
                if not self._pfad_erlaubt(pfad) and not erlaubter_ordner:
                    return self._fehler(403, "Pfad steht nicht im Index")
                if not os.path.exists(pfad):
                    return self._fehler(404, "Datei fehlt auf der Platte")
                if k.get("zeigen", True):   # die App zeigt selbst (zeigen: false), der Browser braucht den Server
                    plattform.im_ordner_zeigen(echt if erlaubter_ordner else pfad)
                return self._json({"ok": True})
            if u.path == "/api/messen" and ("pfade" in k or "suche" in k):    # Paket 6: Mehrfachauswahl
                pfade = [x for x in auswahl_pfade(k, MESSEN_MAX) if os.path.isfile(x)]
                if not pfade:
                    return self._fehler(404, "Keine der Dateien liegt auf der Platte.")
                hinweis = MESSDIENST.dazu(pfade, klick=True)
                return self._json({"ok": True, "ziel": "netz", "hinweis": hinweis, "anzahl": len(pfade)})
            if u.path == "/api/korrektur" and "pack" in k:     # Paket 7: {pack: "...", felder: {lizenz, lizenz_grund, beleg}}
                if "pfade" in k or "suche" in k:
                    return self._fehler(400, "pack nicht zusammen mit pfade oder suche")
                try:
                    felder = korrekturen.pack_pruefen(k.get("pack"), k.get("felder"))
                    pack = nfc(k["pack"])
                    if pack not in INDEX.pack_menge:
                        return self._fehler(400, "Einzelsounds aus dem Netz sind kein Pack, Lizenz je Datei (e)"
                                            if pack in INDEX.packs else "Pack steht nicht in der Bibliothek")
                    e = korrekturen.pack_setzen(pack, felder)
                except korrekturen.KorrekturFehler as e:
                    return self._fehler(400, str(e))
                n = pack_im_index(pack)
                return self._json({"ok": True, "pack": pack, "anzahl": n, "entscheid": e,
                                   "lizenz_offen": lizenz_offen_zahl()})
            if u.path == "/api/korrektur":     # Paket 6: {pfade: [...] | suche: {...}, felder: {...}}
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
            if u.path == "/api/auswahl":       # Paket 6: Pfade aller Treffer einer Suche (Cmd+A: kopieren, ziehen)
                pfade = auswahl_pfade({"suche": k.get("suche")})
                return self._json({"pfade": pfade, "gesamt": len(pfade)})
            if u.path == "/api/messen":
                if not self._pfad_erlaubt(pfad):
                    return self._fehler(403, "Pfad steht nicht im Index")
                if not os.path.isfile(pfad):    # sonst meldete die Liste "gemessen", obwohl nichts lief (Prüfung 20.10.)
                    return self._fehler(404, "Datei fehlt auf der Platte.")
                hinweis = MESSDIENST.dazu([pfad], klick=True)
                return self._json({"ok": True, "ziel": "netz", "hinweis": hinweis})
            if u.path == "/api/netz/laden":
                ids = k.get("ids") or []
                if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                    return self._fehler(400, "ids muss eine Liste sein")
                code, antwort = netz.laden_starten(k.get("quelle") or "archive", str(k.get("id") or ""),
                                                   set(k.get("dateien") or []), bool(k.get("bestaetigt")),
                                                   ids=ids, ordner=str(k.get("ordner") or ""))
                return self._json(antwort, code)
            if u.path == "/api/sammlungen":
                if k.get("suche") is not None:      # Paket 6: Cmd+A über alle Treffer, der Server löst die Suche auf
                    k = dict(k, pfade=auswahl_pfade({"suche": k.pop("suche")}))
                    k.pop("pfad", None)
                try:
                    return self._json(dict(sammlungen.aendern(k, self._pfad_erlaubt,
                                                              lambda p: (INDEX.nach_pfad.get(p) or {}).get("dauer")),
                                           verlauf=verlauf.zahlen(), lizenz_offen=lizenz_offen_zahl()))
                except sammlungen.SammlungFehler as e:
                    return self._fehler(400, str(e))
            if u.path == "/api/verlauf":      # Paket 4: {art: "gehoert"|"gezogen", pfad, datei?}
                try:
                    return self._json({"ok": True, "verlauf": verlauf.eintragen(k, self._pfad_erlaubt)})
                except verlauf.VerlaufFehler as e:
                    return self._fehler(400, str(e))
            if u.path == "/api/stil":
                name = stil_normal(k.get("name"))   # "glas", "kobalt" oder "glas+kobalt"
                if not name:
                    return self._fehler(400, "unbekannter Stil")
                (DATEN / "stil.txt").write_text(name + "\n", encoding="utf-8")
                return self._json({"ok": True, "aktiv": name})
            if u.path == "/api/als_wav":         # Ziehen (03.11.): {pfade, muster} -> je Pfad die WAV-Fassung, wo nötig
                endungen = (".ogg", ".oga", ".flac", ".opus", ".m4a") + ((".mp3",) if k.get("muster") == "immer" else ())
                aus, zahl, fehler, zu_viele = [], 0, "", 0
                for x in [nfc(x) for x in (k.get("pfade") or []) if isinstance(x, str)][:2000]:
                    noetig = (self._pfad_erlaubt(x) or render_bekannt(x)) and os.path.isfile(x) and (
                        x.lower().endswith(endungen) or audioform.ist_ogg_in_wav(x))   # FL-Werkssamples liest nur FL
                    if noetig and zahl >= 30:
                        zu_viele += 1
                    elif noetig:
                        try:
                            x = als_wav(x)
                            zahl += 1
                        except (RuntimeError, OSError) as e:
                            fehler = str(e)
                    aus.append(x)
                hinweis = (f"{zu_viele} weitere Dateien wären zu wandeln; sie gehen als Original" if zu_viele else
                           (fehler + "; gezogen wird das Original") if fehler else "")
                return self._json({"pfade": aus, "hinweis": hinweis})
            if u.path == "/api/ordner":          # {pfad} hinzufügen | {pfad, entfernen: true} | {neu_einlesen: true}
                if k.get("waehlen"):             # im Browser: Ordnerdialog von macOS (die App hat ihren eigenen)
                    try:
                        gewaehlt = plattform.ordner_waehlen()
                    except (OSError, subprocess.TimeoutExpired):
                        return self._json(dict(ORDNER.stand(), ok=False, text="Der Ordnerdialog ging nicht auf; "
                                               "den Pfad ins Feld einfügen"))
                    if not gewaehlt:
                        return self._json(dict(ORDNER.stand(), ok=False, abgebrochen=True, text="Kein Ordner gewählt"))
                    pfad = nfc(gewaehlt)
                if k.get("neu_einlesen"):
                    ORDNER.einlesen()
                    ok, text = True, "Liest neu ein"
                elif k.get("entfernen"):
                    ok, text = ORDNER.entfernen(pfad)
                else:
                    ok, text = ORDNER.hinzufuegen(pfad)
                return self._json(dict(ORDNER.stand(), ok=ok, text=text))      # 200: Ablehnung ist kein Seitenfehler
            if u.path == "/api/konten":          # Konten 03.11.: {looperman: true|false}
                if "looperman" in k:
                    netz.looperman_setzen(bool(k["looperman"]))
                return self._json(dict(self._konten(), ok=True))
            if u.path == "/api/klang":           # {alle: true} rechnet alles (Stunden), {anhalten: true}
                if k.get("anhalten"):
                    ok, text = KLANG.anhalten(), "Angehalten; was fertig ist, bleibt"
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
                netz._SUCHE_CACHE.clear()      # Treffer tragen „Vorschau“ oder „Original“: neu fragen
                return self._json(dict(netz.freesound_stand(), ok=ok, text=text))
            if u.path == "/api/netz/abbrechen":
                return self._json({"ok": netz.abbrechen(k.get("schluessel") or "")})
            return self._fehler(404, "unbekannt")
        except Fehleingabe as e:
            return self._fehler(400, str(e))
        except netz.QuelleFehler as e:
            return self._fehler(e.code, str(e))
        except netz.NichtGefunden as e:
            return self._fehler(404, str(e))
        except urllib.error.URLError as e:
            return self._fehler(502, f"Quelle nicht erreichbar: {getattr(e, 'reason', e)}")
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
    """Fertige App nach einem Update (Prüfung 03.11.): läuft auf dem Port ein Server aus einem App-Paket, dessen Code
    älter ist oder nicht mehr existiert, wird er beendet und dieser startet. Einen Server außerhalb eines App-Pakets
    (Jeremias' Einrichtung) lässt er immer in Ruhe. -> Server oder None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stand", timeout=3) as r:
            d = json.loads(r.read())
    except (OSError, ValueError):
        return None
    alt = d.get("code") or ""
    if not (d.get("paket") or ".app/" in alt + "/") or not isinstance(d.get("pid"), int):
        return None
    if alt == str(HIER) and d.get("code_stand") == CODE_STAND:
        return None                                   # derselbe Stand läuft schon
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


def main():
    global INDEX, PORT, MESSDIENST, ORDNER, KLANG, LEICHT
    ap = argparse.ArgumentParser(description="Open Libary: Sample-Bibliothek durchsuchen, abspielen, Netz-Packs laden")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ohne-browser", action="store_true")
    ap.add_argument("--ersetzen", action="store_true", help="alten Server derselben App nach einem Update beenden")
    a = ap.parse_args()
    PORT = a.port
    url = f"http://127.0.0.1:{a.port}/"
    try:
        srv = Server(("127.0.0.1", a.port), Handler)
    except OSError:
        srv = alten_server_ersetzen(a.port) if a.ersetzen else None
    if srv is None:
        print(f"Open Libary läuft schon: {url}")
        if not a.ohne_browser:
            webbrowser.open(url)
        return
    t0 = time.time()
    LEICHT = leicht.Leicht(leicht_befehl(), LEICHT_DB, None, leicht_kandidaten, leicht_neu_laden)
    INDEX = Index()
    MESSDIENST = Messdienst()

    def beenden(signum, frame):
        # "openlibary stopp" (pkill): laufende gehoer-Messungen mit beenden, sonst rechnen sie als Waisen weiter;
        # gehoer hält bei SIGTERM sauber an und löscht seinen Vermerk "in Arbeit"
        for pid in list(MESSDIENST._pids):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if MESSDIENST._pids and getattr(MESSDIENST, "liste_jetzt", None):
            time.sleep(1)                       # gehoer hat die Liste beim Start gelesen; danach ist sie übrig
            try:
                os.unlink(MESSDIENST.liste_jetzt)
            except OSError:
                pass
        os._exit(0)
    signal.signal(signal.SIGTERM, beenden)
    netz.KONFIG.update(ziel=NETZ_ZIEL, ziel_drums=DRUM_ZIEL, zusaetze=ZUSAETZE, md5_bestand=lambda: INDEX.md5, nach_laden=INDEX.laden,
                       konfig_ordner=KONFIG_ORDNER,
                       geprueft_urls=lambda: {r.get("quelle_url") for r in INDEX.urteile.values()},
                       fertig_datei=fertig_geladen)
    KLANG = klangnaehe.Klang(GEHOER_PY, GEHOER_CODE, KLANG_DB, MESS_PRIO, {
        "OPENLIBARY_KLANG_DB": str(KLANG_DB), "OPENLIBARY_BESTAND": str(BESTAND), "OPENLIBARY_ZUSAETZE": str(ZUSAETZE),
        "OPENLIBARY_EIGENE": str(EIGENE), "OPENLIBARY_GEHOER_CODE": str(GEHOER_CODE)})
    # Konten 03.11.: Looperman-Downloads aus dem Browser (nie unter „Frei (Netz)“, siehe netz.LOOPERMAN_LIZENZ)
    netz.KONFIG["looperman_ziel"] = Path(os.environ.get("OPENLIBARY_LOOPERMAN_ZIEL", MUSIK / "Samples/Looperman"
                                                        if _EIGENE_ABLAGE else ABLAGE / "Looperman"))
    netz.WAECHTER.starten()
    ORDNER = ordner.Ordner(ORDNER_LISTE, EIGENE, ordner_bekannt, ordner_eingelesen, netz.KONFIG["ffprobe"],
                           ohne=[nfc(str(RENDER_ZIEL))])     # gerechnete Fassungen sind keine Samples (Paket 3)
    LEICHT.starten()                             # misst im Hintergrund, nur Lücken, ein Prozess
    takt = threading.Timer(90, ordner_takt)      # erst nach dem Start einlesen, dann stündlich
    takt.daemon = True
    takt.start()
    print(f"Open Libary: {len(INDEX.zeilen)} Samples, {INDEX.gemessen} gemessen, geladen in {time.time() - t0:.1f} s",
          flush=True)
    nachholen = threading.Timer(60, MESSDIENST.nachholen)     # erst nach dem Start, der Mac hat dann Luft
    nachholen.daemon = True
    nachholen.start()
    print(f"Offen unter {url} (Strg+C beendet)", flush=True)
    if not a.ohne_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
