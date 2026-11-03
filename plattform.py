"""What differs per operating system (Mac with Apple silicon and Intel, Windows 10 and 11, Linux).

Standard library only. The rest of the code asks this module instead of using `open -R`, `osascript`, `xattr`,
`nice` or Homebrew paths itself.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from i18n import tx

SYSTEM = "mac" if sys.platform == "darwin" else "windows" if os.name == "nt" else "linux"
WINDOWS = SYSTEM == "windows"
# no black console window for helper programs on Windows
_OHNE_FENSTER = 0x08000000 if WINDOWS else 0          # CREATE_NO_WINDOW
_NIEDRIG = 0x00004000 if WINDOWS else 0               # BELOW_NORMAL_PRIORITY_CLASS


def lebt(pid):
    """Is the process still running? On Windows never os.kill(pid, 0): 0 is CTRL_C_EVENT there, and any other signal
    ends the process via TerminateProcess. Use OpenProcess and GetExitCodeProcess instead (259 = STILL_ACTIVE)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p          # the handle is 64 bit, truncated without restype
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
        return True                                    # running, it just belongs to another user
    except OSError:
        return False


def nur_in_cloud(st):
    """Placeholder of a cloud drive whose content is not on disk: reading would download it first
    (iCloud: SF_DATALESS; OneDrive and other Windows cloud files: OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS)."""
    if getattr(st, "st_flags", 0) & 0x40000000:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & (0x1000 | 0x40000 | 0x400000))


def datenordner():
    """User data of the packaged app and the light analysis."""
    if SYSTEM == "mac":
        return Path.home() / "Library/Application Support/Open Libary"
    if WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "Open Libary"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "open-libary"


def konfigordner():
    """Logins and keys (Freesound, accounts). Mac and Linux as before: ~/.config/openlibary."""
    if WINDOWS:
        return datenordner() / "konfig"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "openlibary"


def protokoll():
    if SYSTEM == "mac":
        return Path.home() / "Library/Logs/openlibary.log"
    return datenordner() / "openlibary.log"


def werkzeug(name):
    """ffmpeg, ffprobe, rubberband: in PATH, from Homebrew or next to the app (folder “werkzeuge”), otherwise None."""
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
    """Extra subprocess arguments on Windows: no console window."""
    return {"creationflags": _OHNE_FENSTER} if WINDOWS else {}


def niedrig(befehl):
    """-> (command, subprocess extras) at low priority: nice on Mac and Linux, priority class on Windows."""
    if WINDOWS:
        return list(befehl), {"creationflags": _NIEDRIG | _OHNE_FENSTER}
    if shutil.which("nice"):
        return ["nice", "-n", "15"] + list(befehl), {}
    return list(befehl), {}


def im_ordner_zeigen(pfad):
    """Show a file in Finder, Explorer or the file manager (a folder: open it)."""
    pfad = str(pfad)
    ordner = os.path.isdir(pfad)
    if SYSTEM == "mac":
        subprocess.run(["open", pfad] if ordner else ["open", "-R", pfad], check=False)
    elif WINDOWS:
        subprocess.run(["explorer", pfad] if ordner else ["explorer", "/select,", pfad], check=False)
    else:
        subprocess.run(["xdg-open", pfad if ordner else os.path.dirname(pfad)], check=False)


def ordner_waehlen():
    """The system's folder dialog for the browser (the apps have their own). -> path, None (cancelled), or raises
    OSError when there is no dialog."""
    if SYSTEM == "mac":
        # translations go into script strings: escape them (AppleScript "…" here, PowerShell '…' below)
        frage = tx("Choose a sample folder for Open Libary").replace("\\", "\\\\").replace('"', '\\"')
        r = subprocess.run(["osascript", "-e", f'POSIX path of (choose folder with prompt "{frage}")'],
                           capture_output=True, text=True, timeout=600)
    elif WINDOWS:
        # UTF-8 without BOM (Windows PowerShell 5.1 otherwise puts a \ufeff before the path); an invisible TopMost window
        # as owner, otherwise the dialog opens behind the browser and the page seems to hang
        skript = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false;"
                  "Add-Type -AssemblyName System.Windows.Forms;"
                  "$f = New-Object System.Windows.Forms.Form -Property @{TopMost = $true; ShowInTaskbar = $false};"
                  "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                  "$d.Description = '" + tx("Choose a sample folder for Open Libary").replace("'", "''") + "';"
                  "if ($d.ShowDialog($f) -eq 'OK') { $d.SelectedPath }; $f.Dispose()")
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", skript], capture_output=True, text=True,
                           timeout=600, encoding="utf-8", **hilfsprozess_args())
    else:
        if not shutil.which("zenity"):
            raise OSError("no folder dialog (zenity is missing)")
        r = subprocess.run(["zenity", "--file-selection", "--directory", f"--title={tx('Sample folder for Open Libary')}"],
                           capture_output=True, text=True, timeout=600)
    pfad = (r.stdout or "").strip().lstrip("\ufeff")
    return pfad if r.returncode == 0 and pfad else None


def herkunft(pfad):
    """Addresses the browser downloaded the file from (download and page address), otherwise [].
    Mac: kMDItemWhereFroms (Safari, Chrome, Firefox). Windows: Zone.Identifier (Edge, Chrome, Firefox write
    HostUrl and ReferrerUrl, “Mark of the Web”). Linux: user.xdg.origin.url and user.xdg.referrer.url (Chrome)."""
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
    """-> reason or None: folders that are too big or belong to the system (Your folders)."""
    heim = os.path.realpath(os.path.expanduser("~"))
    teile = Path(p).parts
    if WINDOWS:
        laufwerk = os.path.splitdrive(p)[0]
        unten = p.lower()
        if len(teile) <= 1 or p.rstrip("\\/") == laufwerk:
            return tx("A whole drive is too big; choose a sample folder on it")
        if unten.rstrip("\\/") == heim.lower() or os.path.dirname(heim).lower() == unten.rstrip("\\/"):
            return tx("The whole user folder is too big; choose a sample folder inside it")
        for gesperrt in ("windows", "program files", "program files (x86)", "programdata", "$recycle.bin"):
            if unten == (laufwerk + "\\" + gesperrt).lower() or unten.startswith((laufwerk + "\\" + gesperrt + "\\").lower()):
                return tx("This folder belongs to the system; choose a sample folder")
        appdata = os.path.join(heim, "AppData").lower()
        if unten == appdata or unten.startswith(appdata + "\\"):
            return tx("This folder holds program data; choose a sample folder")
        return None
    if p in ("/", heim, "/Users", "/home", "/Volumes", "/media", "/mnt", "/System", "/private", "/usr", "/etc") \
            or p.startswith(("/System/", "/private/var/", "/dev", "/proc", "/sys", "/usr/", "/etc/")):
        return tx("This folder is too big or belongs to the system; choose a sample folder inside it")
    if len(teile) < 3 and not p.startswith(("/Volumes/", "/media/", "/mnt/")):
        return tx("This folder is too high up; choose a sample folder inside it")
    bib = os.path.join(heim, "Library")
    if SYSTEM == "mac" and (p == bib or any(p == os.path.join(bib, x) or p.startswith(os.path.join(bib, x) + os.sep)
                                            for x in ("Mobile Documents", "Group Containers", "Containers", "Messages",
                                                      "Mail"))):
        # otherwise voice messages and voice memos would end up in the index (and be playable)
        return tx("This folder holds app data (Messages, Voice Memos, iCloud); choose a sample folder")
    return None
