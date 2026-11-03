# Open Libary einrichten

Open Libary ist eine Sample-Bibliothek für Mac, Windows und Linux. Sie durchsucht deine eigenen Sample-Ordner, spielt sofort ab, und du
ziehst Sounds direkt in deine DAW, ob FL Studio, Ableton, Logic oder eine andere. Dazu sucht sie freie Sounds im Netz (Freesound, ccMixter,
Internet Archive) und legt sie mit Lizenzbeleg in deine Bibliothek.

## Einrichten: die fertige App (eine Minute, kein Terminal)

Es gibt drei Pakete. Python und alles Nötige stecken drin, es muss nichts installiert werden.

| Rechner | Paket |
|---|---|
| Mac mit Apple-Chip (ab macOS 11) oder Intel (ab macOS 10.15) | „Open Libary … Mac.zip“ |
| Windows 10 oder 11, 64 Bit | „Open Libary … Windows.zip“ |
| Linux, 64 Bit: Ubuntu, Mint, Debian | „Open Libary … Linux.deb“ |
| Linux, 64 Bit: andere | „Open Libary … Linux.tar.gz“ |

**Mac**

🟢 Zip öffnen und „Open Libary“ in den Ordner **Programme** ziehen, dann starten.

🟢 Beim ersten Mal meldet macOS einen nicht verifizierten Entwickler: **Systemeinstellungen, Datenschutz und
Sicherheit**, ganz unten bei Open Libary **Dennoch öffnen** (unter macOS 10.15 bis 14: Rechtsklick auf die App, Öffnen).
Danach fragt macOS nach dem Zugriff auf Ordner: **Erlauben**.

**Windows**

🟢 Zip mit Rechtsklick, **Alle extrahieren**, an einen festen Ort, etwa `Dokumente\Open Libary`. Dann
**Open Libary.exe** starten (gern mit Rechtsklick an die Taskleiste anheften).

🟢 Windows SmartScreen meldet beim ersten Mal eine unbekannte App: **Weitere Informationen**, dann **Trotzdem
ausführen**.

🔴 Die App nicht direkt aus dem Zip starten, ohne zu entpacken: dann fehlen ihr Python und der Code.

**Linux**

🟢 Ubuntu, Mint, Debian: die `.deb` doppelklicken und installieren (oder `sudo apt install ./"Open Libary … Linux.deb"`).
Danach steht Open Libary im Startmenü unter Multimedia.

🟢 Andere Systeme: entpacken (`tar xzf "Open Libary … Linux.tar.gz"`) und `open-libary` im entpackten Ordner starten.
Bricht sie mit einer Meldung zur „sandbox“ ab: `./open-libary --no-sandbox`.

**Auf allen Systemen**

🟢 Auf **Ordner hinzufügen …** und deine Sample-Ordner wählen (Drum Kits, Packs, eigene Aufnahmen). Die Dateien
bleiben, wo sie sind. Im Hintergrund misst Open Libary danach Tempo, Grundton und Klang (ein paar Minuten, der Rechner
merkt davon wenig); neue Dateien liest sie beim Start und stündlich nach.

🟢 Sounds ziehst du aus der Liste direkt in deine DAW (FL Studio, Ableton Live, Logic, Cubase, Studio One, Reaper,
Bitwig, Pro Tools). Logic und Pro Tools nehmen kein OGG und FLAC: Open Libary gibt diese beim Ziehen als WAV weiter
(Einstellungen, „Beim Ziehen in die DAW“; für Pro Tools „alles als WAV“).

🔴 Nicht ein ganzes Laufwerk oder den ganzen Benutzerordner hinzufügen, sondern die Sample-Ordner darauf.

🔴 Liegen Samples in OneDrive oder iCloud nur online (Wolken-Symbol), lässt Open Libary sie aus, sonst lüde sie alles
herunter. Wer sie dabei haben will: Rechtsklick auf den Ordner, **Immer auf diesem Gerät behalten**.

Deine Favoriten, Playlists und Einstellungen liegen auf dem Mac in `~/Library/Application Support/Open Libary`, unter
Windows in `%APPDATA%\Open Libary`, unter Linux in `~/.local/share/open-libary`. Ein Update ist einfach die neue App
an die Stelle der alten.

Empfohlen, wenn vorhanden: ffmpeg und rubberband (Mac mit Homebrew: `brew install ffmpeg rubberband`); sie machen das
Anpassen an dein Projekttempo möglich. Ohne sie geht alles andere.

## Einrichten aus dem Quelltext (mit Terminal)

🟢 Den Ordner „Open Libary“ an einen festen Ort legen, etwa `~/Programme/Open Libary`, dann im Terminal:

```
bash ~/Programme/Open\ Libary/einrichten.sh
```

Fehlen die Command Line Tools von Apple, öffnet macOS ein Fenster: „Installieren“, danach den Befehl noch einmal.

## Freesound und Looperman (freiwillig)

Beide laufen über deinen normalen Browser und dein eigenes Konto. Oben im Reiter **Netz** steht die Kontenleiste.

🟢 **Freesound:** Kostenloses Konto auf freesound.org, dann auf freesound.org/apiv2/apply einen eigenen Zugang anlegen,
als Callback URL `http://127.0.0.1:8765/api/freesound/rueckruf`. Client ID und Client secret in Open Libary unter
Einstellungen, Freesound einfügen. Danach im Netz-Reiter **Verbinden**: Downloads kommen als Original.

🔴 Den Freesound-Zugang nicht an andere weitergeben: die Freesound-Bedingungen verbieten das, jeder legt seinen eigenen an.

🟢 **Looperman:** Im Netz-Reiter **Verbinden**, dann im Browser auf looperman.com mit eigenem Konto anmelden. Was du dort
herunterlädst, holt Open Libary aus deinem Downloads-Ordner in die Bibliothek (Ordner „Looperman“) und zeigt es im
Streifen „Von Looperman“: anhören und direkt nach FL ziehen.

🔴 Looperman-Loops nicht weitergeben oder als Sample-Pack verkaufen, und nicht für KI-Training nutzen (Bedingungen von
Looperman).

## Was man wissen sollte

- **Lizenzen:** Eigene Ordner stehen als „unklar“, weil die App nicht weiß, woher deine Samples stammen. Unter
  Bibliothek, „offene Lizenzen Pack für Pack klären“ kannst du je Pack festhalten, was gilt. Netz-Downloads tragen die
  Lizenz der Quelle und einen Beleg im Ordner.
- **Tempo, Tonart, Art** kommen aus dem Dateinamen („Kick“, „120bpm“, „F#m“); was dort fehlt, schätzt die leichte
  Messung: Tempo von Loops und Grundton von 808 und Bass, in der Liste mit „~“ markiert. Ein geschätztes Tempo kann
  halb oder doppelt sein. Tonarten von Loops schätzt sie nicht (zu unsicher). Genauer misst gehoer, ein eigenes
  Messwerkzeug, das Open Libary nutzt, wenn es auf dem Rechner eingerichtet ist.
- **Update:** Das neue Paket entpacken, an einen festen Ort legen und dort `einrichten.sh` starten. Es übernimmt
  Favoriten, Playlists, Notizen, Verlauf, eigene Ordner und Stil aus dem bisherigen Ordner (`sammlungen.json`,
  `verlauf.json`, `korrekturen.json`, `zusaetze.csv`, `eigene.csv`, `ordner.json`, `stil.txt`). Den alten Ordner erst
  danach löschen.
- **Probleme:** Im Terminal zeigt `openlibary log` die letzten Meldungen, `openlibary stopp` beendet den Server.
