"""Light analysis, Python version: tempo, key, root note and a rough sound fingerprint without gehoer, the same on
Mac, Windows and Linux. Needs numpy and soundfile (reads WAV, AIFF, FLAC, OGG, MP3 without ffmpeg); the packaged apps
include both. Same method as app/leichtmessen.swift (the reasoning is there), which stays as the fallback on a Mac
without numpy.

Usage: paths line by line on stdin, one JSON line per file on stdout (fields as in the Swift version).
"""
import io
import json
import math
import os
import sys

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # runs with -I: add our own folder for audioform
import audioform      # noqa: E402

DUR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MOLL = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _oeffnen(pfad):
    """libsndfile does not open FL factory samples (Ogg Vorbis inside WAV) directly: then use the Ogg stream."""
    try:
        return sf.SoundFile(pfad)
    except Exception:
        ogg = audioform.ogg_in_wav(pfad)
        if not ogg:
            raise
        return sf.SoundFile(io.BytesIO(ogg))


def laden(pfad, max_s=30.0):
    with _oeffnen(pfad) as f:
        sr = float(f.samplerate)
        dauer = f.frames / sr if f.frames > 0 else None
        x = f.read(frames=int(max_s * sr), dtype="float32", always_2d=True)
    if x.size == 0:
        raise ValueError("empty")
    x = x.mean(axis=1)
    faktor = 4 if sr >= 88000 else 2 if sr >= 44000 else 1
    if faktor > 1:
        n = len(x) // faktor
        x = x[: n * faktor].reshape(n, faktor).mean(axis=1)
    if dauer is None:
        dauer = len(x) * faktor / sr
    return x.astype(np.float32), sr / faktor, dauer


