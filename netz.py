"""Netz-Teil der Open Libary: freie Samples suchen, prüfen, laden und einsortieren.

Quellen (alle ohne Schlüssel)
  archive.org   Sammlungen. Die Lizenz trägt der Hochladende selbst ein, dort liegen auch CD-Rips und Herstellerpacks
                als "CC0" (gemessen 19.10.2026: Nujabes-CD-Rip, Loopmasters-Breaks, Zero-G). verdacht() prüft Titel,
                Beschreibung, Urheber, Kennung; alles Geladene bleibt "ungeprüft".
  Openverse     Einzelsounds von Freesound (nur source=freesound: sonst kommen Jamendo-Songs und Wikimedia-Aussprachen
                mit). Ladbar ist nur die HQ-Vorschau als MP3 (128 kbps), das Original verlangt einen Freesound-Login.
                Antwortzeit 2 bis 45 s, 4 von 10 Suchen endeten am 20.10.2026 mit 502/504, der zweite Versuch kam oft
                nach 0.1 s: deshalb Frist 20 s, ein automatischer Neuversuch und ein Zwischenspeicher je Suche.
                Ohne Konto höchstens 20 je Seite und 240 Treffer insgesamt.
  Wikimedia     Commons, Originaldateien (WAV, FLAC, OGG) mit Lizenz je Datei. Nur CC0, gemeinfrei und CC BY; BY-SA und
                NC fallen raus, MIDI und Ausspracheaufnahmen (LL-…) auch.
  ccMixter      Acapellas, Stems und Samples als Original (FLAC/MP3) mit Lizenz je Upload. Nur CC BY und CC0 (der Großteil
                dort ist NC oder Sampling+). Höchstens 20 je Abfrage, mehr gibt eine leere Antwort; ebenso schnelle Folgen.

Laden
  Ein Pool mit LADE_PARALLEL Verbindungen arbeitet auf Dateiebene (archive.org seriell 0.053 Dateien/s, parallel 0.62);
  große Dateien in Stücken (446 statt 14 KB/s). Je Datei WIEDERHOLEN Versuche, Fortsetzen, Gesamtfrist; ein Fehler
  überspringt nur diese Datei. Jede fertige Datei wird sofort einsortiert (Art, Loop/One-Shot, BPM, Tonart, Dauer,
  Abtastrate, Kanäle) und in zusaetze.csv geschrieben; dazu ein Beleg (Metadaten zum Ladezeitpunkt) unter _belege/.
"""
import concurrent.futures
import csv
import hashlib
import html
import http.client
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

UA = {"User-Agent": "OpenLibary/1.2 (lokale Sample-Verwaltung)"}
http.client._MAXLINE = 1 << 20   # ccMixter schickt eine Kopfzeile über 64 KB, Python bricht sonst mit LineTooLong ab
LADE_PARALLEL = 6
WIEDERHOLEN = 3
GROSS_BYTES = 300 * 1024 * 1024     # darüber fragt die Oberfläche nach
AUDIO_ENDUNGEN = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg")
ZUSATZ_SPALTEN = ["datei", "pfad", "pack", "art", "tempo", "tonart", "dauer", "lizenz_gefunden", "format",
                  "loop_oneshot", "stimmung", "herkunft", "quelle_url", "lizenz_url", "geladen_am", "md5", "hinweis",
                  "quelldatei", "samplerate", "kanaele", "tags", "urheber", "quelle"]
EINZEL_ORDNER = "Einzelsounds"

# wird von openlibary.py gesetzt
import plattform     # 03.11.2026: Mac, Windows, Linux

KONFIG = {"ziel": None, "ziel_drums": None, "zusaetze": None, "konfig_ordner": plattform.konfigordner(),
          # Konten 03.11.2026: Looperman-Downloads aus dem Browser kommen aus „downloads“ in „looperman_ziel“
          "downloads": Path(os.environ.get("OPENLIBARY_DOWNLOADS", Path.home() / "Downloads")), "looperman_ziel": None, "ffprobe": plattform.werkzeug("ffprobe") or "ffprobe",
          "md5_bestand": set, "nach_laden": lambda: None, "geprueft_urls": lambda: set(),
          "fertig_datei": lambda pfade: None}    # bekommt jede fertig einsortierte Datei (openlibary: gehoer misst sie)


class NichtGefunden(Exception):
    pass


class QuelleFehler(Exception):
    """Quelle drosselt, antwortet nicht oder die Eingabe taugt nicht: als 400/429/503/504 statt 500 melden."""
    def __init__(self, text, code=503):
        super().__init__(text)
        self.code = code


def _holen(url, timeout=25, versuche=1, name="Quelle"):
    """JSON holen; bei Zeitüberschreitung, 5xx oder leerer Antwort nach kurzer Pause noch einmal."""
    letzter = None
    for v in range(versuche + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                roh = r.read()
            if not roh.strip():
                raise ValueError("leere Antwort")
            return json.loads(roh.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 429):
                raise
            letzter = e
            if e.code < 500:
                break
        except (urllib.error.URLError, OSError, ValueError) as e:
            letzter = e
        if v < versuche:
            time.sleep(1.0)
    if "timed out" in str(letzter):
        raise QuelleFehler(f"{name} antwortet nicht (Zeitüberschreitung nach {timeout} s"
                           + (", zweimal versucht)" if versuche else ")"), 504)
    raise QuelleFehler(f"{name} antwortet nicht: {letzter}", 503)


# alte Schnittstelle für openlibary.py und Tests
def http_json(url, timeout=25):
    return _holen(url, timeout, 0)


_SUCHE_CACHE = {}       # (quelle, q, lizenz, seite, extra) -> (Zeit, Ergebnis)
_SUCHE_LOCK = threading.Lock()


def _gemerkt(schluessel, rechnen, dauer=1800):
    with _SUCHE_LOCK:
        t = _SUCHE_CACHE.get(schluessel)
    if t and time.time() - t[0] < dauer:
        return t[1]
    erg = rechnen()
    with _SUCHE_LOCK:
        _SUCHE_CACHE[schluessel] = (time.time(), erg)
        if len(_SUCHE_CACHE) > 400:
            for k in sorted(_SUCHE_CACHE, key=lambda k: _SUCHE_CACHE[k][0])[:150]:
                del _SUCHE_CACHE[k]
    return erg


def lizenz_art(url, kurz=""):
    url, kurz = (url or "").lower(), (kurz or "").lower()
    if "publicdomain/mark" in url or kurz == "pdm":
        return "pdm"      # Public Domain Mark: eine Behauptung, keine Lizenz
    if "publicdomain" in url or kurz == "cc0":
        return "cc0"
    if kurz in ("public domain", "pd") or kurz.startswith(("pd-", "pd ")):
        return "pd"       # "gemeinfrei" ohne Lizenzadresse (Wikimedia): behauptet, nicht CC0 (Prüfer 20.10.: Liszt-Aufnahme)
    if re.search(r"/licenses/by/|^cc[ -]by[ -]\d", url or kurz) and not re.search(r"-sa|-nc|-nd|sampling", url + kurz):
        return "ccby"
    if "/licenses/" in url or kurz.startswith("cc"):
        return "andere"
    return "keine"


def suchtext_pruefen(q):
    q = re.sub(r"\s+", " ", (q or "").strip())
    if not q:
        raise QuelleFehler("Bitte einen Suchbegriff eingeben", 400)
    return q[:200]


# ================================================================ Einsortieren

_ARTEN = [   # Reihenfolge zählt: das Erste, was passt. Atmo, Foley, Glitch vor den Drums (Test 20.10.: Türklingeln
             # wurden "melodisch", "Glitch Loop" wurde "kick", Regen blieb ohne Art)
    # mit Wortgrenzen: ohne sie wurden "Velocity Lead", "Sine wave", "Electricity" und "City Pop Chords" zu Atmo (20.10.)
    ("atmo", r"\bambiences?\b|\batmos?\b|field[\s_-]?record|room[\s_-]?tone|\brain\b|\bwind\b|\bthunder|\bstorm\b|"
             r"\bforest\b|\bbirds?\b|\bcity (ambience|street|noise|traffic|sounds?)|\bstreet\b|\btraffic\b|\bcrowd\b|"
             r"\bocean\b|ocean waves|\briver\b|\bnature\b"),
    ("foley", r"foley|footsteps?|\bdoor|knock|door[\s_-]?bell|\bkeys? jingl|cloth|glass break|breaking glass|\bpaper\b"),
    ("glitch", r"glitch|stutter|bitcrush|digital error"),
    ("fx", r"sound[\s_-]?effect|explosion|gun[\s_-]?shot|\bengine\b|\bcars?\b|crashing|\bsiren|church[\s_-]?bells?|alarm"),
    ("808", r"808"), ("kick", r"kick|\bbd\b|bassdrum"), ("snare", r"snare|\bsnr\b|\bsd\b"), ("clap", r"clap"),
    ("rim", r"\brim"), ("hat", r"hi[\s_-]?hat|\bhh\b|\bhats?\b|\boh\b"),
    ("cymbal", r"cymbal|crash|\bride\b|splash|china"), ("tom", r"\btoms?\b"),
    ("perc", r"perc|shaker|conga|bongo|tamb|cowbell|clave|agogo|\bblock|guiro|cabasa|triangle"),
    ("break", r"break(?!ing)|drum[\s_-]?loop|beat\b|groove|rhythm"),
    ("fx", r"\bfx\b|riser|impact|sweep|noise|whoosh|swoosh|sfx|texture|downlifter|uplifter|reverse|vinyl|crackle|hiss"),
    ("vocal", r"vocal|\bvox\b|chop|acapella|acappella|voice|\bsing"), ("bass", r"\bbass|\bsub\b"),
    ("melodisch", r"pad|keys|piano|synth|guitar|melod|chord|lead|pluck|string|organ|bell|brass|flute|rhodes"),
]
_KEIN_LOOP_VON_SELBST = {"atmo", "foley", "fx", "glitch"}
_TONART = re.compile(r"(?:^|(?<=[\s_\-(\[]))([A-G])(#|b|[Ss]harp|[Ff]lat)?[\s_-]?(min|Min|MIN|minor|Minor|maj|Maj|major|Major|m|moll|Moll|dur|Dur)?(?=[\s_\-.)\]]|$)")
_BPM = re.compile(r"(?<!\d)(\d{2,3})[\s_-]?bpm|bpm[\s_-]?(\d{2,3})(?!\d)", re.I)
_BPM_FREI = re.compile(r"(?:^|[\s_-])(\d{2,3})(?=[\s_-]|$)")


def _getrennt(s):
    """'HiTom' -> 'Hi Tom', 'ClosedHH' -> 'Closed HH', 'kick_loop' -> 'kick loop' (Prüfer 19.10.: HiTom ohne Art)"""
    s = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", s)
    s = re.sub(r"(?i)(hat)(open|closed)", r"\1 \2", s)
    return s.lower().replace("_", " ")


def einordnen(name, ordner="", dauer=None):
    """Art, Loop/One-Shot, BPM, Tonart aus Datei- und Ordnername (wie bestand.csv), dazu die Dauer. Die Art kommt
    zuerst aus dem Dateinamen; der Ordner (oder die Tags) nur, wenn der Name nichts sagt."""
    name_text, ordner_text = _getrennt(os.path.splitext(name)[0]), _getrennt(ordner)
    text = f"{ordner_text} {name_text}"
    art = next((a for a, rx in _ARTEN if re.search(rx, name_text)), "")
    # „Sn 808“, „808 CH“, „808 Cowbell“: ein Schlag der Drum-Machine TR-808, kein 808-Bass (Nutzertest 2: „Sn 808.wav“
    # stand bei „808“ ganz oben). Nennt der Name daneben einen anderen Schlag, gilt der.
    if art == "808" and re.search(r"\b(sn|snr|sd|snare|clap|cp|hh|ch|oh|hats?|cow ?bell|cb|conga|cg|toms?|lt|mt|ht|rim|rs|"
                                  r"clave|cl|cymbal|cy|crash|ride|perc|shaker|maracas?|ma)\b", name_text):
        ohne_808 = re.sub(r"808", " ", name_text)
        ohne_808 = re.sub(r"\bsn\b|\bsnr\b", " snare ", re.sub(r"\b(ch|oh)\b", " hat ", re.sub(r"\bcp\b", " clap ", ohne_808)))
        ohne_808 = re.sub(r"\b(cb|cow ?bell|conga|cg|clave|cl|ma|maracas?)\b", " perc ", ohne_808)
        ohne_808 = re.sub(r"\b(lt|mt|ht)\b", " tom ", re.sub(r"\b(cy)\b", " cymbal ", re.sub(r"\brs\b", " rim ", ohne_808)))
        art = next((a for a, rx in _ARTEN if re.search(rx, ohne_808)), art)
    if not art and re.search(r"\bdrums?\b|\bbeat\b", name_text) and (dauer or 0) >= 2.5:
        art = "break"          # "Epic Tribal Drums" vor den Tags prüfen (ein Tag "bass" machte daraus Bass)
    art = art or next((a for a, rx in _ARTEN if re.search(rx, ordner_text)), "")
    bpm = None
    m = _BPM.search(name) or _BPM.search(ordner)
    if m:
        bpm = int(m.group(1) or m.group(2))
    ist_loop = "loop" in name_text or (dauer is not None and dauer >= 2.5 and art in ("break", "melodisch", "vocal", "bass"))
    if bpm is None and ist_loop:
        for kand in _BPM_FREI.findall(os.path.splitext(name)[0]):
            if 60 <= int(kand) <= 200:
                bpm = int(kand)
                break
    if bpm is not None and not 50 <= bpm <= 300:
        bpm = None
    if art in _KEIN_LOOP_VON_SELBST:          # FX und Atmos werden nicht von der Länge zum Loop
        lo = "loop" if "loop" in name_text else ("oneshot" if dauer is not None and dauer < 10 else "")
    elif art in ("break", "vocal"):           # kurze Breaks und Chops sind trotzdem keine One-Shots (Test 20.10.)
        lo = "loop" if ist_loop or bpm or (dauer or 0) >= 1.5 else ""
    else:
        lo = "loop" if ist_loop or (bpm and dauer and dauer >= 1.9) else ("oneshot" if dauer is not None and dauer < 2.5 else "")
    if not art and re.search(r"drums?\b|perc", text) and (lo == "loop" or (dauer or 0) >= 2.5):
        art, lo = "break", "loop"
    if not art and dauer is not None and dauer >= 30:
        art = "atmo"                          # lange Aufnahme ohne Hinweis: als Field Recording führen
    tonart = ""
    if art not in ("kick", "snare", "clap", "rim", "hat", "cymbal", "perc", "break", "fx", "atmo", "foley", "glitch"):
        t = _TONART.search(os.path.splitext(name)[0])
        # ein nackter Buchstabe gilt nur zwischen _ oder - ("Loop_C_120"), nicht im Satz ("I am a dreamer")
        if t and not (t.group(2) or t.group(3)):
            vor = name[t.start(1) - 1] if t.start(1) else ""
            if vor not in "_-([":
                t = None
        if t:
            vor = (t.group(2) or "").lower()
            vor = "#" if vor in ("#", "sharp") else "b" if vor in ("b", "flat") else ""
            tonart = t.group(1).upper() + vor + ("m" if (t.group(3) or "").lower() in ("m", "min", "minor", "moll") else "")
    return {"art": art, "lo": lo, "bpm": bpm, "tonart": tonart}


def messen(pfad):
    """Dauer, Abtastrate, Kanäle per ffprobe."""
    try:
        r = subprocess.run([KONFIG["ffprobe"], "-v", "error", "-show_entries", "format=duration:stream=sample_rate,channels",
                            "-select_streams", "a:0", "-of", "json", str(pfad)], capture_output=True, text=True, timeout=20,
                           **plattform.hilfsprozess_args())
        d = json.loads(r.stdout or "{}")
        st = (d.get("streams") or [{}])[0]
        return {"dauer": round(float(d.get("format", {}).get("duration")), 3) if d.get("format", {}).get("duration") else None,
                "samplerate": st.get("sample_rate") or "", "kanaele": st.get("channels") or ""}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"dauer": None, "samplerate": "", "kanaele": ""}


