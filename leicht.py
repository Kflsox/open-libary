"""Light analysis: tempo and root note without gehoer, with app/leichtmessen (Swift, Apple libraries only, built by
einrichten.sh). On computers without gehoer, tempo and key used to come only from the file name.

Measured on 03.11.2026 against the manufacturers' values in file names (the author's library):
  Tempo, 300 loops: from confidence 1.5, 227 results: 131 exact, 88 half or double, 8 wrong.
  Tempo again on 05.11.2026, after the octave rule (the bar candidate nearest to 110 BPM instead of weighting by the
  autocorrelation). Chosen only on material that may be learned from (free packs without a clause against machine
  learning, 920 loops, and the synthesized demo library): 97.6 % exact, 13 of 13 demo loops. Checked, measuring only,
  on 1,401 loops of the author's library, one per pack (everything else: Splice, bought and factory packs): exact
  57 % before, 78 % after; shown (confidence from 1.5, now the strongest candidate) 575 right and 439 wrong before,
  847 right and 201 wrong after. What is left is mostly the octave: loops labeled 156 to 190 BPM read half, 60 to
  77 BPM double; the filters still find them with the switch "half/double".
  Root note of 808 and bass, 151 one-shots: 90 with the right pitch class, many others 1 to 2 semitones off.
  Key of loops: 101 of 297 exact, 119 counting the relative key; too unreliable, not used.
So the index takes only tempo (loops without tempo, confidence from 1.5) and root note (808, bass and tonal one-shots
without a pitch, marked uncertain), both with source “leicht”. Name, gehoer and manual corrections always win.

Also a rough sound fingerprint (24 mel bands, mean and spread) for “Similar” on computers without gehoer: a user test
found only 1 or 2 similar sounds for a loop there. The comparison is by cosine on standardized vectors (nachbarn).

One thread analyzes in the background: at most BATCH files per call, one process, nice 15 (about 10 ms per file).
"""
import json
import math
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import plattform

BATCH = 200
TEMPO_AB = 1.5
TONHALTIG_AB = 0.5
VERSION = 3          # of the analysis: 2 = with sound fingerprint, 3 = octave rule for the tempo of loops