def spektren(x, n, hop, start=0, ende=None):
    ende = len(x) if ende is None else ende
    if ende - start < n:
        return np.zeros((0, n // 2), np.float32)
    zahl = (ende - start - n) // hop + 1
    idx = start + np.arange(n)[None, :] + hop * np.arange(zahl)[:, None]
    fenster = np.hanning(n).astype(np.float32)
    return np.abs(np.fft.rfft(x[idx] * fenster, axis=1))[:, : n // 2].astype(np.float32)


def tempo(x, sr, dauer):
    n, hop = 1024, 256
    if len(x) <= n * 8:
        return None
    mag = np.log(1 + 1000 * spektren(x, n, hop))
    fluss = np.maximum(0, np.diff(mag, axis=0, prepend=np.zeros((1, mag.shape[1]), np.float32))).sum(axis=1)
    breite = 16
    kum = np.cumsum(np.concatenate([[0.0], fluss]))
    mittel = np.array([(kum[i + 1] - kum[max(0, i + 1 - breite)]) / min(i + 1, breite) for i in range(len(fluss))])
    huelle = np.maximum(0, fluss - mittel)
    hop_s = hop / sr
    min_lag, max_lag = max(1, int((60 / 200) / hop_s)), int((60 / 60) / hop_s) + 1
    rahmen = len(huelle)
    if rahmen <= max_lag * 2:
        return None
    ak = np.zeros(max_lag + 2)
    for lag in range(min_lag, max_lag + 2):
        ak[lag] = float(np.dot(huelle[: rahmen - lag], huelle[lag:])) / (rahmen - lag)
    ak_mittel = ak[min_lag:max_lag + 1].mean() + 1e-9

    def ak_bei(bpm):
        lag = (60.0 / bpm) / hop_s
        i = int(lag)
        f = lag - i
        if i < 1 or i + 1 >= len(ak):
            return 0.0
        return ak[i] * (1 - f) + ak[i + 1] * f

    if dauer >= 1.5:
        kand = [240.0 * t / dauer for t in (1, 2, 4, 8, 16, 32) if 60 <= 240.0 * t / dauer <= 200]
        if kand:        # whole bars; the octave nearest to 110 BPM (the reasoning is in the Swift version)
            best = min(kand, key=lambda k: abs(math.log2(k / 110)))
            bpm = round(best) if abs(best - round(best)) < 0.15 else best
            return bpm, min(9.9, max(ak_bei(k) for k in kand) / ak_mittel)
    best_lag, best_w = min_lag, -1.0
    for lag in range(min_lag, max_lag + 1):
        bpm = 60.0 / (lag * hop_s)
        w = ak[lag] * math.exp(-0.5 * (math.log2(bpm / 120) / 0.9) ** 2)
        if w > best_w:
            best_w, best_lag = w, lag
    if best_w <= 0:
        return None
    a, b, c = ak[best_lag - 1], ak[best_lag], ak[best_lag + 1]
    versatz = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0
    bpm = 60.0 / ((best_lag + max(-0.5, min(0.5, versatz))) * hop_s)
    bpm = round(bpm) if abs(bpm - round(bpm)) < 0.15 else bpm
    return bpm, min(9.9, ak[best_lag] / ak_mittel) * 0.7


def tonart(x, sr):
    n, hop = 8192, 4096
    if len(x) <= n:
        return None
    mag = spektren(x, n, hop).sum(axis=0)          # summed over time, like the Swift version per frame
    chroma = np.zeros(12)
    for p in range(36, 97):
        f = 440 * 2 ** ((p - 69) / 12)
        s, gewicht = 0.0, 1.0
        for h in range(1, 5):
            b = int(round(h * f * n / sr))
            if 0 < b < n // 2 - 1:
                s += gewicht * float(mag[b - 1:b + 2].max())
            gewicht *= 0.8
        chroma[p % 12] += s
    if chroma.sum() <= 0:
        return None
    werte = []
    for t in range(12):
        gedreht = np.roll(chroma, -t)
        werte.append((float(np.corrcoef(gedreht, DUR)[0, 1]), t, False))
        werte.append((float(np.corrcoef(gedreht, MOLL)[0, 1]), t, True))
    werte.sort(key=lambda w: -w[0])
    erster = werte[0]
    parallele = (erster[1] + 3) % 12 if erster[2] else (erster[1] + 9) % 12
    zweiter = next((w for w in werte[1:] if not (w[1] == parallele and w[2] != erster[2])), werte[1])
    return erster[1], erster[2], max(0.0, erster[0]) * 10 * (erster[0] - zweiter[0])


def yin(x, start, breite, tau_min, tau_max, sr):
    seg = x[start:start + breite + tau_max].astype(np.float64)
    d = np.zeros(tau_max + 1)
    a = seg[:breite]
    for tau in range(1, tau_max + 1):
        diff = seg[tau:tau + breite] - a
        d[tau] = float(np.dot(diff, diff))
    dn = np.ones(tau_max + 1)
    kum = np.cumsum(d[1:])
    taus = np.arange(1, tau_max + 1)
    dn[1:] = np.where(kum > 0, d[1:] * taus / np.maximum(kum, 1e-30), 1)
    wahl = -1
    tau = tau_min
    while tau < tau_max:
        if dn[tau] < 0.15:
            while tau + 1 < tau_max and dn[tau + 1] < dn[tau]:
                tau += 1
            wahl = tau
            break
        tau += 1
    if wahl < 0:
        wahl = tau_min + int(np.argmin(dn[tau_min:tau_max]))
        if dn[wahl] > 0.4:
            return None
    a, b, c = dn[wahl - 1], dn[wahl], dn[min(wahl + 1, tau_max)]
    versatz = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0
    f0 = sr / (wahl + max(-0.5, min(0.5, versatz)))
    return 69 + 12 * math.log2(f0 / 440), 1 - float(dn[wahl])


def grundton(x, sr):
    r = 512
    ende = min(len(x), int(sr))
    if ende < r:
        return None
    energie = [float(np.dot(x[j:j + r], x[j:j + r])) for j in range(0, ende - r + 1, r // 2)]
    spitze = int(np.argmax(energie)) * (r // 2)
    tau_min, tau_max = int(sr / 1000), int(sr / 25)
    breite = int(0.12 * sr)
    toene = []
    for versatz_ms in range(100, 601, 100):
        start = spitze + int(versatz_ms / 1000 * sr)
        if start + breite + tau_max > len(x):
            break
        t = yin(x, start, breite, tau_min, tau_max, sr)
        if t:
            toene.append(t)
    if not toene:
        start = spitze + int(0.04 * sr)
        if start + breite + tau_max > len(x):
            return None
        t = yin(x, start, breite, tau_min, tau_max, sr)
        if not t:
            return None
        toene.append(t)
    toene.sort(key=lambda t: t[0])
    return toene[len(toene) // 2]


def klang_vektor(x, sr):
    n, hop, baender = 1024, 512, 24
    ende = min(len(x), int(10 * sr))
    if ende <= n * 2:
        return None
    mag = spektren(x, n, hop, 0, ende)
    if len(mag) < 2:
        return None
    leistung = mag.astype(np.float64) ** 2

    def mel(f):
        return 2595 * math.log10(1 + f / 700)

    def hz(m):
        return 700 * (10 ** (m / 2595) - 1)
    lo, hi = mel(60), mel(min(8000, sr / 2 - 100))
    punkte = [hz(lo + (hi - lo) * i / (baender + 1)) for i in range(baender + 2)]
    bin_hz = sr / n
    filter_ = np.zeros((baender, n // 2))
    for b in range(baender):
        l, m, r = punkte[b], punkte[b + 1], punkte[b + 2]
        for k in range(max(1, int(l / bin_hz)), min(n // 2 - 1, int(r / bin_hz) + 1) + 1):
            fk = k * bin_hz
            w = (fk - l) / (m - l) if fk < m else (r - fk) / (r - m)
            if w > 0:
                filter_[b, k] = w
    db = 10 * np.log10(leistung @ filter_.T + 1e-10)
    v = np.concatenate([db.mean(axis=0), db.std(axis=0)])
    return [round(float(z), 1) for z in v]


def endlich(z):
    return z if z is not None and math.isfinite(z) else None


def messen(pfad):
    e = {"pfad": pfad}
    try:
        x, sr, dauer = laden(pfad)
        e["dauer"] = round(dauer, 3)
        t = tempo(x, sr, dauer)
        if t and math.isfinite(t[0]) and math.isfinite(t[1]):
            e["bpm"], e["bpm_sicher"] = round(t[0], 2), round(t[1], 2)
        k = tonart(x, sr)
        if k:
            e["ton"], e["moll"], e["ton_sicher"] = k[0], k[1], round(k[2], 2)
        v = klang_vektor(x, sr)
        if v and all(math.isfinite(z) for z in v):
            e["klang"] = v
        g = grundton(x, sr)
        if g and math.isfinite(g[0]) and 0 < g[0] < 140:
            rund = round(g[0])
            e["grundton_midi"], e["grundton_cent"] = int(rund), int(round((g[0] - rund) * 100))
            e["tonhaltig"] = round(g[1], 2)
    except Exception as ex:      # a broken file does not stop the run
        e = {"pfad": pfad, "fehler": f"{type(ex).__name__}: {ex}"[:200]}
    for k in ("dauer", "bpm", "bpm_sicher", "ton_sicher", "tonhaltig"):
        if k in e:
            e[k] = endlich(e[k])
    return e


def main():
    np.seterr(all="ignore")
    # Paths and JSON always UTF-8: on Windows stdin and stdout would be cp1252 (non-ASCII paths would break)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for zeile in sys.stdin:
        pfad = zeile.strip()
        if pfad:
            print(json.dumps(messen(pfad), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