def dauer_messen(pfad):
    return messen(pfad)["dauer"]


# ================================================================ Lizenz-Verdacht

_VERDACHT = [   # starke Hinweise auf fremdes Material
    (r"\brip\b|\bripped\b|cd[\s_-]?rip|vinyl[\s_-]?rip|\bflac rip|\b320\s?kbps|not ripped by me", "Rip von CD oder Platte"),
    (r"from (the )?vinyl|from records?|sampled from|resampled|off (the )?records?|von platte|sped up some", "aus Platten gesampelt"),
    (r"\balbum\b|\bdiscography|greatest hits|soundtrack|\bost\b|\blabel:|\brecords\b|gravadora|\bselo\b|"
     r"released by|lançado", "Album oder Label-Veröffentlichung"),
    # enger gefasst (Prüfer 20.10.: "Mixed in Audacity", "Radio static", "youtube podcast" waren Fehlalarme)
    (r"mixed by|mixtape|\bdj[\s-]?mix|\bdj set\b|mashup|bootleg|\bremix(ed)?\b", "Mix oder Mashup"),
    (r"\bepisode\s*\d|podcast episode|radio show|broadcast|\blive at\b|\blive in\b|in concert|\binterview\b", "Sendung oder Mitschnitt"),
    (r"all rights reserved|respective owners|copyright ©|℗", "Rechtevorbehalt im Text"),
    # Hersteller nur als ganzes Wort und nie im Kontonamen ("SpliceSound" ist ein Freesound-Konto)
    (r"\bloopmasters\b|\bzero-?g\b|\bsplice\b|\bcymatics\b|\bbest service\b|\bsample magic\b|\bvengeance\b|"
     r"\bbig fish audio\b|\bprime loops\b|\bghost syndicate\b|\bcapsun\b|\bproducer loops\b|\bueberschall\b|"
     r"\bspectrasonics\b|\bpixabay\b|\bmixkit\b|\bzapsplat\b|\bsoundsnap\b|\benvato\b|\baudiojungle\b|\bepidemic sound\b",
     "Name eines Herstellers oder Shops"),
    (r"funky drummer|\bamen\b|think break|\(think\)|apache|impeach|levee|skull snaps|synthetic substitution|"
     r"ultimate breaks|james brown|soul pride|famous[\s_-]sample|clyde stubblefield", "bekannter Plattenbreak"),
]


def _jahr(datum, beschreibung):
    """Aufnahmejahr aus dem Datumsfeld, aus der Beschreibung nur mit "Year:" oder "recorded in" davor (Prüfer 20.10.:
    "Rhythm Ace von 1972" ist das Baujahr des Geräts, nicht das Aufnahmejahr)."""
    m = re.match(r"\s*((?:18[89]|19\d|20[0-2])\d)\b", datum or "")
    if not m:
        m = re.search(r"(?:year:?\s*|recorded (?:in |on )?)((?:18[89]|19\d|20[0-2])\d)\b", beschreibung or "", re.I)
    return int(m.group(1)) if m else None


def verdacht(titel, beschreibung="", urheber="", groesse=0, datum="", kennung="", tags=()):
    """(gruende, hinweise): Gründe, warum ein angebliches CC0 vermutlich fremdes Material ist, und schwache Hinweise.
    Leer heißt: nichts gefunden, nicht: sicher."""
    text = f"{titel} {beschreibung} {urheber} {kennung.replace('_', ' ').replace('-', ' ')}".lower()
    ohne_konto = f"{titel} {beschreibung}".lower()
    gruende = [g for rx, g in _VERDACHT if re.search(rx, ohne_konto if g.startswith("Name eines") else text)]
    # Tags nur auf Hersteller und Plattenbreaks prüfen: "remix" (ccMixter, jeder Upload) und "soundtrack" (Freesound:
    # passt in Filmmusik) sind dort Allerweltswörter (Test 20.10.)
    tag_text = " ".join(tags).lower()
    gruende += [g for rx, g in _VERDACHT[-2:] if re.search(rx, tag_text) and g not in gruende]
    hinweise = []
    if groesse and groesse > 400_000_000:
        hinweise.append("über 400 MB")
    j = _jahr(datum, beschreibung)
    grenze = time.localtime().tm_year - 101      # USA: Aufnahmen werden 100 Jahre nach dem Jahresende gemeinfrei
    if j and 1880 <= j <= grenze:
        hinweise.append(f"Aufnahme von {j}: in den USA gemeinfrei, Komposition und EU-Recht prüfen")
    elif j and grenze < j < 1975:
        gruende.append(f"Aufnahme von {j}: in den USA noch geschützt")
    return list(dict.fromkeys(gruende)), hinweise


# ================================================================ Relevanz-Agent

OLLAMA_EMBED = "http://127.0.0.1:11434/api/embed"
EMBED_MODELL = "bge-m3"          # lokales Bedeutungsmodell (Ollama), gratis und offline
_EMB = {}                        # Text -> Vektor
_EMB_LOCK = threading.Lock()
_EINSCHUSS = {"kick", "snare", "clap", "rim", "hat", "cymbal", "tom", "perc", "808"}
_MUELL = re.compile(r"(?i)\b(test|untitled|new recording|recording \d+|audio \d+|voice memo|track \d+)\b")
_VERWANDT = {"kick": {"808"}, "808": {"kick", "bass"}, "hat": {"cymbal"}, "cymbal": {"hat"}, "perc": {"tom", "rim"},
             "snare": {"clap", "rim"}, "clap": {"snare"}, "break": {"perc"}, "fx": {"atmo", "glitch"}, "atmo": {"fx", "foley"}}
_GERAEUSCH = re.compile(r"(?i)\b(punch|fist|karate|martial|fight|football|soccer|ball|door|horse|kung ?fu|body ?hit|"
                        r"slap|stomp on|kicking (a|the)|sports?)\b")


