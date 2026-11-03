"""Sound fingerprint in the server: bridge to klang_dienst.py (neighbors) and klang_vektoren.py (computing). Both run
in the gehoer environment; without it (other computers) verfuegbar() is false and “Similar” keeps comparing by type,
tempo and key.

Key of a file: its md5, without md5 “pfad:<path>” (as in klang_vektoren.py).
"""
import collections
import hashlib
import json
import os
import select
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from i18n import tx

try:
    import fcntl              # Mac and Linux only; the sound fingerprint exists only with gehoer (not on Windows)
except ImportError:
    fcntl = None

HIER = Path(__file__).resolve().parent
RUHE_S = 900            # stop the service after 15 min without a request (it keeps the vectors in memory, about 130 MB)
ANTWORT_S = 20          # longest wait for the service (the first call loads all vectors)
MD5_BIS = 100 << 20     # as in ordner.py: larger files get no md5


class DienstFehler(Exception):
    """The sound service does not answer or answers wrongly (not: “no fingerprint”)."""


def schluessel(md5, pfad):
    return md5 if md5 else "pfad:" + pfad


def datei_schluessel(pfad):
    try:
        if os.path.getsize(pfad) > MD5_BIS:
            return "pfad:" + pfad
        h = hashlib.md5()
        with open(pfad, "rb") as f:
            for stueck in iter(lambda: f.read(1 << 20), b""):
                h.update(stueck)
        return h.hexdigest()
    except OSError:
        return None


