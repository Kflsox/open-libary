"""Rauchtest für Open Libary auf Mac, Windows und Linux (lokal und in GitHub Actions).

Erzeugt eine kleine Test-Bibliothek aus synthetischen Klängen (keine fremden Samples im Repo), startet den Server so,
wie ihn die fertigen Apps starten, legt den Ordner an und prüft: Einlesen, Längen, Wiedergabe jedes Formats, leichte
Messung (Tempo eines Loops, Grundton einer 808), WAV beim Ziehen, gesperrte Ordner, zweiter Start auf demselben Port.

    python tests/rauchtest.py [--python PFAD] [--code ORDNER] [--port 8799]

Braucht numpy und soundfile (im Python, das den Test UND den Server ausführt). Exit 1, wenn eine Prüfung scheitert.
"""
import argparse
import io
import json
import math
import os
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
    print(f"{'OK    ' if ok else 'FEHLER'} {name}{(': ' + str(info)) if info else ''}", flush=True)
    if not ok:
        FEHLER.append(name)


# ------------------------------------------------------------------ Testklänge
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
    """Kick auf jedem Schlag, Hi-Hat auf jedem Achtel: Länge genau takte * 4 Schläge."""
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
    """Wie die Werkssamples von FL Studio: RIFF/WAVE mit Formatkennung 0x674F, im data-Block ein Ogg-Vorbis-Strom."""
    b = io.BytesIO()
    sf.write(b, x, SR, format="OGG", subtype="VORBIS")
    ogg = b.getvalue()
    fmt = struct.pack("<HHIIHH", 0x674F, 1, SR, SR * 2, 2, 16)
    daten = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(ogg)) + ogg
    if len(ogg) % 2:
        daten += b"\0"
    Path(ziel).write_bytes(b"RIFF" + struct.pack("<I", 4 + len(daten)) + b"WAVE" + daten)


def bibliothek(ordner):
    """-> {Dateiname: erwartete Länge in s}"""
    ordner.mkdir(parents=True)
    erwartet = {}

    def schreibe(name, x, **k):
        sf.write(ordner / name, x, SR, **k)
        erwartet[name] = len(x) / SR

    schreibe("Kick 01.wav", kick(), subtype="PCM_16")
    schreibe("808 A.wav", ton(55.0, 1.5, 0.8), subtype="PCM_24")           # A1 = MIDI 33
    schreibe("Groove Loop.wav", loop(120.0, 4), subtype="PCM_16")          # Tempo nur aus der Messung
    schreibe("Snare 02.aif", rauschen(0.4, 12.0), format="AIFF", subtype="PCM_16")
    schreibe("Pad Cm.flac", ton(261.63, 2.0, 0.5), format="FLAC", subtype="PCM_16")
    schreibe("Atmo.ogg", rauschen(2.0, 0.5, 3), format="OGG", subtype="VORBIS")
    try:
        schreibe("Perc 03.mp3", rauschen(0.6, 8.0, 4), format="MP3", subtype="MPEG_LAYER_III")
    except Exception as e:                       # ältere libsndfile ohne MP3-Schreiben: dann eben ohne
        print("Hinweis: MP3 nicht erzeugbar,", e)
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