def _einbetten(texte):
    """Vektoren für Texte aus bge-m3; None, wenn Ollama nicht läuft (dann zählen nur die Regeln)."""
    with _EMB_LOCK:
        fehlt = [t for t in dict.fromkeys(texte) if t not in _EMB]
    if fehlt:
        try:
            req = urllib.request.Request(OLLAMA_EMBED, data=json.dumps({"model": EMBED_MODELL, "input": fehlt}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                vektoren = json.loads(r.read())["embeddings"]
        except (OSError, ValueError, KeyError):
            return None
        with _EMB_LOCK:
            for t, v in zip(fehlt, vektoren):
                n = sum(x * x for x in v) ** 0.5 or 1.0
                _EMB[t] = [x / n for x in v]
            if len(_EMB) > 20000:
                _EMB.clear()
    with _EMB_LOCK:
        return [_EMB.get(t) for t in texte]


def relevanz(q, treffer, text_fn, sammlung=False):
    """Bewertet Treffer 0 bis 100 für Produzenten: Bedeutungsnähe zur Suche (bge-m3) plus Regeln zu Art, Länge,
    Original oder Vorschau, Tempo und Tonart, Verdacht, Müll-Titeln. Schreibt relevanz und warum (Gründe) in jeden
    Treffer. Die Zahl ist über alle Quellen vergleichbar (feste Abbildung der Ähnlichkeit, keine Normierung je Liste)."""
    if not treffer:
        return treffer
    absicht = einordnen(q, "", None)
    q_art, q_loop = absicht["art"], bool(re.search(r"(?i)\bloops?\b|\bbreak", q))
    vek = _einbetten([q.lower()] + [text_fn(t) for t in treffer])
    for i, t in enumerate(treffer):
        punkte, warum = 0.0, []
        if vek and vek[0] and vek[i + 1]:
            aehnlich = sum(a * b for a, b in zip(vek[0], vek[i + 1]))
            sem = min(1.0, max(0.0, (aehnlich - 0.38) / 0.32))     # 0.38 ~ beliebig, 0.70 ~ sehr nah (gemessen)
            punkte += 55 * sem
            if sem > 0.6:
                warum.append("passt zur Suche")
            elif sem < 0.2:
                warum.append("inhaltlich weit weg")
        else:
            # ohne Bedeutungsmodell (Ollama aus, andere Rechner): stehen alle Suchwörter in Titel oder Tags, zählt es wie
            # „passt“, sonst bleibt der Treffer unter der Schwelle der Seite (Prüfung 03.11.: vorher pauschal 40)
            inhalt = (text_fn(t) or "").lower()
            alle = all(re.search(r"(?<![a-z0-9])" + re.escape(w), inhalt) for w in re.findall(r"\w+", q.lower()) if len(w) > 1)
            punkte += 40 if alle else 5
            warum.append("alle Suchwörter im Titel" if alle else "Suchwörter fehlen im Titel")
        art, dauer = t.get("art") or "", t.get("dauer")
        if q_art and art == q_art:
            punkte += 18
            warum.append(f"Art {art} wie gesucht")
        elif q_art and art in _VERWANDT.get(q_art, ()):
            punkte += 10                                   # 808 bei "kick", Becken bei "hat"
            warum.append(f"{art}, verwandt mit {q_art}")
        elif q_art and art and art != q_art and not sammlung:
            punkte -= 10
            warum.append(f"ist {art}, gesucht {q_art}")
        if not sammlung and dauer is not None:
            if q_art in _EINSCHUSS and not q_loop:
                if dauer <= 3:
                    punkte += 10
                    warum.append("kurz wie ein One-Shot")
                elif dauer > 10:
                    punkte -= 15
                    warum.append("zu lang für einen One-Shot")
            elif q_loop or q_art in ("break", "melodisch", "bass", "vocal"):
                if 1.5 <= dauer <= 60:
                    punkte += 6
                elif dauer > 180:
                    punkte -= 8
                    warum.append("ganzer Song, kein Loop")
            if dauer > 600 and q_art != "atmo":
                punkte -= 10
                warum.append("über 10 Minuten")
        if not sammlung:
            if t.get("format") in ("wav", "flac", "aiff"):
                punkte += 6
                warum.append("Original in voller Qualität")
            elif t.get("quelle") == "freesound":
                warum.append("nur MP3-Vorschau")
            if t.get("bpm") or t.get("tonart"):
                punkte += 4
        else:
            dl = t.get("downloads") or 0
            if dl:
                punkte += min(12, 3 * (len(str(int(dl))) - 1))    # grob nach Größenordnung der Downloads
            if (t.get("groesse") or 0) > 400_000_000:
                punkte -= 8
        if t.get("verdacht"):
            punkte -= 40
            warum.append("Verdacht: " + t["verdacht"][0])
        if t.get("lizenz") in ("pd", "pdm"):
            punkte -= 8
        if _MUELL.search(t.get("titel") or ""):
            punkte -= 10
            warum.append("Titel ohne Aussage")
        if q_art in _EINSCHUSS and _GERAEUSCH.search(f"{t.get('titel', '')} {' '.join(t.get('tags') or [])}"):
            punkte -= 25                                   # "Fist Punch or kick" ist ein Schlag, keine Bassdrum
            warum.append("Geräusch, kein Drum-Sample")
        t["relevanz"] = int(max(0, min(100, round(punkte + 15))))
        t["warum"] = warum[:4]
    treffer.sort(key=lambda t: -t["relevanz"])
    return treffer


def _text_einzel(t):
    return f"{t.get('titel', '')}. {t.get('art', '')}. {', '.join((t.get('tags') or [])[:8])}".lower()[:400]


def _text_sammlung(t):
    return f"{t.get('titel', '')}. {(t.get('beschreibung') or '')[:240]}".lower()


# ================================================================ archive.org

LIZENZ_FILTER = {
    "cc0": "licenseurl:(*publicdomain*)",
    "ccby": "licenseurl:(*publicdomain* OR *licenses\\/by\\/*)",   # Schrägstrich unmaskiert = Syntaxfehler
}
NUR_SAMPLES = ('(subject:(samples OR sample OR "sample pack" OR drumkit OR "drum kit" OR loops OR oneshots '
               'OR "drum machine" OR "sound effects" OR sfx OR "field recording" OR ambience OR foley OR "sound effect") '
               'OR title:(samples OR sample OR kit OR loops OR "sample pack" OR sfx OR "sound effects"))')
OHNE_SAMMLUNGEN = ("NOT collection:(librivoxaudio OR podcasts OR audio_podcast OR radioprograms OR oldtimeradio "
                   "OR audio_bookspoetry OR netlabels)")


def _lucene_sauber(text):
    t = re.sub(r'[+\-!(){}\[\]^"~*?:\\/&|]', " ", text)
    t = re.sub(r"\b(AND|OR|NOT|TO)\b", " ", t)          # Operatoren gelten als Wörter (Test 20.10.: "NOT door")
    return re.sub(r"\s+", " ", t).strip()


def ordner_name(titel, quelle="archive.org"):
    s = re.sub(r'[/\\:*?"<>|]+', " ", titel or "").strip()
    return (re.sub(r"\s+", " ", s)[:80] or "ohne Titel") + f" ({quelle})"


def _zusaetze_zeilen():
    zus = KONFIG["zusaetze"]
    if not zus or not Path(zus).is_file():
        return []
    with open(zus, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _geladene_quelldateien():
    """(archive-Kennung, Dateiname im Original) aller schon geladenen Dateien, auch aus entpackten ZIPs."""
    aus = set()
    for r in _zusaetze_zeilen():
        m = re.search(r"archive\.org/details/([^/?#]+)", r.get("quelle_url") or "")
        if m and r.get("quelldatei"):
            aus.add((m.group(1), r["quelldatei"]))
    return aus


def _schon_geladen_ids():
    ids = set()
    for r in _zusaetze_zeilen():
        m = re.search(r"archive\.org/details/([^/?#]+)", r.get("quelle_url") or "")
        if m:
            ids.add(m.group(1))
    for url in KONFIG["geprueft_urls"]():
        m = re.search(r"archive\.org/details/([^/?#]+)", url or "")
        if m:
            ids.add(m.group(1))
    return ids


def _geladene_seiten():
    return {r.get("quelle_url") for r in _zusaetze_zeilen() if r.get("quelle_url")}


def archive_suche(q, lizenz="cc0", seite=1, nur_samples=True):
    q = suchtext_pruefen(q)
    text = _lucene_sauber(q)
    if not text:
        raise QuelleFehler("Der Suchbegriff besteht nur aus Sonderzeichen", 400)

    def abfrage(woerter_oder):
        kern = " OR ".join(text.split()) if woerter_oder else text
        a = f"({kern}) AND mediatype:audio AND {LIZENZ_FILTER.get(lizenz, LIZENZ_FILTER['cc0'])} AND {OHNE_SAMMLUNGEN}"
        if nur_samples:
            a += " AND " + NUR_SAMPLES
        params = [("q", a), ("rows", "40"), ("page", str(max(1, seite))), ("output", "json"), ("sort[]", "downloads desc")]
        for f in ("identifier", "title", "licenseurl", "downloads", "item_size", "creator", "date", "description",
                  "uploader", "publicdate"):
            params.append(("fl[]", f))
        for versuch in range(2):   # BACKEND_ERROR kommt dort zeitweise (Prüfer 20.10.: "sitar" mit CC BY)
            d = _holen("https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params), 25, 1, "archive.org")
            if "response" in d:
                return d["response"]
            time.sleep(1.5)
        raise QuelleFehler("archive.org hat gerade einen internen Fehler bei dieser Suche; gleich nochmal versuchen", 503)

    def rechnen():
        antwort, gelockert = abfrage(False), False
        if not antwort.get("numFound") and len(text.split()) > 1:   # alle Wörter: oft 0 (Test 20.10.: 3 von 5)
            antwort, gelockert = abfrage(True), True
        return antwort, gelockert

    antwort, gelockert = _gemerkt(("archive", text.lower(), lizenz, seite, nur_samples), rechnen)
    geladen = _schon_geladen_ids()
    treffer = []
    for doc in antwort.get("docs", []):
        beschr = doc.get("description") or ""
        beschr = " ".join(beschr) if isinstance(beschr, list) else beschr
        beschr = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", beschr))).strip()
        urheber = doc.get("creator") or ""
        urheber = ", ".join(urheber) if isinstance(urheber, list) else urheber
        titel = doc.get("title") or doc.get("identifier")
        titel = titel if isinstance(titel, str) else titel[0]
        datum = (doc.get("date") or "")[:10]
        gruende, hinweise = verdacht(titel, beschr, urheber, doc.get("item_size") or 0, datum, doc.get("identifier") or "")
        treffer.append({
            "id": doc.get("identifier"), "titel": titel, "lizenz_url": doc.get("licenseurl") or "",
            "lizenz": lizenz_art(doc.get("licenseurl")), "downloads": doc.get("downloads") or 0,
            "groesse": doc.get("item_size") or 0, "urheber": urheber, "datum": datum,
            "konto": doc.get("uploader") or "", "beschreibung": beschr[:400],
            "geladen": doc.get("identifier") in geladen, "verdacht": gruende, "hinweise": hinweise,
        })
    relevanz(q, treffer, _text_sammlung, sammlung=True)     # Verdächtige sinken dabei nach unten
    if gelockert:
        # Musiker-Test 03.11.2026: „amen break“ (CC0) fand nichts mit beiden Wörtern, gelockert kamen 40 Stadtgeräusche
        # mit „Break“ im Titel und bis 46 Punkten. Was nicht alle Suchwörter enthält, sinkt unter die Schwelle der Seite
        # (30) und bleibt nur über „zeigen“ sichtbar.
        def gleich(text):    # "Lo-Fi" wie "lofi", "drums" wie "drum"
            return re.sub(r"lo[\s_-]?fi", "lofi", re.sub(r"hi[\s_-]?hat", "hihat", text.lower()))
        such = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"\w+", gleich(q)) if len(w) > 1]
        for t in treffer:
            inhalt = gleich(f"{t['titel']} {t['beschreibung']}")
            # am Wortanfang: „amen“ soll nicht in „Sacramento“ stecken, „break“ nicht in „Daybreak“ (Prüfung 03.11.)
            if not all(re.search(r"(?<![a-z0-9])" + re.escape(w), inhalt) for w in such):
                t["relevanz"] = min(t["relevanz"], 20)
                t["warum"] = ["enthält nicht alle Suchwörter"] + t["warum"][:3]
        treffer.sort(key=lambda t: -t["relevanz"])
    vorausladen([t["id"] for t in treffer if not t["verdacht"]][:8])
    return {"gesamt": antwort.get("numFound", 0), "treffer": treffer, "gelockert": gelockert, "seite": seite}


_META = {}            # Kennung -> (Zeit, Metadaten); archive.org brauchte am 19.10.2026 bis 60 s je Dateiliste
_META_LOCK = threading.Lock()
_VORAUS = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="voraus")


def _metadaten(ident):
    with _META_LOCK:
        t = _META.get(ident)
    if t and time.time() - t[0] < 900:
        return t[1]
    d = _holen("https://archive.org/metadata/" + urllib.parse.quote(ident), 90, 1, "archive.org")
    with _META_LOCK:
        _META[ident] = (time.time(), d)
        if len(_META) > 300:
            for k in sorted(_META, key=lambda k: _META[k][0])[:100]:
                del _META[k]
    return d


def vorausladen(idents):
    for i in idents:
        _VORAUS.submit(lambda i=i: _metadaten(i))


def archive_dateien(ident):
    if not re.match(r"^[A-Za-z0-9._-]+$", ident or ""):
        raise NichtGefunden("ungültige Kennung")
    d = _metadaten(ident)
    if not d or not d.get("metadata"):
        # archive.org liefert für gesperrte Einträge eine leere Antwort (Test 20.10.: JV-11759-1937)
        raise NichtGefunden(f"{ident} ist bei archive.org nicht mehr abrufbar, vermutlich wegen Rechten zurückgezogen")
    md = d["metadata"]
    bestand = KONFIG["md5_bestand"]()
    dateien, archive = [], []
    for f in d.get("files", []):
        name = f.get("name", "")
        if f.get("source") != "original":
            continue
        url = f"https://archive.org/download/{urllib.parse.quote(ident)}/{urllib.parse.quote(name)}"
        groesse = int(f.get("size") or 0)
        if name.lower().endswith(AUDIO_ENDUNGEN):
            e = einordnen(os.path.basename(name), os.path.dirname(name), zahl_oder_none(f.get("length")))
            dateien.append({"name": name, "groesse": groesse, "url": url, "md5": f.get("md5") or "",
                            "schon_da": (f.get("md5") or "") in bestand, "art": e["art"], "lo": e["lo"],
                            "bpm": e["bpm"], "tonart": e["tonart"], "dauer": zahl_oder_none(f.get("length"))})
        elif name.lower().endswith(".zip"):
            archive.append({"name": name, "groesse": groesse, "url": url, "md5": f.get("md5") or "", "zip": True,
                            "schon_da": (ident, name) in _geladene_quelldateien(), "art": "zip", "lo": "",
                            "dauer": None})
        elif name.lower().endswith((".rar", ".7z")):
            archive.append({"name": name, "groesse": groesse, "url": url, "zip": False, "art": "archiv",
                            "schon_da": False, "nicht_lesbar": "RAR und 7z kann die Open Libary nicht entpacken"})
    titel = md.get("title") or ident
    urheber = md.get("creator") or ""
    beschr = md.get("description") or ""
    beschr = " ".join(beschr) if isinstance(beschr, list) else beschr
    gruende, hinweise = verdacht(str(titel), re.sub(r"<[^>]+>", " ", beschr), str(urheber), 0, md.get("date") or "", ident)
    return {"id": ident, "quelle": "archive.org", "titel": titel if isinstance(titel, str) else titel[0],
            "lizenz_url": md.get("licenseurl") or "", "lizenz": lizenz_art(md.get("licenseurl")),
            "urheber": urheber if isinstance(urheber, str) else ", ".join(urheber),
            "konto": md.get("uploader") or "", "hochgeladen": md.get("publicdate") or "",
            "seite": f"https://archive.org/details/{ident}", "verdacht": gruende, "hinweise": hinweise,
            "metadaten": md, "dateien": dateien + archive}


def zahl_oder_none(s):
    try:
        if s in (None, ""):
            return None
        if isinstance(s, str) and ":" in s:   # archive.org schreibt Längen teils als mm:ss
            teile = [float(x) for x in s.split(":")]
            return round(sum(t * 60 ** i for i, t in enumerate(reversed(teile))), 3)
        return round(float(s), 3)
    except ValueError:
        return None


# ================================================================ Einzelsounds: Freesound (Openverse), Wikimedia, ccMixter

_EINZEL = {}      # "quelle:id" -> Treffer aus der eigenen Antwort; geladen wird nur, was hier steht
_ENDUNG = {"zip": "zip", "mp32": "mp3", "mpeg": "mp3", "mp3": "mp3", "wav": "wav", "x-wav": "wav", "wave": "wav", "flac": "flac",
           "x-flac": "flac", "ogg": "ogg", "oga": "ogg", "aiff": "aiff", "aif": "aiff", "x-aiff": "aiff"}


def quell_kopf(url):
    """ccMixter sperrt Abrufe ohne eigenen Referer (Hotlink-Schutz, Prüfer 20.10.: 403 bei jeder Datei)."""
    return {"Referer": "https://ccmixter.org/"} if "ccmixter.org/" in (url or "") else {}


def _endung(fmt, url=""):
    f = (fmt or "").lower().split("/")[-1]
    if f in _ENDUNG:
        return _ENDUNG[f]
    e = os.path.splitext(urllib.parse.urlparse(url).path)[1].lstrip(".").lower()
    return _ENDUNG.get(e, "mp3")


def _einzel_treffer(quelle, ident, titel, urheber, lizenz, lizenz_url, seite, url, fmt, dauer, groesse, tags,
                    beschreibung="", bpm=None, hinweis=""):
    titel = (titel or "").strip()
    e = einordnen(titel, " ".join(tags[:8]), dauer)
    gruende, hinweise = verdacht(titel, beschreibung, urheber, 0, "", "", tags)
    if lizenz == "pdm":
        gruende.append("nur Public Domain Mark: eine Behauptung, keine Lizenz")
    if lizenz == "pd":
        gruende.append("nur „gemeinfrei“ behauptet, ohne Lizenzadresse: Aufnahme und Komposition prüfen")
    t = {"schluessel": f"{quelle}:{ident}", "quelle": quelle, "id": str(ident), "titel": titel, "urheber": urheber or "",
         "lizenz": lizenz, "lizenz_url": lizenz_url or "", "seite": seite or "", "url": url, "format": _endung(fmt, url),
         "dauer": dauer, "groesse": groesse or 0, "tags": [x for x in tags if x][:10], "art": e["art"], "lo": e["lo"],
         "bpm": bpm or e["bpm"], "tonart": e["tonart"], "verdacht": gruende, "hinweise": hinweise, "hinweis": hinweis,
         "geladen": False}
    _EINZEL[t["schluessel"]] = t
    return t


def _geladen_markieren(treffer):
    seiten = _geladene_seiten()
    for t in treffer:
        t["geladen"] = bool(t["seite"]) and t["seite"] in seiten
    return treffer


# ---------------------------------------------------------------- Freesound direkt (Musiker-Test 03.11.2026)
# Über Openverse brauchte „808“ 29 s und „rhodes chord“ endete mit 504; dazu gab es nur die MP3-Vorschau. Mit einem
# eigenen API-Zugang (freesound.org/apiv2/apply: Client-ID und Client-Secret, das Secret ist zugleich der API-Schlüssel)
# sucht Open Libary direkt; nach einmaligem Login (OAuth2, Token 24 h, erneuert sich über den Refresh-Token) lädt sie das
# Original über /apiv2/sounds/<id>/download/. Quelle der Abläufe: freesound.org/docs/api/authentication.html und
# resources_apiv2.html (gelesen am 03.11.2026: /apiv2/search/ ersetzt seit 11/2025 /search/text/).
FS_API = "https://freesound.org/apiv2"
FS_FELDER = "id,name,tags,description,license,type,duration,filesize,samplerate,username,previews,url,bpm"
FS_SEITE = 30
_FS_LOCK = threading.Lock()


def _fs_datei():
    return Path(KONFIG["konfig_ordner"]) / "freesound.json"


def _fs_zugang_datei():
    """API-Zugang (Client-ID, Secret) je Nutzer. Nicht im Code-Ordner: die Freesound-Bedingungen verbieten, Schlüssel an
    Dritte weiterzugeben („may be revoked if you share them with any third party“, Prüfung 03.11.2026)."""
    return Path(KONFIG["konfig_ordner"]) / "freesound_zugang.json"


def _json_lesen(pfad):
    try:
        with open(pfad, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def freesound_konto():
    """Persönlicher Login (Token, state) plus Client-ID und Secret aus dem App-Zugang (freesound_app.json)."""
    d = _json_lesen(_fs_datei())
    app = _json_lesen(_fs_zugang_datei())
    if app.get("client_id") and app.get("client_secret"):
        if d.get("client_id") and d["client_id"] != app["client_id"]:     # anderer App-Zugang: alter Login gilt nicht
            d = {}
        d.update(client_id=app["client_id"], client_secret=app["client_secret"])
    return d


def _fs_speichern(d):
    d = {k: v for k, v in d.items() if k != "client_secret"}      # das Secret steht nur im App-Zugang
    ziel = _fs_datei()
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # Schlüssel und Token: nur der Benutzer liest
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ziel)


def freesound_stand():
    """Für die Einstellungen, ohne Geheimnisse: {"schluessel": bool, "login": bool, "name": str}."""
    d = freesound_konto()
    return {"schluessel": bool(d.get("client_secret")), "login": bool(d.get("refresh_token")),
            "client_id": d.get("client_id", ""), "name": d.get("benutzer", "")}


def freesound_schluessel_setzen(client_id, client_secret):
    """Prüft den Schlüssel mit einer kleinen Suche und speichert ihn; -> (ok, Text)."""
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8,80}", client_secret) or not re.fullmatch(r"[A-Za-z0-9]{4,80}", client_id):
        return False, "Client-ID und Client-Secret aus freesound.org/apiv2/apply einfügen (nur Buchstaben und Ziffern)"
    try:
        _holen(f"{FS_API}/search/?" + urllib.parse.urlencode({"query": "kick", "page_size": "1", "token": client_secret}),
               12, 0, "Freesound")
    except urllib.error.HTTPError as e:
        return False, f"Freesound lehnt den Schlüssel ab ({e.code}). Stimmt das Client-Secret?"
    except QuelleFehler as e:
        return False, str(e)
    with _FS_LOCK:
        ziel = _fs_zugang_datei()
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"client_id": client_id, "client_secret": client_secret,
                       "hinweis": "eigener API-Zugang bei Freesound; nicht weitergeben (Freesound-Bedingungen)"},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, ziel)
        d = _json_lesen(_fs_datei())
        if d.get("client_id") and d["client_id"] != client_id:      # anderer Zugang: alter Login gilt nicht mehr
            _fs_speichern({})
    return True, "Zugang gespeichert: Freesound sucht jetzt direkt; „Verbinden“ holt danach die Originale"


