# Fremde Software in Open Libary

Open Libary selbst steht unter der GNU Affero General Public License, Version 3 oder neuer (`LICENSE`). Diese Liste
nennt, was die fertigen Apps an fremder Software mitbringen und was Open Libary nur benutzt, wenn es auf dem Rechner
schon da ist. Die Lizenzen sind so angegeben, wie die Quelle sie nennt; das ist eine Zusammenfassung, keine
Rechtsberatung. Die vollständigen Lizenztexte liegen in den Apps bei den jeweiligen Teilen.

## In den fertigen Apps enthalten

| Teil | Wofür | Lizenz | Lizenztext in der App |
|---|---|---|---|
| Python 3.12.13 (python-build-standalone, Stand 20260623) | führt den Server aus | PSF-2.0; mitgebaute Bibliotheken (OpenSSL, SQLite, zlib, libffi, xz, bzip2 und weitere) unter ihren eigenen Lizenzen | `LICENSE.txt` im Python-Ordner |
| numpy 2.5.4 | Rechnen für die leichte Messung | BSD-3-Clause; die Wheels bringen OpenBLAS (BSD-3-Clause) und unter Linux und Windows die GCC-Laufzeit (GPL-3.0 mit GCC Runtime Library Exception) mit | `numpy-2.5.4.dist-info/licenses/` |
| soundfile 0.14.0 | Audiodateien lesen und schreiben | BSD-3-Clause | `soundfile-0.14.0.dist-info/LICENSE` |
| libsndfile 1.2.2 (im soundfile-Wheel, mit libogg, libvorbis, libFLAC, libopus, mpg123, LAME) | Audio-Dekodierung | LGPL-2.1-or-later; libogg, libvorbis, libFLAC, libopus BSD-Lizenzen; mpg123 LGPL-2.1; LAME LGPL-2.0 | `_soundfile_data/COPYING` |
| cffi 2.1.1, pycparser 3.12, typing_extensions 4.16.0 | Unterbau von soundfile | MIT, BSD-3-Clause, PSF-2.0 | jeweilige `dist-info` |
| Electron 44.9.1 mit Chromium und Node.js (nur Windows und Linux) | Programmfenster | MIT; Chromium und seine Bestandteile unter ihren Lizenzen | `LICENSE.electron.txt`, `LICENSES.chromium.html` |

libsndfile liegt als eigene, austauschbare Bibliothek neben dem Programm (`libsndfile_x64.dll` bzw. die `.dylib`
und `.so` im soundfile-Paket). Wer sie gegen eine eigene Fassung tauschen will, kann das tun; den Quelltext gibt es
unter https://github.com/libsndfile/libsndfile.

## Benutzt, wenn vorhanden (nicht enthalten)

| Programm | Wofür | Lizenz |
|---|---|---|
| ffmpeg, ffprobe | Formate wandeln, Längen messen, Varispeed | LGPL-2.1-or-later oder GPL, je nach Build |
| Rubber Band | Tempo und Tonhöhe ändern | GPL-2.0-or-later |
| gehoer | genaue Messung von Tempo, Tonart und Klang (eigenes Werkzeug des Autors, bisher nicht veröffentlicht) | AGPL-3.0-or-later |

## Dienste im Netz

Open Libary fragt Freesound, ccMixter und das Internet Archive über deren öffentliche Schnittstellen ab und lädt nur
Sounds unter CC0, CC BY oder gemeinfrei. Für Freesound legt jeder Nutzer seinen eigenen Zugang an; ein Schlüssel ist
nicht enthalten, weil die Freesound-Bedingungen die Weitergabe verbieten. Looperman wird nicht abgefragt: Open Libary
übernimmt nur Dateien, die der Nutzer selbst im Browser von looperman.com geladen hat.
