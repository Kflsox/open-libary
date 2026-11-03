"""Smoke test for Open Libary on Mac, Windows and Linux (locally and in GitHub Actions).

Builds a small test library from synthetic sounds (no third-party samples in the repo), starts the server the way the
finished apps start it, adds the folder and checks: scanning, lengths, playback of every format, the light analysis
(tempo of a loop, root note of an 808), WAV when dragging, blocked folders, interface language, and a second start on
the same port.

    python tests/rauchtest.py [--python PATH] [--code FOLDER] [--port 8799]

Needs numpy and soundfile (in the Python that runs the test AND the server). Exit 1 when a check fails.
"""
import argparse
import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
FEHLER = []


def pruefe(name, ok, info=""):
    print(f"{'OK    ' if ok else 'FAILED'} {name}{(': ' + str(info)) if info else ''}", flush=True)
    if not ok:
        FEHLER.append(name)


# ------------------------------------------------------------------ test sounds
def kick(dauer=0.5):
    t = np.arange(int(dauer * SR)) / SR
    f = 50 + 70 * np.exp(-t * 25)
    return (np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 6)).astype(np.float32) * 0.9


def ton(freq, dauer, abkling=1.5):
    t = np.arange(int(dauer * SR)) / SR
    return (np.sin(2 * np.pi * freq * t) * np.exp(-t * abkling)).astype(np.float32) * 0.8


def rauschen(dauer, abkling=30.0, saat=1):
    t = np.arange(int(dauer * SR)) / SR
    return (np.random.default_rng(saat).standard_normal(len(t)) * np.exp(-t * abkling)).astype(np.float32) * 0.4


def loop(bpm=120.0, takte=4):
    """Kick on every beat, hi-hat on every eighth: length exactly takte * 4 beats."""
    schlag = 60.0 / bpm
    n = int(round(takte * 4 * schlag * SR))
    x = np.zeros(n, np.float32)
    k, h = kick(0.3), rauschen(0.05, 80.0, 2)
    for i in range(takte * 8):
        p = int(round(i * schlag / 2 * SR))
        quelle = k if i % 2 == 0 else h
        stueck = quelle[: n - p]
        x[p:p + len(stueck)] += stueck
    return np.clip(x, -1, 1)


def ogg_im_wav(x, ziel):
    """Like FL Studio's factory samples: RIFF/WAVE with format tag 0x674F, an Ogg Vorbis stream in the data chunk."""
    b = io.BytesIO()
    sf.write(b, x, SR, format="OGG", subtype="VORBIS")
    ogg = b.getvalue()
    fmt = struct.pack("<HHIIHH", 0x674F, 1, SR, SR * 2, 2, 16)
    daten = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(ogg)) + ogg
    if len(ogg) % 2:
        daten += b"\0"
    Path(ziel).write_bytes(b"RIFF" + struct.pack("<I", 4 + len(daten)) + b"WAVE" + daten)


def bibliothek(ordner):
    """-> {file name: expected length in s}"""
    ordner.mkdir(parents=True)
    erwartet = {}

    def schreibe(name, x, **k):
        sf.write(ordner / name, x, SR, **k)
        erwartet[name] = len(x) / SR

    schreibe("Kick 01.wav", kick(), subtype="PCM_16")
    schreibe("808 A.wav", ton(55.0, 1.5, 0.8), subtype="PCM_24")           # A1 = MIDI 33
    schreibe("Groove Loop.wav", loop(120.0, 4), subtype="PCM_16")          # tempo only from the analysis
    schreibe("Snare 02.aif", rauschen(0.4, 12.0), format="AIFF", subtype="PCM_16")
    schreibe("Pad Cm.flac", ton(261.63, 2.0, 0.5), format="FLAC", subtype="PCM_16")
    schreibe("Atmo.ogg", rauschen(2.0, 0.5, 3), format="OGG", subtype="VORBIS")
    try:
        schreibe("Perc 03.mp3", rauschen(0.6, 8.0, 4), format="MP3", subtype="MPEG_LAYER_III")
    except Exception as e:                       # older libsndfile cannot write MP3: then without it
        print("Note: cannot create MP3,", e)
    x = ton(98.0, 1.0, 2.0)
    ogg_im_wav(x, ordner / "FL Snare.wav")
    erwartet["FL Snare.wav"] = len(x) / SR
    return erwartet


