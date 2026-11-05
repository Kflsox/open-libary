"""Web part of Open Libary: search, check, download and file free samples.

Sources (none needs a key)
  archive.org   Collections. The uploader enters the license, so CD rips and manufacturer packs also sit there
                as "CC0" (measured 2026-10-19: a Nujabes CD rip, Loopmasters breaks, Zero-G). verdacht() checks title,
                description, creator, identifier; everything downloaded stays "ungeprüft" (unchecked).
  Openverse     Single sounds from Freesound (only source=freesound: otherwise Jamendo songs and Wikimedia
                pronunciations come along). Only the HQ preview as MP3 (128 kbps) can be downloaded, the original needs a
                Freesound sign-in. Response time 2 to 45 s, 4 of 10 searches ended with 502/504 on 2026-10-20, the second
                attempt often came back after 0.1 s: hence a 20 s limit, one automatic retry and a cache per search.
                Without an account at most 20 per page and 240 results in total.
  Wikimedia     Commons, original files (WAV, FLAC, OGG) with a license per file. Only CC0, public domain and CC BY;
                BY-SA and NC are dropped, so are MIDI and pronunciation recordings (LL-…).
  ccMixter      Acapellas, stems and samples as originals (FLAC/MP3) with a license per upload. Only CC BY and CC0 (most
                of it is NC or Sampling+). At most 20 per query, more gives an empty response; so do quick repeated queries.

Downloading
  A pool with LADE_PARALLEL connections works per file (archive.org serial 0.053 files/s, parallel 0.62);
  large files in parts (446 instead of 14 KB/s). Per file WIEDERHOLEN attempts, resume, overall time limit; an error
  skips only that file. Every finished file is filed right away (type, loop/one-shot, BPM, key, duration,
  sample rate, channels) and written to zusaetze.csv; plus a record (metadata at download time) under _belege/.
"""
import codecs
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

from i18n import tx, N_

UA = {"User-Agent": "OpenLibary/1.2 (local sample manager)"}
http.client._MAXLINE = 1 << 20   # ccMixter sends a header line over 64 KB, otherwise Python aborts with LineTooLong
LADE_PARALLEL = 6
WIEDERHOLEN = 3
GROSS_BYTES = 300 * 1024 * 1024     # above this the interface asks first
AUDIO_ENDUNGEN = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg")
ZUSATZ_SPALTEN = ["datei", "pfad", "pack", "art", "tempo", "tonart", "dauer", "lizenz_gefunden", "format",
                  "loop_oneshot", "stimmung", "herkunft", "quelle_url", "lizenz_url", "geladen_am", "md5", "hinweis",
                  "quelldatei", "samplerate", "kanaele", "tags", "urheber", "quelle"]
EINZEL_ORDNER = "Einzelsounds"

# set by openlibary.py
import plattform     # Mac, Windows, Linux

KONFIG = {"ziel": None, "ziel_drums": None, "zusaetze": None, "konfig_ordner": plattform.konfigordner(),
          # Looperman downloads from the browser move from "downloads" into "looperman_ziel"
          "downloads": Path(os.environ.get("OPENLIBARY_DOWNLOADS", Path.home() / "Downloads")), "looperman_ziel": None, "ffprobe": plattform.werkzeug("ffprobe") or "ffprobe",
          "md5_bestand": set, "nach_laden": lambda: None, "geprueft_urls": lambda: set(),
          "fertig_datei": lambda pfade: None}    # receives every file once it is filed (openlibary: gehoer analyzes it)


class NichtGefunden(Exception):
    pass


class QuelleFehler(Exception):
    """Source throttles, does not respond, or the input is unusable: report as 400/429/503/504 instead of 500."""
    def __init__(self, text, code=503):
        super().__init__(text)
        self.code = code


def _cp1252_ersatz(e):
    """A single byte that is not UTF-8 is read as Windows-1252: ccMixter sends some descriptions that way (byte 0x94 =
    a closing quote), the rest of the same response is UTF-8."""
    return e.object[e.start:e.start + 1].decode("cp1252", errors="replace"), e.start + 1


codecs.register_error("openlibary_cp1252", _cp1252_ersatz)


