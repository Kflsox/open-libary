"""Problem reports: incidents (crash, freeze) and the snapshot that goes along only when the user sends it.

Incidents are small JSON files in <data>/vorfaelle/, one per incident: {"id", "art", "zeit", "version", "details"}.
They come from three places:
  - this server at start: sitzung.json from the last run is still there and its process is gone (crash, kill -9,
    power loss). A clean end deletes it (SIGTERM handler, end of main; Electron deletes it after ending the server
    the hard way on Windows).
  - this server while running: a request has been running longer than HAENGER_S (freeze, with the stacks of all
    threads), or the whole process stood still: faulthandler's own watchdog thread writes the stacks to stapel.txt
    when the regular re-arming stops, even with the GIL held.
  - the app shells (Electron, Mac) through POST /api/vorfall: the page froze or its process died.
The page asks about open incidents (GET /api/vorfaelle) and offers the report form. Nothing leaves the computer
unless the user presses Send; what is sent is exactly the text the form showed.
"""
import faulthandler
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import plattform
from i18n import N_

# The receiver (empfaenger/ in the repo): files each report as an issue in a private repo of the author.
URL = os.environ.get("OPENLIBARY_BERICHT_URL", "https://open-libary-berichte.open-libary-berichte.workers.dev/bericht")
HAENGER_S = float(os.environ.get("OPENLIBARY_HAENGER_S") or 45)     # a request longer than this is a freeze
STAPEL_S = 60                   # whole process stood still this long: faulthandler writes the stacks
PROTOKOLL_ZEILEN = 300
SCHNAPPSCHUSS_MAX = 50_000      # characters (at most 200 KB in UTF-8); with the text it stays under GitHub's limit
TEXT_MAX, KONTAKT_MAX = 5000, 200
ARTEN = ("crash", "freeze", "manual")
# long by nature, not a freeze: audio to the player, conversions and matched versions (ffmpeg, rubberband), web
# searches, downloads and accounts (the network can be slow), the folder dialog (waits for the user)
AUSNAHMEN = ("/audio", "/api/als_wav", "/api/fassung", "/api/netz", "/api/freesound", "/api/konten", "/api/ordner",
             "/api/bericht")
AUDIO_ENDUNGEN = ("wav", "wave", "aif", "aiff", "aifc", "flac", "mp3", "ogg", "m4a", "opus")
AUDIO = re.compile(r"[^/\\\n\r\t\"'<>|:*?]+\.(" + "|".join(AUDIO_ENDUNGEN) + r")\b", re.I)
# a path below the home folder or a sample folder (after replacing them); \\ for paths in repr() and JSON
PFAD = re.compile(r"(~|<folder \d+>)((?:(?:\\\\|[/\\])[^/\\\n\r\t\"'<>|*?]+)+)")
ENDUNG = re.compile(r"\.([A-Za-z0-9]{1,5})(?![A-Za-z0-9])")