# ------------------------------------------------------------------ Server
def hol(basis, pfad, daten=None, roh=False, frist=60):
    rq = urllib.request.Request(basis + pfad, data=json.dumps(daten).encode() if daten is not None else None,
                                headers={"Content-Type": "application/json"} if daten is not None else {})
    with urllib.request.urlopen(rq, timeout=frist) as r:
        b = r.read()
        return (r.headers.get("Content-Type", ""), b) if roh else json.loads(b)


def fehlertext(basis, pfad, daten):
    """The server's error text for a request that must fail (400)."""
    try:
        hol(basis, pfad, daten)
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}").get("fehler")
    return None


def server_befehl(python, code, port):
    # the way app/main.m and app/fenster/main.js start it
    return [python, "-B", "-E", "-s", "-X", "utf8", str(Path(code) / "openlibary.py"), "--port", str(port), "--ohne-browser"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--code", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--port", type=int, default=8799)
    a = ap.parse_args()
    basis = f"http://127.0.0.1:{a.port}"
    # under the home folder: on the Mac the system temp lives in /private/var, which Open Libary blocks as a system folder
    basis_tmp = Path.home() / ".cache"
    basis_tmp.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="openlibary-rauchtest-", dir=basis_tmp))
    ordner = tmp / "Übungs Kit (Größe)"            # non-ASCII on purpose: umlauts and parentheses in paths
    erwartet = bibliothek(ordner)
    print(f"Test folder {ordner} with {len(erwartet)} files")
    umgebung = dict(os.environ, OPENLIBARY_DATEN=str(tmp / "daten"), OPENLIBARY_PAKET="1",
                    OPENLIBARY_KONFIG=str(tmp / "konfig"), OPENLIBARY_DOWNLOADS=str(tmp / "downloads"),
                    OPENLIBARY_RENDERS=str(tmp / "renders"), OPENLIBARY_NETZ_ZIEL=str(tmp / "netz"),
                    OPENLIBARY_DRUM_ZIEL=str(tmp / "drums"), OPENLIBARY_GEHOER=str(tmp / "kein-gehoer"))
    for d in ("daten", "konfig", "downloads"):
        (tmp / d).mkdir()
    protokoll = open(tmp / "server.log", "w", encoding="utf-8")
    zusatz = {"creationflags": 0x08000000} if os.name == "nt" else {}
    srv = subprocess.Popen(server_befehl(a.python, a.code, a.port), env=umgebung, stdout=protokoll,
                           stderr=subprocess.STDOUT, **zusatz)
    try:
        t0 = time.time()
        while True:
            try:
                st = hol(basis, "/api/stand")
                break
            except (urllib.error.URLError, OSError):
                if srv.poll() is not None or time.time() - t0 > 90:
                    raise SystemExit("Server does not start:\n" + (tmp / "server.log").read_text(encoding="utf-8")[-3000:])
                time.sleep(0.5)
        pruefe("server starts", True, f"{time.time() - t0:.1f} s, system {st.get('system')}, package {st.get('paket')}")

        r = hol(basis, "/api/ordner", {"pfad": str(ordner)})
        pruefe("add folder", r.get("ok"), r.get("text"))
        gesperrt = hol(basis, "/api/ordner", {"pfad": str(Path.home())})
        pruefe("home folder blocked", not gesperrt.get("ok"), gesperrt.get("text"))

        # scanning, then the light analysis (starts 30 s after the server)
        ende = time.time() + 240
        while time.time() < ende:
            o, s = hol(basis, "/api/ordner"), hol(basis, "/api/stand")
            l = o.get("leicht") or {}
            if not o.get("laeuft") and s.get("alle", 0) >= len(erwartet) and l.get("offen") and \
                    not l.get("laeuft") and l.get("fertig", 0) >= l.get("offen", 0):
                break
            time.sleep(2)
        o = hol(basis, "/api/ordner")
        pruefe("light analysis ran", (o.get("leicht") or {}).get("fertig", 0) >= len(erwartet), o.get("leicht"))
        zeilen = {Path(z["pfad"]).name: z for z in hol(basis, "/api/suche?q=").get("zeilen", [])}
        pruefe("all files in the index", set(erwartet) <= set(zeilen), sorted(set(erwartet) - set(zeilen)))

        for name, soll in erwartet.items():
            z = zeilen.get(name) or {}
            ist = z.get("dauer")
            pruefe(f"length {name}", ist is not None and abs(ist - soll) < 0.08, f"{ist} instead of {soll:.3f}")

        lp = zeilen.get("Groove Loop.wav") or {}
        pruefe("loop tempo analyzed", lp.get("bpm") is not None and abs(float(lp["bpm"]) - 120) < 2,
               f"{lp.get('bpm')} ({lp.get('bpm_quelle')})")
        b8 = zeilen.get("808 A.wav") or {}
        pruefe("808 root note analyzed", b8.get("grundton") is not None, f"{b8.get('grundton')} (type {b8.get('art')})")

        for name in erwartet:
            z = zeilen.get(name)
            if not z:
                continue
            try:
                art, daten = hol(basis, "/audio?pfad=" + urllib.parse.quote(z["pfad"]), roh=True)
                ok = len(daten) > 100
                if name.endswith((".aif", "FL Snare.wav")):        # must arrive as PCM WAV, otherwise no browser plays it
                    ok = ok and daten[:4] == b"RIFF" and struct.unpack("<H", daten[20:22])[0] in (1, 3, 0xFFFE)
                pruefe(f"playback {name}", ok, f"{art}, {len(daten)} bytes")
            except Exception as e:
                pruefe(f"playback {name}", False, e)

        pfade = [zeilen[n]["pfad"] for n in ("Pad Cm.flac", "FL Snare.wav", "Kick 01.wav") if n in zeilen]
        r = hol(basis, "/api/als_wav", {"pfade": pfade, "muster": "noetig"})
        neu = r.get("pfade") or []
        pruefe("drag: FLAC as WAV", len(neu) == 3 and neu[0] != pfade[0] and neu[0].lower().endswith(".wav")
               and Path(neu[0]).is_file(), neu[:1])
        pruefe("drag: FL sample as WAV", len(neu) == 3 and neu[1] != pfade[1] and Path(neu[1]).is_file(), neu[1:2])
        pruefe("drag: plain WAV stays", len(neu) == 3 and neu[2] == pfade[2])

        zweiter = subprocess.run(server_befehl(a.python, a.code, a.port), env=umgebung, capture_output=True,
                                 timeout=60, **zusatz)
        ausgabe = (zweiter.stdout + zweiter.stderr).decode("utf-8", "replace")
        pruefe("second start finds the running server", re.search(r"already running|läuft schon", ausgabe) is not None,
               ausgabe.strip()[-120:])
        pruefe("first server keeps answering", hol(basis, "/api/stand").get("pid") == st.get("pid"))
        seite = hol(basis, "/", roh=True)[1].decode("utf-8", "replace")
        pruefe("page is served", "<title>" in seite and "Open Libary" in seite)

        # interface language: English by default, German from lang/de.json for the page and the server's messages
        sp = hol(basis, "/api/sprache")
        pruefe("language: English by default", sp.get("sprache") == "en" and "de" in (sp.get("sprachen") or {}), sp)
        pruefe("language: English page has no table", "const TRANSLATION = {};" in seite and 'const LANG = "en"' in seite)
        pruefe("language: English server message", fehlertext(basis, "/api/stil", {"name": "x"}) == "unknown style")
        pruefe("language: unknown code refused", fehlertext(basis, "/api/sprache", {"sprache": "xx"}) == "unknown language")
        hol(basis, "/api/sprache", {"sprache": "de"})
        seite_de = hol(basis, "/", roh=True)[1].decode("utf-8", "replace")
        pruefe("language: German page carries the table", 'const LANG = "de"' in seite_de
               and '"Library":"Bibliothek"' in seite_de.replace('": "', '":"'))
        pruefe("language: German server message", fehlertext(basis, "/api/stil", {"name": "x"}) == "unbekannter Stil")
        pruefe("language: saved", (tmp / "daten" / "sprache.txt").read_text(encoding="utf-8").strip() == "de")
        hol(basis, "/api/sprache", {"sprache": "en"})
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
        protokoll.close()
        log = (tmp / "server.log").read_text(encoding="utf-8", errors="replace")
        if "Traceback" in log:
            print("--- server log ---\n" + log[-4000:])
            FEHLER.append("traceback in the server log")
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FEHLER)} failed" + (": " + ", ".join(FEHLER) if FEHLER else ""))
    sys.exit(1 if FEHLER else 0)


if __name__ == "__main__":
    main()