def freesound_trennen():
    with _FS_LOCK:
        d = freesound_konto()
        for k in ("access_token", "refresh_token", "ablauf", "benutzer", "state"):
            d.pop(k, None)
        _fs_speichern(d)


def freesound_login_url():
    """Adresse für den einmaligen Login; merkt sich state gegen fremde Rückrufe."""
    with _FS_LOCK:
        d = freesound_konto()
        if not d.get("client_id"):
            return None
        d["state"] = hashlib.sha256(os.urandom(32)).hexdigest()[:24]
        _fs_speichern(d)
    return f"{FS_API}/oauth2/authorize/?" + urllib.parse.urlencode(
        {"client_id": d["client_id"], "response_type": "code", "state": d["state"]})


def _fs_token_holen(daten):
    req = urllib.request.Request(f"{FS_API}/oauth2/access_token/", data=urllib.parse.urlencode(daten).encode(),
                                 headers=dict(UA, **{"Content-Type": "application/x-www-form-urlencoded"}))
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def freesound_code_tauschen(code, state=None):
    """Code aus dem Rückruf (oder von Hand eingefügt) gegen Token tauschen; -> (ok, Text). Der Code gilt 10 min, einmal."""
    code = (code or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{6,80}", code):
        return False, "Der Code fehlt oder sieht falsch aus"
    with _FS_LOCK:
        d = freesound_konto()
        if not d.get("client_secret"):
            return False, "Erst Client-ID und Client-Secret eintragen"
        if state is not None and state != d.get("state"):
            return False, "Der Rückruf gehört nicht zu diesem Login (state passt nicht); Login neu starten"
        try:
            t = _fs_token_holen({"client_id": d["client_id"], "client_secret": d["client_secret"],
                                 "grant_type": "authorization_code", "code": code})
        except urllib.error.HTTPError as e:
            return False, f"Freesound nimmt den Code nicht an ({e.code}); er gilt nur 10 Minuten und nur einmal"
        except (OSError, ValueError) as e:
            return False, f"Freesound antwortet nicht: {e}"
        d.update(access_token=t["access_token"], refresh_token=t["refresh_token"],
                 ablauf=time.time() + float(t.get("expires_in") or 86399) - 120)
        d.pop("state", None)
        _fs_speichern(d)
    try:    # Name für die Einstellungen (nicht nötig zum Laden)
        ich = _fs_mit_token(f"{FS_API}/me/")
        with _FS_LOCK:
            d = freesound_konto()
            d["benutzer"] = ich.get("username", "")
            _fs_speichern(d)
    except Exception:
        pass
    return True, "Mit Freesound verbunden: Downloads kommen jetzt als Original"


def freesound_token():
    """Gültiges Zugriffs-Token oder None (kein Login, widerrufen). Erneuert über den Refresh-Token, wenn es abläuft."""
    with _FS_LOCK:
        d = freesound_konto()
        if not d.get("refresh_token"):
            return None
        if d.get("access_token") and time.time() < float(d.get("ablauf") or 0):
            return d["access_token"]
        try:
            t = _fs_token_holen({"client_id": d["client_id"], "client_secret": d["client_secret"],
                                 "grant_type": "refresh_token", "refresh_token": d["refresh_token"]})
        except urllib.error.HTTPError:      # widerrufen oder abgelaufen: Login ist weg, Vorschau bleibt
            for k in ("access_token", "refresh_token", "ablauf"):
                d.pop(k, None)
            _fs_speichern(d)
            return None
        except (OSError, ValueError):
            return None
        d.update(access_token=t["access_token"], refresh_token=t.get("refresh_token", d["refresh_token"]),
                 ablauf=time.time() + float(t.get("expires_in") or 86399) - 120)
        _fs_speichern(d)
        return d["access_token"]


def _fs_mit_token(url):
    tok = freesound_token()
    if not tok:
        raise QuelleFehler("nicht bei Freesound angemeldet", 401)
    req = urllib.request.Request(url, headers=dict(UA, Authorization="Bearer " + tok))
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


_FS_ZEITEN = []          # Startzeiten der Original-Downloads (Freesound: 30 je Minute und 500 am Tag je Zugang)


def _fs_drossel(je_minute=25):
    """Höchstens 25 Originale je Minute starten (Grenze laut API-Doku 30; LADE_PARALLEL 6 risse sie bei Kits)."""
    while True:
        with _FS_LOCK:
            jetzt = time.time()
            _FS_ZEITEN[:] = [t for t in _FS_ZEITEN if jetzt - t < 60]
            if len(_FS_ZEITEN) < je_minute:
                _FS_ZEITEN.append(jetzt)
                return
            warte = 60 - (jetzt - _FS_ZEITEN[0]) + 0.1
        time.sleep(warte)


def freesound_direkt_suche(q, lizenz, seite, konto):
    filt = 'license:"Creative Commons 0"' if lizenz != "ccby" else 'license:("Creative Commons 0" OR "Attribution")'
    params = {"query": q, "filter": filt, "fields": FS_FELDER, "page_size": str(FS_SEITE), "page": str(seite),
              "token": konto["client_secret"]}

    def rechnen():
        try:
            return _holen(f"{FS_API}/search/?" + urllib.parse.urlencode(params), 12, 1, "Freesound")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise QuelleFehler("Freesound lehnt den API-Schlüssel ab; in den Einstellungen neu eintragen", 401) from None
            raise QuelleFehler("Freesound lässt gerade keine Abfrage zu; in einer Minute nochmal", 429) from None

    d = _gemerkt(("freesound-direkt", q.lower(), lizenz, seite), rechnen)
    original = bool(konto.get("refresh_token"))
    treffer = []
    for r in d.get("results", []):
        lz = lizenz_art(r.get("license"))
        if lz not in ("cc0", "ccby"):
            continue
        vorschau = (r.get("previews") or {}).get("preview-hq-mp3") or (r.get("previews") or {}).get("preview-lq-mp3")
        if not vorschau:
            continue
        typ = (r.get("type") or "").lower()
        # nur Formate, die Index und DAW sicher lesen; m4a und anderes kommt als Vorschau (Prüfung 03.11.: m4a wäre
        # sonst als .mp3 mit AAC-Inhalt gespeichert worden)
        original_hier = original and typ in ("wav", "aif", "aiff", "flac", "mp3", "ogg")
        bpm = r.get("bpm") if isinstance(r.get("bpm"), (int, float)) and 40 <= r.get("bpm") <= 250 else None
        t = _einzel_treffer(
            "freesound", r.get("id"), r.get("name"), r.get("username"), lz, r.get("license"),
            r.get("url") or f"https://freesound.org/s/{r.get('id')}/", vorschau,
            typ if original_hier else "mp3", r.get("duration"), r.get("filesize") if original_hier else None,
            list(r.get("tags") or []), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.get("description") or ""))[:300],
            bpm=bpm,
            hinweis=(f"Original von Freesound ({typ})" if original_hier
                     else f"HQ-Vorschau (MP3); das Original ({typ}) lädt Open Libary nicht" if original
                     else "HQ-Vorschau (MP3); das Original lädt Open Libary nach dem Freesound-Login in den Einstellungen"))
        if typ in ("wav", "aif", "aiff", "flac", "mp3", "ogg"):
            t["original_url"] = f"{FS_API}/sounds/{r.get('id')}/download/"
            t["original_format"] = _endung(typ)
        treffer.append(t)
    relevanz(q, treffer, _text_einzel)
    gesamt = d.get("count") or 0
    return {"gesamt": gesamt, "treffer": _geladen_markieren(treffer), "seite": seite, "mehr": seite * FS_SEITE < gesamt,
            "weg": "direkt", "original": original}


