"""Leichte Messung (Nutzertest 2, 03.11.2026): Tempo und Grundton ohne gehoer, mit app/leichtmessen (Swift, nur
Apple-Bibliotheken, einrichten.sh baut es mit). Auf Rechnern ohne gehoer kamen Tempo und Tonart nur aus dem Namen.

Gemessen am 03.11.2026 an Hersteller-Angaben aus Dateinamen (Bibliothek Jeremias):
  Tempo, 300 Loops: ab Sicherheit 1,5 liefern 227: 131 genau, 88 halb oder doppelt, 8 falsch.
  Grundton 808 und Bass, 151 One-Shots: 90 Tonklasse richtig, viele übrige 1 bis 2 Halbtöne daneben.
  Tonart von Loops: 101 von 297 genau, 119 mit Parallele; zu unsicher, wird nicht genutzt.
Darum übernimmt der Index nur Tempo (Loops ohne Tempo, Sicherheit ab 1,5) und Grundton (808, Bass und tonale One-Shots
ohne Ton, als unsicher), beides mit Quelle „leicht“. Name, gehoer und Korrekturen von Hand gehen immer vor.

Dazu ein grober Klang-Fingerabdruck (24 Mel-Bänder, Mittel und Streuung) für „Ähnliche“ auf Rechnern ohne gehoer:
Nutzertest 2 fand dort zu einem Loop nur 1 bis 2 Ähnliche. Verglichen wird standardisiert per Kosinus (nachbarn).

Ein Faden misst im Hintergrund: höchstens BATCH Dateien je Aufruf, ein Prozess, nice 15 (rund 10 ms je Datei).
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


class Leicht:
    def __init__(self, befehl, db, _prio, kandidaten, neu_laden):
        self.befehl, self.db = list(befehl) if befehl else None, Path(db)     # leichtmessen.py mit Python oder Swift
        self.kandidaten = kandidaten        # () -> [pfad] ohne gehoer-Messung, bei denen Tempo oder Grundton fehlt
        self.neu_laden = neu_laden          # () -> Index neu laden (im Hintergrund)
        self.lock = threading.Lock()
        self.wecker = threading.Event()
        self.stand = {"laeuft": False, "fertig": 0, "offen": 0, "fehler": ""}
        self._cache = (None, {})
        self._vek = (None, [], [])          # (Stand, Pfade, standardisierte Einheitsvektoren)

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
            con.execute("ALTER TABLE werte ADD COLUMN klang TEXT")      # seit dem Klang-Fingerabdruck (03.11.)
        if "v" not in spalten:          # Stand der Messung: 2 = mit Klang-Fingerabdruck; ältere Zeilen gelten als offen
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
        """pfad -> dict, zwischengespeichert bis sich die Datenbank ändert (der Index fragt bei jedem Neuladen)."""
        return self._werte_mit_stand()[1]

    def starten(self):
        if self.da():
            threading.Thread(target=self._lauf, daemon=True, name="leicht").start()

    def wecken(self):
        self.wecker.set()

    def _lauf(self):
        time.sleep(30)                      # erst nach dem Start, der Index ist dann da
        while True:
            try:
                self._durchgang()
            except Exception as e:
                with self.lock:
                    self.stand["fehler"] = f"{type(e).__name__}: {e}"[:200]
            self.wecker.wait(600)           # neue Dateien kommen über wecken(), sonst alle 10 min nachsehen
            self.wecker.clear()

    def _durchgang(self):
        con = self._verbinden()
        try:
            # Zeilen vor dem Klang-Fingerabdruck (v leer) gelten als offen; jede andere ist bekannt, auch ohne Klang (zu
            # kurz) oder mit Fehler (Prüfung 03.11.: 2.127 Dateien unter 0,1 s wurden sonst alle 10 min neu gemessen)
            bekannt = {r[0]: (r[1], r[2]) for r in con.execute(
                "SELECT pfad, groesse, mtime FROM werte WHERE v >= 2 OR fehler IS NOT NULL")}
        finally:
            con.close()
        offen = []
        for p in self.kandidaten():
            try:
                st = os.stat(p)
            except OSError:
                continue
            if plattform.nur_in_cloud(st):          # nicht durchs Messen die halbe Cloud herunterladen
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
                if time.time() - letztes_laden > 120:    # zwischendurch höchstens alle 2 min neu laden
                    self.neu_laden()
                    letztes_laden = time.time()
                time.sleep(0.5)
        finally:
            with self.lock:
                self.stand["laeuft"] = False
            if neu:                  # nur neu laden, wenn Werte kamen (sonst leerte jeder Durchgang die Zwischenspeicher)
                self.neu_laden()

    def _messen(self, pfade):
        """-> Zahl der geschriebenen Zeilen. Hängt oder stürzt leichtmessen an einer Datei, bekommt genau diese einen
        Fehlervermerk und der Rest läuft weiter (Prüfung 03.11.: sonst blockierte eine Datei alle folgenden)."""
        jetzt = time.strftime("%Y-%m-%d %H:%M")
        geschrieben = 0
        while pfade:
            try:
                befehl, zusatz = plattform.niedrig(self.befehl)     # nice bzw. niedrige Prioritätsklasse
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
                                  ("leichtmessen hängt oder stürzt an dieser Datei", jetzt, None, 2))
                except OSError:
                    pass
                fehlt = fehlt[1:]
            elif fehlt:
                fehlt = []           # sauber beendet, aber ohne Zeile: nicht endlos neu versuchen
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
                           d.get("fehler"), jetzt, json.dumps(d["klang"]) if d.get("klang") else None, 2))
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
        """Standardisierte Einheitsvektoren aller Dateien mit Klang-Fingerabdruck, neu bei jeder Änderung."""
        stand, werte = self._werte_mit_stand()    # Stand und Werte aus einem Griff (Prüfung 03.11.: Race)
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
        """{pfad: kosinus} der k ähnlichsten nach dem groben Klang-Fingerabdruck, oder None ohne Fingerabdruck."""
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
