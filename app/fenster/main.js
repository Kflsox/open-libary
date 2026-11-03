// Open Libary for Windows and Linux: an Electron window around the page. It starts the bundled Python server and
// offers the same bridge as the Mac app (app/main.m): ziehen, kopieren, finder, ordner, sprache. The page sees no
// difference (preload.js creates window.webkit.messageHandlers).
// Drag into the DAW: webContents.startDrag, on Windows the system's real drag session (OLE, CF_HDROP).
const { app, BrowserWindow, ipcMain, dialog, shell, clipboard, nativeImage, Menu } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");
const { spawn } = require("child_process");

const PORT = 8765;
const ADRESSE = `http://127.0.0.1:${PORT}/`;
const WIN = process.platform === "win32";
// packaged: Python and code live under resources; during development OPENLIBARY_RES
const RES = app.isPackaged ? process.resourcesPath : (process.env.OPENLIBARY_RES || path.join(__dirname, "res"));
const PY = WIN ? path.join(RES, "python", "python.exe") : path.join(RES, "python", "bin", "python3");
const CODE = path.join(RES, "openlibary", "openlibary.py");
// same as plattform.datenordner() in the server
const DATEN = WIN ? path.join(app.getPath("appData"), "Open Libary")
  : path.join(process.env.XDG_DATA_HOME || path.join(app.getPath("home"), ".local", "share"), "open-libary");

// Interface language: English unless the user chose German (Settings > Language). Before the page loads it comes from
// sprache.txt in the data folder; the page reports its language on every load, then the menus follow.
let deutsch = false;
try { deutsch = fs.readFileSync(path.join(DATEN, "sprache.txt"), "utf8").trim() === "de"; } catch (e) {}
const L = (en, de) => (deutsch ? de : en);

let fenster = null;
let server = null;

// second start: only bring the existing window forward, no second server (it would end the first one)
const einzig = app.requestSingleInstanceLock();
if (!einzig) app.quit();
app.on("second-instance", () => {
  if (!fenster) return;
  if (fenster.isMinimized()) fenster.restore();
  fenster.focus();
});

function seite(titel, text) {
  const html = `<body style="background:#161512;color:#e9e4d8;font:14px 'Segoe UI',system-ui,sans-serif;padding:40px">`
    + `<b>${titel}</b><br>${text}</body>`;
  fenster.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
}

function laeuft(fertig) {
  const anfrage = http.get(ADRESSE + "api/stand", r => { r.resume(); fertig(r.statusCode === 200); });
  anfrage.on("error", () => fertig(false));
  anfrage.setTimeout(2000, () => anfrage.destroy());
}

function serverStarten() {
  fs.mkdirSync(DATEN, { recursive: true });
  const protokoll = fs.openSync(path.join(DATEN, "openlibary.log"), "a");
  // --ersetzen: the same version already runs -> nothing; an older version of this app -> replace it.
  // -B: no .pyc into the program folder, -E -s: no PYTHON* variables, no user packages from outside.
  // -X utf8: files, pipes and log in UTF-8 instead of cp1252 (PYTHONIOENCODING had no effect because of -E).
  server = spawn(PY, ["-B", "-E", "-s", "-X", "utf8", CODE, "--port", String(PORT), "--ohne-browser", "--ersetzen"], {
    env: { ...process.env, OPENLIBARY_DATEN: DATEN, OPENLIBARY_PAKET: "1" },
    stdio: ["ignore", protokoll, protokoll], windowsHide: true,
  });
  server.on("error", e => seite(L("The Open Libary server does not start.", "Der Open Libary-Server startet nicht."), String(e)));
}

function warten(n = 0) {
  laeuft(ok => {
    if (!fenster) return;
    if (ok) fenster.loadURL(ADRESSE);
    else if (n > 150) seite(L("The Open Libary server does not start.", "Der Open Libary-Server startet nicht."),
      L("The log names the reason: ", "Den Grund nennt das Protokoll: ") + `<code>${path.join(DATEN, "openlibary.log")}</code>`);
    else setTimeout(() => warten(n + 1), 400);
  });
}