def freesound_suche(q, lizenz="cc0", seite=1):
    """Freesound direkt (mit eigenem API-Schlüssel) oder über Openverse."""
    q = suchtext_pruefen(q)
    konto = freesound_konto()
    if konto.get("client_secret"):
        return freesound_direkt_suche(q, lizenz, seite, konto)
    if seite > 12:
        raise QuelleFehler("Ohne Konto zeigt Openverse höchstens 240 Treffer (12 Seiten)", 400)
    lizenzen = "cc0,pdm,by" if lizenz == "ccby" else "cc0,pdm"   # Unbekanntes gilt als das Strengere
    params = {"q": q, "license": lizenzen, "source": "freesound", "page_size": "20", "page": str(seite)}

    def rechnen():
        try:
            # Musiker-Test 03.11.: „808“ brauchte 29 s, „rhodes chord“ endete mit 504; nach 15 s ist Schluss
            return _holen("https://api.openverse.org/v1/audio/?" + urllib.parse.urlencode(params), 15, 0, "Freesound (Openverse)")
        except urllib.error.HTTPError as e:
            raise QuelleFehler("Openverse lässt gerade keine Abfrage zu; in einer Minute nochmal", 429) from None

    d = _gemerkt(("freesound", q.lower(), lizenz, seite), rechnen)
    treffer = []
    for r in d.get("results", []):
        lz = lizenz_art(r.get("license_url"), r.get("license"))
        if lz not in ("cc0", "pdm", "ccby"):
            continue
        tags = [t.get("name", "") for t in (r.get("tags") or [])]
        treffer.append(_einzel_treffer(
            "freesound", r.get("id"), r.get("title"), r.get("creator"), lz, r.get("license_url"),
            r.get("foreign_landing_url"), r.get("url"), r.get("filetype"),
            (r.get("duration") or 0) / 1000 or None, r.get("filesize"), tags,
            hinweis="HQ-Vorschau (MP3 128 kbps); das Original gibt Freesound nur nach Login heraus"))
    relevanz(q, treffer, _text_einzel)
    return {"gesamt": d.get("result_count", 0), "treffer": _geladen_markieren(treffer), "seite": seite,
            "mehr": seite < min(12, d.get("page_count") or 1)}


def wikimedia_suche(q, lizenz="cc0", seite=1):
    q = suchtext_pruefen(q)
    params = {"action": "query", "generator": "search", "gsrsearch": f"{q} filetype:audio", "gsrnamespace": "6",
              "gsrlimit": "20", "gsroffset": str((seite - 1) * 20), "prop": "imageinfo",
              "iiprop": "url|size|mime|extmetadata|metadata", "format": "json"}
    d = _gemerkt(("wikimedia", q.lower(), seite), lambda: _holen(
        "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params), 20, 1, "Wikimedia Commons"))
    treffer = []
    for p in sorted((d.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0)):
        titel = re.sub(r"^File:", "", p.get("title", ""))
        ii = (p.get("imageinfo") or [{}])[0]
        mime = ii.get("mime") or ""
        if "midi" in mime or not mime.startswith(("audio/", "application/ogg")):
            continue
        if re.match(r"(?i)LL-|.*pronunciation|^[a-z]{2,3}-[^ ]+\.(ogg|wav|mp3)$", titel):   # Ausspracheaufnahmen
            continue
        em = ii.get("extmetadata") or {}
        kurz = (em.get("LicenseShortName") or {}).get("value", "")
        lz = lizenz_art((em.get("LicenseUrl") or {}).get("value", ""), kurz)
        if lz not in (("cc0", "pd") if lizenz == "cc0" else ("cc0", "pd", "ccby")):
            continue
        dauer = None   # Länge steht in den Dateimetadaten (Ogg: length, WAV/FLAC: playtime_seconds)
        for m in ii.get("metadata") or []:
            if m.get("name") in ("length", "playtime_seconds"):
                dauer = zahl_oder_none(m.get("value"))
                break
        urheber = html.unescape(re.sub(r"<[^>]+>", " ", (em.get("Artist") or {}).get("value", "")))
        urheber = re.sub(r"\s+", " ", urheber).strip()
        if not urheber or re.search(r"(?i)unknown author|unbekannt", urheber):
            urheber = "Urheber unbekannt"
        beschr = html.unescape(re.sub(r"<[^>]+>", " ", (em.get("ImageDescription") or {}).get("value", ""))).strip()
        tags = [c for c in re.split(r"\|", (em.get("Categories") or {}).get("value", "")) if c][:8]
        treffer.append(_einzel_treffer(
            "wikimedia", p.get("pageid"), titel, urheber[:80], lz,
            (em.get("LicenseUrl") or {}).get("value", ""),   # keine Adresse erfinden, wenn die Seite keine nennt
            ii.get("descriptionurl"), ii.get("url"), mime, dauer, ii.get("size"), tags, beschr[:300],
            hinweis=f"Original von Wikimedia Commons, Lizenz laut Seite: {kurz}"))
    relevanz(q, treffer, _text_einzel)
    return {"gesamt": (d.get("query") or {}).get("searchinfo", {}).get("totalhits", len(treffer)),
            "treffer": _geladen_markieren(treffer), "seite": seite, "mehr": "continue" in d}


_CCM_LOCK = threading.Lock()
_CCM_ZEIT = {"letzte": 0.0}


def ccmixter_suche(q, lizenz="cc0", seite=1):
    """Acapellas, Stems, Samples. Nur CC BY und CC0 (cc0-Filter: nur CC0; dort gibt es kaum welche)."""
    q = suchtext_pruefen(q)
    params = {"f": "json", "search": q, "limit": "20", "offset": str((seite - 1) * 20), "sort": "rank"}

    def rechnen():
        with _CCM_LOCK:   # schnelle Folgeabfragen geben dort eine leere Antwort
            warten = 1.2 - (time.time() - _CCM_ZEIT["letzte"])
            if warten > 0:
                time.sleep(warten)
            try:
                return _holen("http://ccmixter.org/api/query?" + urllib.parse.urlencode(params), 20, 2, "ccMixter")
            finally:
                _CCM_ZEIT["letzte"] = time.time()

    d = _gemerkt(("ccmixter", q.lower(), seite), rechnen)
    treffer = []
    for x in d if isinstance(d, list) else []:
        lz = lizenz_art(x.get("license_url"))
        if lz not in (("cc0",) if lizenz == "cc0" else ("cc0", "ccby")):
            continue
        ext = x.get("upload_extra") or {}
        alle_tags = ",".join([ext.get("usertags", ""), ext.get("ccud", ""), x.get("upload_tags", "")]).lower()
        if not re.search(r"acappella|acapella|sample|stem|loop|instrumental|\bdrums?\b", alle_tags):
            continue      # ganze Remixe ohne Samples, Stems oder Acapellas überspringen
        dateien = x.get("files") or []
        typ = "Acapella" if "acappella" in alle_tags or "acapella" in alle_tags else (
            "Stems" if "stem" in alle_tags else "Sample")
        # Stems liegen dort meist als ZIP bei (Prüfer 20.10.: sonst kam nur eine Spur); das ZIP wird beim Laden entpackt
        beste = None
        for wunsch in (("zip", "flac", "wav", "mp3") if typ == "Stems" else ("flac", "wav", "mp3")):
            beste = next((f for f in dateien if f.get("file_name", "").lower().endswith("." + wunsch)
                          and not re.search(r"(?i)preview|_lo\b", f.get("file_name", ""))), None)
            if beste:
                break
        if not beste:
            continue
        hoer = next((f for f in dateien if f.get("file_name", "").lower().endswith(".mp3")), beste)
        tags = [t for t in re.split(r"[,\s]+", alle_tags) if t and not t.startswith(("bpm_", "44k", "48k"))]
        titel = f"{x.get('upload_name', '')} ({typ})"
        treffer.append(_einzel_treffer(
            "ccmixter", x.get("upload_id"), titel, x.get("user_real_name") or x.get("user_name"), lz,
            x.get("license_url"), x.get("file_page_url"), beste.get("download_url"),
            beste.get("file_name") if beste.get("file_name", "").lower().endswith(".zip")
            else (beste.get("file_format_info") or {}).get("mime_type") or beste.get("file_name"),
            zahl_oder_none((beste.get("file_format_info") or {}).get("ps")), beste.get("file_rawsize") or 0,
            tags[:10], "",   # Beschreibung dort fast immer "remix of …"
            bpm=ext.get("bpm") if isinstance(ext.get("bpm"), int) else None,
            hinweis=f"Original von ccMixter ({x.get('license_name', '')})"))
        treffer[-1]["hoer_url"] = hoer.get("download_url")   # Vorhören über die MP3, auch wenn das ZIP geladen wird
    relevanz(q, treffer, _text_einzel)
    return {"gesamt": None, "treffer": _geladen_markieren(treffer), "seite": seite, "mehr": len(d) >= 20}


EINZEL_QUELLEN = {"freesound": freesound_suche, "wikimedia": wikimedia_suche, "ccmixter": ccmixter_suche}


# ================================================================ Laden

class Ladung:
    def __init__(self, schluessel, titel, ordner):
        self.schluessel, self.titel, self.ordner = schluessel, titel, ordner
        self.status, self.text = "laeuft", ""
        self.dateien_gesamt = self.dateien_fertig = self.uebersprungen = 0
        self.bytes_gesamt = self.bytes_fertig = 0
        self.fehler = []           # (name, grund)
        self.abbrechen = False
        self.start = time.time()
        self.ende = None
        self.pack = ""             # Pack-Name in der Bibliothek, für "In der Bibliothek zeigen"
        self.pfade = []            # fertig einsortierte Dateien (die Ladeliste zeigt je Datei den Stand der Messung)
        self.lock = threading.Lock()

    def bericht(self):
        dauer = (self.ende or time.time()) - self.start
        with self.lock:
            pfade = list(self.pfade)
        return {"titel": self.titel, "status": self.status, "text": self.text, "ordner": str(self.ordner),
                "pack": self.pack, "dateien_gesamt": self.dateien_gesamt, "dateien_fertig": self.dateien_fertig,
                "uebersprungen": self.uebersprungen, "bytes_gesamt": self.bytes_gesamt,
                "bytes_fertig": self.bytes_fertig, "fehler": self.fehler[-20:], "fehler_zahl": len(self.fehler),
                "sekunden": round(dauer, 1), "ende": self.ende, "pfade": pfade}


LADUNGEN = {}
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=LADE_PARALLEL, thread_name_prefix="laden")
_ZUS_LOCK = threading.Lock()
_NACH_LADEN = {"zeit": 0.0, "timer": None}


DRUM_ARTEN = {"kick", "808", "snare", "clap", "rim", "hat", "cymbal", "tom", "perc"}
ART_ORDNER = {"kick": "Kicks", "808": "808s", "snare": "Snares", "clap": "Claps", "rim": "Rims", "hat": "Hi-Hats",
              "cymbal": "Becken", "tom": "Toms", "perc": "Percussion", "break": "Drum-Loops", "vocal": "Vocals",
              "fx": "FX", "atmo": "Atmo", "foley": "Foley", "glitch": "Glitch", "melodisch": "Melodisch", "bass": "Bass"}


def einzel_zielordner(t, kit=""):
    """Wohin ein Einzelsound kommt: ein benanntes Kit zusammen, sonst nach Art in die FL-Browser-Ordner. Drum-One-Shots
    nach Drum Kits/Frei (Netz)/Einzelsounds/<Art>, Loops, Vocals, FX, Atmos nach Samples/Frei (Netz)/Einzelsounds/<Art>."""
    if kit:
        return Path(KONFIG["ziel"]) / EINZEL_ORDNER / kit
    art = t.get("art") or ""
    drums = art in DRUM_ARTEN and t.get("lo") != "loop"
    return Path(KONFIG["ziel_drums"] if drums else KONFIG["ziel"]) / EINZEL_ORDNER / ART_ORDNER.get(art, "Sonstiges")


def _sauberer_name(s, laenge=80):
    if plattform.WINDOWS:
        laenge = min(laenge, 60)        # MAX_PATH 260: Ordner und Datei zusammen dürfen dort nicht ausufern
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]+', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip().lstrip(". ")    # "." und ".." wären Ordnerverweise (Prüfer 20.10.)
    if len(s) > laenge:     # gekürzt: Kürzel aus dem vollen Namen, sonst hießen „… take 1“ und „… take 2“ gleich (Prüfer 03.11.)
        s = s[:laenge - 7].rstrip(". ") + " " + hashlib.md5(s.encode()).hexdigest()[:6]
    s = s.rstrip(". ")      # Windows: kein Punkt und kein Leerzeichen am Ende eines Namens (03.11.2026)
    # Windows: CON, PRN, AUX, NUL, COM1 bis 9, LPT1 bis 9 sind Geräte, auch mit Endung („con.wav“ schriebe in die
    # Konsole; unter CrossOver geprüft 03.11.2026)
    if plattform.WINDOWS and re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?", s):
        s = "_" + s
    return s