def _holen(url, timeout=25, versuche=1, name=N_("The source")):
    """Fetch JSON; on a timeout, 5xx or empty response, try once more after a short pause."""
    letzter = None
    for v in range(versuche + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                roh = r.read()
            if not roh.strip():
                raise ValueError(tx("empty response"))
            return json.loads(roh.decode("utf-8", errors="openlibary_cp1252"))
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
        raise QuelleFehler(tx("{name} is not responding (timed out after {timeout} s", name=tx(name), timeout=timeout)
                           + (tx(", tried twice)") if versuche else ")"), 504)
    raise QuelleFehler(tx("{name} is not responding: {error}", name=tx(name), error=letzter), 503)


# old interface for openlibary.py and tests
def http_json(url, timeout=25):
    return _holen(url, timeout, 0)


_SUCHE_CACHE = {}       # (source, q, license, page, extra) -> (time, result)
_SUCHE_LOCK = threading.Lock()


def suche_vergessen():
    """Forget cached search results (they carry texts in the language of the moment, e.g. after a language switch)."""
    with _SUCHE_LOCK:
        _SUCHE_CACHE.clear()


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
        return "pdm"      # Public Domain Mark: a claim, not a license
    if "publicdomain" in url or kurz == "cc0":
        return "cc0"
    if kurz in ("public domain", "pd") or kurz.startswith(("pd-", "pd ")):
        return "pd"       # "public domain" without a license URL (Wikimedia): claimed, not CC0 (a reviewer found a Liszt recording)
    if re.search(r"/licenses/by/|^cc[ -]by[ -]\d", url or kurz) and not re.search(r"-sa|-nc|-nd|sampling", url + kurz):
        return "ccby"
    if "/licenses/" in url or kurz.startswith("cc"):
        return "andere"
    return "keine"


def suchtext_pruefen(q):
    q = re.sub(r"\s+", " ", (q or "").strip())
    if not q:
        raise QuelleFehler(tx("Please enter a search term"), 400)
    return q[:200]


# ================================================================ Filing

_ARTEN = [   # order matters: the first match wins. Atmo, Foley, Glitch before the drums (in testing doorbells
             # became "melodisch", "Glitch Loop" became "kick", rain got no type)
    # with word boundaries: without them "Velocity Lead", "Sine wave", "Electricity" and "City Pop Chords" became atmo
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
    """'HiTom' -> 'Hi Tom', 'ClosedHH' -> 'Closed HH', 'kick_loop' -> 'kick loop' (a reviewer found HiTom without a type)"""
    s = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", s)
    s = re.sub(r"(?i)(hat)(open|closed)", r"\1 \2", s)
    return s.lower().replace("_", " ")


def einordnen(name, ordner="", dauer=None):
    """Type, loop/one-shot, BPM, key from file and folder name (like bestand.csv), plus the duration. The type comes
    from the file name first; the folder (or the tags) only when the name says nothing."""
    name_text, ordner_text = _getrennt(os.path.splitext(name)[0]), _getrennt(ordner)
    text = f"{ordner_text} {name_text}"
    art = next((a for a, rx in _ARTEN if re.search(rx, name_text)), "")
    # "Sn 808", "808 CH", "808 Cowbell": one hit of the TR-808 drum machine, not an 808 bass ("Sn 808.wav" used to rank
    # first for "808"). If the name also names another hit, that one wins.
    if art == "808" and re.search(r"\b(sn|snr|sd|snare|clap|cp|hh|ch|oh|hats?|cow ?bell|cb|conga|cg|toms?|lt|mt|ht|rim|rs|"
                                  r"clave|cl|cymbal|cy|crash|ride|perc|shaker|maracas?|ma)\b", name_text):
        ohne_808 = re.sub(r"808", " ", name_text)
        ohne_808 = re.sub(r"\bsn\b|\bsnr\b", " snare ", re.sub(r"\b(ch|oh)\b", " hat ", re.sub(r"\bcp\b", " clap ", ohne_808)))
        ohne_808 = re.sub(r"\b(cb|cow ?bell|conga|cg|clave|cl|ma|maracas?)\b", " perc ", ohne_808)
        ohne_808 = re.sub(r"\b(lt|mt|ht)\b", " tom ", re.sub(r"\b(cy)\b", " cymbal ", re.sub(r"\brs\b", " rim ", ohne_808)))
        art = next((a for a, rx in _ARTEN if re.search(rx, ohne_808)), art)
    if not art and re.search(r"\bdrums?\b|\bbeat\b", name_text) and (dauer or 0) >= 2.5:
        art = "break"          # check "Epic Tribal Drums" before the tags (a "bass" tag turned it into bass)
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
    if art in _KEIN_LOOP_VON_SELBST:          # FX and atmos do not become loops because of their length
        lo = "loop" if "loop" in name_text else ("oneshot" if dauer is not None and dauer < 10 else "")
    elif art in ("break", "vocal"):           # short breaks and chops are still not one-shots
        lo = "loop" if ist_loop or bpm or (dauer or 0) >= 1.5 else ""
    else:
        lo = "loop" if ist_loop or (bpm and dauer and dauer >= 1.9) else ("oneshot" if dauer is not None and dauer < 2.5 else "")
    if not art and re.search(r"drums?\b|perc", text) and (lo == "loop" or (dauer or 0) >= 2.5):
        art, lo = "break", "loop"
    if bpm is None and lo == "loop" and not ist_loop:
        # a loop only through its folder or length ("Drums/Loops/Garage Shuffle 132"): a free number in the name is the
        # tempo here too, as in names with "loop" (the light analysis heard half of it, 66)
        bpm = next((int(k) for k in _BPM_FREI.findall(os.path.splitext(name)[0]) if 60 <= int(k) <= 200), None)
    if not art and dauer is not None and dauer >= 30:
        art = "atmo"                          # long recording without a hint: treat it as a field recording
    tonart = ""
    if art not in ("kick", "snare", "clap", "rim", "hat", "cymbal", "perc", "break", "fx", "atmo", "foley", "glitch"):
        t = _TONART.search(os.path.splitext(name)[0])
        # a bare letter only counts between _ or - ("Loop_C_120"), not inside a sentence ("I am a dreamer")
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
    """Duration, sample rate, channels via ffprobe."""
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


# ================================================================ License suspicion

_VERDACHT = [   # strong hints at someone else's material
    (r"\brip\b|\bripped\b|cd[\s_-]?rip|vinyl[\s_-]?rip|\bflac rip|\b320\s?kbps|not ripped by me", N_("Rip from CD or vinyl")),
    (r"from (the )?vinyl|from records?|sampled from|resampled|off (the )?records?|von platte|sped up some", N_("sampled from records")),
    (r"\balbum\b|\bdiscography|greatest hits|soundtrack|\bost\b|\blabel:|\brecords\b|gravadora|\bselo\b|"
     r"released by|lançado", N_("Album or label release")),
    # narrower (a reviewer found false alarms: "Mixed in Audacity", "Radio static", "youtube podcast")
    (r"mixed by|mixtape|\bdj[\s-]?mix|\bdj set\b|mashup|bootleg|\bremix(ed)?\b", N_("Mix or mashup")),
    (r"\bepisode\s*\d|podcast episode|radio show|broadcast|\blive at\b|\blive in\b|in concert|\binterview\b", N_("Broadcast or live recording")),
    (r"all rights reserved|respective owners|copyright ©|℗", N_("Rights reserved in the text")),
    # manufacturers only as a whole word and never in the account name ("SpliceSound" is a Freesound account)
    (r"\bloopmasters\b|\bzero-?g\b|\bsplice\b|\bcymatics\b|\bbest service\b|\bsample magic\b|\bvengeance\b|"
     r"\bbig fish audio\b|\bprime loops\b|\bghost syndicate\b|\bcapsun\b|\bproducer loops\b|\bueberschall\b|"
     r"\bspectrasonics\b|\bpixabay\b|\bmixkit\b|\bzapsplat\b|\bsoundsnap\b|\benvato\b|\baudiojungle\b|\bepidemic sound\b",
     N_("Name of a manufacturer or shop")),
    (r"funky drummer|\bamen\b|think break|\(think\)|apache|impeach|levee|skull snaps|synthetic substitution|"
     r"ultimate breaks|james brown|soul pride|famous[\s_-]sample|clyde stubblefield", N_("well-known record break")),
]


def _jahr(datum, beschreibung):
    """Recording year from the date field; from the description only with "Year:" or "recorded in" before it ("Rhythm
    Ace von 1972" is the year the device was built, not the recording year)."""
    m = re.match(r"\s*((?:18[89]|19\d|20[0-2])\d)\b", datum or "")
    if not m:
        m = re.search(r"(?:year:?\s*|recorded (?:in |on )?)((?:18[89]|19\d|20[0-2])\d)\b", beschreibung or "", re.I)
    return int(m.group(1)) if m else None


def verdacht(titel, beschreibung="", urheber="", groesse=0, datum="", kennung="", tags=()):
    """(gruende, hinweise): reasons why a supposed CC0 is probably someone else's material, and weak hints.
    Empty means: nothing found, not: safe."""
    text = f"{titel} {beschreibung} {urheber} {kennung.replace('_', ' ').replace('-', ' ')}".lower()
    ohne_konto = f"{titel} {beschreibung}".lower()
    gruende = [tx(g) for rx, g in _VERDACHT if re.search(rx, ohne_konto if g.startswith("Name of a") else text)]
    # check tags only for manufacturers and record breaks: "remix" (ccMixter, every upload) and "soundtrack" (Freesound:
    # fits film music) are everyday words there
    tag_text = " ".join(tags).lower()
    gruende += [tx(g) for rx, g in _VERDACHT[-2:] if re.search(rx, tag_text) and tx(g) not in gruende]
    hinweise = []
    if groesse and groesse > 400_000_000:
        hinweise.append(tx("over 400 MB"))
    j = _jahr(datum, beschreibung)
    grenze = time.localtime().tm_year - 101      # USA: recordings enter the public domain 100 years after the end of the year
    if j and 1880 <= j <= grenze:
        hinweise.append(tx("Recording from {year}: public domain in the US, check the composition and EU law", year=j))
    elif j and grenze < j < 1975:
        gruende.append(tx("Recording from {year}: still protected in the US", year=j))
    return list(dict.fromkeys(gruende)), hinweise


# ================================================================ Relevance agent

OLLAMA_EMBED = "http://127.0.0.1:11434/api/embed"
EMBED_MODELL = "bge-m3"          # local meaning model (Ollama), free and offline
_EMB = {}                        # text -> vector
_EMB_LOCK = threading.Lock()
_EINSCHUSS = {"kick", "snare", "clap", "rim", "hat", "cymbal", "tom", "perc", "808"}
_MUELL = re.compile(r"(?i)\b(test|untitled|new recording|recording \d+|audio \d+|voice memo|track \d+)\b")
_VERWANDT = {"kick": {"808"}, "808": {"kick", "bass"}, "hat": {"cymbal"}, "cymbal": {"hat"}, "perc": {"tom", "rim"},
             "snare": {"clap", "rim"}, "clap": {"snare"}, "break": {"perc"}, "fx": {"atmo", "glitch"}, "atmo": {"fx", "foley"}}
_GERAEUSCH = re.compile(r"(?i)\b(punch|fist|karate|martial|fight|football|soccer|ball|door|horse|kung ?fu|body ?hit|"
                        r"slap|stomp on|kicking (a|the)|sports?)\b")


def _einbetten(texte):
    """Vectors for texts from bge-m3; None when Ollama is not running (then only the rules count)."""
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
    """Scores results 0 to 100 for producers: closeness in meaning to the search (bge-m3) plus rules on type, length,
    original or preview, tempo and key, suspicion, junk titles. Writes relevanz and warum (reasons) into every
    result. The number is comparable across all sources (fixed mapping of the similarity, no normalization per list)."""
    if not treffer:
        return treffer
    absicht = einordnen(q, "", None)
    q_art, q_loop = absicht["art"], bool(re.search(r"(?i)\bloops?\b|\bbreak", q))
    vek = _einbetten([q.lower()] + [text_fn(t) for t in treffer])
    for i, t in enumerate(treffer):
        punkte, warum = 0.0, []
        if vek and vek[0] and vek[i + 1]:
            aehnlich = sum(a * b for a, b in zip(vek[0], vek[i + 1]))
            sem = min(1.0, max(0.0, (aehnlich - 0.38) / 0.32))     # 0.38 ~ arbitrary, 0.70 ~ very close (measured)
            punkte += 55 * sem
            if sem > 0.6:
                warum.append(tx("matches the search"))
            elif sem < 0.2:
                warum.append(tx("far from the search in content"))
        else:
            # without a meaning model (Ollama off, other computers): if all search words are in the title or tags, it
            # counts like "matches", otherwise the result stays below the page's threshold (it used to get a flat 40)
            inhalt = (text_fn(t) or "").lower()
            alle = all(re.search(r"(?<![a-z0-9])" + re.escape(w), inhalt) for w in re.findall(r"\w+", q.lower()) if len(w) > 1)
            punkte += 40 if alle else 5
            warum.append(tx("all search words in the title") if alle else tx("search words missing from the title"))
        art, dauer = t.get("art") or "", t.get("dauer")
        if q_art and art == q_art:
            punkte += 18
            warum.append(tx("type {art}, as searched", art=art))
        elif q_art and art in _VERWANDT.get(q_art, ()):
            punkte += 10                                   # 808 for "kick", cymbal for "hat"
            warum.append(tx("{art}, related to {wanted}", art=art, wanted=q_art))
        elif q_art and art and art != q_art and not sammlung:
            punkte -= 10
            warum.append(tx("is {art}, searched for {wanted}", art=art, wanted=q_art))
        if not sammlung and dauer is not None:
            if q_art in _EINSCHUSS and not q_loop:
                if dauer <= 3:
                    punkte += 10
                    warum.append(tx("short like a one-shot"))
                elif dauer > 10:
                    punkte -= 15
                    warum.append(tx("too long for a one-shot"))
            elif q_loop or q_art in ("break", "melodisch", "bass", "vocal"):
                if 1.5 <= dauer <= 60:
                    punkte += 6
                elif dauer > 180:
                    punkte -= 8
                    warum.append(tx("full song, not a loop"))
            if dauer > 600 and q_art != "atmo":
                punkte -= 10
                warum.append(tx("over 10 minutes"))
        if not sammlung:
            if t.get("format") in ("wav", "flac", "aiff"):
                punkte += 6
                warum.append(tx("original in full quality"))
            elif t.get("quelle") == "freesound":
                warum.append(tx("MP3 preview only"))
            if t.get("bpm") or t.get("tonart"):
                punkte += 4
        else:
            dl = t.get("downloads") or 0
            if dl:
                punkte += min(12, 3 * (len(str(int(dl))) - 1))    # roughly by order of magnitude of the downloads
            if (t.get("groesse") or 0) > 400_000_000:
                punkte -= 8
        if t.get("verdacht"):
            punkte -= 40
            warum.append(f"{tx('Suspicion')}: " + t["verdacht"][0])
        if t.get("lizenz") in ("pd", "pdm"):
            punkte -= 8
        if _MUELL.search(t.get("titel") or ""):
            punkte -= 10
            warum.append(tx("meaningless title"))
        if q_art in _EINSCHUSS and _GERAEUSCH.search(f"{t.get('titel', '')} {' '.join(t.get('tags') or [])}"):
            punkte -= 25                                   # "Fist Punch or kick" is a hit, not a bass drum
            warum.append(tx("noise, not a drum sample"))
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
    "ccby": "licenseurl:(*publicdomain* OR *licenses\\/by\\/*)",   # an unescaped slash is a syntax error
}
NUR_SAMPLES = ('(subject:(samples OR sample OR "sample pack" OR drumkit OR "drum kit" OR loops OR oneshots '
               'OR "drum machine" OR "sound effects" OR sfx OR "field recording" OR ambience OR foley OR "sound effect") '
               'OR title:(samples OR sample OR kit OR loops OR "sample pack" OR sfx OR "sound effects"))')
OHNE_SAMMLUNGEN = ("NOT collection:(librivoxaudio OR podcasts OR audio_podcast OR radioprograms OR oldtimeradio "
                   "OR audio_bookspoetry OR netlabels)")


def _lucene_sauber(text):
    t = re.sub(r'[+\-!(){}\[\]^"~*?:\\/&|]', " ", text)
    t = re.sub(r"\b(AND|OR|NOT|TO)\b", " ", t)          # operators count as words (in testing: "NOT door")
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
    """(archive identifier, file name in the original) of all files downloaded so far, also from unpacked ZIPs."""
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
        raise QuelleFehler(tx("The search term contains only special characters"), 400)

    def abfrage(woerter_oder):
        kern = " OR ".join(text.split()) if woerter_oder else text
        a = f"({kern}) AND mediatype:audio AND {LIZENZ_FILTER.get(lizenz, LIZENZ_FILTER['cc0'])} AND {OHNE_SAMMLUNGEN}"
        if nur_samples:
            a += " AND " + NUR_SAMPLES
        params = [("q", a), ("rows", "40"), ("page", str(max(1, seite))), ("output", "json"), ("sort[]", "downloads desc")]
        for f in ("identifier", "title", "licenseurl", "downloads", "item_size", "creator", "date", "description",
                  "uploader", "publicdate"):
            params.append(("fl[]", f))
        for versuch in range(2):   # BACKEND_ERROR shows up there at times (a reviewer hit it with "sitar" under CC BY)
            d = _holen("https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params), 25, 1, "archive.org")
            if "response" in d:
                return d["response"]
            time.sleep(1.5)
        raise QuelleFehler(tx("archive.org has an internal error with this search right now; try again in a moment"), 503)

    def rechnen():
        antwort, gelockert = abfrage(False), False
        if not antwort.get("numFound") and len(text.split()) > 1:   # all words: often 0 (in testing 3 of 5)
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
    relevanz(q, treffer, _text_sammlung, sammlung=True)     # suspicious ones sink to the bottom
    if gelockert:
        # "amen break" (CC0) found nothing with both words; relaxed, it brought 40 city sounds with "Break" in the title
        # and up to 46 points. Whatever does not contain all search words sinks below the page's threshold (30) and
        # stays visible only through "show".
        def gleich(text):    # "Lo-Fi" like "lofi", "drums" like "drum"
            return re.sub(r"lo[\s_-]?fi", "lofi", re.sub(r"hi[\s_-]?hat", "hihat", text.lower()))
        such = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"\w+", gleich(q)) if len(w) > 1]
        for t in treffer:
            inhalt = gleich(f"{t['titel']} {t['beschreibung']}")
            # at the start of a word: "amen" must not match inside "Sacramento", "break" not inside "Daybreak"
            if not all(re.search(r"(?<![a-z0-9])" + re.escape(w), inhalt) for w in such):
                t["relevanz"] = min(t["relevanz"], 20)
                t["warum"] = [tx("does not contain all search words")] + t["warum"][:3]
        treffer.sort(key=lambda t: -t["relevanz"])
    vorausladen([t["id"] for t in treffer if not t["verdacht"]][:8])
    return {"gesamt": antwort.get("numFound", 0), "treffer": treffer, "gelockert": gelockert, "seite": seite}