function sicherExtern(url) {
  if (/^https?:\/\//i.test(url) && !url.startsWith(ADRESSE)) shell.openExternal(url);
}

function fensterBauen() {
  fenster = new BrowserWindow({
    width: 1400, height: 860, backgroundColor: "#161512", title: "Open Libary", autoHideMenuBar: true,
    icon: path.join(__dirname, "ziehen.png"),
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, sandbox: true,
                      autoplayPolicy: "no-user-gesture-required", spellcheck: false },
  });
  // foreign addresses never open in the window (it has the bridges): use the default browser
  fenster.webContents.setWindowOpenHandler(({ url }) => { sicherExtern(url); return { action: "deny" }; });
  fenster.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith(ADRESSE) && !url.startsWith("data:")) { e.preventDefault(); sicherExtern(url); }
  });
  fenster.on("closed", () => { fenster = null; });
  seite(L("Open Libary is starting …", "Open Libary startet …"),
    L("One moment. On the first start the Windows firewall may ask: <b>Allow</b> is not needed, Open Libary only talks "
      + "to this computer.", "Einen Moment, beim ersten Start fragt die Windows-Firewall eventuell nach: <b>Zulassen</b> "
      + "ist nicht nötig, Open Libary spricht nur mit diesem Rechner."));
  serverStarten();
  warten();
}

function menueBauen() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: "Open Libary", submenu: [{ role: "quit", label: L("Quit", "Beenden") }] },
    { label: L("Edit", "Bearbeiten"), submenu: [{ role: "cut", label: L("Cut", "Ausschneiden") },
      { role: "copy", label: L("Copy", "Kopieren") }, { role: "paste", label: L("Paste", "Einsetzen") },
      { role: "selectAll", label: L("Select All", "Alles auswählen") }] },
    { label: L("View", "Ansicht"), submenu: [{ label: L("Reload", "Neu laden"), accelerator: "CmdOrCtrl+R",
      click: () => fenster && fenster.loadURL(ADRESSE) }, { role: "togglefullscreen", label: L("Full Screen", "Vollbild") }] },
  ]));
}

// Bridge (like WKScriptMessageHandler in the Mac app)
const ziehBild = nativeImage.createFromPath(path.join(__dirname, "ziehen.png")).resize({ width: 32, height: 32 });
ipcMain.on("ziehen", (e, pfade) => {
  if (!Array.isArray(pfade)) return;
  const dateien = pfade.filter(p => typeof p === "string" && fs.existsSync(p));
  if (!dateien.length) return;
  e.sender.startDrag({ file: dateien[0], files: dateien, icon: ziehBild });
});
ipcMain.on("kopieren", (e, text) => { if (typeof text === "string") clipboard.writeText(text); });
ipcMain.on("finder", (e, pfad) => {
  if (typeof pfad !== "string" || !fs.existsSync(pfad)) return;
  if (fs.statSync(pfad).isDirectory()) shell.openPath(pfad); else shell.showItemInFolder(pfad);
});
ipcMain.on("ordner", async e => {
  const r = await dialog.showOpenDialog(fenster, {
    title: L("Choose sample folders for Open Libary", "Sample-Ordner für Open Libary wählen"),
    buttonLabel: L("Add", "Hinzufügen"),
    properties: ["openDirectory", "multiSelections"],
  });
  if (r.canceled || !r.filePaths.length) return;
  e.sender.executeJavaScript(`window.ordnerGewaehlt && window.ordnerGewaehlt(${JSON.stringify(r.filePaths)})`);
});
ipcMain.on("sprache", (e, code) => {
  const neu = code === "de";
  if (neu !== deutsch) { deutsch = neu; menueBauen(); }
});

if (einzig) app.whenReady().then(() => {
  menueBauen();
  fensterBauen();
});
app.on("window-all-closed", () => app.quit());
// the server belongs to this window: end it on quit (on Windows an invisible python.exe would stay behind)
app.on("will-quit", () => { if (server && server.exitCode === null) server.kill(); });
