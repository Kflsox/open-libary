// Open Libary report receiver: validation, issue building, request flow.
// Plain module without Workers imports, so test.mjs can load it in Node.
// worker.js is the entry point Cloudflare runs.
// SPDX-License-Identifier: AGPL-3.0-only

export const REPO = "Kflsox/open-libary-berichte";
export const MAX_KOERPER = 256 * 1024; // request body, bytes
export const MAX_TEXT = 5000; // characters
export const MAX_KONTAKT = 200; // characters
export const MAX_SYSTEM = 100; // characters
export const MAX_SNAPSHOT = 50000; // characters (at most 200 KB in UTF-8)
export const MAX_ISSUE = 65000; // GitHub rejects bodies over 65,536 characters
export const LIMIT = 5; // accepted reports per client (IPv4 address or IPv6 /64) ...
export const FENSTER_MS = 10 * 60 * 1000; // ... within this sliding window
export const TAGES_LIMIT = 200; // accepted reports per UTC day, all clients together
export const GESAMT = "gesamt"; // Durable Object name of the daily counter

const ARTEN = { crash: "Crash", freeze: "Freeze", manual: "Report" };
const TITEL_TEXT = 60; // characters of the user's text in the issue title

// ---------------------------------------------------------------------------
// Text helpers

/** Number of Unicode code points (what a user would call characters). */
export function zeichen(s) {
  let n = 0;
  for (const _ of s) n++;
  return n;
}

/** Unify line breaks, drop control characters and bidi overrides, keep tabs and newlines. */
export function normalisieren(s) {
  return s
    .replace(/\r\n?/g, "\n")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F\u202A-\u202E\u2066-\u2069]/g, "");
}

/** One line, whitespace collapsed. */
export function einzeilig(s) {
  return normalisieren(s).replace(/\s+/g, " ").trim();
}