_META = {}            # identifier -> (time, metadata); archive.org took up to 60 s per file list (2026-10-19)
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
        raise NichtGefunden(tx("invalid identifier"))
    d = _metadaten(ident)
    if not d or not d.get("metadata"):
        # archive.org returns an empty response for blocked items (in testing: JV-11759-1937)
        raise NichtGefunden(tx("{ident} is no longer available on archive.org, probably withdrawn for rights reasons", ident=ident))
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
                            "schon_da": False, "nicht_lesbar": tx("Open Libary cannot unpack RAR and 7z")})
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
        if isinstance(s, str) and ":" in s:   # archive.org sometimes writes lengths as mm:ss
            teile = [float(x) for x in s.split(":")]
            return round(sum(t * 60 ** i for i, t in enumerate(reversed(teile))), 3)
        return round(float(s), 3)
    except ValueError:
        return None


# ================================================================ Single sounds: Freesound (Openverse), Wikimedia, ccMixter

_EINZEL = {}      # "quelle:id" -> result from our own response; only what is listed here can be downloaded
_ENDUNG = {"zip": "zip", "mp32": "mp3", "mpeg": "mp3", "mp3": "mp3", "wav": "wav", "x-wav": "wav", "wave": "wav", "flac": "flac",
           "x-flac": "flac", "ogg": "ogg", "oga": "ogg", "aiff": "aiff", "aif": "aiff", "x-aiff": "aiff"}


