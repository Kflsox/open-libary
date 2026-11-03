"""Was je Betriebssystem anders ist (03.11.2026, „max kompi“: Mac mit Apple-Chip und Intel, Windows 10 und 11, Linux).

Alles hier nur Standardbibliothek. Der übrige Code fragt dieses Modul statt selbst `open -R`, `osascript`, `xattr`,
`nice` oder Homebrew-Pfade zu benutzen.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM = "mac" if sys.platform == "darwin" else "windows" if os.name == "nt" else "linux"
WINDOWS = SYSTEM == "windows"
# kein schwarzes Konsolenfenster bei Hilfsprogrammen auf Windows
_OHNE_FENSTER = 0x08000000 if WINDOWS else 0          # CREATE_NO_WINDOW
_NIEDRIG = 0x00004000 if WINDOWS else 0               # BELOW_NORMAL_PRIORITY_CLASS


def lebt(pid):
    """Läuft der Prozess noch? Auf Windows nie os.kill(pid, 0): 0 ist dort CTRL_C_EVENT, und jedes andere Signal
    beendet den Prozess per TerminateProcess. Stattdessen OpenProcess und GetExitCodeProcess (259 = STILL_ACTIVE)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p          # Handle ist 64 Bit, ohne restype abgeschnitten
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True                                    # läuft, gehört nur einem anderen Nutzer
    except OSError:
        return False


def nur_in_cloud(st):
    """Platzhalter einer Cloud-Ablage, dessen Inhalt nicht auf der Platte liegt: Lesen würde ihn erst herunterladen
    (iCloud: SF_DATALESS; OneDrive und andere Windows-Cloud-Dateien: OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS)."""
    if getattr(st, "st_flags", 0) & 0x40000000:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & (0x1000 | 0x40000 | 0x400000))


def datenordner():
    """Nutzerdaten der fertigen App und die leichte Messung."""
    if SYSTEM == "mac":
        return Path.home() / "Library/Application Support/Open Libary"
    if WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "Open Libary"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "open-libary"


def konfigordner():
    """Logins und Schlüssel (Freesound, Konten). Mac und Linux wie bisher ~/.config/openlibary."""
    if WINDOWS:
        return datenordner() / "konfig"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "openlibary"


def protokoll():
    if SYSTEM == "mac":
        return Path.home() / "Library/Logs/openlibary.log"
    return datenordner() / "openlibary.log"


def werkzeug(name):
    """ffmpeg, ffprobe, rubberband: im PATH, bei Homebrew oder neben der App (Ordner „werkzeuge“), sonst None."""
    exe = name + (".exe" if WINDOWS else "")
    gefunden = shutil.which(name)
    if gefunden:
        return gefunden
    orte = [Path(__file__).resolve().parent / "werkzeuge" / exe]
    if SYSTEM == "mac":
        orte += [Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name]
    elif SYSTEM == "linux":
        orte += [Path("/usr/bin") / name, Path("/usr/local/bin") / name]
    for o in orte:
        if o.is_file():
            return str(o)
    return None


def hilfsprozess_args():
    """Zusätze für subprocess auf Windows: kein Konsolenfenster."""
    return {"creationflags": _OHNE_FENSTER} if WINDOWS else {}


def niedrig(befehl):
    """-> (Befehl, subprocess-Zusätze) mit niedriger Priorität: nice auf Mac und Linux, Prioritätsklasse auf Windows."""
    if WINDOWS:
        return list(befehl), {"creationflags": _NIEDRIG | _OHNE_FENSTER}
    if shutil.which("nice"):
        return ["nice", "-n", "15"] + list(befehl), {}
    return list(befehl), {}


def im_ordner_zeigen(pfad):
    """Datei im Finder, Explorer oder Dateimanager zeigen (Ordner: öffnen)."""
    pfad = str(pfad)
    ordner = os.path.isdir(pfad)
    if SYSTEM == "mac":
        subprocess.run(["open", pfad] if ordner else ["open", "-R", pfad], check=False)
    elif WINDOWS:
        subprocess.run(["explorer", pfad] if ordner else ["explorer", "/select,", pfad], check=False)
    else:
        subprocess.run(["xdg-open", pfad if ordner else os.path.dirname(pfad)], check=False)


