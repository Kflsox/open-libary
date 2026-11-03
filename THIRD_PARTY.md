# Third-party software in Open Libary

Open Libary itself is licensed under the GNU Affero General Public License, version 3 or later (`LICENSE`). This list
names the third-party software the finished apps include, and what Open Libary only uses when it is already on the
computer. Licenses are given as the source states them; this is a summary, not legal advice. The full license texts
are included in the apps next to each part.

## Included in the finished apps

| Part | Used for | License | License text in the app |
|---|---|---|---|
| Python 3.12.13 (python-build-standalone, release 20260623) | runs the server | PSF-2.0; bundled libraries (OpenSSL, SQLite, zlib, libffi, xz, bzip2 and others) under their own licenses | `LICENSE.txt` in the Python folder |
| numpy 2.5.4 | math for the light analysis | BSD-3-Clause; the wheels include OpenBLAS (BSD-3-Clause) and, on Linux and Windows, the GCC runtime (GPL-3.0 with GCC Runtime Library Exception) | `numpy-2.5.4.dist-info/licenses/` |
| soundfile 0.14.0 | reading and writing audio files | BSD-3-Clause | `soundfile-0.14.0.dist-info/LICENSE` |
| libsndfile 1.2.2 (in the soundfile wheel, with libogg, libvorbis, libFLAC, libopus, mpg123, LAME) | audio decoding | LGPL-2.1-or-later; libogg, libvorbis, libFLAC, libopus BSD licenses; mpg123 LGPL-2.1; LAME LGPL-2.0 | `_soundfile_data/COPYING` |
| cffi 2.1.1, pycparser 3.12, typing_extensions 4.16.0 | used by soundfile | MIT, BSD-3-Clause, PSF-2.0 | each package's `dist-info` |
| Electron 44.9.1 with Chromium and Node.js (Windows and Linux only) | app window | MIT; Chromium and its components under their own licenses | `LICENSE.electron.txt`, `LICENSES.chromium.html` |

libsndfile ships as a separate, replaceable library next to the program (`libsndfile_x64.dll`, or the `.dylib` and
`.so` in the soundfile package). You may replace it with your own build; the source code is at
https://github.com/libsndfile/libsndfile.

## Used if present (not included)

| Program | Used for | License |
|---|---|---|
| ffmpeg, ffprobe | converting formats, measuring durations, varispeed | LGPL-2.1-or-later or GPL, depending on the build |
| Rubber Band | changing tempo and pitch | GPL-2.0-or-later |
| gehoer | accurate analysis of tempo, key and sound (the author's own tool, not published yet) | AGPL-3.0-or-later |

## Web services

Open Libary queries Freesound, ccMixter and the Internet Archive through their public APIs and downloads only sounds
under CC0, CC BY or public domain. For Freesound every user creates their own access; no key is included, because the
Freesound terms forbid passing it on. Looperman is not queried: Open Libary only takes in files the user downloaded
from looperman.com in their own browser.