def laden_starten(quelle, ident="", auswahl=None, bestaetigt=False, ids=None, ordner=""):
    """Startet eine Ladung. quelle "archive" (ident, auswahl) oder "einzel" (ids aus der letzten Suche, ordner).
    Rückgabe (code, dict): 200 gestartet, 409 läuft schon oder braucht Bestätigung."""
    if quelle == "archive":
        schluessel = f"archive:{ident}"
    elif quelle == "einzel":
        ids = [i for i in (ids or []) if i in _EINZEL]
        if not ids:
            return 400, {"fehler": "Sounds nicht aus einer der letzten Suchen; bitte neu suchen"}
        schluessel = "einzel:" + hashlib.md5("|".join(sorted(ids)).encode()).hexdigest()[:12]
    else:
        return 400, {"fehler": "unbekannte Quelle"}
    alt = LADUNGEN.get(schluessel)
    if alt and alt.status == "laeuft":
        return 409, {"fehler": "Diese Ladung läuft schon", "schluessel": schluessel}

    if quelle == "archive":
        info = archive_dateien(ident)
        if info["lizenz"] not in ("cc0", "ccby", "pdm"):
            return 400, {"fehler": f"Lizenz ist {info['lizenz_url'] or 'nicht angegeben'}: weder CC0 noch CC BY"}
        dateien = [dict(f, lizenz=info["lizenz"], lizenz_url=info["lizenz_url"], urheber=info["urheber"],
                        seite=info["seite"], quelle="archive.org")
                   for f in info["dateien"] if (not auswahl or f["name"] in auswahl)
                   and not f.get("nicht_lesbar") and not f.get("schon_da")]
        # Drum-Packs (überwiegend One-Shots) in den FL-Ordner Drum Kits, alles andere nach Samples
        arten = [f.get("art") for f in dateien]
        drums = sum(1 for a, f in zip(arten, dateien) if a in DRUM_ARTEN and f.get("lo") != "loop") > len(dateien) / 2
        ziel = Path(KONFIG["ziel_drums"] if drums else KONFIG["ziel"]) / ordner_name(info["titel"])
        titel, pack = info["titel"], ordner_name(info["titel"])
    else:
        kit = _sauberer_name(ordner)       # leer: jeder Sound nach seiner Art in den passenden FL-Ordner
        dateien, info = [], {"seite": "", "lizenz": "", "lizenz_url": ""}
        seiten = _geladene_seiten()
        for i in ids:
            t = _EINZEL[i]
            # nie eine URL vom Aufrufer übernehmen (Prüfer 19.10.: file:///etc/hosts landete sonst im Ladeordner)
            if not str(t.get("url", "")).startswith("https://") and not (t["quelle"] == "ccmixter" and t["url"].startswith("http://ccmixter.org/")):
                continue
            if t["lizenz"] not in ("cc0", "ccby", "pdm", "pd") or (t["seite"] and t["seite"] in seiten):
                continue
            name = _sauberer_name(re.sub(r"(?i)\.(wav|aiff?|flac|mp3|ogg)$", "", t["titel"]) + " - " + (t["urheber"] or "unbekannt"), 120)
            url, fmt, hinweis, auth = t["url"], t["format"], t["hinweis"], ""
            # Freesound direkt mit Login: das Original statt der Vorschau (Musiker-Test 03.11.2026)
            if t.get("original_url") and freesound_token():
                url, fmt, auth = t["original_url"], t["original_format"], "freesound"
                hinweis = f"Original von Freesound ({fmt})"
            dateien.append({"name": f"{name}.{fmt}", "groesse": t.get("groesse") or 0, "url": url, "md5": "", "auth": auth,
                            "lizenz": t["lizenz"], "lizenz_url": t["lizenz_url"], "urheber": t["urheber"],
                            "seite": t["seite"], "hinweis": hinweis, "tags": t["tags"], "quelle": t["quelle"],
                            "titel_original": t["titel"], "beleg": t, "verdacht": t["verdacht"],
                            "zip": t["format"] == "zip", "zielordner": einzel_zielordner(t, kit)})
        # gleicher Name, andere Quelle (gleicher Titel und Urheber, oder ein Rest von früher): mit Kürzel der Quelle,
        # sonst nähme _eine_datei_laden die fremde Datei als „schon geladen“ (Prüfer 03.11.: Vorschauen haben Größe 0)
        belegt = set()
        for f in dateien:
            p = f["zielordner"] / f["name"]
            if str(p).lower() in belegt or p.exists():
                stamm, _, endung = f["name"].rpartition(".")
                f["name"] = f"{stamm} {hashlib.md5((f['seite'] or f['url']).encode()).hexdigest()[:6]}.{endung}"
            belegt.add(str(f["zielordner"] / f["name"]).lower())
        ordner_alle = {f["zielordner"] for f in dateien}
        if kit:
            ziel, pack = Path(KONFIG["ziel"]) / EINZEL_ORDNER / kit, kit
        elif len(ordner_alle) == 1:
            ziel = next(iter(ordner_alle)) if ordner_alle else Path(KONFIG["ziel"]) / EINZEL_ORDNER
            pack = ziel.name
        else:
            ziel, pack = Path(KONFIG["ziel"]) / EINZEL_ORDNER, ""
        if len(dateien) == 1:
            titel = dateien[0]["titel_original"]
        else:
            titel = f"{len(dateien)} Sounds" + (f" in „{kit}“" if kit else ", nach Art einsortiert")
    if not dateien:
        return 200, {"ok": True, "hinweis": "Nichts zu laden: alles schon in der Bibliothek"}
    summe = sum(f["groesse"] for f in dateien)
    if summe > GROSS_BYTES and not bestaetigt:
        return 409, {"fehler": "groß", "bytes": summe, "dateien": len(dateien)}

    ladung = Ladung(schluessel, titel, ziel)
    ladung.dateien_gesamt, ladung.bytes_gesamt, ladung.pack = len(dateien), summe, pack
    LADUNGEN[schluessel] = ladung
    if quelle == "archive":
        (ziel / "inhalt").mkdir(parents=True, exist_ok=True)
        _beleg_sammlung(ziel, info)
    else:
        for f in dateien:
            f["zielordner"].mkdir(parents=True, exist_ok=True)
    offen = {"n": len(dateien)}

    def eine(f):
        try:
            if ladung.abbrechen:
                with ladung.lock:
                    ladung.uebersprungen += 1
                return
            ordner_f = f.get("zielordner") or ziel / "inhalt"
            pfad = _eine_datei_laden(ladung, f, ordner_f)
            if f.get("zip"):
                neue = _zip_entpacken(pfad, ordner_f)
                with ladung.lock:   # ein ZIP zählt als so viele Dateien, wie es enthält
                    ladung.dateien_gesamt += max(0, len(neue) - 1)
                    ladung.dateien_fertig += max(0, len(neue) - 1)
                for p in neue:
                    _einsortieren_und_eintragen(p, info, quelle, dict(f, md5=_md5(p)))
            else:
                neue = [pfad]
                _einsortieren_und_eintragen(pfad, info, quelle, f)
            with ladung.lock:
                ladung.dateien_fertig += 1
                ladung.pfade.extend(str(p) for p in neue)
            _index_bald_neu()
            try:
                KONFIG["fertig_datei"]([str(p) for p in neue])    # in die Warteschlange der Messung
            except Exception as e:
                print(f"Messen nicht eingereiht: {e}", flush=True)
        except Exception as e:   # eine kaputte Datei hält die übrigen nicht auf
            with ladung.lock:
                if ladung.abbrechen and "abgebrochen" in str(e):
                    ladung.uebersprungen += 1      # Abbrechen ist kein Fehler (Test 20.10.: zählte als 6 Fehler)
                else:
                    ladung.fehler.append((f["name"], str(e)[:200]))
        finally:
            with ladung.lock:
                offen["n"] -= 1
                letzte = offen["n"] == 0
            if letzte:
                # erst den Index neu laden, dann "fertig" melden: sonst fragt die Oberfläche zu früh und sieht nur
                # einen Teil der Dateien (Test 19.10.: 60 von 99); bis dahin "wird eingetragen" statt "lädt"
                with ladung.lock:
                    ladung.status = "eintragen"
                try:
                    KONFIG["nach_laden"]()
                finally:
                    with ladung.lock:
                        ladung.ende = time.time()
                        ladung.status = ("abgebrochen" if ladung.abbrechen else
                                         "fertig" if not ladung.fehler else "fertig mit Fehlern")

    for f in dateien:
        _POOL.submit(eine, f)
    return 200, {"ok": True, "schluessel": schluessel, "dateien": len(dateien), "bytes": summe}