/** A backtick fence longer than any backtick run in s (at least three). */
export function zaun(s) {
  let laengste = 0;
  for (const m of s.matchAll(/`+/g)) laengste = Math.max(laengste, m[0].length);
  return "`".repeat(Math.max(3, laengste + 1));
}

/**
 * Fenced code block. Nothing inside can close it early, and GitHub renders the
 * content literally: no @mentions, no HTML, no Markdown.
 */
export function codeBlock(s) {
  const z = zaun(s);
  return `${z}text\n${s.replace(/\n+$/, "")}\n${z}`;
}

/** Inline code span for short single-line values. */
export function inlineCode(s) {
  let laengste = 0;
  for (const m of s.matchAll(/`+/g)) laengste = Math.max(laengste, m[0].length);
  const z = "`".repeat(laengste + 1);
  const rand = s.startsWith("`") || s.endsWith("`") ? " " : "";
  return `${z}${rand}${s}${rand}${z}`;
}

/** First n code points of s, with an ellipsis when cut. */
function anfang(s, n) {
  const teile = Array.from(s);
  if (teile.length <= n) return s;
  return teile.slice(0, n).join("").trimEnd() + "…";
}

/**
 * Keep the first `behalten` UTF-16 units of s (version, system and incident
 * stacks come first) and drop the rest with a note. Cuts at a line break when
 * one is close, never inside a surrogate pair.
 */
export function endeKuerzen(s, behalten) {
  if (s.length <= behalten) return s;
  let ende = Math.max(0, behalten);
  const zeile = s.lastIndexOf("\n", ende);
  if (zeile > 0 && ende - zeile < 200) ende = zeile;
  const c = s.charCodeAt(ende - 1);
  if (c >= 0xd800 && c <= 0xdbff) ende -= 1;
  const weg = zeichen(s.slice(ende));
  return `${s.slice(0, ende)}\n\n[... ${weg} characters removed from the end to fit GitHub's size limit ...]`;
}

// ---------------------------------------------------------------------------
// Validation

function nein(fehler) {
  return { ok: false, fehler };
}

/**
 * Check a parsed request body against the contract.
 * Returns { ok: true, bericht } with cleaned fields or { ok: false, fehler }.
 */
export function berichtPruefen(daten) {
  if (daten === null || typeof daten !== "object" || Array.isArray(daten)) {
    return nein("body must be a JSON object");
  }
  const { app, version, system, art, text, kontakt, snapshot } = daten;

  if (app !== "open-libary") return nein("app must be open-libary");
  if (typeof version !== "string" || !/^\d{1,4}\.\d{1,4}(\.\d{1,4})?$/.test(version)) {
    return nein("version must look like 1.2 or 1.2.2");
  }
  if (typeof system !== "string" || !einzeilig(system)) return nein("system is required");
  if (zeichen(system) > MAX_SYSTEM) return nein(`system is longer than ${MAX_SYSTEM} characters`);
  if (typeof art !== "string" || !Object.hasOwn(ARTEN, art)) {
    return nein("art must be crash, freeze or manual");
  }
  if (typeof text !== "string" || !normalisieren(text).trim()) return nein("text is required");
  if (zeichen(text) > MAX_TEXT) return nein(`text is longer than ${MAX_TEXT} characters`);
  if (kontakt !== undefined && kontakt !== null && typeof kontakt !== "string") {
    return nein("kontakt must be a string or null");
  }
  if (typeof kontakt === "string" && zeichen(kontakt) > MAX_KONTAKT) {
    return nein(`kontakt is longer than ${MAX_KONTAKT} characters`);
  }
  if (snapshot !== undefined && snapshot !== null && typeof snapshot !== "string") {
    return nein("snapshot must be a string or null");
  }
  if (typeof snapshot === "string" && zeichen(snapshot) > MAX_SNAPSHOT) {
    return nein(`snapshot is longer than ${MAX_SNAPSHOT} characters`);
  }

  return {
    ok: true,
    bericht: {
      version,
      system: einzeilig(system),
      art,
      text: normalisieren(text).trim(),
      kontakt: typeof kontakt === "string" ? einzeilig(kontakt) : "",
      snapshot: typeof snapshot === "string" ? normalisieren(snapshot) : null,
    },
  };
}

// ---------------------------------------------------------------------------
// Issue building

/** "mac", "windows", "linux" or "other". */
export function systemFamilie(system) {
  const s = system.toLowerCase();
  if (/\b(mac|mac ?os|macintosh|os ?x|darwin)\b/.test(s)) return "mac";
  if (/\bwin(dows|32|64|nt)?\b/.test(s)) return "windows";
  if (/\b(linux|ubuntu|debian|fedora|mint|manjaro|opensuse|suse|gentoo|nixos|zorin)\b/.test(s)) {
    return "linux";
  }
  return "other";
}

export function labelsBauen(bericht) {
  return [bericht.art, systemFamilie(bericht.system)];
}

export function titelBauen(bericht) {
  const kurz = anfang(einzeilig(bericht.text), TITEL_TEXT);
  return `[${ARTEN[bericht.art]}] ${bericht.version} · ${bericht.system}: ${kurz}`;
}

function snapshotTeil(snapshot, gekuerzt) {
  const titel = gekuerzt ? "Snapshot (shortened)" : "Snapshot";
  return `\n\n<details><summary>${titel}</summary>\n\n${codeBlock(snapshot)}\n\n</details>\n`;
}

/** Markdown issue body, never longer than `grenze` characters. */
export function inhaltBauen(bericht, grenze = MAX_ISSUE) {
  const kopf = [
    "### What happened",
    "",
    codeBlock(bericht.text),
    "",
    "### Contact",
    "",
    bericht.kontakt ? codeBlock(bericht.kontakt) : "_none given_",
    "",
    `- **Version:** ${bericht.version}`,
    `- **System:** ${inlineCode(bericht.system)}`,
  ].join("\n");

  if (bericht.snapshot === null || !bericht.snapshot.trim()) {
    return `${kopf}\n\n_No snapshot attached._\n`;
  }

  let inhalt = kopf + snapshotTeil(bericht.snapshot, false);
  if (inhalt.length <= grenze) return inhalt;

  // Shrink the snapshot until the whole body fits. The fence length depends
  // on what is left, so measure the real result each round.
  let behalten = grenze - kopf.length - 300;
  for (let runde = 0; runde < 40 && behalten > 0; runde++) {
    inhalt = kopf + snapshotTeil(endeKuerzen(bericht.snapshot, behalten), true);
    if (inhalt.length <= grenze) return inhalt;
    const verfuegbar = grenze - kopf.length;
    const belegt = inhalt.length - kopf.length;
    behalten = Math.floor((behalten * verfuegbar) / belegt) - 1;
  }
  return `${kopf}\n\n_Snapshot omitted: too large for one issue._\n`;
}

// ---------------------------------------------------------------------------
// Rate limiting

/**
 * Expand an IPv6 address to eight lowercase 4-digit groups.
 * Accepts "::" compression, a trailing IPv4 part and a zone (%eth0). Returns null when invalid.
 */
export function ipv6Ausschreiben(ip) {
  let a = ip.trim().toLowerCase();
  const zone = a.indexOf("%");
  if (zone !== -1) a = a.slice(0, zone);
  const v4 = /(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(a);
  if (v4) {
    const o = v4.slice(1).map(Number);
    if (o.some((x) => x > 255)) return null;
    a = a.slice(0, v4.index) + ((o[0] << 8) | o[1]).toString(16) + ":" + ((o[2] << 8) | o[3]).toString(16);
  }
  const teile = a.split("::");
  if (teile.length > 2) return null;
  const links = teile[0] ? teile[0].split(":") : [];
  const rechts = teile.length === 2 && teile[1] ? teile[1].split(":") : [];
  let gruppen = links;
  if (teile.length === 2) {
    const fehlen = 8 - links.length - rechts.length;
    if (fehlen < 1) return null;
    gruppen = [...links, ...Array(fehlen).fill("0"), ...rechts];
  }
  if (gruppen.length !== 8 || !gruppen.every((g) => /^[0-9a-f]{1,4}$/.test(g))) return null;
  return gruppen.map((g) => g.padStart(4, "0"));
}

/**
 * Rate limit key for a client address: the IPv4 address itself, or the /64
 * prefix of an IPv6 address (one household or server usually owns a whole /64).
 * IPv4-mapped IPv6 addresses count as their IPv4 address.
 */
export function clientSchluessel(ip) {
  const roh = (ip || "").trim();
  if (!roh) return "unknown";
  if (!roh.includes(":")) return `v4:${roh}`;
  const g = ipv6Ausschreiben(roh);
  if (!g) return `roh:${roh.toLowerCase()}`;
  if (g.slice(0, 5).every((x) => x === "0000") && g[5] === "ffff") {
    const n = parseInt(g[6] + g[7], 16);
    return `v4:${[n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join(".")}`;
  }
  return `v6:${g.slice(0, 4).join(":")}/64`;
}

/**
 * Daily cap for all clients together. stand is { tag: "YYYY-MM-DD", anzahl } or null.
 * Returns { erlaubt, stand, wartenSek } where stand is what to store.
 */
export function tagPruefen(stand, jetzt, limit = TAGES_LIMIT) {
  const tag = new Date(jetzt).toISOString().slice(0, 10);
  const anzahl = stand && stand.tag === tag ? stand.anzahl : 0;
  if (anzahl >= limit) {
    const mitternacht = Date.parse(`${tag}T00:00:00Z`) + 24 * 60 * 60 * 1000;
    return { erlaubt: false, stand: { tag, anzahl }, wartenSek: Math.max(1, Math.ceil((mitternacht - jetzt) / 1000)) };
  }
  return { erlaubt: true, stand: { tag, anzahl: anzahl + 1 }, wartenSek: 0 };
}

/**
 * Sliding window over the timestamps of accepted reports.
 * Returns { erlaubt, zeiten, wartenSek } where zeiten is the list to store.
 */
export function fensterPruefen(zeiten, jetzt, limit = LIMIT, fenster = FENSTER_MS) {
  const frisch = zeiten.filter((t) => typeof t === "number" && jetzt - t < fenster);
  if (frisch.length >= limit) {
    const wartenSek = Math.max(1, Math.ceil((Math.min(...frisch) + fenster - jetzt) / 1000));
    return { erlaubt: false, zeiten: frisch, wartenSek };
  }
  return { erlaubt: true, zeiten: [...frisch, jetzt], wartenSek: 0 };
}

// ---------------------------------------------------------------------------
// HTTP

function antwort(status, daten, kopfzeilen = {}) {
  return new Response(JSON.stringify(daten), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...kopfzeilen },
  });
}

/** Read the body, stop as soon as it exceeds max bytes. Returns null when too large. */
export async function koerperLesen(request, max) {
  if (!request.body) return new Uint8Array(0);
  const leser = request.body.getReader();
  const teile = [];
  let summe = 0;
  for (;;) {
    const { done, value } = await leser.read();
    if (done) break;
    summe += value.byteLength;
    if (summe > max) {
      await leser.cancel();
      return null;
    }
    teile.push(value);
  }
  const alles = new Uint8Array(summe);
  let pos = 0;
  for (const t of teile) {
    alles.set(t, pos);
    pos += t.byteLength;
  }
  return alles;
}

/** POST the issue. Returns { ok }. Details go to the Worker log only. */
export async function issueAnlegen(bericht, env) {
  if (!env.GITHUB_TOKEN) {
    console.error("GITHUB_TOKEN is not set");
    return { ok: false };
  }
  let gh;
  try {
    gh = await fetch(`https://api.github.com/repos/${REPO}/issues`, {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "Content-Type": "application/json",
        "User-Agent": "open-libary-berichte",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({
        title: titelBauen(bericht),
        body: inhaltBauen(bericht),
        labels: labelsBauen(bericht),
      }),
      signal: AbortSignal.timeout(15000),
    });
  } catch (e) {
    console.error(`GitHub request failed: ${e && e.name}`);
    return { ok: false };
  }
  if (gh.status !== 201) {
    let meldung = "";
    try {
      meldung = String((await gh.json()).message || "").slice(0, 200);
    } catch {
      // not JSON, status is enough
    }
    console.error(`GitHub answered ${gh.status}: ${meldung}`);
    return { ok: false };
  }
  return { ok: true };
}

/** The whole request flow. worker.js passes every request here. */
export async function bearbeiten(request, env) {
  const url = new URL(request.url);
  if (url.pathname !== "/bericht") return antwort(404, { ok: false, fehler: "not found" });
  if (request.method !== "POST") {
    return antwort(405, { ok: false, fehler: "method not allowed" }, { Allow: "POST" });
  }

  const typ = (request.headers.get("content-type") || "").toLowerCase();
  if (!typ.startsWith("application/json")) {
    return antwort(400, { ok: false, fehler: "content type must be application/json" });
  }
  if (Number(request.headers.get("content-length")) > MAX_KOERPER) {
    return antwort(400, { ok: false, fehler: "body is larger than 256 KB" });
  }
  const roh = await koerperLesen(request, MAX_KOERPER);
  if (roh === null) return antwort(400, { ok: false, fehler: "body is larger than 256 KB" });

  let daten;
  try {
    daten = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(roh));
  } catch {
    return antwort(400, { ok: false, fehler: "body is not valid JSON" });
  }
  const geprueft = berichtPruefen(daten);
  if (!geprueft.ok) return antwort(400, { ok: false, fehler: geprueft.fehler });

  // Only valid reports count towards the limit.
  // Per client first, so a client that is already refused cannot use up the daily cap.
  const ns = env.ZAEHLER;
  const schluessel = clientSchluessel(request.headers.get("cf-connecting-ip"));
  let client;
  let tag = { erlaubt: true };
  try {
    client = await ns.get(ns.idFromName(schluessel)).zaehle();
    if (client.erlaubt) tag = await ns.get(ns.idFromName(GESAMT)).zaehleTag();
  } catch (e) {
    console.error(`rate limiter failed: ${e && e.message}`);
    return antwort(503, { ok: false, fehler: "temporarily unavailable, try again later" });
  }
  if (!client.erlaubt) {
    return antwort(
      429,
      { ok: false, fehler: "too many reports from this address, try again in a few minutes" },
      { "Retry-After": String(client.wartenSek) },
    );
  }
  if (!tag.erlaubt) {
    console.error("daily report limit reached");
    return antwort(
      429,
      { ok: false, fehler: "daily report limit reached, try again tomorrow" },
      { "Retry-After": String(tag.wartenSek) },
    );
  }

  const ergebnis = await issueAnlegen(geprueft.bericht, env);
  if (!ergebnis.ok) {
    return antwort(502, { ok: false, fehler: "could not file the report, try again later" });
  }
  return antwort(200, { ok: true });
}