def quell_kopf(url):
    """ccMixter blocks requests without its own Referer (hotlink protection; a reviewer got 403 on every file)."""
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
        gruende.append(tx("only a Public Domain Mark: a claim, not a license"))
    if lizenz == "pd":
        gruende.append(tx("only claimed as “public domain”, without a license URL: check the recording and the composition"))
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


# ---------------------------------------------------------------- Freesound directly
# Through Openverse "808" took 29 s and "rhodes chord" ended with 504, and there was only the MP3 preview. With its own
# API access (freesound.org/apiv2/apply: client ID and client secret, the secret is also the API key) Open Libary
# searches directly; after a one-time sign-in (OAuth2, token 24 h, renewed through the refresh token) it downloads the
# original via /apiv2/sounds/<id>/download/. Source of these flows: freesound.org/docs/api/authentication.html and
# resources_apiv2.html (read on 2026-11-03: /apiv2/search/ has replaced /search/text/ since 11/2025).
FS_API = "https://freesound.org/apiv2"
FS_FELDER = "id,name,tags,description,license,type,duration,filesize,samplerate,username,previews,url,bpm"
FS_SEITE = 30
_FS_LOCK = threading.Lock()


def _fs_datei():
    return Path(KONFIG["konfig_ordner"]) / "freesound.json"


def _fs_zugang_datei():
    """API access (client ID, secret) per user. Not in the code folder: the Freesound terms forbid passing keys on to
    third parties ("may be revoked if you share them with any third party", checked 2026-11-03)."""
    return Path(KONFIG["konfig_ordner"]) / "freesound_zugang.json"


def _json_lesen(pfad):
    try:
        with open(pfad, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def freesound_konto():
    """Personal sign-in (token, state) plus client ID and secret from the app access (freesound_app.json)."""
    d = _json_lesen(_fs_datei())
    app = _json_lesen(_fs_zugang_datei())
    if app.get("client_id") and app.get("client_secret"):
        if d.get("client_id") and d["client_id"] != app["client_id"]:     # different app access: the old sign-in no longer applies
            d = {}
        d.update(client_id=app["client_id"], client_secret=app["client_secret"])
    return d


def _fs_speichern(d):
    d = {k: v for k, v in d.items() if k != "client_secret"}      # the secret is stored only in the app access file
    ziel = _fs_datei()
    ziel.parent.mkdir(parents=True, exist_ok=True)
    tmp = ziel.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # key and token: readable by the user only
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ziel)


def freesound_stand():
    """For Settings, without secrets: {"schluessel": bool, "login": bool, "name": str}."""
    d = freesound_konto()
    return {"schluessel": bool(d.get("client_secret")), "login": bool(d.get("refresh_token")),
            "client_id": d.get("client_id", ""), "name": d.get("benutzer", "")}


def freesound_schluessel_setzen(client_id, client_secret):
    """Checks the key with a small search and saves it; -> (ok, text)."""
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{8,80}", client_secret) or not re.fullmatch(r"[A-Za-z0-9]{4,80}", client_id):
        return False, tx("Paste the client ID and client secret from freesound.org/apiv2/apply (letters and digits only)")
    try:
        _holen(f"{FS_API}/search/?" + urllib.parse.urlencode({"query": "kick", "page_size": "1", "token": client_secret}),
               12, 0, "Freesound")
    except urllib.error.HTTPError as e:
        return False, tx("Freesound rejects the key ({code}). Is the client secret correct?", code=e.code)
    except QuelleFehler as e:
        return False, str(e)
    with _FS_LOCK:
        ziel = _fs_zugang_datei()
        ziel.parent.mkdir(parents=True, exist_ok=True)
        tmp = ziel.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"client_id": client_id, "client_secret": client_secret,
                       "hinweis": "your own Freesound API access; do not share it (Freesound terms)"},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, ziel)
        d = _json_lesen(_fs_datei())
        if d.get("client_id") and d["client_id"] != client_id:      # different access: the old sign-in no longer applies
            _fs_speichern({})
    return True, tx("Access saved: Freesound now searches directly; “Connect” then fetches the originals")


def freesound_trennen():
    with _FS_LOCK:
        d = freesound_konto()
        for k in ("access_token", "refresh_token", "ablauf", "benutzer", "state"):
            d.pop(k, None)
        _fs_speichern(d)


