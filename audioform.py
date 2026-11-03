"""Audioformate für alle Systeme (03.11.2026, „max kompi“).

- AIFF nach WAV ohne jede Abhängigkeit: Browser unter Windows und Linux spielen kein AIFF, und dort fehlt oft ffmpeg.
  Unkomprimiertes AIFF und AIFC (NONE, twos, sowt, fl32, fl64), 8 bis 32 Bit.
- wav_aus: beliebige Datei als WAV, für DAWs, die kein OGG, FLAC oder MP3 nehmen (Logic, Pro Tools). Reihenfolge:
  eigener AIFF-Wandler, soundfile (in den fertigen Apps dabei), ffmpeg.
- ogg_in_wav: FL Studio legt seine Werkssamples als WAV mit Ogg Vorbis darin ab (Formatkennung 0x674F und
  Verwandte). Weder Browser noch libsndfile noch ffmpeg öffnen das direkt; der Ogg-Strom wird herausgelöst und dann
  normal dekodiert. Ohne das blieben genau die Samples der meistgenutzten DAW stumm.
"""
import array
import math
import os
import shutil
import struct
import subprocess
import sys


_OGG_IN_WAV = {0x674F, 0x6750, 0x6751, 0x676F, 0x6770, 0x6771}


def ist_ogg_in_wav(pfad):
    """Nur der Kopf: WAV mit Ogg-Vorbis-Kennung? (für die Wiedergabe, ohne den ganzen Strom zu lesen)"""
    if not str(pfad).lower().endswith((".wav", ".wave")):
        return False
    try:
        with open(pfad, "rb") as f:
            kopf = f.read(12)
            if len(kopf) < 12 or kopf[:4] != b"RIFF" or kopf[8:12] != b"WAVE":
                return False
            for _ in range(16):
                c = f.read(8)
                if len(c) < 8:
                    return False
                name, n = c[:4], struct.unpack("<I", c[4:])[0]
                if name == b"fmt ":
                    roh = f.read(2)
                    return len(roh) == 2 and struct.unpack("<H", roh)[0] in _OGG_IN_WAV
                f.seek(n + (n % 2), 1)
    except (OSError, struct.error):
        return False
    return False


def ogg_kopf(pfad):
    """OGG (Vorbis oder Opus): -> (Sekunden, Rate, Kanäle) aus dem Kennungsblock und der Granule-Position der letzten
    Seite, ohne zu dekodieren; None, wenn nicht lesbar. Unter Windows fehlt meist ffprobe, sonst bliebe die Länge leer."""
    try:
        with open(pfad, "rb") as f:
            anfang = f.read(4096)
            f.seek(0, 2)
            groesse = f.tell()
            f.seek(max(0, groesse - 65536))
            ende = f.read()
    except OSError:
        return None
    if anfang[:4] != b"OggS":
        return None
    i, j = anfang.find(b"\x01vorbis"), anfang.find(b"OpusHead")
    if i >= 0 and i + 16 <= len(anfang):
        kanaele, rate, vorlauf = anfang[i + 11], struct.unpack("<I", anfang[i + 12:i + 16])[0], 0
        takt = rate
    elif j >= 0 and j + 16 <= len(anfang):
        kanaele, vorlauf = anfang[j + 9], struct.unpack("<H", anfang[j + 10:j + 12])[0]
        rate = struct.unpack("<I", anfang[j + 12:j + 16])[0] or 48000
        takt = 48000                                   # Opus zählt die Granule immer in 48 kHz
    else:
        return None
    pos = ende.rfind(b"OggS")
    while pos >= 0:
        if pos + 14 <= len(ende):
            frames = int.from_bytes(ende[pos + 6:pos + 14], "little", signed=True)
            if frames > 0:
                return round((frames - vorlauf) / takt, 3), rate, kanaele
        pos = ende.rfind(b"OggS", 0, pos)
    return None


def ogg_in_wav(pfad):
    """Ogg-Vorbis-Strom aus einem WAV-Container -> bytes, sonst None (auch bei jedem normalen WAV)."""
    if not str(pfad).lower().endswith((".wav", ".wave")):
        return None
    try:
        with open(pfad, "rb") as f:
            kopf = f.read(12)
            if len(kopf) < 12 or kopf[:4] != b"RIFF" or kopf[8:12] != b"WAVE":
                return None
            art = None
            for _ in range(64):
                c = f.read(8)
                if len(c) < 8:
                    return None
                name, n = c[:4], struct.unpack("<I", c[4:])[0]
                if name == b"fmt ":
                    roh = f.read(n)
                    art = struct.unpack("<H", roh[:2])[0] if len(roh) >= 2 else None
                    if art not in _OGG_IN_WAV:
                        return None
                    if n % 2:
                        f.seek(1, 1)
                elif name == b"data":
                    if art is None:
                        return None
                    daten = f.read(n if n else -1)
                    break
                else:
                    f.seek(n + (n % 2), 1)
            else:
                return None
    except (OSError, struct.error):
        return None
    i = daten.find(b"OggS")
    return _vorbis_seiten(daten[i:]) if i >= 0 else None


