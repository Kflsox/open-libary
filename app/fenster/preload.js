// Bridge for the page: the same shape as in the Mac app (WKWebView), so seite.html needs no special case:
// window.webkit.messageHandlers.<name>.postMessage(value) goes to the main process (main.js).
const { contextBridge, ipcRenderer } = require("electron");
const sende = name => ({ postMessage: wert => ipcRenderer.send(name, wert) });
contextBridge.exposeInMainWorld("webkit", {
  messageHandlers: { ziehen: sende("ziehen"), kopieren: sende("kopieren"), finder: sende("finder"), ordner: sende("ordner"),
                     sprache: sende("sprache") },
});
