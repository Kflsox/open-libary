# Setting up Open Libary

Open Libary is a sample library for Mac, Windows and Linux. It searches your own sample folders, plays instantly, and
you drag sounds straight into your DAW, whether FL Studio, Ableton, Logic or another. It also finds free sounds on the
web (Freesound, ccMixter, Internet Archive) and adds them to your library with a license record.

## Setup: the finished app (one minute, no terminal)

There are packages for three systems, on the [download page](https://github.com/Kflsox/open-libary/releases/latest).
Python and everything else it needs are inside, nothing has to be installed.

| Computer | Package |
|---|---|
| Mac with Apple chip (macOS 11 or later) or Intel (macOS 10.15 or later) | `Open-Libary-…-Mac.zip` |
| Windows 10 or 11, 64-bit | `Open-Libary-…-Windows-Setup.exe` |
| Linux, 64-bit: Ubuntu, Mint, Debian | `Open-Libary-…-Linux.deb` |
| Linux, 64-bit: others | `Open-Libary-…-Linux.tar.gz` |

**Mac**

🟢 Open the zip and drag "Open Libary" into your **Applications** folder, then start it.

🟢 The first time, macOS warns that it cannot verify the developer: open **System Settings, Privacy & Security** and at
the very bottom, next to Open Libary, click **Open Anyway** (on macOS 10.15 to 14: right-click the app, **Open**).
macOS then asks for access to folders: **Allow**.

**Windows**

🟢 Start the Setup. It installs Open Libary for your user (no admin rights needed), puts it in the Start menu and on
the desktop, and opens it.

🟢 The first time, Windows SmartScreen reports an unrecognized app: **More info**, then **Run anyway**.

🟢 To uninstall: **Settings, Apps**, Open Libary, **Uninstall**. Your favorites and settings stay.

Without installing, the `Windows.zip` works too: right-click it, **Extract All**, to a permanent place such as
`Documents\Open Libary`, then start **Open Libary.exe**.

🔴 Don't start the app straight from the zip without extracting it: it would be missing Python and its code.

**Linux**

🟢 Ubuntu, Mint, Debian: double-click the `.deb` and install it (or `sudo apt install ./Open-Libary-*-Linux.deb`).
Open Libary then appears in the app menu under Sound & Video (Multimedia on some systems).

🟢 Other systems: extract it (`tar xzf Open-Libary-*-Linux.tar.gz`) and start `open-libary` in the extracted folder.
If it stops with a message about the "sandbox": `./open-libary --no-sandbox`.

**On all systems**

🟢 Click **Add folder …** and choose your sample folders (drum kits, packs, your own recordings). The files stay where
they are. Open Libary then analyzes tempo, root note and sound in the background (a few minutes, your computer hardly
notices); it picks up new files at every start and once an hour.

🟢 Drag sounds from the list straight into your DAW (FL Studio, Ableton Live, Logic, Cubase, Studio One, Reaper, Bitwig,
Pro Tools). Logic and Pro Tools do not accept OGG and FLAC: Open Libary hands these over as WAV when you drag them (in
**Settings**, under dragging into the DAW; for Pro Tools choose everything as WAV).

The interface is in English. To switch to German: **Settings**, **Language**.

🔴 Don't add a whole drive or your whole user folder, only the sample folders on it.

🔴 Samples in OneDrive or iCloud that are only online (cloud icon) are skipped, otherwise Open Libary would download
everything. To include them: right-click the folder, **Always keep on this device**.

Your favorites, playlists and settings are stored on Mac in `~/Library/Application Support/Open Libary`, on Windows in
`%APPDATA%\Open Libary`, on Linux in `~/.local/share/open-libary`. To update, simply put the new app in place of the
old one (on Windows: run the new Setup).

Recommended if available: ffmpeg and rubberband (Mac with Homebrew: `brew install ffmpeg rubberband`); they make
matching to your project tempo possible. Everything else works without them.

## Setup from source (Mac, with Terminal)

🟢 Put the "Open Libary" folder in a permanent place, such as `~/Programs/Open Libary`, then in Terminal:

```
bash ~/Programs/Open\ Libary/einrichten.sh
```

If Apple's Command Line Tools are missing, macOS opens a window: **Install**, then run the command again.

## Freesound and Looperman (optional)

Both work through your normal browser and your own account. The account bar is at the top of the **Web** tab.

🟢 **Freesound:** create a free account on freesound.org, then create your own API access at freesound.org/apiv2/apply,
with `http://127.0.0.1:8765/api/freesound/rueckruf` as the Callback URL. Paste the Client ID and Client secret into
Open Libary under **Settings**, Freesound. Then click **Connect** in the Web tab: downloads come as originals.

🔴 Don't pass your Freesound access on to others: the Freesound terms forbid it, everyone creates their own.

🟢 **Looperman:** click **Connect** in the Web tab, then sign in on looperman.com in your browser with your own account.
What you download there, Open Libary takes from your Downloads folder into the library (folder "Looperman") and shows
in the "From Looperman" strip: listen and drag it straight into FL.

🔴 Don't pass Looperman loops on, sell them as a sample pack or use them for AI training (Looperman's terms).

## Good to know

- **Licenses:** samples from your own folders show the license as unclear, because the app cannot know where they come
  from. In the **Library**, the button for sorting out unclear licenses pack by pack (left column, under More) lets
  you record for each pack what applies. Web downloads carry the license of their source and a license record in the
  folder.
- **Tempo, key, type** come from the file name ("Kick", "120bpm", "F#m"); what is missing there, the light analysis
  estimates: tempo of loops and root note of 808 and bass, marked with "~" in the list. An estimated tempo can be half
  or double the real one. It does not estimate the key of loops (too unreliable). gehoer, the author's own analysis
  tool (not published yet), measures more accurately; Open Libary uses it when it is set up on the computer.
- **Something went wrong?** After a crash or a freeze Open Libary asks what happened; **Report a problem …** in
  Settings works any time. Before sending you can read everything that goes along; names of your folders and files
  stay out unless you include them. Questions and ideas: [r/Open_Libary](https://www.reddit.com/r/Open_Libary/).
- **Update (source install):** extract the new package, put it in a permanent place and run `einrichten.sh` there. It
  takes over favorites, playlists, notes, history, your folders and style from the previous folder
  (`sammlungen.json`, `verlauf.json`, `korrekturen.json`, `zusaetze.csv`, `eigene.csv`, `ordner.json`, `stil.txt`).
  Delete the old folder only after that.
- **Problems (source install):** in Terminal, `openlibary log` shows the latest messages, `openlibary stop` stops the
  server.
