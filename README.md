# Open Libary

A sample library for producers that runs on your own computer: it searches your sample folders, plays instantly, and
you drag sounds straight into your DAW. It also finds free sounds on the web and adds them to your library with a
license record. No account, no cloud, your files stay where they are.

Runs on macOS, Windows and Linux. The interface is in English and German: Settings > Language switches it. English is
the default; the app menus switch along with it.

The name is "Libary" on purpose: "Open Library" is already the Internet Archive's book catalog.

## What it does

- **Your folders:** scans WAV, AIFF, FLAC, MP3, OGG, including FL Studio's factory samples. Nothing is moved.
- **Find:** search, filters by type (Kick, Snare, 808, Loop, One-Shot …), tempo, key and length, "Similar" by sound,
  Random for browsing, Favorites, playlists, notes with clickable timestamps, History.
- **Listen:** playback with waveform, regions, matching to the project tempo and key (tempo, pitch, varispeed; this
  needs ffmpeg and rubberband).
- **Drag into the DAW:** FL Studio, Ableton Live, Logic, Cubase, Studio One, Reaper, Bitwig, Pro Tools. Formats a DAW
  cannot read (OGG and FLAC in Logic and Pro Tools, the FL factory samples outside FL) are handed over as WAV.
- **Analysis without extra programs:** tempo of loops, root note of 808 and bass, a rough sound fingerprint. Estimated
  values are marked with "~" in the list.
- **Web:** search, preview and download free sounds from Freesound, ccMixter and the Internet Archive, only CC0, CC BY
  and public domain, each with a license record. Freesound optionally with your own account (original files),
  Looperman through your own browser.
- **Sort out licenses:** for your own folders you record, pack by pack, what applies.

## Setup

The finished apps (Mac from macOS 10.15, Apple chip and Intel; Windows 10 and 11; Linux as .deb and tar.gz) bring
everything they need, nothing has to be installed. Step by step, including what to do the first time you open an
unsigned app: [GUIDE.md](GUIDE.md).

From source (Python 3.9 or newer; the analysis also needs `numpy` and `soundfile`):

```
OPENLIBARY_DATEN=~/.local/share/open-libary python3 openlibary.py
```

Then `http://127.0.0.1:8765/` opens in the browser. Dragging into the DAW works only in the apps, not in the browser.

## Building the apps

```
bash app/pythons_holen.sh                         # standalone Pythons with numpy and soundfile (needs uv)
bash app/app_paket.sh <target folder>             # Mac, Universal (Objective-C shell, needs the Command Line Tools)
cd app/fenster && npm ci && cd ../..
bash app/fenster_paket.sh windows <target folder> # Windows x64 (Electron)
bash app/fenster_paket.sh linux <target folder>   # Linux x64, .deb and tar.gz
```

## Testing

`python tests/rauchtest.py` is the smoke test: it builds a small test library from synthetic sounds, starts the server
and checks scanning, durations, playback of every format, analysis, WAV on drag, the interface language and a
second start. `python tests/check_translations.py` checks that every text has an entry in each `lang/*.json`. The workflow
"Check" (`.github/workflows/pruefen.yml`) runs it on Windows, Ubuntu and Mac (Apple chip and Intel) and, on request,
also builds and starts the finished apps.

## Translations

The texts live in `lang/<code>.json`: one JSON object that maps each English text to its translation, one entry per
text. Placeholders such as `{n}` stay as they are. A text without an entry shows in English.

To add a language: copy `lang/de.json` to `lang/<code>.json`, translate the values (not the keys), and add the code
with the language's own name to `LANGUAGES` in `i18n.py`. `python tests/check_translations.py` lists every text that is still missing.

## License

GNU Affero General Public License, version 3 or later ([LICENSE](LICENSE)). Third-party software that the apps
include is listed in [THIRD_PARTY.md](THIRD_PARTY.md).
