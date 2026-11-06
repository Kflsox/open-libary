// Open Libary report receiver (Cloudflare Worker entry point).
// Accepts bug reports from the app's local server and files each one as an
// issue in the private repo Kflsox/open-libary-berichte. The logic lives in
// bericht.js; workerd treats every named export here as an entrypoint, so
// this file exports only the handler and the Durable Object class.
// SPDX-License-Identifier: AGPL-3.0-only

import { DurableObject } from "cloudflare:workers";
import { bearbeiten, fensterPruefen, tagPruefen, FENSTER_MS } from "./bericht.js";

/**
 * Rate limit counters. One instance per client (IPv4 address or IPv6 /64,
 * addressed by idFromName) uses zaehle(): it stores only the timestamps of
 * accepted reports and deletes itself 10 minutes after the last one.
 * The single instance named "gesamt" uses zaehleTag() for the daily cap.
 */
export class Zaehler extends DurableObject {
  async zaehle() {
    const jetzt = Date.now();
    const zeiten = (await this.ctx.storage.get("zeiten")) || [];
    const ergebnis = fensterPruefen(zeiten, jetzt);
    if (ergebnis.erlaubt) {
      await this.ctx.storage.put("zeiten", ergebnis.zeiten);
      await this.ctx.storage.setAlarm(jetzt + FENSTER_MS);
    }
    return { erlaubt: ergebnis.erlaubt, wartenSek: ergebnis.wartenSek };
  }

  async zaehleTag() {
    const ergebnis = tagPruefen((await this.ctx.storage.get("tag")) || null, Date.now());
    if (ergebnis.erlaubt) await this.ctx.storage.put("tag", ergebnis.stand);
    return { erlaubt: ergebnis.erlaubt, wartenSek: ergebnis.wartenSek };
  }

  async alarm() {
    await this.ctx.storage.deleteAll();
  }
}

export default {
  fetch(request, env) {
    return bearbeiten(request, env);
  },
};