def ogg_in_wav_dauer(pfad, rate):
    """Sekunden eines FL-Werksamples: Granule-Position der letzten Ogg-Seite (= Zahl der Frames) durch die Rate.
    Aus der Größe des data-Blocks gerechnet käme rund ein Siebtel heraus (Vorbis ist komprimiert)."""
    ogg = ogg_in_wav(pfad)
    if not ogg or not rate:
        return None
    pos = ogg.rfind(b"OggS")
    if pos < 0 or pos + 14 > len(ogg):
        return None
    frames = int.from_bytes(ogg[pos + 6:pos + 14], "little", signed=True)
    return round(frames / rate, 3) if frames > 0 else None


def _vorbis_seiten(daten):
    """Nur die Seiten des Vorbis-Stroms: FL Studio legt in manche Dateien einen zweiten Datenstrom (andere
    Seriennummer, kein Vorbis), und hinter dem Strom stehen teils Fremdblöcke (smpl) im data-Block."""
    seiten, vorbis, pos = [], set(), 0
    while pos + 27 <= len(daten) and daten[pos:pos + 4] == b"OggS":
        segmente = daten[pos + 26]
        kopf = 27 + segmente
        if pos + kopf > len(daten):
            break
        laenge = kopf + sum(daten[pos + 27:pos + kopf])
        if pos + laenge > len(daten):
            break
        seriell = daten[pos + 14:pos + 18]
        if daten[pos + 5] & 0x02 and daten[pos + kopf:pos + kopf + 7] == b"\x01vorbis":
            vorbis.add(seriell)
        seiten.append((seriell, pos, laenge))
        pos += laenge
    if not seiten:
        return daten
    if not vorbis:
        return daten[:pos]
    return b"".join(daten[p:p + n] for ser, p, n in seiten if ser in vorbis)


def _extended(b):
    """80-Bit-IEEE (AIFF-Abtastrate) -> float."""
    exp = ((b[0] & 0x7F) << 8) | b[1]
    mant = int.from_bytes(b[2:10], "big")
    if exp == 0 and mant == 0:
        return 0.0
    vorzeichen = -1 if b[0] & 0x80 else 1
    return vorzeichen * mant * math.pow(2, exp - 16383 - 63)


def aiff_kopf(pfad):
    """-> (Sekunden, Rate, Kanäle, Bits) aus dem COMM-Block, ohne die Audiodaten zu lesen; None, wenn kein AIFF.
    Ersetzt aifc beim Einlesen (das Modul fehlt ab Python 3.13 und kennt kein AIFC mit fl32)."""
    try:
        with open(pfad, "rb") as f:
            kopf = f.read(12)
            if len(kopf) < 12 or kopf[:4] != b"FORM" or kopf[8:12] not in (b"AIFF", b"AIFC"):
                return None
            for _ in range(64):
                c = f.read(8)
                if len(c) < 8:
                    return None
                name, groesse = c[:4], struct.unpack(">I", c[4:])[0]
                if name == b"COMM":
                    roh = f.read(18)
                    if len(roh) < 18:
                        return None
                    kanaele, frames, bits = struct.unpack(">hIh", roh[:8])
                    rate = _extended(roh[8:18])
                    if not rate or rate <= 0 or kanaele <= 0:
                        return None
                    return round(frames / rate, 3), int(round(rate)), kanaele, bits
                f.seek(groesse + (groesse % 2), 1)
    except (OSError, struct.error, OverflowError, ValueError):
        return None
    return None


