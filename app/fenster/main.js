// Open Libary für Windows und Linux (03.11.2026, „max kompi“): Electron-Fenster um die Seite, startet den
// eingepackten Python-Server und bietet dieselbe Brücke wie die Mac-App (app/main.m): ziehen, kopieren, finder,
// ordner. Die Seite merkt keinen Unterschied (preload.js legt window.webkit.messageHandlers an).
// Ziehen in die DAW: webContents.startDrag, auf Windows die echte Ziehsitzung des Systems (OLE, CF_HDROP).
const { app, BrowserWindow, ipcMain, dialog, shell, clipboard, nativeImage, Menu } = require("electron");
const path = require("path");
const fs = require("fs");
const http = require("http");
const { spawn } = require("child_process");

const PORT = 8765;
const ADRESSE = `http://127.0.0.1:${PORT}/`;
const WIN = process.platform === "win32";
// eingepackt: Python und Code liegen unter resources; beim Entwickeln OPENLIBARY_RES
const RES = app.isPackaged ? process.resourcesPath : (process.env.OPENLIBARY_RES || path.join(__dirname, "res"));
const PY = WIN ? path.join(RES, "python", "python.exe") : path.join(RES, "python", "bin", "python3");
const CODE = path.join(RES, "openlibary", "openlibary.py");
// wie plattform.datenordner() im Server
const DATEN = WIN ? path.join(app.getPath("appData"), "Open Libary")
  : path.join(process.env.XDG_DATA_HOME || path.join(app.getPath("home"), ".local", "share"), "open-libary");

let fenster = null;
let server = null;

// zweiter Start: nur das vorhandene Fenster nach vorn, kein zweiter Server (der beendete sonst den ersten)
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
  // --ersetzen: derselbe Stand läuft schon -> nichts; ein älterer Stand dieser App -> ersetzen.
  // -B: keine .pyc in den Programmordner schreiben, -E -s: keine PYTHON*-Variablen, keine Nutzer-Pakete von außen.
  // -X utf8: Dateien, Pipes und Protokoll in UTF-8 statt cp1252 (PYTHONIOENCODING wirkte wegen -E nicht).
  server = spawn(PY, ["-B", "-E", "-s", "-X", "utf8", CODE, "--port", String(PORT), "--ohne-browser", "--ersetzen"], {
    env: { ...process.env, OPENLIBARY_DATEN: DATEN, OPENLIBARY_PAKET: "1" },
    stdio: ["ignore", protokoll, protokoll], windowsHide: true,
  });
  server.on("error", e => seite("Der Open Libary-Server startet nicht.", String(e)));
}

function warten(n = 0) {
  laeuft(ok => {
    if (!fenster) return;
    if (ok) fenster.loadURL(ADRESSE);
    else if (n > 150) seite("Der Open Libary-Server startet nicht.",
      `Den Grund nennt das Protokoll: <code>${path.join(DATEN, "openlibary.log")}</code>`);
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
  // fremde Adressen nie im Fenster (es hat die Brücken): in den Standardbrowser
  fenster.webContents.setWindowOpenHandler(({ url }) => { sicherExtern(url); return { action: "deny" }; });
  fenster.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith(ADRESSE) && !url.startsWith("data:")) { e.preventDefault(); sicherExtern(url); }
  });
  fenster.on("closed", () => { fenster = null; });
  seite("Open Libary startet …", "Einen Moment, beim ersten Start fragt die Windows-Firewall eventuell nach: <b>Zulassen</b> "
    + "ist nicht nötig, Open Libary spricht nur mit diesem Rechner.");
  serverStarten();
  warten();
}

// Brücke (wie WKScriptMessageHandler in der Mac-App)
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
    title: "Sample-Ordner für Open Libary wählen", buttonLabel: "Hinzufügen",
    properties: ["openDirectory", "multiSelections"],
  });
  if (r.canceled || !r.filePaths.length) return;
  e.sender.executeJavaScript(`window.ordnerGewaehlt && window.ordnerGewaehlt(${JSON.stringify(r.filePaths)})`);
});

if (einzig) app.whenReady().then(() => {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: "Open Libary", submenu: [{ role: "quit", label: "Beenden" }] },
    { label: "Bearbeiten", submenu: [{ role: "cut", label: "Ausschneiden" }, { role: "copy", label: "Kopieren" },
      { role: "paste", label: "Einsetzen" }, { role: "selectAll", label: "Alles auswählen" }] },
    { label: "Ansicht", submenu: [{ label: "Neu laden", accelerator: "CmdOrCtrl+R", click: () => fenster && fenster.loadURL(ADRESSE) },
      { role: "togglefullscreen", label: "Vollbild" }] },
  ]));
  fensterBauen();
});
app.on("window-all-closed", () => app.quit());
// der Server gehört zu diesem Fenster: beim Beenden mit beenden (auf Windows bliebe sonst ein unsichtbares python.exe)
app.on("will-quit", () => { if (server && server.exitCode === null) server.kill(); });
