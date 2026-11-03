"""Sound fingerprint per file: “Similar” used to compare only type, tempo and key, not the sound. Here PE-A-Frame
(gehoer, facebook/pe-a-frame-base, Apache-2.0, local) computes one vector per file: mean of the frame vectors over the
first 30 s, only frames up to 30 dB below the loudest (gehoer klang.pe_mittel), normalized, stored as float16 in a
SQLite file. The key is the md5 of the file (moves such as FL Studio 2025 -> 2026 then cost nothing), without md5
“pfad:<path>”.

Measured on 03.11.2026 (M3 Pro, MPS): 0.19 s per file (median); on 48 files of six types, 107 of 144 nearest
neighbors were of the same type.

Runs in the gehoer environment (numpy, torch):
  ~/.local/share/gehoer/venv/bin/python klang_vektoren.py --liste auftrag.tsv     (per line: schluessel TAB pfad)
  ~/.local/share/gehoer/venv/bin/python klang_vektoren.py --alle                  (bestand.csv, zusaetze.csv, eigene.csv)
Keys already computed are skipped. Progress and errors are kept in the database (tables meta, fehler).
"""
import argparse
import csv
import fcntl
import json
import os
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

HIER = Path(__file__).resolve().parent
try:                                     # own setup as in openlibary.py (lokal.json, not in the repo)
    with open(HIER / "lokal.json", encoding="utf-8") as _f:
        LOKAL = {k: os.path.expanduser(v) for k, v in json.load(_f).items() if isinstance(v, str) and v}
except (OSError, ValueError, AttributeError):
    LOKAL = {}
GEHOER_CODE = Path(os.environ.get("OPENLIBARY_GEHOER_CODE") or LOKAL.get("gehoer_code") or Path.home() / ".local/share/gehoer/code")
DB = Path(os.environ.get("OPENLIBARY_KLANG_DB", Path.home() / ".cache/gehoer/openlibary_klang.sqlite"))
MODELL = "pe-a-frame-base, Mittel 30 s, -30 dB"       # stored with each vector, kept as is (Mittel: mean)
FENSTER_S = 30.0


def db_oeffnen(pfad=DB):
    pfad.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(pfad), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS vektoren (schluessel TEXT PRIMARY KEY, pfad TEXT, vektor BLOB, modell TEXT, am TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS fehler (schluessel TEXT PRIMARY KEY, pfad TEXT, text TEXT, am TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return con


def schluessel(md5, pfad):
    return md5 if md5 else "pfad:" + pfad


def alle_auftraege():
    """All files from the sources of the index, the first mention wins (as in the index)."""
    musik = Path(LOKAL["musik"]) if LOKAL.get("musik") else HIER / "kein-musikordner"
    quellen = [Path(os.environ.get("OPENLIBARY_BESTAND") or musik / "Bibliothek/bestand.csv"),
               Path(os.environ.get("OPENLIBARY_ZUSAETZE", HIER / "zusaetze.csv")),
               Path(os.environ.get("OPENLIBARY_EIGENE", HIER / "eigene.csv"))]
    gesehen, aus = set(), []
    for q in quellen:
        if not q.is_file():
            continue
        with open(q, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                p = unicodedata.normalize("NFC", os.path.expanduser(r.get("pfad") or ""))     # like the index
                if not p or p in gesehen:
                    continue
                gesehen.add(p)
                aus.append((schluessel(r.get("md5") or "", p), p))
    return aus


def sperre():
    """Only one run at a time, even when the server restarts and leaves a run behind: otherwise two writers on the
    same database and two models on the GPU. The lock holds until the process ends."""
    DB.parent.mkdir(parents=True, exist_ok=True)
    f = open(str(DB) + ".lauf", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("already running", flush=True)
        sys.exit(3)
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--liste")
    ap.add_argument("--alle", action="store_true")
    a = ap.parse_args()
    if a.liste:
        with open(a.liste, encoding="utf-8") as f:
            auftraege = [tuple(z.rstrip("\n").split("\t", 1)) for z in f if "\t" in z]
    elif a.alle:
        auftraege = alle_auftraege()
    else:
        ap.error("--liste or --alle")
    _sperre = sperre()     # noqa: F841 (stays open until the end)
    con = db_oeffnen()
    fertig = {r[0] for r in con.execute("SELECT schluessel FROM vektoren")} | \
        {r[0] for r in con.execute("SELECT schluessel FROM fehler")}
    offen, doppelt = [], set()
    for k, p in auftraege:
        if k not in fertig and k not in doppelt and os.path.isfile(p):
            offen.append((k, p))
            doppelt.add(k)
    gesamt = len(offen)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('lauf', ?)", (f"{os.getpid()} {time.time():.0f} 0 {gesamt}",))
    con.commit()
    if not offen:
        print("nothing to compute", flush=True)
        return

    sys.path.insert(0, str(GEHOER_CODE))
    import numpy as np
    from gehoer import klang, laden
    if klang._pe() is None:
        print("PE-A-Frame missing: " + str(klang._PE.get("grund")), flush=True)
        sys.exit(2)
    t0, n = time.time(), 0
    for k, p in offen:
        try:
            a = laden.lade(p, sr=klang.PE_SR, mono=True, dauer_s=FENSTER_S)
            a = np.asarray(a[0] if isinstance(a, tuple) else a, np.float32)[: int(FENSTER_S * klang.PE_SR)]
            if a.size < klang.PE_HOP or float(np.max(np.abs(a))) < 1e-5:
                raise ValueError("silent or too short")
            frames = klang._pe_stueck(a)
            v = klang.pe_mittel(frames, klang.frame_pegel(a, len(frames)))
            v = v / (np.linalg.norm(v) + 1e-9)
            con.execute("INSERT OR REPLACE INTO vektoren VALUES (?, ?, ?, ?, ?)",
                        (k, p, v.astype(np.float16).tobytes(), MODELL, time.strftime("%Y-%m-%d %H:%M")))
        except Exception as e:      # a broken file does not stop the run
            con.execute("INSERT OR REPLACE INTO fehler VALUES (?, ?, ?, ?)",
                        (k, p, f"{type(e).__name__}: {e}"[:300], time.strftime("%Y-%m-%d %H:%M")))
        n += 1
        if n % 50 == 0 or n == gesamt:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('lauf', ?)", (f"{os.getpid()} {t0:.0f} {n} {gesamt}",))
            con.commit()
        if n % 500 == 0:
            print(f"{n}/{gesamt} in {time.time() - t0:.0f} s", flush=True)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('lauf', ?)", (f"0 {t0:.0f} {n} {gesamt}",))
    con.commit()
    print(f"done: {n} files in {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    main()