class Berichte:
    def __init__(self, daten, version, zustand=lambda: {}, ordner=lambda: [], protokolle=(), marken=None):
        self.daten = Path(daten)
        self.ordner = self.daten / "vorfaelle"
        self.sitzung = self.daten / "sitzung.json"
        self.stapel = self.ordner / "stapel.txt"
        self.version = version
        self.zustand = zustand          # () -> dict with counts for the snapshot (no names)
        self.eigene_ordner = ordner     # () -> sample folders, replaced by <folder n> in the snapshot
        self.protokolle = [Path(p) for p in protokolle]
        self.marken = marken or {}      # {path: label} kept readable in stack traces: the app code, Python
        self.lock = threading.Lock()
        self.laufend = {}               # token -> [start, path, reported]
        self._stapel_datei = None

    # ------------------------------------------------------------------ start and end
    def starten(self, port=None):
        self.ordner.mkdir(parents=True, exist_ok=True)
        alt = self._lesen(self.sitzung)
        stapel = self._stapel_alt()
        # The last run on the same port is over for sure: this server holds the port now (a still running one would
        # have kept it). Otherwise ask for its process; process numbers come back after a while, hence the port first.
        vorbei = alt and alt.get("pid") != os.getpid() and (
            (port is not None and alt.get("port") == port) or not plattform.lebt(alt.get("pid")))
        if vorbei:
            self.vorfall("crash", {"was": "Open Libary ended without closing (crash, forced quit or power loss)",
                                   "seit": alt.get("start"), "version_damals": alt.get("version"),
                                   "stapel": stapel, "protokoll": self._protokoll_ende()})
        elif stapel:
            self.vorfall("freeze", {"was": "Open Libary stood still for more than a minute", "stapel": stapel})
        self._schreiben(self.sitzung, {"pid": os.getpid(), "port": port, "version": self.version,
                                       "start": time.strftime("%Y-%m-%d %H:%M:%S")})
        # Whole process standing still: not on Windows, where Modern Standby freezes processes while the clock that
        # tells sleep apart keeps running (each wake-up would look like a freeze). Slow requests count there too.
        if not plattform.WINDOWS:
            try:
                self._stapel_datei = open(self.stapel, "w", encoding="utf-8")
                faulthandler.dump_traceback_later(STAPEL_S, repeat=False, file=self._stapel_datei)
            except (OSError, RuntimeError):
                self._stapel_datei = None
        threading.Thread(target=self._waechter, daemon=True, name="waechter").start()

    def beenden(self):
        """Clean end: no crash report at the next start."""
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        for p in (self.sitzung, self.stapel):
            try:
                p.unlink()
            except OSError:
                pass

    def _stapel_alt(self):
        try:
            t = self.stapel.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        try:
            self.stapel.unlink()
        except OSError:
            pass
        return t[-20000:]

    # ------------------------------------------------------------------ freeze watch
    def anfrage(self, pfad):
        return _Anfrage(self, pfad)

    def _waechter(self):
        wach, uhr = wachzeit(), time.time()
        while True:
            time.sleep(5)
            wach_neu, uhr_neu = wachzeit(), time.time()
            schlief = (uhr_neu - uhr) - (wach_neu - wach) > 10      # the computer slept in between
            wach, uhr = wach_neu, uhr_neu
            if self._stapel_datei is not None:
                try:
                    if os.fstat(self._stapel_datei.fileno()).st_size > 0:      # faulthandler fired: stood still
                        self._stapel_datei.flush()
                        text = self.stapel.read_text(encoding="utf-8", errors="replace").strip()
                        self._stapel_datei.seek(0)
                        self._stapel_datei.truncate()
                        if text and not schlief:
                            self.vorfall("freeze", {"was": "Open Libary stood still for more than a minute",
                                                    "stapel": text[-20000:]})
                    # re-arm: only a process that stands still for STAPEL_S lets it fire
                    faulthandler.dump_traceback_later(STAPEL_S, repeat=False, file=self._stapel_datei)
                except Exception:
                    pass
            jetzt = wachzeit()
            with self.lock:
                if schlief:               # requests that were running while asleep start counting again
                    for a in self.laufend.values():
                        a[0] = jetzt
                haengt = [(t, a) for t, a in self.laufend.items()
                          if not a[2] and jetzt - a[0] > HAENGER_S and not a[1].startswith(AUSNAHMEN)]
                for _, a in haengt:
                    a[2] = True
            for _, (start, pfad, _) in haengt:
                # the page translates this exact wording (brWasText in seite.html)
                self.vorfall("freeze", {"was": f"A request has been running for {int(jetzt - start)} s",
                                        "anfrage": pfad, "stapel": alle_stapel()})

    # ------------------------------------------------------------------ incidents
    def vorfall(self, art, details):
        if art not in ARTEN:
            art = "manual"
        v = {"id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6], "art": art,
             "zeit": time.strftime("%Y-%m-%d %H:%M:%S"), "version": self.version, "details": details or {}}
        self.ordner.mkdir(parents=True, exist_ok=True)
        self._schreiben(self.ordner / f"{v['id']}.json", v)
        return v

    def alle(self):
        aus = []
        for p in sorted(self.ordner.glob("*.json")):
            v = self._lesen(p)
            if isinstance(v, dict) and v.get("id") == p.stem:
                aus.append(v)
        return aus

    def offen(self):
        return [v for v in self.alle() if not v.get("erledigt")]

    def erledigen(self, ids):
        """Asked about (sent or "not now"): no more questions. Kept 30 days for later reports, then deleted."""
        alt = time.time() - 30 * 86400
        for v in self.alle():
            p = self.ordner / f"{v['id']}.json"
            if v["id"] in ids and not v.get("erledigt"):
                v["erledigt"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._schreiben(p, v)
            elif v.get("erledigt"):
                try:
                    if p.stat().st_mtime < alt:
                        p.unlink()
                except OSError:
                    pass

    def fragen(self, an=None):
        """Ask after crashes and freezes (default on); an=True/False saves it."""
        p = self.daten / "berichte.json"
        d = self._lesen(p) or {}
        if an is not None:
            d["fragen"] = bool(an)
            self._schreiben(p, d)
        return d.get("fragen", True)

    # ------------------------------------------------------------------ snapshot
    def schnappschuss(self, ids=(), mit_dateinamen=False):
        """Plain text, anonymized: the user sees exactly this before sending."""
        z = []
        try:
            zustand = self.zustand() or {}
        except Exception as e:                  # a broken counter must not break the report
            zustand = {"error while counting": f"{type(e).__name__}: {e}"}
        z.append(f"Open Libary {self.version} ({'app' if os.environ.get('OPENLIBARY_PAKET') else 'source'})")
        z.append(f"System: {system_text()}, Python {platform.python_version()}")
        for k, w in zustand.items():
            z.append(f"{k}: {w}")
        gewaehlt = [v for v in self.alle() if v["id"] in set(ids)]
        for v in gewaehlt:
            z.append("")
            z.append(f"== Incident {v['zeit']}: {v['art']} (version {v.get('version')})")
            d = dict(v.get("details") or {})
            for k, name in (("was", "what"), ("anfrage", "request"), ("seit", "running since"),
                            ("version_damals", "version then"), ("quelle", "noticed by")):
                if d.get(k):
                    z.append(f"{name}: {d[k]}")
            for k in ("stapel", "protokoll"):
                if d.get(k):
                    z.append(f"-- {'stacks' if k == 'stapel' else 'log before the incident'}:")
                    z.append(str(d[k]).rstrip())
        kopf = anonym("\n".join(z), self.eigene_ordner(), mit_dateinamen, self.marken)
        log = anonym(self._protokoll_ende(), self.eigene_ordner(), mit_dateinamen, self.marken)
        if len(kopf) > SCHNAPPSCHUSS_MAX * 2 // 3:         # many incidents: keep the beginning
            kopf = kopf[:SCHNAPPSCHUSS_MAX * 2 // 3] + "\n[… shortened …]"
        platz = SCHNAPPSCHUSS_MAX - len(kopf) - 100
        if len(log) > platz:                                # the log loses its oldest lines first
            log = "[… older lines left out …]\n" + log[-platz:].split("\n", 1)[-1]
        return f"{kopf}\n\n== Log, last lines\n{log}"

    def _protokoll_ende(self):
        # the file this server writes its output to; only without one the first candidate that exists (another
        # install's newer log must not end up in the report)
        p = eigene_ausgabe() or next((q for q in self.protokolle if q.is_file()), None)
        if p is None:
            return "(no log found)"
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 200_000))
                zeilen = f.read().decode("utf-8", "replace").splitlines()
        except OSError as e:
            return f"(log not readable: {e})"
        return "\n".join(zeilen[-PROTOKOLL_ZEILEN:])

    # ------------------------------------------------------------------ sending
    def senden(self, art, text, kontakt, schnappschuss, ids):
        """-> (ok, message for the user). schnappschuss: the text the form showed, or None."""
        if not URL:
            return False, N_("Sending is not set up in this version.")
        daten = {"app": "open-libary", "version": self.version, "system": system_text(),
                 "art": art if art in ARTEN else "manual", "text": text[:TEXT_MAX],
                 "kontakt": (kontakt or "")[:KONTAKT_MAX], "snapshot": schnappschuss[:SCHNAPPSCHUSS_MAX] if schnappschuss else None}
        req = urllib.request.Request(URL, data=json.dumps(daten, ensure_ascii=False).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": f"OpenLibary/{self.version}"})
        # one deadline for everything (name lookup, a dead IPv6 route, the receiver's way to GitHub): the page waits
        # 45 s, the timeout of urlopen only counts per attempt
        aus = {}

        def schicken():
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    aus["status"] = r.status
            except urllib.error.HTTPError as e:
                aus["status"] = e.code
            except (OSError, ValueError):
                aus["status"] = None
        t = threading.Thread(target=schicken, daemon=True, name="bericht")
        t.start()
        t.join(30)
        status = aus.get("status")
        if status is None:
            return False, N_("No connection to the report service. Please try again later.")
        if status == 429:
            return False, N_("Too many reports in a short time. Please try again in a few minutes.")
        ok = 200 <= status < 300
        if ok:
            self.erledigen(set(ids))
        return ok, N_("Thank you, the report has arrived.") if ok else N_("The report did not arrive. Please try again later.")

    # ------------------------------------------------------------------ files
    @staticmethod
    def _lesen(p):
        try:
            return json.loads(Path(p).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _schreiben(p, d):
        p = Path(p)
        tmp = p.with_name(p.name + ".tmp")
        try:
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass


class _Anfrage:
    def __init__(self, b, pfad):
        self.b, self.pfad = b, pfad.split("?")[0]

    def __enter__(self):
        self.t = object()
        with self.b.lock:
            self.b.laufend[self.t] = [wachzeit(), self.pfad, False]

    def __exit__(self, *a):
        with self.b.lock:
            self.b.laufend.pop(self.t, None)


def wachzeit():
    """Seconds the computer was awake: a laptop that slept is no freeze. time.monotonic stops during sleep on macOS
    and Linux; on Windows it keeps counting, there QueryUnbiasedInterruptTime does not."""
    if plattform.WINDOWS:
        try:
            import ctypes
            n = ctypes.c_ulonglong(0)
            if ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(n)):
                return n.value / 1e7
        except Exception:
            pass
    return time.monotonic()


def eigene_ausgabe():
    """Path of the file behind this process's standard output (the log), or None (terminal, pipe, unknown)."""
    try:
        if sys.platform.startswith("linux"):
            p = os.readlink("/proc/self/fd/1")
        elif sys.platform == "darwin":
            import fcntl
            p = fcntl.fcntl(1, 50, b"\0" * 1024).split(b"\0", 1)[0].decode("utf-8", "replace")     # F_GETPATH
        elif plattform.WINDOWS:
            import ctypes
            import msvcrt
            puffer = ctypes.create_unicode_buffer(1024)
            n = ctypes.windll.kernel32.GetFinalPathNameByHandleW(ctypes.c_void_p(msvcrt.get_osfhandle(1)), puffer, 1024, 0)
            p = puffer.value[4:] if n and puffer.value.startswith("\\\\?\\") else (puffer.value if n else "")
        else:
            return None
        return Path(p) if p and os.path.isfile(p) else None
    except (OSError, ValueError):
        return None


def alle_stapel():
    namen = {t.ident: t.name for t in threading.enumerate()}
    teile = []
    for ident, rahmen in sys._current_frames().items():
        if ident == threading.get_ident():
            continue
        teile.append(f"Thread {namen.get(ident, ident)}:\n" + "".join(traceback.format_stack(rahmen)[-12:]))
    return "\n".join(teile)[-20000:]


def system_text():
    if plattform.SYSTEM == "mac":
        name = f"macOS {platform.mac_ver()[0]}"
    elif plattform.WINDOWS:
        name = f"Windows {platform.release()} ({platform.version()})"
    else:
        try:
            d = dict(z.split("=", 1) for z in Path("/etc/os-release").read_text().splitlines() if "=" in z)
            name = d.get("PRETTY_NAME", "Linux").strip('"')
        except OSError:
            name = "Linux"
    return f"{name} {platform.machine()}"[:100]


def anonym(text, ordner=(), mit_dateinamen=False, marken=None):
    """For the snapshot. Always: the app code and Python -> <app>, <python> (stack traces stay readable), the user's
    sample folders -> <folder n>, the home folder -> ~, the user name -> <user>. Unless file names are wanted: below
    ~ and the sample folders only the kind of file stays (~/…/<file>.zip, <folder 1>/…/<audio 2>.wav), and audio
    names elsewhere become <audio n>.wav. Each path also in its Windows spellings, case-insensitive."""
    heim = str(Path.home())
    ersetzen = sorted(((str(p), m) for p, m in (marken or {}).items() if p), key=lambda x: len(x[0]), reverse=True)
    ersetzen += [(o, f"<folder {i}>") for i, o in enumerate(sorted({str(o) for o in ordner if o}, key=len, reverse=True), 1)]
    ersetzen.append((heim, "~"))
    for alt, neu in ersetzen:
        if len(alt) < 4:
            continue
        for v in sorted({alt, alt.replace("\\", "/"), alt.replace("/", "\\"), alt.replace("\\", "\\\\"),
                         alt.replace("/", "\\\\")}, key=len, reverse=True):
            text = re.sub(re.escape(v), lambda m: neu, text, flags=re.I)
    nutzer = Path(heim).name
    if len(nutzer) >= 3:             # the user name elsewhere (temp paths, another spelling of the profile)
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(nutzer)}(?![A-Za-z0-9])", "<user>", text, flags=re.I)
    if mit_dateinamen:
        return text
    nummern = {}

    def audio(name, endung):
        n = nummern.setdefault(name.lower(), len(nummern) + 1)
        return f"<audio {n}>.{endung.lower()}"

    def pfad(m):
        wurzel, rest = m.group(1), m.group(2)
        trenner = "\\" if "\\" in rest and "/" not in rest else "/"
        letzter = [t for t in re.split(r"[/\\]+", rest) if t][-1]
        e = ENDUNG.search(letzter)
        if not e:
            return f"{wurzel}{trenner}…"
        datei = audio(letzter[:e.end()], e.group(1)) if e.group(1).lower() in AUDIO_ENDUNGEN \
            else f"<file>.{e.group(1).lower()}"
        return f"{wurzel}{trenner}…{trenner}{datei}"
    text = PFAD.sub(pfad, text)
    return AUDIO.sub(lambda m: m.group(0) if m.group(0).startswith("<audio ") else audio(m.group(0), m.group(1)), text)