def _beleg_sammlung(ziel, info):
    """QUELLE.txt, LIZENZ.txt und die Metadaten zum Ladezeitpunkt (falls der Eintrag später geändert oder gesperrt wird)."""
    jetzt = time.strftime("%Y-%m-%d %H:%M")
    urheber = info.get("urheber") or "nicht angegeben"
    (ziel / "QUELLE.txt").write_text(
        f"Quelle: {info['seite']}\nTitel: {info['titel']}\nUrheber laut Quelle: {urheber}\n"
        f"Hochgeladen von Konto: {info.get('konto') or 'nicht angegeben'} am {info.get('hochgeladen') or '?'}\n"
        f"Geladen: {jetzt} mit der Open Libary\nMetadaten zum Ladezeitpunkt: _belege/metadaten.json\n", encoding="utf-8")
    hinweis = (f'Namensnennung nötig: "{info["titel"]}" von {urheber} ({info["seite"]}), Lizenz {info["lizenz_url"]}\n'
               if info["lizenz"] == "ccby" else "Keine Namensnennung nötig.\n")
    verd = ("\nVERDACHT beim Laden: " + "; ".join(info.get("verdacht") or []) + "\n") if info.get("verdacht") else ""
    pdm = "\nNur Public Domain Mark: eine Behauptung des Hochladenden, keine Lizenz.\n" if info["lizenz"] == "pdm" else ""
    (ziel / "LIZENZ.txt").write_text(
        f"Lizenz laut archive.org: {info['lizenz_url']}\n{hinweis}{verd}{pdm}\n"
        "UNGEPRÜFT: die Lizenz hat der Hochladende selbst eingetragen. Vor kommerzieller Nutzung prüfen, ob er Urheber "
        "ist (keine Plattenbreaks, keine Herstellerpacks, bei alten Aufnahmen Jahr und Komposition).\n", encoding="utf-8")
    (ziel / "_belege").mkdir(exist_ok=True)
    (ziel / "_belege" / "metadaten.json").write_text(
        json.dumps({"geladen_am": jetzt, "seite": info["seite"], "metadaten": info.get("metadaten")},
                   ensure_ascii=False, indent=1), encoding="utf-8")


STUECK_AB = 1_500_000   # größere Dateien in STUECKE Teilen gleichzeitig (archive.org drosselt je Verbindung auf ~15 KB/s)
STUECKE = 6