class Klang:
    def __init__(self, python, gehoer_code, db, prio, umgebung):
        self.python, self.gehoer_code, self.db = Path(python), Path(gehoer_code), Path(db)
        self.prio, self.umgebung = list(prio), dict(umgebung)
        self.lock = threading.Lock()            # stdin/stdout of the service: one request at a time
        self.proc, self.zuletzt = None, 0.0
        self.cache = collections.OrderedDict()  # (schluessel, stempel) -> {schluessel: cosine} or None
        self.rlock = threading.Lock()           # computing: at most one run
        self.rechner, self.warte = None, []
        self._zahl = (None, 0)
        threading.Thread(target=self._wache, daemon=True, name="klang-wache").start()

    # ---------------------------------------------------------- State
    def verfuegbar(self):
        return self.python.is_file() and (self.gehoer_code / "gehoer/klang.py").is_file()

    def stempel(self):
        try:
            return tuple(os.stat(p).st_mtime for p in (self.db, Path(str(self.db) + "-wal")) if p.exists())
        except OSError:
            return ()

    def _lesen(self, sql):
        if not self.db.exists():
            return []
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=5)
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def zahl(self):
        st = self.stempel()
        if self._zahl[0] != st:
            try:
                self._zahl = (st, self._lesen("SELECT count(*) FROM vektoren")[0][0])
            except (sqlite3.Error, IndexError):
                self._zahl = (st, 0)
        return self._zahl[1]

    def stand(self):
        d = {"verfuegbar": self.verfuegbar(), "zahl": 0, "laeuft": False, "fertig": 0, "gesamt": 0, "fehler": 0}
        if not d["verfuegbar"]:
            return d
        d["zahl"] = self.zahl()
        with self.rlock:
            d["laeuft"] = self._laeuft()
            d["wartet"] = len(self.warte)
        try:
            lauf = dict(self._lesen("SELECT k, v FROM meta")).get("lauf", "")
            pid, start, fertig, gesamt = (lauf.split() + ["0"] * 4)[:4]
            d.update(fertig=int(fertig), gesamt=int(gesamt), start=float(start))
            d["fehler"] = self._lesen("SELECT count(*) FROM fehler")[0][0]
        except (sqlite3.Error, ValueError, IndexError):
            pass
        return d

    # ---------------------------------------------------------- Neighbors
    def _dienst(self):
        if self.proc is not None and self.proc.poll() is None:
            return self.proc
        self.proc = subprocess.Popen([str(self.python), "-I", str(HIER / "klang_dienst.py"), str(self.db)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1)
        return self.proc

    def _frage(self, anfrage):
        with self.lock:
            p = self._dienst()
            self.zuletzt = time.time()
            try:
                p.stdin.write(json.dumps(anfrage) + "\n")
                p.stdin.flush()
                bereit, _, _ = select.select([p.stdout], [], [], ANTWORT_S)
                if not bereit:
                    raise TimeoutError("sound service does not answer")
                zeile = p.stdout.readline()
                if not zeile:
                    raise EOFError("sound service ended")
                return json.loads(zeile)
            except (OSError, ValueError, TimeoutError, EOFError):
                p.kill()
                self.proc = None
                return None

    def nachbarn(self, k, n=4000):
        """{schluessel: cosine} of the n nearest, or None (no fingerprint for k). If the service fails: DienstFehler
        (not cached, so the next call asks again)."""
        if not self.verfuegbar() or not self.db.exists():
            return None
        c = (k, n, self.stempel())
        if c in self.cache:
            self.cache.move_to_end(c)
            return self.cache[c]
        a = self._frage({"nah": k, "k": n})
        if not a or not a.get("ok"):
            raise DienstFehler((a or {}).get("fehler") or "sound service does not answer")
        erg = {s: w for s, w in a["nah"]} if a["nah"] is not None else None
        self.cache[c] = erg
        while len(self.cache) > 64:
            self.cache.popitem(last=False)
        return erg

    def _wache(self):
        while True:
            time.sleep(60)
            with self.lock:
                if self.proc is not None and self.proc.poll() is None and time.time() - self.zuletzt > RUHE_S:
                    self.proc.terminate()
                    self.proc = None
            with self.rlock:       # catch up on jobs that waited for another process's run
                if self.warte and not self._laeuft():
                    self._starten(["--liste", self._liste_schreiben()])

    def _fremder_lauf(self):
        """Does another process hold the lock of klang_vektoren.py (a run from an earlier server start)?"""
        if fcntl is None:
            return False
        try:
            with open(str(self.db) + ".lauf", "a") as f:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except OSError:
            return True

    def _laeuft(self):
        """Caller holds rlock."""
        return (self.rechner is not None and self.rechner.poll() is None) or self._fremder_lauf()

    # ---------------------------------------------------------- Computing
    def rechnen_lassen(self, auftraege):
        """auftraege: [(schluessel, pfad)]. In the background, one after another, at low priority."""
        if not self.verfuegbar() or not auftraege:
            return
        with self.rlock:
            self.warte.extend(auftraege)
            if self._laeuft():          # afterwards (own run: _nach_lauf, another process's run: _wache)
                return
            self._starten(["--liste", self._liste_schreiben()])

    def alle_rechnen(self):
        if not self.verfuegbar():
            return False, tx("The sound fingerprint needs gehoer (the author's analysis tool, not published yet)")
        with self.rlock:
            if self._laeuft():
                return False, tx("Already running")
            self.warte = []
            self._starten(["--alle"])
        return True, tx("Computing in the background; the number above goes up")

    def anhalten(self):
        with self.rlock:
            self.warte = []
            if self.rechner is not None and self.rechner.poll() is None:
                self.rechner.terminate()
                return True
            if self._fremder_lauf():     # run from an earlier server start: its pid is in meta “lauf”
                try:
                    pid = int((dict(self._lesen("SELECT k, v FROM meta")).get("lauf", "0").split() or ["0"])[0])
                    if pid > 0:
                        os.kill(pid, signal.SIGTERM)
                        return True
                except (sqlite3.Error, ValueError, OSError):
                    pass
        return False

    def _liste_schreiben(self):
        fd, pfad = tempfile.mkstemp(prefix="openlibary-klang-", suffix=".tsv")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for k, p in self.warte:
                f.write(f"{k}\t{p}\n")
        self.warte = []
        return pfad

    def _starten(self, argumente):
        """Caller holds rlock."""
        self.rechner = subprocess.Popen(self.prio + [str(self.python), "-I", str(HIER / "klang_vektoren.py")] + argumente,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        env=dict(os.environ, **self.umgebung))
        threading.Thread(target=self._nach_lauf, args=(self.rechner, argumente), daemon=True).start()

    def _nach_lauf(self, proc, argumente):
        proc.wait()
        if argumente[0] == "--liste":
            try:
                os.unlink(argumente[1])
            except OSError:
                pass
        with self.rlock:
            if self.warte and (self.rechner is proc):
                self._starten(["--liste", self._liste_schreiben()])