class Leicht:
    def __init__(self, befehl, db, _prio, kandidaten, neu_laden):
        self.befehl, self.db = list(befehl) if befehl else None, Path(db)     # leichtmessen.py with Python, or the Swift build
        self.kandidaten = kandidaten        # () -> [path] without gehoer analysis that lack tempo or root note
        self.neu_laden = neu_laden          # () -> reload the index (in the background)
        self.lock = threading.Lock()
        self.wecker = threading.Event()
        self.stand = {"laeuft": False, "fertig": 0, "offen": 0, "fehler": ""}
        self._cache = (None, {})
        self._vek = (None, [], [])          # (state, paths, standardized unit vectors)

    def stempel(self):
        try:
            return self.db.stat().st_mtime
        except OSError:
            return 0

    def da(self):
        return bool(self.befehl)

    def _verbinden(self):
        self.db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.db), timeout=30)
        con.execute("CREATE TABLE IF NOT EXISTS werte (pfad TEXT PRIMARY KEY, groesse INTEGER, mtime REAL, bpm REAL, "
                    "bpm_sicher REAL, ton INTEGER, moll INTEGER, ton_sicher REAL, grundton_midi INTEGER, "
                    "grundton_cent INTEGER, tonhaltig REAL, dauer REAL, fehler TEXT, am TEXT)")
        spalten = {r[1] for r in con.execute("PRAGMA table_info(werte)")}
        if "klang" not in spalten:
            con.execute("ALTER TABLE werte ADD COLUMN klang TEXT")      # added with the sound fingerprint
        if "v" not in spalten:          # version of the analysis: 2 = with sound fingerprint; older rows count as pending
            con.execute("ALTER TABLE werte ADD COLUMN v INTEGER")
        return con

    def _werte_mit_stand(self):
        try:
            st = self.db.stat().st_mtime
        except OSError:
            return None, {}
        c = self._cache
        if c[0] == st:
            return c
        con = self._verbinden()
        try:
            con.row_factory = sqlite3.Row
            d = {r["pfad"]: dict(r) for r in con.execute("SELECT * FROM werte WHERE fehler IS NULL")}
        finally:
            con.close()
        self._cache = (st, d)
        return st, d

    def werte(self):
        """path -> dict, cached until the database changes (the index asks on every reload)."""
        return self._werte_mit_stand()[1]

    def starten(self):
        if self.da():
            threading.Thread(target=self._lauf, daemon=True, name="leicht").start()

    def wecken(self):
        self.wecker.set()

    def _lauf(self):
        time.sleep(30)                      # only after startup, the index is there by then
        while True:
            try:
                self._durchgang()
            except Exception as e:
                with self.lock:
                    self.stand["fehler"] = f"{type(e).__name__}: {e}"[:200]
            self.wecker.wait(600)           # new files arrive via wecken(), otherwise check every 10 min
            self.wecker.clear()

    def _durchgang(self):
        con = self._verbinden()
        try:
            # Rows from before the sound fingerprint (v empty) count as pending, and rows with a tempo from before the
            # octave rule (v 2); every other row is known, even without a fingerprint (too short) or with an error
            # (otherwise 2,127 files under 0.1 s were analyzed again every 10 min)
            bekannt = {r[0]: (r[1], r[2]) for r in con.execute(
                "SELECT pfad, groesse, mtime FROM werte WHERE v >= ? OR (v >= 2 AND bpm IS NULL) OR fehler IS NOT NULL",
                (VERSION,))}
        finally:
            con.close()
        offen = []
        for p in self.kandidaten():
            try:
                st = os.stat(p)
            except OSError:
                continue
            if plattform.nur_in_cloud(st):          # do not download half the cloud just to analyze it
                continue
            if bekannt.get(p) != (st.st_size, st.st_mtime):
                offen.append(p)
        if not offen:
            return
        with self.lock:
            self.stand.update(laeuft=True, offen=len(offen), fertig=0, fehler="")
        letztes_laden, neu = time.time(), 0
        try:
            for i in range(0, len(offen), BATCH):
                neu += self._messen(offen[i:i + BATCH])
                with self.lock:
                    self.stand["fertig"] = min(len(offen), i + BATCH)
                if time.time() - letztes_laden > 120:    # reload at most every 2 min in between
                    self.neu_laden()
                    letztes_laden = time.time()
                time.sleep(0.5)
        finally:
            with self.lock:
                self.stand["laeuft"] = False
            if neu:                  # reload only when values arrived (otherwise every pass emptied the caches)
                self.neu_laden()

    def _messen(self, pfade):
        """-> number of rows written. If leichtmessen hangs or crashes on a file, exactly that file gets an error note
        and the rest goes on (otherwise one file blocked all the following ones)."""
        jetzt = time.strftime("%Y-%m-%d %H:%M")
        geschrieben = 0
        while pfade:
            try:
                befehl, zusatz = plattform.niedrig(self.befehl)     # nice, or a low priority class
                zusatz["env"] = dict(os.environ, PYTHONIOENCODING="utf-8")
                r = subprocess.run(befehl, input="\n".join(pfade) + "\n", capture_output=True, text=True,
                                   encoding="utf-8", timeout=30 + 3 * len(pfade), **zusatz)
                ausgabe, sauber = r.stdout, r.returncode == 0
            except subprocess.TimeoutExpired as e:
                roh = e.stdout or b""
                ausgabe, sauber = roh.decode("utf-8", "replace") if isinstance(roh, bytes) else roh, False
            zeilen, gesehen = self._zeilen(ausgabe, jetzt)
            fehlt = [p for p in pfade if p not in gesehen]
            if fehlt and not sauber:
                try:
                    st = os.stat(fehlt[0])
                    zeilen.append((fehlt[0], st.st_size, st.st_mtime) + (None,) * 9 +
                                  ("leichtmessen hangs or crashes on this file", jetzt, None, VERSION))
                except OSError:
                    pass
                fehlt = fehlt[1:]
            elif fehlt:
                fehlt = []           # finished cleanly but without a row: do not retry forever
            self._schreiben(zeilen)
            geschrieben += len(zeilen)
            pfade = fehlt
        return geschrieben

    def _zeilen(self, ausgabe, jetzt):
        zeilen, gesehen = [], set()
        for z in ausgabe.splitlines():
            try:
                d = json.loads(z)
                st = os.stat(d["pfad"])
            except (ValueError, KeyError, OSError):
                continue
            gesehen.add(d["pfad"])
            zeilen.append((d["pfad"], st.st_size, st.st_mtime, d.get("bpm"), d.get("bpm_sicher"), d.get("ton"),
                           int(bool(d.get("moll"))) if d.get("ton") is not None else None, d.get("ton_sicher"),
                           d.get("grundton_midi"), d.get("grundton_cent"), d.get("tonhaltig"), d.get("dauer"),
                           d.get("fehler"), jetzt, json.dumps(d["klang"]) if d.get("klang") else None, VERSION))
        return zeilen, gesehen

    def _schreiben(self, zeilen):
        if zeilen:
            con = self._verbinden()
            try:
                con.executemany("INSERT OR REPLACE INTO werte (pfad, groesse, mtime, bpm, bpm_sicher, ton, moll, "
                                "ton_sicher, grundton_midi, grundton_cent, tonhaltig, dauer, fehler, am, klang, v) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", zeilen)
                con.commit()
            finally:
                con.close()

    def _vektoren(self):
        """Standardized unit vectors of all files with a sound fingerprint, rebuilt on every change."""
        stand, werte = self._werte_mit_stand()    # state and values in one read (otherwise a race)
        if self._vek[0] == stand:
            return self._vek[1], self._vek[2]
        pfade, roh = [], []
        for p, w in werte.items():
            if w.get("klang"):
                try:
                    v = json.loads(w["klang"])
                except ValueError:
                    continue
                if len(v) == 48:
                    pfade.append(p)
                    roh.append(v)
        if len(roh) < 2:
            self._vek = (stand, [], [])
            return [], []
        n = len(roh)
        mittel = [sum(v[i] for v in roh) / n for i in range(48)]
        streu = [math.sqrt(sum((v[i] - mittel[i]) ** 2 for v in roh) / n) or 1.0 for i in range(48)]
        vek = []
        for v in roh:
            z = [(v[i] - mittel[i]) / streu[i] for i in range(48)]
            laenge = math.sqrt(sum(x * x for x in z)) or 1.0
            vek.append([x / laenge for x in z])
        self._vek = (stand, pfade, vek)
        return pfade, vek

    def nachbarn(self, pfad, k=3000):
        """{path: cosine} of the k most similar by the rough sound fingerprint, or None without a fingerprint."""
        pfade, vek = self._vektoren()
        try:
            i = pfade.index(pfad)
        except ValueError:
            return None
        q = vek[i]
        werte = [(sum(a * b for a, b in zip(q, v)), j) for j, v in enumerate(vek) if j != i]
        werte.sort(reverse=True)
        return {pfade[j]: round(w, 4) for w, j in werte[:k]}

    def bericht(self):
        with self.lock:
            return dict(self.stand, da=self.da())