def _in_stuecken_laden(ladung, f, tmp):
    """Lädt eine große Datei in STUECKE Byte-Bereichen parallel und setzt sie zusammen. False, wenn der Server keine
    Bereiche kann (dann lädt der Aufrufer am Stück)."""
    groesse = f["groesse"]
    grenzen = [(i * groesse // STUECKE, (i + 1) * groesse // STUECKE - 1) for i in range(STUECKE)]
    teile = [tmp.with_name(f"{tmp.name}.{i}") for i in range(STUECKE)]
    fehler = []

    def teil(i):
        a, b = grenzen[i]
        for versuch in range(WIEDERHOLEN):
            schon = teile[i].stat().st_size if teile[i].is_file() else 0
            if schon >= b - a + 1:
                return
            try:
                kopf = dict(UA, Range=f"bytes={a + schon}-{b}")
                with urllib.request.urlopen(urllib.request.Request(f["url"], headers=kopf), timeout=30) as r:
                    if r.status != 206:
                        raise ValueError("keine Bereiche")
                    with open(teile[i], "ab") as out:
                        while True:
                            if ladung.abbrechen:
                                raise RuntimeError("abgebrochen")
                            s = r.read(1 << 16)
                            if not s:
                                break
                            out.write(s)
                            with ladung.lock:
                                ladung.bytes_fertig += len(s)
                return
            except ValueError as e:
                fehler.append(e)
                return
            except (urllib.error.URLError, OSError, RuntimeError) as e:
                if "abgebrochen" in str(e):
                    fehler.append(e)
                    return
                time.sleep((2, 6, 15)[versuch])
        fehler.append(RuntimeError(f"Stück {i} nach {WIEDERHOLEN} Versuchen"))

    faeden = [threading.Thread(target=teil, args=(i,), daemon=True) for i in range(STUECKE)]
    for t in faeden:
        t.start()
    for t in faeden:
        t.join()
    if any(isinstance(e, ValueError) for e in fehler):
        for p in teile:
            if p.is_file():
                with ladung.lock:
                    ladung.bytes_fertig -= p.stat().st_size
                p.unlink()
        return False
    if fehler:
        raise fehler[0]
    with open(tmp, "wb") as out:
        for p in teile:
            with open(p, "rb") as src:
                shutil.copyfileobj(src, out, 1 << 20)
            p.unlink()
    return True


def _eine_datei_laden(ladung, f, ordner):
    pfad = ordner / f["name"]
    pfad.parent.mkdir(parents=True, exist_ok=True)
    if pfad.is_file() and (not f["groesse"] or pfad.stat().st_size == f["groesse"]):
        with ladung.lock:
            ladung.bytes_fertig += pfad.stat().st_size
        return pfad
    tmp = pfad.with_name(pfad.name + ".teil")
    # in Stücken nur bei archive.org (dort hilft es, Faktor 30); ccMixter bricht parallele Bereiche ab (Test 20.10.)
    if (f.get("groesse") or 0) >= STUECK_AB and "archive.org/" in f["url"] and _in_stuecken_laden(ladung, f, tmp):
        if f.get("md5"):
            h = hashlib.md5()
            with open(tmp, "rb") as fh:
                for s in iter(lambda: fh.read(1 << 20), b""):
                    h.update(s)
            if h.hexdigest() != f["md5"]:
                tmp.unlink()
                raise ValueError("Prüfsumme falsch (in Stücken geladen)")
        os.replace(tmp, pfad)
        return pfad
    frist = time.time() + 120 + (f["groesse"] or 0) / 20_000   # 20 KB/s als Untergrenze, dazu 2 min
    letzter = None
    for versuch in range(WIEDERHOLEN):
        if ladung.abbrechen:
            raise RuntimeError("abgebrochen")
        schon = tmp.stat().st_size if tmp.is_file() else 0
        kopf = dict(UA, **quell_kopf(f["url"]))
        if f.get("auth") == "freesound":       # Original über die API: nur mit gültigem Login
            _fs_drossel()
            tok = freesound_token()
            if not tok:
                raise QuelleFehler("Freesound-Login abgelaufen oder widerrufen; in den Einstellungen neu verbinden", 401)
            kopf["Authorization"] = "Bearer " + tok
        if schon:
            kopf["Range"] = f"bytes={schon}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(f["url"], headers=kopf), timeout=30) as r:
                if not f.get("groesse") and not schon:   # unbekannte Größe: Fortschritt aus Content-Length (Test 20.10.)
                    try:
                        f["groesse"] = int(r.headers.get("Content-Length") or 0)
                        with ladung.lock:
                            ladung.bytes_gesamt += f["groesse"]
                    except ValueError:
                        pass
                if schon and r.status != 206:   # Server kann nicht fortsetzen: von vorn
                    with ladung.lock:
                        ladung.bytes_fertig -= schon
                    schon = 0
                with open(tmp, "ab" if schon else "wb") as out:
                    while True:
                        if ladung.abbrechen:
                            raise RuntimeError("abgebrochen")
                        if time.time() > frist:
                            raise TimeoutError("Gesamtfrist überschritten")
                        stueck = r.read(1 << 16)
                        if not stueck:
                            break
                        out.write(stueck)
                        with ladung.lock:
                            ladung.bytes_fertig += len(stueck)
            if f.get("md5"):
                h = hashlib.md5()
                with open(tmp, "rb") as fh:
                    for s in iter(lambda: fh.read(1 << 20), b""):
                        h.update(s)
                if h.hexdigest() != f["md5"]:
                    tmp.unlink()
                    raise ValueError("Prüfsumme falsch")
            os.replace(tmp, pfad)
            return pfad
        except urllib.error.HTTPError as e:
            if e.code == 429 and f.get("auth") == "freesound":   # Kontingent der App bei Freesound
                raise QuelleFehler("Freesound lässt gerade keine Originale mehr zu (höchstens 30 je Minute und 500 am "
                                   "Tag je Zugang); später nochmal oder ohne Login die Vorschau", 429) from None
            letzter = e                     # sonst wie bisher: kurz warten, nochmal
            time.sleep((2, 6, 15)[versuch])
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            letzter = e
            if "abgebrochen" in str(e):
                raise
            time.sleep((2, 6, 15)[versuch])
    raise RuntimeError(f"nach {WIEDERHOLEN} Versuchen: {letzter}")


def _zip_entpacken(zip_pfad, ordner):
    ziel = ordner / (_sauberer_name(Path(zip_pfad).stem) or "Pack")
    wurzel = ziel.resolve()
    neue, belegt = [], set()
    with zipfile.ZipFile(zip_pfad) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.lower().endswith(AUDIO_ENDUNGEN) or "__MACOSX" in name:
                continue
            # Namen im Zip sind fremde Eingabe: Laufwerk ("C:"), Wurzel, ".." und unter Windows verbotene Zeichen weg,
            # jeder Teil einzeln gesäubert (03.11.2026; vorher blieb "C:\\..\\x.wav" unter Windows ein Weg nach draußen)
            teile = [t for t in name.split("/") if t not in ("", ".", "..")]
            if not teile:
                continue
            endung = Path(teile[-1]).suffix.lower()
            ordner_teile = [t for t in (_sauberer_name(t) for t in teile[:-1]) if t]
            datei = (_sauberer_name(teile[-1][: -len(endung)], 80 - len(endung)) or "Sample") + endung
            p = ziel.joinpath(*ordner_teile, datei)
            if plattform.WINDOWS and len(str(p)) > 240:      # tief verschachtelt: flach mit Kurzkennung statt Abbruch
                p = ziel / (hashlib.md5(name.encode()).hexdigest()[:6] + " " + datei)
            if str(p).lower() in belegt:        # zwei Einträge, ein Name (Säubern, Groß/klein): nicht überschreiben
                p = p.with_name(p.stem + " " + hashlib.md5(name.encode()).hexdigest()[:6] + p.suffix)
            try:
                p.resolve().relative_to(wurzel)
            except (ValueError, OSError):
                continue
            belegt.add(str(p).lower())
            p.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(p, "wb") as out:
                shutil.copyfileobj(src, out)
            neue.append(p)
    os.unlink(zip_pfad)
    return neue


def _md5(pfad):
    h = hashlib.md5()
    with open(pfad, "rb") as fh:
        for s in iter(lambda: fh.read(1 << 20), b""):
            h.update(s)
    return h.hexdigest()


def _einsortieren_und_eintragen(pfad, info, quelle, f):
    pfad = Path(pfad)
    m = messen(pfad)
    dauer = m["dauer"]
    pack, innen, basis = pfad.parent.name, "", pfad.parent
    for teil in pfad.parents:
        if teil.name == "inhalt":
            # nur die Ordner im Pack zählen (vorher ging der ganze Pfad durch: "scratchpad" machte alles "melodisch")
            pack, innen, basis = teil.parent.name, str(pfad.parent.relative_to(teil)), teil.parent
            break
    tags = f.get("tags") or []
    e = einordnen(pfad.name, " ".join(([] if innen == "." else [innen]) + tags[:8]), dauer)
    if f.get("beleg") and f["beleg"].get("bpm") and not e["bpm"]:
        e["bpm"] = f["beleg"]["bpm"]
    lizenz = f.get("lizenz") or info.get("lizenz")
    hinweise = [f.get("hinweis") or ""]
    if lizenz == "ccby":
        hinweise.append(f"CC BY: Namensnennung nötig ({f.get('urheber') or info.get('urheber') or 'Urheber siehe Beleg'})")
    if lizenz == "pdm":
        hinweise.append("nur Public Domain Mark, keine Lizenz")
    if dauer and dauer >= 30 and e["art"] in ("atmo", "fx", "foley", ""):
        hinweise.append(f"Field Recording, {int(dauer // 60)}:{int(dauer % 60):02d} min")
    zeile = {
        "datei": pfad.name, "pfad": str(pfad), "pack": pack, "art": e["art"], "tempo": e["bpm"] or "",
        "tonart": e["tonart"], "dauer": dauer if dauer is not None else "", "lizenz_gefunden": "ungeprüft",
        "format": pfad.suffix.lstrip(".").lower(), "loop_oneshot": e["lo"], "stimmung": "",
        "herkunft": "openlibary", "quelle_url": f.get("seite") or info.get("seite") or "",
        "lizenz_url": f.get("lizenz_url") or info.get("lizenz_url") or "", "geladen_am": time.strftime("%Y-%m-%d %H:%M"),
        "md5": f.get("md5") or _md5(pfad), "quelldatei": f.get("name") if quelle == "archive" else "",
        "hinweis": "; ".join(x for x in hinweise if x), "samplerate": m["samplerate"], "kanaele": m["kanaele"],
        "tags": ", ".join(tags[:10]), "urheber": f.get("urheber") or info.get("urheber") or "",
        "quelle": f.get("quelle") or "",
    }
    with _ZUS_LOCK:
        zus = Path(KONFIG["zusaetze"])
        neu = not zus.is_file()
        if not neu:
            with open(zus, newline="", encoding="utf-8") as fh:
                leser = csv.DictReader(fh)
                alte_spalten = leser.fieldnames or []
                zeilen = list(leser)
            if any(r.get("pfad") == zeile["pfad"] for r in zeilen):
                return
            if alte_spalten != ZUSATZ_SPALTEN:   # neue Spalten: Datei einmal mit allen Spalten neu schreiben
                with open(zus, "w", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=ZUSATZ_SPALTEN, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(zeilen)
        with open(zus, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=ZUSATZ_SPALTEN, extrasaction="ignore")
            if neu:
                w.writeheader()
            w.writerow(zeile)
        if quelle == "einzel":   # Beleg je Sound: Zeile in lizenzen.csv und die Trefferdaten zum Ladezeitpunkt
            lz = basis / "lizenzen.csv"
            neu_lz = not lz.is_file()
            with open(lz, "a", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                if neu_lz:
                    w.writerow(["datei", "titel_original", "urheber", "quelle", "lizenz", "lizenz_url", "seite",
                                "namensnennung_noetig", "geladen_am", "md5", "hinweis", "verdacht"])
                w.writerow([pfad.name, f.get("titel_original"), f.get("urheber"), f.get("quelle"), f.get("lizenz"),
                            f.get("lizenz_url"), f.get("seite"), "ja" if lizenz == "ccby" else "nein",
                            zeile["geladen_am"], zeile["md5"], f.get("hinweis"), "; ".join(f.get("verdacht") or [])])
            if f.get("beleg"):
                (basis / "_belege").mkdir(exist_ok=True)
                (basis / "_belege" / (re.sub(r"[^A-Za-z0-9_-]+", "_", f["beleg"]["schluessel"]) + ".json")).write_text(
                    json.dumps(dict(f["beleg"], geladen_am=zeile["geladen_am"], datei=pfad.name, md5=zeile["md5"]),
                               ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- Looperman (Konten 03.11.2026)
# Looperman hat keine API und verbietet Programme („We strictly prohibit unauthorised access or downloading of data using
# any computer program“) und das Einbetten der Seite („frame, mask“), looperman.com/help/terms, gelesen 03.11.2026.
# Open Libary fragt Looperman deshalb nie selbst ab und zeigt die Seite nicht: man sucht und lädt im eigenen Browser mit
# eigenem Konto. Ist Looperman verbunden (konten.json), holt der Wächter fertige Downloads aus ~/Downloads, deren
# Herkunft looperman.com ist (kMDItemWhereFroms, schreibt der Browser beim Laden), in die Bibliothek.
LOOPERMAN_HINWEIS = ("Looperman: mit eigenem Konto im Browser geladen; lizenzfrei in eigener Musik, nicht als Loop oder "
                     "Sample weitergeben; nicht für KI-Training; Herkunft der Aufnahme vom Hochladenden nicht geprüft")
LOOPERMAN_LIZENZ = """Looperman (looperman.com), mit eigenem Konto im Browser geladen, von Open Libary einsortiert.

Bedingungen laut looperman.com/help/terms (gelesen am 03.11.2026), Kurzfassung:
- Registrierte Nutzer dürfen Loops lizenzfrei kopieren, bearbeiten und in eigenen Werken verwenden, auch verkaufen.
  Die Lizenz gilt für das eigene Konto und ist nicht übertragbar.
- Die Loops selbst nicht weitergeben oder als Sample-Pack verkaufen.
- Acapellas: teils mit Namensnennung, steht beim jeweiligen Upload.
- Kein Training und keine Verbesserung von Algorithmen oder KI mit diesen Inhalten (darum nie in gehoer-Lerndaten).
Die Herkunft einer Aufnahme prüft Looperman nicht; vor einer Veröffentlichung anhören und die Seite in lizenzen.csv prüfen.
Dieser Ordner liegt bewusst nicht unter „Frei (Netz)“: die Bibliotheks-Skripte würden ihn dort als frei werten.
"""
_LM_NAME = re.compile(r"(?i)^looperman-[a-z]-(\d+)-(\d+)-(.+)$")
_TEILWEISE = (".crdownload", ".download", ".part", ".partial", ".tmp")


def _konten_datei():
    return Path(KONFIG["konfig_ordner"]) / "konten.json"


def looperman_an():
    return bool(_json_lesen(_konten_datei()).get("looperman"))


def looperman_setzen(an):
    d = _json_lesen(_konten_datei())
    d["looperman"] = bool(an)
    ziel = _konten_datei()
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ziel)
    if an:
        WAECHTER.wecken()


def herkunft(pfad):
    """Adressen, von denen der Browser die Datei geladen hat (je System anders gespeichert, plattform.herkunft)."""
    return plattform.herkunft(pfad)


def ist_looperman(adressen):
    return any(re.match(r"^https?://([a-z0-9-]+\.)*looperman\.com(/|$)", a or "", re.I) for a in adressen)


def uebernehmen(datei, adressen):
    """Looperman-Download aus ~/Downloads in die Bibliothek: Ablage, LIZENZ.txt, Beleg, zusaetze.csv, Messen, Klang.
    -> endgültiger Pfad."""
    roh = Path(datei)
    ziel_ordner = Path(KONFIG["looperman_ziel"])
    ziel_ordner.mkdir(parents=True, exist_ok=True)
    if not (ziel_ordner / "LIZENZ.txt").exists():
        (ziel_ordner / "LIZENZ.txt").write_text(LOOPERMAN_LIZENZ, encoding="utf-8")
        (ziel_ordner / "QUELLE.txt").write_text("https://www.looperman.com/loops\n", encoding="utf-8")
    m = _LM_NAME.match(roh.stem)
    titel = m.group(3).replace("-", " ").replace("_", " ").strip() if m else roh.stem
    ident = m.group(2) if m else ""
    name = _sauberer_name(f"{titel} (Looperman {ident})" if ident else titel, 120) + roh.suffix.lower()
    seite = next((a for a in adressen if re.search(r"looperman\.com/(loops|tracks|acapellas)/detail/", a)), "") or \
        next((a for a in adressen if ist_looperman([a])), "")
    ziel = ziel_ordner / name
    n = 2
    while ziel.exists():
        if _md5(ziel) == _md5(roh):        # schon einmal geladen: die Kopie bleibt in Downloads liegen
            return str(ziel)
        ziel = ziel_ordner / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    shutil.move(str(roh), str(ziel))
    f = {"tags": [], "lizenz": "ungeprüft", "hinweis": LOOPERMAN_HINWEIS, "seite": seite, "urheber": "",
         "quelle": "looperman", "titel_original": titel, "verdacht": [], "lizenz_url": "https://www.looperman.com/help/terms",
         "beleg": {"schluessel": f"looperman:{ident or ziel.stem}", "quelle": "looperman", "seite": seite,
                   "herkunft": adressen, "datei_original": roh.name,
                   "lizenz": "Looperman-Bedingungen (siehe LIZENZ.txt)"}}
    _einsortieren_und_eintragen(ziel, {"seite": seite}, "einzel", f)
    KONFIG["fertig_datei"]([str(ziel)])
    _index_bald_neu(sofort=True)
    return str(ziel)


class Waechter:
    """Schaut alle 3 s in ~/Downloads, solange Looperman verbunden ist. Übernommen wird nur, was fertig geladen ist
    (zweimal gleiche Größe und Zeit), Audio ist und laut Browser von looperman.com kommt."""
    TAKT_S = 3.0

    def __init__(self):
        self.lock = threading.Lock()
        self.wecker = threading.Event()
        self.vorher = {}            # Pfad -> (Größe, mtime) beim letzten Blick
        self.erledigt = set()       # nicht von Looperman oder fehlgeschlagen: nicht jedes Mal neu prüfen
        self.stand = {"zuletzt": [], "fehler": "", "geprueft_um": 0.0}
        self.gestartet = False

    def starten(self):
        if not self.gestartet:
            self.gestartet = True
            threading.Thread(target=self._lauf, daemon=True, name="looperman").start()

    def wecken(self):
        self.wecker.set()

    def _lauf(self):
        while True:
            self.wecker.wait(self.TAKT_S)
            self.wecker.clear()
            if not looperman_an() or not KONFIG.get("looperman_ziel"):
                continue
            try:
                self._blick()
            except Exception as e:
                with self.lock:
                    self.stand["fehler"] = f"{type(e).__name__}: {e}"[:200]

    def _blick(self):
        ordner = Path(KONFIG["downloads"])
        jetzt = {}
        try:
            eintraege = list(os.scandir(ordner))
        except OSError as e:
            # deutsch statt der Systemmeldung („No such file or directory“, Nutzertest 2)
            grund = {2: "den Ordner gibt es nicht", 1: "macOS gibt Open Libary keinen Zugriff (Systemeinstellungen, "
                     "Datenschutz, Dateien und Ordner: Downloads)", 13: "macOS gibt Open Libary keinen Zugriff "
                     "(Systemeinstellungen, Datenschutz, Dateien und Ordner: Downloads)"}.get(e.errno, "nicht lesbar")
            with self.lock:
                self.stand["fehler"] = f"Downloads ({ordner}): {grund}"
            return
        for e in eintraege:
            n = e.name
            if not e.is_file() or n.startswith(".") or n.lower().endswith(_TEILWEISE) or \
                    not n.lower().endswith(AUDIO_ENDUNGEN) or e.path in self.erledigt:
                continue
            st = e.stat()
            jetzt[e.path] = (st.st_size, st.st_mtime)
        for pfad, kennung in jetzt.items():
            if self.vorher.get(pfad) != kennung or kennung[0] == 0:
                continue            # lädt noch (oder gerade erst aufgetaucht): beim nächsten Blick
            adressen = herkunft(pfad)
            if not ist_looperman(adressen):
                self.erledigt.add(pfad)
                continue
            try:
                ziel = uebernehmen(pfad, adressen)
                with self.lock:
                    self.stand["zuletzt"] = ([{"pfad": ziel, "um": time.time()}] + self.stand["zuletzt"])[:20]
                    self.stand["fehler"] = ""
            except Exception as ex:
                self.erledigt.add(pfad)
                with self.lock:
                    self.stand["fehler"] = f"{os.path.basename(pfad)}: {type(ex).__name__}: {ex}"[:200]
        self.vorher = jetzt
        with self.lock:
            self.stand["geprueft_um"] = time.time()

    def bericht(self):
        with self.lock:
            return dict(self.stand, an=looperman_an(), downloads=str(KONFIG["downloads"]),
                        ziel=str(KONFIG.get("looperman_ziel") or ""))


WAECHTER = Waechter()


def uebernommen(quelle, anzahl=30):
    """Die zuletzt aus dem Kontofenster übernommenen Dateien (neueste zuerst), nur solche, die noch da sind."""
    aus = []
    for r in reversed(_zusaetze_zeilen()):
        if r.get("quelle") == quelle and os.path.isfile(r.get("pfad") or ""):
            aus.append({"pfad": r["pfad"], "datei": r.get("datei"), "seite": r.get("quelle_url"),
                        "geladen_am": r.get("geladen_am"), "urheber": r.get("urheber")})
            if len(aus) >= anzahl:
                break
    return aus


def pfad_von_seite(seite):
    """Schon geladener Netz-Sound (Seite des Treffers) -> Pfad in der Bibliothek oder None."""
    for r in reversed(_zusaetze_zeilen()):
        if seite and r.get("quelle_url") == seite and os.path.isfile(r.get("pfad") or ""):
            return r["pfad"]
    return None


def _index_bald_neu(sofort=False):
    """Index höchstens alle 10 s neu laden, im Hintergrund (vorher lud jede Anfrage selbst neu: Spitzen bis 9 s)."""
    jetzt = time.time()
    if sofort or jetzt - _NACH_LADEN["zeit"] > 10:
        _NACH_LADEN["zeit"] = jetzt
        threading.Thread(target=KONFIG["nach_laden"], daemon=True).start()


def abbrechen(schluessel):
    l = LADUNGEN.get(schluessel)
    if not l or l.status != "laeuft":
        return False
    l.abbrechen = True
    return True


def stand():
    # fertige Ladungen nach einer Stunde vergessen
    for k in [k for k, l in LADUNGEN.items() if l.ende and time.time() - l.ende > 3600]:
        del LADUNGEN[k]
    return {k: l.bericht() for k, l in LADUNGEN.items()}
