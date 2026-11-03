"""Neighbors by sound. Runs as its own process in the gehoer environment (numpy), because the server has only the
standard library: 63,000 vectors times 1,024 values in pure Python would take seconds per request.

Reads all vectors from the sound database (klang_vektoren.py), subtracts the library mean and normalizes again:
averaged embeddings otherwise all lie close together; centered, the distances spread out.
Protocol over stdin/stdout, one JSON line per request:
  {"nah": "<schluessel>", "k": 3000}  ->  {"ok": true, "nah": [[schluessel, cosine], ...]}  (without the reference itself;
                                           "nah": null when the reference has no fingerprint; error: "ok": false)
  {"stand": 1}                         ->  {"ok": true, "zahl": 12345}
When the database changes, the service reloads before the next answer.
"""
import json
import os
import sqlite3
import sys

import numpy as np

DB = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.cache/gehoer/openlibary_klang.sqlite")
BLOCK = 8192


class Vektoren:
    def __init__(self):
        self.stempel, self.schluessel, self.index, self.m = None, [], {}, np.zeros((0, 1024), np.float16)

    def _stempel(self):
        try:
            return tuple(os.stat(p).st_mtime for p in (DB, DB + "-wal") if os.path.exists(p))
        except OSError:
            return None

    def aktuell(self):
        st = self._stempel()
        if st == self.stempel:
            return
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        try:
            reihen = con.execute("SELECT schluessel, vektor FROM vektoren").fetchall()
        finally:
            con.close()
        self.stempel = st
        if not reihen:
            self.schluessel, self.index, self.m = [], {}, np.zeros((0, 1024), np.float16)
            return
        m = np.frombuffer(b"".join(r[1] for r in reihen), np.float16).reshape(len(reihen), -1).astype(np.float32)
        m -= m.mean(axis=0, keepdims=True)
        m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
        self.schluessel = [r[0] for r in reihen]
        self.index = {k: i for i, k in enumerate(self.schluessel)}
        self.m = m.astype(np.float16)

    def nah(self, k, n):
        i = self.index.get(k)
        if i is None:
            return None
        q = self.m[i].astype(np.float32)
        s = np.empty(len(self.m), np.float32)
        for a in range(0, len(self.m), BLOCK):
            s[a:a + BLOCK] = self.m[a:a + BLOCK].astype(np.float32) @ q
        s[i] = -2.0
        n = min(n, len(s) - 1)
        if n <= 0:
            return []
        oben = np.argpartition(-s, n - 1)[:n]
        oben = oben[np.argsort(-s[oben])]
        return [[self.schluessel[j], round(float(s[j]), 4)] for j in oben]


def main():
    v = Vektoren()
    for zeile in sys.stdin:
        try:
            a = json.loads(zeile)
            v.aktuell()
            if "nah" in a:
                r = v.nah(str(a["nah"]), int(a.get("k", 3000)))
                aus = {"ok": True, "nah": r}      # None: there is no fingerprint for this key
            else:
                aus = {"ok": True, "zahl": len(v.schluessel)}
        except Exception as e:      # a broken request does not end the service
            aus = {"ok": False, "fehler": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(aus) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