def aiff_zu_wav(quelle, ziel):
    """Unkomprimiertes AIFF/AIFC als WAV schreiben. Löst ValueError aus bei komprimiertem AIFC (dann ffmpeg)."""
    with open(quelle, "rb") as f:
        kopf = f.read(12)
        if len(kopf) < 12 or kopf[:4] != b"FORM" or kopf[8:12] not in (b"AIFF", b"AIFC"):
            raise ValueError("kein AIFF")
        komm = daten = None
        art = b"NONE"
        while True:
            c = f.read(8)
            if len(c) < 8:
                break
            name, groesse = c[:4], struct.unpack(">I", c[4:])[0]
            if name == b"COMM":
                roh = f.read(groesse)
                kanaele, frames, bits = struct.unpack(">hIh", roh[:8])
                rate = _extended(roh[8:18])
                if kopf[8:12] == b"AIFC" and len(roh) >= 22:
                    art = roh[18:22]
                komm = (kanaele, frames, bits, rate)
            elif name == b"SSND":
                versatz = struct.unpack(">I", f.read(4))[0]
                f.read(4)
                f.read(versatz)
                daten = f.read(groesse - 8 - versatz)
            else:
                f.seek(groesse, 1)
            if groesse % 2:
                f.seek(1, 1)
    if not komm or daten is None:
        raise ValueError("AIFF ohne COMM oder SSND")
    kanaele, frames, bits, rate = komm
    breite = (bits + 7) // 8
    gleitend = art in (b"fl32", b"FL32", b"fl64", b"FL64")
    if art not in (b"NONE", b"twos", b"sowt") and not gleitend:
        raise ValueError(f"komprimiertes AIFC ({art.decode(errors='replace')})")
    if gleitend:
        breite = 8 if art.lower() == b"fl64" else 4
        bits = breite * 8
    daten = daten[: frames * kanaele * breite]
    if art != b"sowt":                      # AIFF ist Big Endian, WAV Little Endian
        if breite == 1:
            daten = bytes((b + 128) & 0xFF for b in daten)        # 8 Bit: AIFF mit, WAV ohne Vorzeichen
        elif breite in (2, 4, 8):
            typ = {2: "h", 4: "i" if array.array("i").itemsize == 4 else "l", 8: "d"}[breite]
            a = array.array(typ)
            a.frombytes(daten)
            a.byteswap()
            daten = a.tobytes()
        elif breite == 3:
            neu = bytearray(len(daten))
            neu[0::3], neu[1::3], neu[2::3] = daten[2::3], daten[1::3], daten[0::3]
            daten = bytes(neu)
    elif breite == 1:
        daten = bytes((b + 128) & 0xFF for b in daten)
    fmt_art = 3 if gleitend else 1
    blockausrichtung = kanaele * breite
    fmt = struct.pack("<HHIIHH", fmt_art, kanaele, int(round(rate)), int(round(rate)) * blockausrichtung,
                      blockausrichtung, bits)
    tmp = str(ziel) + ".teil"
    with open(tmp, "wb") as out:
        out.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(daten) + (len(daten) % 2)) + b"WAVE")
        out.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        out.write(b"data" + struct.pack("<I", len(daten)) + daten)
        if len(daten) % 2:
            out.write(b"\0")
    os.replace(tmp, ziel)


def wav_aus(quelle, ziel, ffmpeg=None):
    """Datei als WAV nach ziel. -> ziel. Löst RuntimeError aus, wenn kein Weg geht."""
    endung = os.path.splitext(quelle)[1].lower()
    if endung in (".aif", ".aiff", ".aifc"):
        try:
            aiff_zu_wav(quelle, ziel)
            return ziel
        except ValueError:
            pass
    letzter = None
    ogg = ogg_in_wav(quelle)           # FL-Werkssample: Ogg-Strom statt der Datei dekodieren
    try:
        import io
        import soundfile as sf        # fertige Apps: libsndfile liest WAV, AIFF, FLAC, OGG, MP3
        with sf.SoundFile(io.BytesIO(ogg) if ogg else quelle) as f:
            sub = "PCM_24" if f.subtype in ("PCM_24", "PCM_32", "FLOAT", "DOUBLE") else "PCM_16"
            tmp = str(ziel) + ".teil.wav"
            with sf.SoundFile(tmp, "w", samplerate=f.samplerate, channels=f.channels, subtype=sub, format="WAV") as o:
                while True:
                    block = f.read(65536, dtype="float32", always_2d=True)
                    if not len(block):
                        break
                    o.write(block)
        os.replace(tmp, ziel)
        return ziel
    except ImportError:
        pass
    except Exception as e:
        letzter = e
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if ffmpeg and os.path.exists(ffmpeg):
        tmp = str(ziel) + ".teil.wav"
        eingabe = ["-f", "ogg", "-i", "pipe:0"] if ogg else ["-i", quelle]
        r = subprocess.run([ffmpeg, "-v", "error", "-y"] + eingabe + ["-c:a", "pcm_s24le" if endung == ".flac"
                            else "pcm_s16le", tmp], input=ogg, capture_output=True, timeout=300,
                           **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        if r.returncode == 0:
            os.replace(tmp, ziel)
            return ziel
        letzter = r.stderr.decode(errors="replace")[-200:]
    raise RuntimeError(f"Wandeln nach WAV ging nicht ({letzter or 'weder soundfile noch ffmpeg da'})")
