// Brücke für die Seite (03.11.2026): dieselbe Form wie in der Mac-App (WKWebView), damit seite.html nichts
// unterscheiden muss: window.webkit.messageHandlers.<name>.postMessage(wert) geht an den Hauptprozess (main.js).
const { contextBridge, ipcRenderer } = require("electron");
const sende = name => ({ postMessage: wert => ipcRenderer.send(name, wert) });
contextBridge.exposeInMainWorld("webkit", {
  messageHandlers: { ziehen: sende("ziehen"), kopieren: sende("kopieren"), finder: sende("finder"), ordner: sende("ordner") },
});
