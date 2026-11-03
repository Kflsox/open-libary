# Open Libary

Eine Sample-Bibliothek für Producer, die auf dem eigenen Rechner läuft: Sie durchsucht deine Sample-Ordner, spielt
sofort ab, und du ziehst Sounds direkt in deine DAW. Dazu sucht sie freie Sounds im Netz und legt sie mit Lizenzbeleg
in deine Bibliothek. Kein Konto, keine Cloud, deine Dateien bleiben, wo sie sind.

*A local sample library for music producers on macOS, Windows and Linux. The interface is in German.*

Der Name ist mit Absicht „Libary“: „Open Library“ heißt schon der Bücherkatalog des Internet Archive.

## Was sie kann

- **Eigene Ordner** einlesen: WAV, AIFF, FLAC, MP3, OGG, auch die Werkssamples von FL Studio. Nichts wird verschoben.
- **Finden:** Suche, Filter nach Art (Kick, Snare, 808, Loop, One-Shot …), Tempo, Tonart und Länge, „Ähnliche“ nach
  Klang, Zufall zum Stöbern, Favoriten, Playlists, Notizen mit anklickbaren Zeitmarken, Verlauf.
- **Hören:** Wiedergabe mit Wellenform, Ausschnitte, Anpassen an Projekttempo und Tonart (Tempo, Tonhöhe, Varispeed;
  dafür braucht es ffmpeg und rubberband).
- **Ziehen in die DAW:** FL Studio, Ableton Live, Logic, Cubase, Studio One, Reaper, Bitwig, Pro Tools. Formate, die
  eine DAW nicht liest (OGG und FLAC in Logic und Pro Tools, die FL-Werkssamples außerhalb von FL), gibt sie dabei als
  WAV weiter.
- **Messen ohne Zusatzprogramme:** Tempo von Loops, Grundton von 808 und Bass, grober Klang-Fingerabdruck. Geschätzte
  Werte sind in der Liste mit „~“ markiert.
- **Netz:** freie Sounds von Freesound, ccMixter und dem Internet Archive suchen, vorhören und laden, nur CC0, CC BY
  und gemeinfrei, jeweils mit Lizenzbeleg. Freesound auf Wunsch mit eigenem Konto (Originaldateien), Looperman über
  den eigenen Browser.
- **Lizenzen klären:** Für eigene Ordner hältst du Pack für Pack fest, was gilt.

## Einrichten

Die fertigen Apps (Mac ab macOS 10.15, Apple-Chip und Intel; Windows 10 und 11; Linux als .deb und tar.gz) bringen
alles mit, es muss nichts installiert werden. Schritt für Schritt, auch was beim ersten Öffnen einer nicht signierten
App zu tun ist: [ANLEITUNG.md](ANLEITUNG.md).

Aus dem Quelltext (Python 3.9 oder neuer, für die Messung zusätzlich `numpy` und `soundfile`):

```
OPENLIBARY_DATEN=~/.local/share/open-libary python3 openlibary.py
```

Dann öffnet sich `http://127.0.0.1:8765/` im Browser. Ziehen in die DAW geht nur in den Apps, nicht im Browser.

## Apps bauen

```
bash app/pythons_holen.sh                      # eigenständige Pythons mit numpy und soundfile (braucht uv)
bash app/app_paket.sh <Zielordner>             # Mac, Universal (Objective-C-Hülle, braucht die Command Line Tools)
cd app/fenster && npm ci && cd ../..
bash app/fenster_paket.sh windows <Zielordner> # Windows x64 (Electron)
bash app/fenster_paket.sh linux <Zielordner>   # Linux x64, .deb und tar.gz
```

## Prüfen

`python tests/rauchtest.py` erzeugt eine kleine Test-Bibliothek aus synthetischen Klängen, startet den Server und
prüft Einlesen, Längen, Wiedergabe jedes Formats, Messung, WAV beim Ziehen und den zweiten Start. Der Workflow
„Prüfen“ (`.github/workflows/pruefen.yml`) lässt das auf Windows, Ubuntu und Mac (Apple-Chip und Intel) laufen und
baut und startet auf Wunsch die fertigen Apps.

## Lizenz

GNU Affero General Public License, Version 3 oder neuer ([LICENSE](LICENSE)). Was die Apps an fremder Software
mitbringen, steht in [DRITTANBIETER.md](DRITTANBIETER.md).