def ordner_waehlen():
    """Ordnerdialog des Systems für den Browser (die Apps haben einen eigenen). -> Pfad, None (abgebrochen) oder
    löst OSError aus, wenn es keinen Dialog gibt."""
    if SYSTEM == "mac":
        r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt '
                            '"Sample-Ordner für Open Libary wählen")'], capture_output=True, text=True, timeout=600)
    elif WINDOWS:
        # UTF-8 ohne BOM (Windows PowerShell 5.1 setzt sonst ein \ufeff vor den Pfad); unsichtbares Fenster mit
        # TopMost als Besitzer, sonst öffnet der Dialog hinter dem Browser und die Seite scheint zu hängen
        skript = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false;"
                  "Add-Type -AssemblyName System.Windows.Forms;"
                  "$f = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false};"
                  "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                  "$d.Description = 'Sample-Ordner für Open Libary wählen';"
                  "if ($d.ShowDialog($f) -eq 'OK') { $d.SelectedPath }; $f.Dispose()")
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", skript], capture_output=True, text=True,
                           timeout=600, encoding="utf-8", **hilfsprozess_args())
    else:
        if not shutil.which("zenity"):
            raise OSError("kein Ordnerdialog (zenity fehlt)")
        r = subprocess.run(["zenity", "--file-selection", "--directory", "--title=Sample-Ordner für Open Libary"],
                           capture_output=True, text=True, timeout=600)
    pfad = (r.stdout or "").strip().lstrip("\ufeff")
    return pfad if r.returncode == 0 and pfad else None


def herkunft(pfad):
    """Adressen, von denen der Browser die Datei geladen hat (Download- und Seitenadresse), sonst [].
    Mac: kMDItemWhereFroms (Safari, Chrome, Firefox). Windows: Zone.Identifier (Edge, Chrome, Firefox schreiben
    HostUrl und ReferrerUrl, „Mark of the Web“). Linux: user.xdg.origin.url und user.xdg.referrer.url (Chrome)."""
    try:
        if SYSTEM == "mac":
            r = subprocess.run(["xattr", "-px", "com.apple.metadata:kMDItemWhereFroms", str(pfad)],
                               capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                return []
            import plistlib
            d = plistlib.loads(bytes.fromhex("".join(r.stdout.split())))
            return [str(x) for x in d] if isinstance(d, list) else []
        if WINDOWS:
            with open(str(pfad) + ":Zone.Identifier", encoding="utf-8", errors="replace") as f:
                text = f.read()
            return [m.group(1).strip() for m in re.finditer(r"(?im)^(?:HostUrl|ReferrerUrl)=(.+)$", text)]
        aus = []
        for name in ("user.xdg.origin.url", "user.xdg.referrer.url"):
            try:
                aus.append(os.getxattr(str(pfad), name).decode("utf-8", "replace"))
            except OSError:
                pass
        return aus
    except Exception:
        return []


def gesperrter_ordner(p):
    """-> Grund oder None: Ordner, die zu groß sind oder dem System gehören (Eigene Ordner)."""
    heim = os.path.realpath(os.path.expanduser("~"))
    teile = Path(p).parts
    if WINDOWS:
        laufwerk = os.path.splitdrive(p)[0]
        unten = p.lower()
        if len(teile) <= 1 or p.rstrip("\\/") == laufwerk:
            return "Ein ganzes Laufwerk ist zu groß; einen Sample-Ordner darauf wählen"
        if unten.rstrip("\\/") == heim.lower() or os.path.dirname(heim).lower() == unten.rstrip("\\/"):
            return "Der ganze Benutzerordner ist zu groß; einen Sample-Ordner darunter wählen"
        for gesperrt in ("windows", "program files", "program files (x86)", "programdata", "$recycle.bin"):
            if unten == (laufwerk + "\\" + gesperrt).lower() or unten.startswith((laufwerk + "\\" + gesperrt + "\\").lower()):
                return "Dieser Ordner gehört dem System; einen Sample-Ordner wählen"
        appdata = os.path.join(heim, "AppData").lower()
        if unten == appdata or unten.startswith(appdata + "\\"):
            return "Dieser Ordner hält Daten von Programmen; einen Sample-Ordner wählen"
        return None
    if p in ("/", heim, "/Users", "/home", "/Volumes", "/media", "/mnt", "/System", "/private", "/usr", "/etc") \
            or p.startswith(("/System/", "/private/var/", "/dev", "/proc", "/sys", "/usr/", "/etc/")):
        return "Dieser Ordner ist zu groß oder gehört dem System; einen Sample-Ordner darunter wählen"
    if len(teile) < 3 and not p.startswith(("/Volumes/", "/media/", "/mnt/")):
        return "Dieser Ordner ist zu weit oben; einen Sample-Ordner darunter wählen"
    bib = os.path.join(heim, "Library")
    if SYSTEM == "mac" and (p == bib or any(p == os.path.join(bib, x) or p.startswith(os.path.join(bib, x) + os.sep)
                                            for x in ("Mobile Documents", "Group Containers", "Containers", "Messages",
                                                      "Mail"))):
        # sonst kämen Sprachnachrichten und Sprachmemos in den Index (und wären abspielbar)
        return "Dieser Ordner hält Daten von Apps (Nachrichten, Memos, iCloud); einen Sample-Ordner wählen"
    return None