def freesound_login_url():
    """URL for the one-time sign-in; remembers state against foreign callbacks."""
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
    """Exchange the code from the callback (or pasted by hand) for a token; -> (ok, text). The code is valid for 10 min,
    once."""
    code = (code or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{6,80}", code):
        return False, tx("The code is missing or looks wrong")
    with _FS_LOCK:
        d = freesound_konto()
        if not d.get("client_secret"):
            return False, tx("Enter the client ID and client secret first")
        if state is not None and state != d.get("state"):
            return False, tx("The callback does not belong to this sign-in (state does not match); start the sign-in again")
        try:
            t = _fs_token_holen({"client_id": d["client_id"], "client_secret": d["client_secret"],
                                 "grant_type": "authorization_code", "code": code})
        except urllib.error.HTTPError as e:
            return False, tx("Freesound does not accept the code ({code}); it is valid for 10 minutes and only once", code=e.code)
        except (OSError, ValueError) as e:
            return False, tx("Freesound is not responding: {error}", error=e)
        d.update(access_token=t["access_token"], refresh_token=t["refresh_token"],
                 ablauf=time.time() + float(t.get("expires_in") or 86399) - 120)
        d.pop("state", None)
        _fs_speichern(d)
    try:    # name for Settings (not needed for downloading)
        ich = _fs_mit_token(f"{FS_API}/me/")
        with _FS_LOCK:
            d = freesound_konto()
            d["benutzer"] = ich.get("username", "")
            _fs_speichern(d)
    except Exception:
        pass
    return True, tx("Connected to Freesound: downloads now come as originals")


def freesound_token():
    """Valid access token or None (no sign-in, revoked). Renews it through the refresh token when it expires."""
    with _FS_LOCK:
        d = freesound_konto()
        if not d.get("refresh_token"):
            return None
        if d.get("access_token") and time.time() < float(d.get("ablauf") or 0):
            return d["access_token"]
        try:
            t = _fs_token_holen({"client_id": d["client_id"], "client_secret": d["client_secret"],
                                 "grant_type": "refresh_token", "refresh_token": d["refresh_token"]})
        except urllib.error.HTTPError:      # revoked or expired: the sign-in is gone, the preview stays
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
        raise QuelleFehler(tx("not signed in to Freesound"), 401)
    req = urllib.request.Request(url, headers=dict(UA, Authorization="Bearer " + tok))
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


_FS_ZEITEN = []          # start times of original downloads (Freesound: 30 per minute and 500 per day per access key)


def _fs_drossel(je_minute=25):
    """Start at most 25 originals per minute (the API docs say 30; LADE_PARALLEL 6 broke that limit with kits)."""
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
                raise QuelleFehler(tx("Freesound rejects the API key; enter it again in Settings"), 401) from None
            raise QuelleFehler(tx("Freesound is not accepting requests right now; try again in a minute"), 429) from None

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
        # only formats the index and the DAW read reliably; m4a and others come as the preview (m4a would otherwise have
        # been saved as .mp3 with AAC inside)
        original_hier = original and typ in ("wav", "aif", "aiff", "flac", "mp3", "ogg")
        bpm = r.get("bpm") if isinstance(r.get("bpm"), (int, float)) and 40 <= r.get("bpm") <= 250 else None
        t = _einzel_treffer(
            "freesound", r.get("id"), r.get("name"), r.get("username"), lz, r.get("license"),
            r.get("url") or f"https://freesound.org/s/{r.get('id')}/", vorschau,
            typ if original_hier else "mp3", r.get("duration"), r.get("filesize") if original_hier else None,
            list(r.get("tags") or []), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.get("description") or ""))[:300],
            bpm=bpm,
            hinweis=(tx("Original from Freesound ({format})", format=typ) if original_hier
                     else tx("HQ preview (MP3); Open Libary does not download the original ({format})", format=typ) if original
                     else tx("HQ preview (MP3); Open Libary downloads the original after the Freesound sign-in in Settings")))
        if typ in ("wav", "aif", "aiff", "flac", "mp3", "ogg"):
            t["original_url"] = f"{FS_API}/sounds/{r.get('id')}/download/"
            t["original_format"] = _endung(typ)
        treffer.append(t)
    relevanz(q, treffer, _text_einzel)
    gesamt = d.get("count") or 0
    return {"gesamt": gesamt, "treffer": _geladen_markieren(treffer), "seite": seite, "mehr": seite * FS_SEITE < gesamt,
            "weg": "direkt", "original": original}


def freesound_suche(q, lizenz="cc0", seite=1):
    """Freesound directly (with your own API key) or through Openverse."""
    q = suchtext_pruefen(q)
    konto = freesound_konto()
    if konto.get("client_secret"):
        return freesound_direkt_suche(q, lizenz, seite, konto)
    if seite > 12:
        raise QuelleFehler(tx("Without an account Openverse shows at most 240 results (12 pages)"), 400)
    lizenzen = "cc0,pdm,by" if lizenz == "ccby" else "cc0,pdm"   # unknown counts as the stricter one
    params = {"q": q, "license": lizenzen, "source": "freesound", "page_size": "20", "page": str(seite)}

    def rechnen():
        try:
            # "808" took 29 s, "rhodes chord" ended with 504; stop after 15 s
            return _holen("https://api.openverse.org/v1/audio/?" + urllib.parse.urlencode(params), 15, 0, "Freesound (Openverse)")
        except urllib.error.HTTPError as e:
            raise QuelleFehler(tx("Openverse is not accepting requests right now; try again in a minute"), 429) from None

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
            hinweis=tx("HQ preview (MP3 128 kbps); Freesound releases the original only after sign-in")))
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
        if re.match(r"(?i)LL-|.*pronunciation|^[a-z]{2,3}-[^ ]+\.(ogg|wav|mp3)$", titel):   # pronunciation recordings
            continue
        em = ii.get("extmetadata") or {}
        kurz = (em.get("LicenseShortName") or {}).get("value", "")
        lz = lizenz_art((em.get("LicenseUrl") or {}).get("value", ""), kurz)
        if lz not in (("cc0", "pd") if lizenz == "cc0" else ("cc0", "pd", "ccby")):
            continue
        dauer = None   # the length is in the file metadata (Ogg: length, WAV/FLAC: playtime_seconds)
        for m in ii.get("metadata") or []:
            if m.get("name") in ("length", "playtime_seconds"):
                dauer = zahl_oder_none(m.get("value"))
                break
        urheber = html.unescape(re.sub(r"<[^>]+>", " ", (em.get("Artist") or {}).get("value", "")))
        urheber = re.sub(r"\s+", " ", urheber).strip()
        if not urheber or re.search(r"(?i)unknown author|unbekannt", urheber):
            urheber = tx("Unknown author")
        beschr = html.unescape(re.sub(r"<[^>]+>", " ", (em.get("ImageDescription") or {}).get("value", ""))).strip()
        tags = [c for c in re.split(r"\|", (em.get("Categories") or {}).get("value", "")) if c][:8]
        treffer.append(_einzel_treffer(
            "wikimedia", p.get("pageid"), titel, urheber[:80], lz,
            (em.get("LicenseUrl") or {}).get("value", ""),   # do not invent a URL when the page names none
            ii.get("descriptionurl"), ii.get("url"), mime, dauer, ii.get("size"), tags, beschr[:300],
            hinweis=tx("Original from Wikimedia Commons, license according to the page: {license}", license=kurz)))
    relevanz(q, treffer, _text_einzel)
    return {"gesamt": (d.get("query") or {}).get("searchinfo", {}).get("totalhits", len(treffer)),
            "treffer": _geladen_markieren(treffer), "seite": seite, "mehr": "continue" in d}