def server_befehl(python, code, port):
    # so wie app/main.m und app/fenster/main.js ihn starten
    return [python, "-B", "-E", "-s", "-X", "utf8", str(Path(code) / "openlibary.py"), "--port", str(port), "--ohne-browser"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--code", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--port", type=int, default=8799)
    a = ap.parse_args()
    basis = f"http://127.0.0.1:{a.port}"
    # unter dem Benutzerordner: das System-Temp liegt auf dem Mac in /private/var, das Open Libary als Systemordner sperrt
    basis_tmp = Path.home() / ".cache"
    basis_tmp.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="openlibary-rauchtest-", dir=basis_tmp))
    ordner = tmp / "Übungs Kit (Größe)"
    erwartet = bibliothek(ordner)
    print(f"Testordner {ordner} mit {len(erwartet)} Dateien")
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
                    raise SystemExit("Server startet nicht:\n" + (tmp / "server.log").read_text(encoding="utf-8")[-3000:])
                time.sleep(0.5)
        pruefe("Server startet", True, f"{time.time() - t0:.1f} s, System {st.get('system')}, Paket {st.get('paket')}")

        r = hol(basis, "/api/ordner", {"pfad": str(ordner)})
        pruefe("Ordner hinzufügen", r.get("ok"), r.get("text"))
        gesperrt = hol(basis, "/api/ordner", {"pfad": str(Path.home())})
        pruefe("Benutzerordner gesperrt", not gesperrt.get("ok"), gesperrt.get("text"))

        # Einlesen, dann leichte Messung (startet 30 s nach dem Server)
        ende = time.time() + 240
        while time.time() < ende:
            o, s = hol(basis, "/api/ordner"), hol(basis, "/api/stand")
            l = o.get("leicht") or {}
            if not o.get("laeuft") and s.get("alle", 0) >= len(erwartet) and l.get("offen") and \
                    not l.get("laeuft") and l.get("fertig", 0) >= l.get("offen", 0):
                break
            time.sleep(2)
        o = hol(basis, "/api/ordner")
        pruefe("leichte Messung gelaufen", (o.get("leicht") or {}).get("fertig", 0) >= len(erwartet), o.get("leicht"))
        zeilen = {Path(z["pfad"]).name: z for z in hol(basis, "/api/suche?q=").get("zeilen", [])}
        pruefe("alle Dateien im Index", set(erwartet) <= set(zeilen), sorted(set(erwartet) - set(zeilen)))

        for name, soll in erwartet.items():
            z = zeilen.get(name) or {}
            ist = z.get("dauer")
            pruefe(f"Länge {name}", ist is not None and abs(ist - soll) < 0.08, f"{ist} statt {soll:.3f}")

        lp = zeilen.get("Groove Loop.wav") or {}
        pruefe("Tempo des Loops gemessen", lp.get("bpm") is not None and abs(float(lp["bpm"]) - 120) < 2,
               f"{lp.get('bpm')} ({lp.get('bpm_quelle')})")
        b8 = zeilen.get("808 A.wav") or {}
        pruefe("Grundton der 808 gemessen", b8.get("grundton") is not None, f"{b8.get('grundton')} (Art {b8.get('art')})")

        for name in erwartet:
            z = zeilen.get(name)
            if not z:
                continue
            try:
                art, daten = hol(basis, "/audio?pfad=" + urllib.parse.quote(z["pfad"]), roh=True)
                ok = len(daten) > 100
                if name.endswith((".aif", "FL Snare.wav")):        # muss als PCM-WAV kommen, sonst spielt kein Browser
                    ok = ok and daten[:4] == b"RIFF" and struct.unpack("<H", daten[20:22])[0] in (1, 3, 0xFFFE)
                pruefe(f"Wiedergabe {name}", ok, f"{art}, {len(daten)} Bytes")
            except Exception as e:
                pruefe(f"Wiedergabe {name}", False, e)

        pfade = [zeilen[n]["pfad"] for n in ("Pad Cm.flac", "FL Snare.wav", "Kick 01.wav") if n in zeilen]
        r = hol(basis, "/api/als_wav", {"pfade": pfade, "muster": "noetig"})
        neu = r.get("pfade") or []
        pruefe("Ziehen: FLAC als WAV", len(neu) == 3 and neu[0] != pfade[0] and neu[0].lower().endswith(".wav")
               and Path(neu[0]).is_file(), neu[:1])
        pruefe("Ziehen: FL-Sample als WAV", len(neu) == 3 and neu[1] != pfade[1] and Path(neu[1]).is_file(), neu[1:2])
        pruefe("Ziehen: normales WAV bleibt", len(neu) == 3 and neu[2] == pfade[2])

        zweiter = subprocess.run(server_befehl(a.python, a.code, a.port), env=umgebung, capture_output=True,
                                 timeout=60, **zusatz)
        ausgabe = (zweiter.stdout + zweiter.stderr).decode("utf-8", "replace")
        pruefe("zweiter Start erkennt den laufenden Server", "läuft schon" in ausgabe, ausgabe.strip()[-120:])
        pruefe("erster Server antwortet weiter", hol(basis, "/api/stand").get("pid") == st.get("pid"))
        seite = hol(basis, "/", roh=True)[1].decode("utf-8", "replace")
        pruefe("Seite wird ausgeliefert", "<title>" in seite and "Open Libary" in seite)
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
        protokoll.close()
        log = (tmp / "server.log").read_text(encoding="utf-8", errors="replace")
        if "Traceback" in log:
            print("--- Server-Protokoll ---\n" + log[-4000:])
            FEHLER.append("Traceback im Server-Protokoll")
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FEHLER)} Fehler" + (": " + ", ".join(FEHLER) if FEHLER else ""))
    sys.exit(1 if FEHLER else 0)


if __name__ == "__main__":
    main()