_CCM_LOCK = threading.Lock()
_CCM_ZEIT = {"letzte": 0.0}


def ccmixter_suche(q, lizenz="cc0", seite=1):
    """Acapellas, stems, samples. Only CC BY and CC0 (cc0 filter: only CC0; there are hardly any)."""
    q = suchtext_pruefen(q)
    params = {"f": "json", "search": q, "limit": "20", "offset": str((seite - 1) * 20), "sort": "rank"}

    def rechnen():
        with _CCM_LOCK:   # quick repeated queries get an empty response there
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
            continue      # skip full remixes without samples, stems or acapellas
        dateien = x.get("files") or []
        typ = "Acapella" if "acappella" in alle_tags or "acapella" in alle_tags else (
            "Stems" if "stem" in alle_tags else "Sample")
        # stems mostly come as an attached ZIP there (otherwise only one track came, found by a reviewer); the ZIP is
        # unpacked on download
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
            tags[:10], "",   # the description there is almost always "remix of …"
            bpm=ext.get("bpm") if isinstance(ext.get("bpm"), int) else None,
            hinweis=tx("Original from ccMixter ({license})", license=x.get('license_name', ''))))
        treffer[-1]["hoer_url"] = hoer.get("download_url")   # preview through the MP3, even when the ZIP is downloaded
    relevanz(q, treffer, _text_einzel)
    return {"gesamt": None, "treffer": _geladen_markieren(treffer), "seite": seite, "mehr": len(d) >= 20}


EINZEL_QUELLEN = {"freesound": freesound_suche, "wikimedia": wikimedia_suche, "ccmixter": ccmixter_suche}


# ================================================================ Downloading

class Ladung:
    def __init__(self, schluessel, titel, ordner):
        self.schluessel, self.titel, self.ordner = schluessel, titel, ordner
        self.status, self.text = "laeuft", ""
        self.dateien_gesamt = self.dateien_fertig = self.uebersprungen = 0
        self.bytes_gesamt = self.bytes_fertig = 0
        self.fehler = []           # (name, reason)
        self.abbrechen = False
        self.start = time.time()
        self.ende = None
        self.pack = ""             # pack name in the library, for "Show in library"
        self.pfade = []            # files filed so far (the download list shows the analysis state per file)
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
    """Where a single sound goes: a named kit together, otherwise by type into the FL browser folders. Drum one-shots
    to Drum Kits/Frei (Netz)/Einzelsounds/<type>, loops, vocals, FX, atmos to Samples/Frei (Netz)/Einzelsounds/<type>."""
    if kit:
        return Path(KONFIG["ziel"]) / EINZEL_ORDNER / kit
    art = t.get("art") or ""
    drums = art in DRUM_ARTEN and t.get("lo") != "loop"
    return Path(KONFIG["ziel_drums"] if drums else KONFIG["ziel"]) / EINZEL_ORDNER / ART_ORDNER.get(art, "Sonstiges")


def _sauberer_name(s, laenge=80):
    if plattform.WINDOWS:
        laenge = min(laenge, 60)        # MAX_PATH 260: folder and file together must not get too long there
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]+', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip().lstrip(". ")    # "." and ".." would be folder references
    if len(s) > laenge:     # shortened: a short hash of the full name, otherwise "… take 1" and "… take 2" would match
        s = s[:laenge - 7].rstrip(". ") + " " + hashlib.md5(s.encode()).hexdigest()[:6]
    s = s.rstrip(". ")      # Windows: no dot and no space at the end of a name
    # Windows: CON, PRN, AUX, NUL, COM1 to 9, LPT1 to 9 are devices, even with an extension ("con.wav" would write to
    # the console; checked under CrossOver)
    if plattform.WINDOWS and re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?", s):
        s = "_" + s
    return s


def laden_starten(quelle, ident="", auswahl=None, bestaetigt=False, ids=None, ordner=""):
    """Starts a download. quelle "archive" (ident, auswahl) or "einzel" (ids from the last search, ordner).
    Returns (code, dict): 200 started, 409 already running or needs confirmation."""
    if quelle == "archive":
        schluessel = f"archive:{ident}"
    elif quelle == "einzel":
        ids = [i for i in (ids or []) if i in _EINZEL]
        if not ids:
            return 400, {"fehler": tx("These sounds are not from one of the recent searches; please search again")}
        schluessel = "einzel:" + hashlib.md5("|".join(sorted(ids)).encode()).hexdigest()[:12]
    else:
        return 400, {"fehler": tx("unknown source")}
    alt = LADUNGEN.get(schluessel)
    if alt and alt.status == "laeuft":
        return 409, {"fehler": tx("This download is already running"), "schluessel": schluessel}

    if quelle == "archive":
        info = archive_dateien(ident)
        if info["lizenz"] not in ("cc0", "ccby", "pdm"):
            return 400, {"fehler": tx("License is {license}: neither CC0 nor CC BY", license=info['lizenz_url'] or tx("not stated"))}
        dateien = [dict(f, lizenz=info["lizenz"], lizenz_url=info["lizenz_url"], urheber=info["urheber"],
                        seite=info["seite"], quelle="archive.org")
                   for f in info["dateien"] if (not auswahl or f["name"] in auswahl)
                   and not f.get("nicht_lesbar") and not f.get("schon_da")]
        # drum packs (mostly one-shots) into the FL folder Drum Kits, everything else into Samples
        arten = [f.get("art") for f in dateien]
        drums = sum(1 for a, f in zip(arten, dateien) if a in DRUM_ARTEN and f.get("lo") != "loop") > len(dateien) / 2
        ziel = Path(KONFIG["ziel_drums"] if drums else KONFIG["ziel"]) / ordner_name(info["titel"])
        titel, pack = info["titel"], ordner_name(info["titel"])
    else:
        kit = _sauberer_name(ordner)       # empty: every sound by its type into the matching FL folder
        dateien, info = [], {"seite": "", "lizenz": "", "lizenz_url": ""}
        seiten = _geladene_seiten()
        for i in ids:
            t = _EINZEL[i]
            # never take a URL from the caller (a reviewer otherwise got file:///etc/hosts into the download folder)
            if not str(t.get("url", "")).startswith("https://") and not (t["quelle"] == "ccmixter" and t["url"].startswith("http://ccmixter.org/")):
                continue
            if t["lizenz"] not in ("cc0", "ccby", "pdm", "pd") or (t["seite"] and t["seite"] in seiten):
                continue
            name = _sauberer_name(re.sub(r"(?i)\.(wav|aiff?|flac|mp3|ogg)$", "", t["titel"]) + " - " + (t["urheber"] or "unbekannt"), 120)
            url, fmt, hinweis, auth = t["url"], t["format"], t["hinweis"], ""
            # Freesound directly with a sign-in: the original instead of the preview
            if t.get("original_url") and freesound_token():
                url, fmt, auth = t["original_url"], t["original_format"], "freesound"
                hinweis = tx("Original from Freesound ({format})", format=fmt)
            dateien.append({"name": f"{name}.{fmt}", "groesse": t.get("groesse") or 0, "url": url, "md5": "", "auth": auth,
                            "lizenz": t["lizenz"], "lizenz_url": t["lizenz_url"], "urheber": t["urheber"],
                            "seite": t["seite"], "hinweis": hinweis, "tags": t["tags"], "quelle": t["quelle"],
                            "titel_original": t["titel"], "beleg": t, "verdacht": t["verdacht"],
                            "zip": t["format"] == "zip", "zielordner": einzel_zielordner(t, kit)})
        # same name, different source (same title and creator, or a leftover from before): add a short hash of the
        # source, otherwise _eine_datei_laden would take the other file as "already downloaded" (previews have size 0)
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
            titel = tx("{n} sounds", n=len(dateien)) + (f" {tx('in “{kit}”', kit=kit)}" if kit else tx(", sorted by type"))
    if not dateien:
        return 200, {"ok": True, "hinweis": tx("Nothing to download: everything is already in the library")}
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
                with ladung.lock:   # a ZIP counts as many files as it contains
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
                KONFIG["fertig_datei"]([str(p) for p in neue])    # into the analysis queue
            except Exception as e:
                print(f"Analysis not queued: {e}", flush=True)
        except Exception as e:   # one broken file does not hold up the others
            with ladung.lock:
                if ladung.abbrechen and "abgebrochen" in str(e):
                    ladung.uebersprungen += 1      # canceling is not an error (in testing it counted as 6 errors)
                else:
                    ladung.fehler.append((f["name"], str(e)[:200]))
        finally:
            with ladung.lock:
                offen["n"] -= 1
                letzte = offen["n"] == 0
            if letzte:
                # reload the index first, then report "fertig": otherwise the interface asks too early and sees only
                # part of the files (in testing 60 of 99); until then "eintragen" instead of "laeuft"
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
    """QUELLE.txt, LIZENZ.txt and the metadata at download time (in case the item is changed or blocked later)."""
    jetzt = time.strftime("%Y-%m-%d %H:%M")
    urheber = info.get("urheber") or "not stated"
    (ziel / "QUELLE.txt").write_text(
        f"Source: {info['seite']}\nTitle: {info['titel']}\nCreator according to the source: {urheber}\n"
        f"Uploaded by account: {info.get('konto') or 'not stated'} on {info.get('hochgeladen') or '?'}\n"
        f"Downloaded: {jetzt} with Open Libary\nMetadata at download time: _belege/metadaten.json\n", encoding="utf-8")
    hinweis = (f'Attribution required: "{info["titel"]}" by {urheber} ({info["seite"]}), license {info["lizenz_url"]}\n'
               if info["lizenz"] == "ccby" else "No attribution required.\n")
    verd = ("\nSUSPICION at download time: " + "; ".join(info.get("verdacht") or []) + "\n") if info.get("verdacht") else ""
    pdm = "\nOnly a Public Domain Mark: a claim by the uploader, not a license.\n" if info["lizenz"] == "pdm" else ""
    (ziel / "LIZENZ.txt").write_text(
        f"License according to archive.org: {info['lizenz_url']}\n{hinweis}{verd}{pdm}\n"
        "UNCHECKED: the uploader entered the license. Before commercial use, check that they are the author "
        "(no record breaks, no manufacturer packs, for old recordings the year and the composition).\n", encoding="utf-8")
    (ziel / "_belege").mkdir(exist_ok=True)
    (ziel / "_belege" / "metadaten.json").write_text(
        json.dumps({"geladen_am": jetzt, "seite": info["seite"], "metadaten": info.get("metadaten")},
                   ensure_ascii=False, indent=1), encoding="utf-8")


STUECK_AB = 1_500_000   # larger files in STUECKE parts at once (archive.org throttles each connection to ~15 KB/s)
STUECKE = 6


def _in_stuecken_laden(ladung, f, tmp):
    """Downloads a large file in STUECKE byte ranges in parallel and joins them. False when the server cannot serve
    ranges (then the caller downloads it in one piece)."""
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
                        raise ValueError("no range support")
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
        fehler.append(RuntimeError(tx("part {i} failed after {n} attempts", i=i, n=WIEDERHOLEN)))

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
    # in parts only for archive.org (it helps there, factor 30); ccMixter aborts parallel ranges
    if (f.get("groesse") or 0) >= STUECK_AB and "archive.org/" in f["url"] and _in_stuecken_laden(ladung, f, tmp):
        if f.get("md5"):
            h = hashlib.md5()
            with open(tmp, "rb") as fh:
                for s in iter(lambda: fh.read(1 << 20), b""):
                    h.update(s)
            if h.hexdigest() != f["md5"]:
                tmp.unlink()
                raise ValueError(tx("checksum mismatch (downloaded in parts)"))
        os.replace(tmp, pfad)
        return pfad
    frist = time.time() + 120 + (f["groesse"] or 0) / 20_000   # 20 KB/s as the lower bound, plus 2 min
    letzter = None
    for versuch in range(WIEDERHOLEN):
        if ladung.abbrechen:
            raise RuntimeError("abgebrochen")
        schon = tmp.stat().st_size if tmp.is_file() else 0
        kopf = dict(UA, **quell_kopf(f["url"]))
        if f.get("auth") == "freesound":       # original through the API: only with a valid sign-in
            _fs_drossel()
            tok = freesound_token()
            if not tok:
                raise QuelleFehler(tx("Freesound sign-in expired or revoked; reconnect in Settings"), 401)
            kopf["Authorization"] = "Bearer " + tok
        if schon:
            kopf["Range"] = f"bytes={schon}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(f["url"], headers=kopf), timeout=30) as r:
                if not f.get("groesse") and not schon:   # unknown size: progress from Content-Length
                    try:
                        f["groesse"] = int(r.headers.get("Content-Length") or 0)
                        with ladung.lock:
                            ladung.bytes_gesamt += f["groesse"]
                    except ValueError:
                        pass
                if schon and r.status != 206:   # the server cannot resume: start over
                    with ladung.lock:
                        ladung.bytes_fertig -= schon
                    schon = 0
                with open(tmp, "ab" if schon else "wb") as out:
                    while True:
                        if ladung.abbrechen:
                            raise RuntimeError("abgebrochen")
                        if time.time() > frist:
                            raise TimeoutError(tx("overall time limit exceeded"))
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
                    raise ValueError(tx("checksum mismatch"))
            os.replace(tmp, pfad)
            return pfad
        except urllib.error.HTTPError as e:
            if e.code == 429 and f.get("auth") == "freesound":   # the app's quota at Freesound
                raise QuelleFehler(tx("Freesound is not allowing more originals right now (at most 30 per minute and 500 per day per access key); try again later, or use the preview without sign-in"), 429) from None
            letzter = e                     # otherwise as before: wait briefly, try again
            time.sleep((2, 6, 15)[versuch])
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            letzter = e
            if "abgebrochen" in str(e):
                raise
            time.sleep((2, 6, 15)[versuch])
    raise RuntimeError(tx("after {n} attempts: {error}", n=WIEDERHOLEN, error=letzter))


def _zip_entpacken(zip_pfad, ordner):
    ziel = ordner / (_sauberer_name(Path(zip_pfad).stem) or "Pack")
    wurzel = ziel.resolve()
    neue, belegt = [], set()
    with zipfile.ZipFile(zip_pfad) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or not name.lower().endswith(AUDIO_ENDUNGEN) or "__MACOSX" in name:
                continue
            # names in the zip are foreign input: drop the drive ("C:"), the root, ".." and characters forbidden on
            # Windows, clean each part on its own (before, "C:\\..\\x.wav" stayed a way out on Windows)
            teile = [t for t in name.split("/") if t not in ("", ".", "..")]
            if not teile:
                continue
            endung = Path(teile[-1]).suffix.lower()
            ordner_teile = [t for t in (_sauberer_name(t) for t in teile[:-1]) if t]
            datei = (_sauberer_name(teile[-1][: -len(endung)], 80 - len(endung)) or "Sample") + endung
            p = ziel.joinpath(*ordner_teile, datei)
            if plattform.WINDOWS and len(str(p)) > 240:      # deeply nested: flat with a short hash instead of failing
                p = ziel / (hashlib.md5(name.encode()).hexdigest()[:6] + " " + datei)
            if str(p).lower() in belegt:        # two entries, one name (cleaning, case): do not overwrite
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
            # only the folders inside the pack count (before, the whole path went in: "scratchpad" made everything "melodisch")
            pack, innen, basis = teil.parent.name, str(pfad.parent.relative_to(teil)), teil.parent
            break
    tags = f.get("tags") or []
    e = einordnen(pfad.name, " ".join(([] if innen == "." else [innen]) + tags[:8]), dauer)
    if f.get("beleg") and f["beleg"].get("bpm") and not e["bpm"]:
        e["bpm"] = f["beleg"]["bpm"]
    lizenz = f.get("lizenz") or info.get("lizenz")
    hinweise = [f.get("hinweis") or ""]
    if lizenz == "ccby":
        hinweise.append(tx("CC BY: attribution required ({author})", author=f.get('urheber') or info.get('urheber') or tx("author: see the record")))
    if lizenz == "pdm":
        hinweise.append(tx("only a Public Domain Mark, not a license"))
    if dauer and dauer >= 30 and e["art"] in ("atmo", "fx", "foley", ""):
        hinweise.append(tx("Field recording, {time} min", time=f"{int(dauer // 60)}:{int(dauer % 60):02d}"))
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
            if alte_spalten != ZUSATZ_SPALTEN:   # new columns: rewrite the file once with all columns
                with open(zus, "w", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=ZUSATZ_SPALTEN, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(zeilen)
        with open(zus, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=ZUSATZ_SPALTEN, extrasaction="ignore")
            if neu:
                w.writeheader()
            w.writerow(zeile)
        if quelle == "einzel":   # record per sound: a row in lizenzen.csv and the result data at download time
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


# ---------------------------------------------------------------- Looperman
# Looperman has no API and forbids programs ("We strictly prohibit unauthorised access or downloading of data using
# any computer program") and embedding the site ("frame, mask"), looperman.com/help/terms, read 2026-11-03.
# So Open Libary never queries Looperman itself and does not show the site: you search and download in your own
# browser with your own account. When Looperman is connected (konten.json), the watcher moves finished downloads from
# ~/Downloads whose origin is looperman.com (kMDItemWhereFroms, written by the browser on download) into the library.
LOOPERMAN_HINWEIS = N_("Looperman: downloaded with your own account in the browser; royalty-free in your own music, do not pass it on as a loop or sample; not for AI training; origin of the uploaded recording not checked")
LOOPERMAN_LIZENZ = """Looperman (looperman.com), downloaded with your own account in the browser, filed by Open Libary.

Terms according to looperman.com/help/terms (read on 2026-11-03), summary:
- Registered users may copy, edit and use loops royalty-free in their own works, including selling them.
  The license applies to your own account and is not transferable.
- Do not pass on the loops themselves or sell them as a sample pack.
- Acapellas: some require attribution, stated on each upload.
- No training or improving of algorithms or AI with this content (so never in gehoer training data).
Looperman does not check where a recording comes from; before a release, listen to it and check the page in lizenzen.csv.
This folder deliberately does not sit under "Frei (Netz)": the library scripts would treat it as free there.
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
    """URLs the browser downloaded the file from (stored differently on each system, plattform.herkunft)."""
    return plattform.herkunft(pfad)


def ist_looperman(adressen):
    return any(re.match(r"^https?://([a-z0-9-]+\.)*looperman\.com(/|$)", a or "", re.I) for a in adressen)


def uebernehmen(datei, adressen):
    """Looperman download from ~/Downloads into the library: placement, LIZENZ.txt, record, zusaetze.csv, analysis,
    sound fingerprint. -> final path."""
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
        if _md5(ziel) == _md5(roh):        # already downloaded once: the copy stays in Downloads
            return str(ziel)
        ziel = ziel_ordner / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    shutil.move(str(roh), str(ziel))
    f = {"tags": [], "lizenz": "ungeprüft", "hinweis": tx(LOOPERMAN_HINWEIS), "seite": seite, "urheber": "",
         "quelle": "looperman", "titel_original": titel, "verdacht": [], "lizenz_url": "https://www.looperman.com/help/terms",
         "beleg": {"schluessel": f"looperman:{ident or ziel.stem}", "quelle": "looperman", "seite": seite,
                   "herkunft": adressen, "datei_original": roh.name,
                   "lizenz": "Looperman terms (see LIZENZ.txt)"}}
    _einsortieren_und_eintragen(ziel, {"seite": seite}, "einzel", f)
    KONFIG["fertig_datei"]([str(ziel)])
    _index_bald_neu(sofort=True)
    return str(ziel)


class Waechter:
    """Looks into ~/Downloads every 3 s while Looperman is connected. Takes over only what has finished downloading
    (same size and time twice), is audio and, according to the browser, comes from looperman.com."""
    TAKT_S = 3.0

    def __init__(self):
        self.lock = threading.Lock()
        self.wecker = threading.Event()
        self.vorher = {}            # path -> (size, mtime) at the last look
        self.erledigt = set()       # not from Looperman or failed: do not check again every time
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
            # our own wording instead of the system message ("No such file or directory" confused a tester)
            grund = {2: tx("the folder does not exist"), 1: tx("macOS does not give Open Libary access (System Settings, Privacy & Security, Files and Folders: Downloads)"),
                     13: tx("macOS does not give Open Libary access (System Settings, Privacy & Security, Files and Folders: Downloads)")}.get(e.errno, tx("not readable"))
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
                continue            # still downloading (or just appeared): at the next look
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
    """The files most recently taken over from the account window (newest first), only those still present."""
    aus = []
    for r in reversed(_zusaetze_zeilen()):
        if r.get("quelle") == quelle and os.path.isfile(r.get("pfad") or ""):
            aus.append({"pfad": r["pfad"], "datei": r.get("datei"), "seite": r.get("quelle_url"),
                        "geladen_am": r.get("geladen_am"), "urheber": r.get("urheber")})
            if len(aus) >= anzahl:
                break
    return aus


def pfad_von_seite(seite):
    """Web sound already downloaded (page of the result) -> path in the library, or None."""
    for r in reversed(_zusaetze_zeilen()):
        if seite and r.get("quelle_url") == seite and os.path.isfile(r.get("pfad") or ""):
            return r["pfad"]
    return None


def _index_bald_neu(sofort=False):
    """Reload the index at most every 10 s, in the background (before, every request reloaded it: peaks up to 9 s)."""
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
    # forget finished downloads after an hour
    for k in [k for k, l in LADUNGEN.items() if l.ende and time.time() - l.ende > 3600]:
        del LADUNGEN[k]
    return {k: l.bericht() for k, l in LADUNGEN.items()}
