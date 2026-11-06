// Tests for bericht.js and worker.js without network. Run: node test.mjs (Node 22.15 or later).
// SPDX-License-Identifier: AGPL-3.0-only

import assert from "node:assert/strict";
import { registerHooks } from "node:module";

// worker.js imports "cloudflare:workers", which only exists inside workerd.
// Replace it with a minimal stand-in for these tests.
const ATTRAPPE =
  "data:text/javascript," +
  encodeURIComponent("export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }");
registerHooks({
  resolve(spezifizierer, kontext, weiter) {
    if (spezifizierer === "cloudflare:workers") return { url: ATTRAPPE, shortCircuit: true };
    return weiter(spezifizierer, kontext);
  },
});

const eintrag = await import("./worker.js");
const w = { ...(await import("./bericht.js")), Zaehler: eintrag.Zaehler };
const worker = eintrag.default;

// ---------------------------------------------------------------------------
// Tiny runner

let bestanden = 0;
let fehlgeschlagen = 0;
async function test(name, fn) {
  try {
    await fn();
    bestanden++;
    console.log(`ok   ${name}`);
  } catch (e) {
    fehlgeschlagen++;
    console.log(`FAIL ${name}\n     ${e.message.split("\n").join("\n     ")}`);
  }
}

// ---------------------------------------------------------------------------
// Helpers

function gueltig(extra = {}) {
  return {
    app: "open-libary",
    version: "1.2.2",
    system: "macOS 15.1 arm64",
    art: "crash",
    text: "The app crashed while I dragged a sample.",
    kontakt: "someone@example.com",
    snapshot: "Open Libary 1.2.2\nPython 3.12\n",
    ...extra,
  };
}

function bericht(extra = {}) {
  const r = w.berichtPruefen(gueltig(extra));
  assert.equal(r.ok, true, `expected valid report, got: ${r.fehler}`);
  return r.bericht;
}

// Marks each Markdown line as inside a fenced code block or not (CommonMark rules
// for backtick and tilde fences). Fence lines themselves count as inside.
function codeZeilen(md) {
  const zeilen = md.split("\n");
  const drin = [];
  let offen = null;
  for (const z of zeilen) {
    if (!offen) {
      const m = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(z);
      if (m && !(m[1][0] === "`" && m[2].includes("`"))) {
        offen = { c: m[1][0], n: m[1].length };
        drin.push(true);
      } else {
        drin.push(false);
      }
    } else {
      const m = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(z);
      if (m && m[1][0] === offen.c && m[1].length >= offen.n) offen = null;
      drin.push(true);
    }
  }
  return { zeilen, drin, offenAmEnde: offen !== null };
}

// Every line containing `nadel` must sit inside a code block.
function nurImCode(md, nadel) {
  const { zeilen, drin, offenAmEnde } = codeZeilen(md);
  assert.equal(offenAmEnde, false, "a code block is left open");
  let gefunden = 0;
  zeilen.forEach((z, i) => {
    if (z.includes(nadel)) {
      gefunden++;
      assert.equal(drin[i], true, `"${nadel}" escaped the code block on line ${i + 1}: ${z}`);
    }
  });
  assert.ok(gefunden > 0, `"${nadel}" not found at all`);
}

// The structural line `zeile` must appear exactly once outside code blocks
// (copies typed by the user may only exist inside them).
function ausserhalbCode(md, zeile) {
  const { zeilen, drin } = codeZeilen(md);
  const aussen = zeilen.filter((z, i) => z === zeile && !drin[i]).length;
  assert.equal(aussen, 1, `expected "${zeile}" exactly once outside code, found ${aussen}`);
}

const LOSE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;

function fakeStorage() {
  const daten = new Map();
  let alarm = null;
  return {
    async get(k) {
      return structuredClone(daten.get(k));
    },
    async put(k, v) {
      daten.set(k, structuredClone(v));
    },
    async deleteAll() {
      daten.clear();
    },
    async setAlarm(t) {
      alarm = t;
    },
    async getAlarm() {
      return alarm;
    },
    daten,
  };
}

function fakeNamespace() {
  const objekte = new Map();
  return {
    idFromName: (name) => `id:${name}`,
    get(id) {
      if (!objekte.has(id)) objekte.set(id, new w.Zaehler({ storage: fakeStorage() }, {}));
      return objekte.get(id);
    },
  };
}

function anfrage(body, { pfad = "/bericht", methode = "POST", ip = "203.0.113.7", typ = "application/json" } = {}) {
  const init = { method: methode, headers: { "cf-connecting-ip": ip } };
  if (typ) init.headers["content-type"] = typ;
  if (body !== undefined) init.body = typeof body === "string" ? body : JSON.stringify(body);
  return new Request(`https://berichte.example${pfad}`, init);
}

// Global fetch mock: records calls, answers with whatever `naechste` returns.
const aufrufe = [];
let naechste = () => new Response(JSON.stringify({ number: 1 }), { status: 201 });
globalThis.fetch = async (url, init) => {
  aufrufe.push({ url: String(url), init });
  return naechste();
};

// Keep the expected error logs out of the test output, but remember them.
const protokoll = [];
console.error = (...teile) => protokoll.push(teile.join(" "));

const TOKEN = "github_pat_TEST_ONLY_not_a_real_token";
function umgebung(extra = {}) {
  return { GITHUB_TOKEN: TOKEN, ZAEHLER: fakeNamespace(), ...extra };
}

async function json(res) {
  return JSON.parse(await res.text());
}

// ---------------------------------------------------------------------------
// Validation

await test("valid report passes and is cleaned", () => {
  const r = w.berichtPruefen(gueltig({ text: "line one\r\nline two\u0000\u202E", kontakt: "  me@example.com \n" }));
  assert.equal(r.ok, true);
  assert.equal(r.bericht.text, "line one\nline two");
  assert.equal(r.bericht.kontakt, "me@example.com");
});

await test("wrong app is rejected", () => {
  for (const app of ["openlibary", "Open-Libary", undefined, 1]) {
    const r = w.berichtPruefen(gueltig({ app }));
    assert.equal(r.ok, false);
    assert.match(r.fehler, /app/);
  }
});

await test("version format", () => {
  for (const v of ["1.2", "1.2.2", "10.0.15"]) assert.equal(w.berichtPruefen(gueltig({ version: v })).ok, true, v);
  for (const v of ["1", "1.2.3.4", "v1.2", "1.2-beta", "", 1.2, null]) {
    assert.equal(w.berichtPruefen(gueltig({ version: v })).ok, false, String(v));
  }
});

await test("art must be crash, freeze or manual", () => {
  for (const art of ["crash", "freeze", "manual"]) assert.equal(w.berichtPruefen(gueltig({ art })).ok, true);
  for (const art of ["Crash", "bug", "", "constructor", "__proto__", null]) {
    assert.equal(w.berichtPruefen(gueltig({ art })).ok, false, String(art));
  }
});

await test("text required, at most 5000 characters", () => {
  assert.equal(w.berichtPruefen(gueltig({ text: undefined })).ok, false);
  assert.equal(w.berichtPruefen(gueltig({ text: "  \n\t" })).ok, false);
  assert.equal(w.berichtPruefen(gueltig({ text: "x".repeat(5000) })).ok, true);
  assert.equal(w.berichtPruefen(gueltig({ text: "x".repeat(5001) })).ok, false);
  // counted in characters, not UTF-16 units
  assert.equal(w.berichtPruefen(gueltig({ text: "🎵".repeat(5000) })).ok, true);
});

await test("kontakt optional, at most 200 characters", () => {
  assert.equal(w.berichtPruefen(gueltig({ kontakt: null })).ok, true);
  assert.equal(w.berichtPruefen(gueltig({ kontakt: undefined })).ok, true);
  assert.equal(w.berichtPruefen(gueltig({ kontakt: "u/".padEnd(200, "x") })).ok, true);
  assert.equal(w.berichtPruefen(gueltig({ kontakt: "x".repeat(201) })).ok, false);
  assert.equal(w.berichtPruefen(gueltig({ kontakt: 42 })).ok, false);
});

await test("system required, at most 100 characters", () => {
  assert.equal(w.berichtPruefen(gueltig({ system: "" })).ok, false);
  assert.equal(w.berichtPruefen(gueltig({ system: "x".repeat(100) })).ok, true);
  assert.equal(w.berichtPruefen(gueltig({ system: "x".repeat(101) })).ok, false);
});

await test("snapshot null or string up to 50,000 characters", () => {
  assert.equal(w.berichtPruefen(gueltig({ snapshot: null })).ok, true);
  assert.equal(w.berichtPruefen(gueltig({ snapshot: "a".repeat(50000) })).ok, true);
  assert.equal(w.berichtPruefen(gueltig({ snapshot: "a".repeat(50001) })).ok, false);
  // counted in characters: 50,000 four-byte characters (200 KB) are fine
  assert.equal(w.berichtPruefen(gueltig({ snapshot: "🎵".repeat(50000) })).ok, true);
  assert.equal(w.berichtPruefen(gueltig({ snapshot: "🎵".repeat(50001) })).ok, false);
  assert.equal(w.berichtPruefen(gueltig({ snapshot: { a: 1 } })).ok, false);
});

await test("non-object bodies are rejected", () => {
  for (const d of [null, [], "text", 5]) assert.equal(w.berichtPruefen(d).ok, false);
});

// ---------------------------------------------------------------------------
// Title, labels, body

await test("title format and shortening", () => {
  assert.equal(
    w.titelBauen(bericht({ text: "App froze\nwhen I dragged" })),
    "[Crash] 1.2.2 · macOS 15.1 arm64: App froze when I dragged",
  );
  assert.match(w.titelBauen(bericht({ art: "freeze" })), /^\[Freeze\] /);
  assert.match(w.titelBauen(bericht({ art: "manual" })), /^\[Report\] /);
  const lang = w.titelBauen(bericht({ text: "word ".repeat(100) }));
  const textTeil = lang.split(": ").slice(1).join(": ");
  assert.ok(textTeil.endsWith("…"));
  assert.ok(w.zeichen(textTeil) <= 61, textTeil);
  assert.ok(!lang.includes("\n"));
});

await test("labels per OS family", () => {
  const faelle = {
    "macOS 15.1 arm64": "mac",
    "Mac OS X 10.15": "mac",
    "Darwin 24.1.0 arm64": "mac",
    "Windows 11 AMD64": "windows",
    "win32 10.0.22631": "windows",
    "Linux Mint 22.3 x86_64": "linux",
    "Ubuntu 24.04 LTS": "linux",
    "Linux 6.8 virtual machine": "linux",
    "FreeBSD 14.1": "other",
  };
  for (const [system, familie] of Object.entries(faelle)) {
    assert.deepEqual(w.labelsBauen(bericht({ system, art: "freeze" })), ["freeze", familie], system);
  }
});

await test("mentions and HTML from text and contact stay inside code blocks", () => {
  const b = bericht({
    text: "@octocat please look\n<img src=x onerror=alert(1)>\n# not a heading\n</details>",
    kontakt: "@someone <b>bold</b>",
  });
  const md = w.inhaltBauen(b);
  nurImCode(md, "@octocat");
  nurImCode(md, "<img");
  nurImCode(md, "# not a heading");
  nurImCode(md, "@someone");
  ausserhalbCode(md, "### Contact");
  ausserhalbCode(md, "</details>");
});

await test("system with mention or backticks is an inline code span", () => {
  const md = w.inhaltBauen(bericht({ system: "`@octocat` <b>" }));
  assert.ok(md.includes("- **System:** `` `@octocat` <b> ``"), md);
});

await test("fence is longer than any backtick run inside", () => {
  const b = bericht({ text: "before\n```\ninjected\n``````\n## Heading?\nafter", snapshot: "x ```` y" });
  const md = w.inhaltBauen(b);
  assert.ok(md.includes("```````text\nbefore"), "text fence should be 7 backticks");
  assert.ok(md.includes("`````text\nx ```` y"), "snapshot fence should be 5 backticks");
  nurImCode(md, "## Heading?");
  nurImCode(md, "injected");
  ausserhalbCode(md, "### Contact");
  ausserhalbCode(md, "</details>");
});

await test("snapshot is folded into details", () => {
  const md = w.inhaltBauen(bericht());
  assert.ok(md.includes("<details><summary>Snapshot</summary>\n\n```text\nOpen Libary 1.2.2"));
  assert.ok(md.includes("- **Version:** 1.2.2"));
  assert.ok(md.includes("- **System:** `macOS 15.1 arm64`"));
  assert.ok(w.inhaltBauen(bericht({ snapshot: null })).includes("_No snapshot attached._"));
  assert.ok(w.inhaltBauen(bericht({ kontakt: null })).includes("_none given_"));
});

await test("large snapshot keeps its beginning, drops the end, body stays under 65,000", () => {
  // 50,000 characters, but about 85,000 UTF-16 units because of the emoji
  const zeilen = [];
  let n = 0;
  for (let i = 0; ; i++) {
    const z = `line ${i} ${"🎵".repeat(20)}`;
    if (n + w.zeichen(z) + 1 > 50000) break;
    zeilen.push(z);
    n += w.zeichen(z) + 1;
  }
  const snap = zeilen.join("\n");
  const b = bericht({ snapshot: snap, text: "`".repeat(2000) + "\n" + "t".repeat(2999), kontakt: "`".repeat(200) });
  const md = w.inhaltBauen(b);
  assert.ok(md.length <= 65000, `length ${md.length}`);
  assert.ok(md.length > 60000, `should use the available space, got ${md.length}`);
  assert.ok(md.includes("<summary>Snapshot (shortened)</summary>"));
  assert.match(md, /\[\.\.\. \d+ characters removed from the end to fit GitHub's size limit \.\.\.\]\n`{3,}\n/);
  assert.ok(md.includes("```text\n" + zeilen[0] + "\n"), "beginning kept");
  assert.ok(!md.includes(zeilen.at(-1)), "end dropped");
  assert.ok(!LOSE_SURROGATE.test(md), "split surrogate pair");
  ausserhalbCode(md, "</details>");
});

await test("pathological snapshots still fit", () => {
  for (const snap of ["`".repeat(50000), "🎵".repeat(50000), "a`".repeat(25000)]) {
    const md = w.inhaltBauen(bericht({ snapshot: snap, text: "`".repeat(5000) }));
    assert.ok(md.length <= 65000, `length ${md.length}`);
    assert.ok(!LOSE_SURROGATE.test(md), "split surrogate pair");
    assert.equal(codeZeilen(md).offenAmEnde, false);
    ausserhalbCode(md, "### Contact");
  }
});

// ---------------------------------------------------------------------------
// Rate limiting

await test("sliding window: 5 per 10 minutes", () => {
  let zeiten = [];
  const t0 = 1_000_000;
  for (let i = 0; i < 5; i++) {
    const r = w.fensterPruefen(zeiten, t0 + i * 1000);
    assert.equal(r.erlaubt, true);
    zeiten = r.zeiten;
  }
  const sechs = w.fensterPruefen(zeiten, t0 + 5000);
  assert.equal(sechs.erlaubt, false);
  assert.equal(sechs.wartenSek, 595);
  assert.equal(w.fensterPruefen(zeiten, t0 + 10 * 60 * 1000).erlaubt, true);
});

await test("client key: IPv4 address, IPv6 /64 prefix", () => {
  const k = w.clientSchluessel;
  assert.equal(k("203.0.113.7"), "v4:203.0.113.7");
  assert.equal(k("2001:db8::1"), "v6:2001:0db8:0000:0000/64");
  assert.equal(k("2001:db8::1"), k("2001:db8:0:0:1::2"), "compressed forms in one /64");
  assert.equal(k("2001:DB8:0000:0000:ffff:ffff:ffff:ffff"), k("2001:db8::1"), "case and full form");
  assert.equal(k("2001:db8::1%eth0"), k("2001:db8::1"), "zone ignored");
  assert.notEqual(k("2001:db8:0:1::1"), k("2001:db8::1"), "next /64 is another client");
  assert.equal(k("::ffff:203.0.113.7"), "v4:203.0.113.7", "IPv4-mapped counts as IPv4");
  assert.equal(k("::ffff:cb00:7107"), "v4:203.0.113.7");
  assert.equal(k("::1"), "v6:0000:0000:0000:0000/64");
  assert.equal(k(null), "unknown");
  assert.equal(k("2001:db8:::1"), "roh:2001:db8:::1", "invalid stays its own key");
  assert.deepEqual(w.ipv6Ausschreiben("2001:db8::192.0.2.1"), ["2001", "0db8", "0000", "0000", "0000", "0000", "c000", "0201"]);
  for (const kaputt of ["1:2:3:4:5:6:7:8:9", "1::2::3", "2001:db8::g", "::ffff:1.2.3.256"]) {
    assert.equal(w.ipv6Ausschreiben(kaputt), null, kaputt);
  }
});

await test("daily cap: 200 per UTC day, resets at midnight", () => {
  const morgen = Date.parse("2026-11-05T08:00:00Z");
  let stand = null;
  for (let i = 0; i < 200; i++) {
    const r = w.tagPruefen(stand, morgen + i);
    assert.equal(r.erlaubt, true, `report ${i + 1}`);
    stand = r.stand;
  }
  const voll = w.tagPruefen(stand, morgen + 1000);
  assert.equal(voll.erlaubt, false);
  assert.equal(voll.wartenSek, 16 * 3600 - 1);
  const naechsterTag = w.tagPruefen(stand, Date.parse("2026-11-06T00:00:01Z"));
  assert.equal(naechsterTag.erlaubt, true);
  assert.deepEqual(naechsterTag.stand, { tag: "2026-11-06", anzahl: 1 });
});

await test("Durable Object counts, sets an alarm and cleans up", async () => {
  const storage = fakeStorage();
  const z = new w.Zaehler({ storage }, {});
  for (let i = 0; i < 5; i++) assert.equal((await z.zaehle()).erlaubt, true);
  const voll = await z.zaehle();
  assert.equal(voll.erlaubt, false);
  assert.ok(voll.wartenSek > 590 && voll.wartenSek <= 600);
  assert.ok((await storage.getAlarm()) > Date.now());
  await z.alarm();
  assert.equal(storage.daten.size, 0);
});

await test("Durable Object daily counter stops at 200", async () => {
  const storage = fakeStorage();
  const z = new w.Zaehler({ storage }, {});
  for (let i = 0; i < 200; i++) assert.equal((await z.zaehleTag()).erlaubt, true);
  assert.equal((await z.zaehleTag()).erlaubt, false);
  assert.equal((await storage.get("tag")).anzahl, 200);
  assert.equal(await storage.getAlarm(), null, "daily counter needs no alarm");
});

// ---------------------------------------------------------------------------
// fetch handler

await test("handler files an issue (mocked GitHub)", async () => {
  aufrufe.length = 0;
  naechste = () => new Response(JSON.stringify({ number: 7 }), { status: 201 });
  const res = await worker.fetch(anfrage(gueltig({ art: "freeze", system: "Windows 11 AMD64" })), umgebung());
  assert.equal(res.status, 200);
  assert.deepEqual(await json(res), { ok: true });
  assert.equal(aufrufe.length, 1);
  const { url, init } = aufrufe[0];
  assert.equal(url, "https://api.github.com/repos/Kflsox/open-libary-berichte/issues");
  assert.equal(init.method, "POST");
  assert.equal(init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(init.headers["User-Agent"], "open-libary-berichte");
  assert.equal(init.headers.Accept, "application/vnd.github+json");
  const issue = JSON.parse(init.body);
  assert.equal(issue.title, "[Freeze] 1.2.2 · Windows 11 AMD64: The app crashed while I dragged a sample.");
  assert.deepEqual(issue.labels, ["freeze", "windows"]);
  assert.ok(issue.body.includes("### What happened"));
});

await test("GitHub failure gives 502 without token or GitHub text", async () => {
  aufrufe.length = 0;
  naechste = () =>
    new Response(JSON.stringify({ message: "Bad credentials", documentation_url: "https://docs.github.com" }), {
      status: 401,
    });
  const res = await worker.fetch(anfrage(gueltig(), { ip: "198.51.100.1" }), umgebung());
  assert.equal(res.status, 502);
  const text = await res.text();
  assert.equal(JSON.parse(text).ok, false);
  for (const verboten of [TOKEN, "Bad credentials", "docs.github.com"]) assert.ok(!text.includes(verboten), verboten);
  assert.ok(protokoll.some((z) => z.includes("401")), "status should be logged for the owner");
  assert.ok(!protokoll.some((z) => z.includes(TOKEN)), "token must not be logged");
});

await test("network error and missing token give 502", async () => {
  aufrufe.length = 0;
  naechste = () => {
    throw new TypeError("network down");
  };
  assert.equal((await worker.fetch(anfrage(gueltig(), { ip: "198.51.100.2" }), umgebung())).status, 502);
  aufrufe.length = 0;
  const res = await worker.fetch(anfrage(gueltig(), { ip: "198.51.100.3" }), umgebung({ GITHUB_TOKEN: undefined }));
  assert.equal(res.status, 502);
  assert.equal(aufrufe.length, 0, "no request without token");
});

await test("routing: 404 and 405", async () => {
  const e = umgebung();
  assert.equal((await worker.fetch(anfrage(undefined, { pfad: "/", methode: "GET" }), e)).status, 404);
  assert.equal((await worker.fetch(anfrage(gueltig(), { pfad: "/issues" }), e)).status, 404);
  const r = await worker.fetch(anfrage(undefined, { methode: "GET" }), e);
  assert.equal(r.status, 405);
  assert.equal(r.headers.get("allow"), "POST");
});

await test("bad bodies give 400 with ok false", async () => {
  const e = umgebung();
  const faelle = [
    anfrage("{not json"),
    anfrage(""),
    anfrage(gueltig({ app: "other" })),
    anfrage(gueltig(), { typ: "text/plain" }),
    anfrage(new Uint8Array([0x7b, 0xff, 0x7d])), // invalid UTF-8
  ];
  for (const a of faelle) {
    const res = await worker.fetch(a, e);
    assert.equal(res.status, 400);
    const d = await json(res);
    assert.equal(d.ok, false);
    assert.equal(typeof d.fehler, "string");
  }
});

await test("oversize body: by Content-Length and by actual length", async () => {
  aufrufe.length = 0;
  const e = umgebung();
  // Declared size too large: rejected before reading.
  const angekuendigt = {
    url: "https://berichte.example/bericht",
    method: "POST",
    headers: new Headers({ "content-type": "application/json", "content-length": String(300 * 1024) }),
    body: null,
  };
  const r1 = await worker.fetch(angekuendigt, e);
  assert.equal(r1.status, 400);
  assert.match((await json(r1)).fehler, /256 KB/);
  // No usable Content-Length, real body too large.
  const gross = JSON.stringify(gueltig({ snapshot: "a".repeat(300 * 1024) }));
  const r2 = await worker.fetch(anfrage(gross), e);
  assert.equal(r2.status, 400);
  assert.match((await json(r2)).fehler, /256 KB/);
  assert.equal(aufrufe.length, 0);
});

await test("handler: 6th report from one IP within 10 minutes gets 429", async () => {
  naechste = () => new Response("{}", { status: 201 });
  const e = umgebung();
  // invalid requests do not count
  for (let i = 0; i < 8; i++) await worker.fetch(anfrage("{", { ip: "192.0.2.50" }), e);
  for (let i = 0; i < 5; i++) {
    assert.equal((await worker.fetch(anfrage(gueltig(), { ip: "192.0.2.50" }), e)).status, 200, `report ${i + 1}`);
  }
  const res = await worker.fetch(anfrage(gueltig(), { ip: "192.0.2.50" }), e);
  assert.equal(res.status, 429);
  assert.equal((await json(res)).ok, false);
  assert.ok(Number(res.headers.get("retry-after")) > 0);
  assert.equal((await worker.fetch(anfrage(gueltig(), { ip: "192.0.2.51" }), e)).status, 200, "other IP");
});

await test("handler: IPv6 addresses in one /64 share the limit", async () => {
  naechste = () => new Response("{}", { status: 201 });
  const e = umgebung();
  for (let i = 1; i <= 5; i++) {
    const ip = i % 2 ? `2001:db8::${i}` : `2001:db8:0:0:${i}::2`;
    assert.equal((await worker.fetch(anfrage(gueltig(), { ip }), e)).status, 200, ip);
  }
  assert.equal((await worker.fetch(anfrage(gueltig(), { ip: "2001:DB8:0:0:abcd::9" }), e)).status, 429);
  assert.equal((await worker.fetch(anfrage(gueltig(), { ip: "2001:db8:0:1::1" }), e)).status, 200, "other /64");
});

await test("handler: daily cap gives 429 and files nothing", async () => {
  aufrufe.length = 0;
  const echt = fakeNamespace();
  const gefragt = [];
  const ns = {
    idFromName: (name) => {
      gefragt.push(name);
      return `id:${name}`;
    },
    get: (id) => (id === "id:gesamt" ? { zaehleTag: async () => ({ erlaubt: false, wartenSek: 4321 }) } : echt.get(id)),
  };
  const res = await worker.fetch(anfrage(gueltig(), { ip: "198.51.100.77" }), umgebung({ ZAEHLER: ns }));
  assert.equal(res.status, 429);
  assert.equal(res.headers.get("retry-after"), "4321");
  const d = await json(res);
  assert.equal(d.ok, false);
  assert.match(d.fehler, /daily report limit reached/);
  assert.deepEqual(gefragt, ["v4:198.51.100.77", "gesamt"]);
  assert.equal(aufrufe.length, 0);
});

await test("handler: a refused client does not use up the daily cap", async () => {
  naechste = () => new Response("{}", { status: 201 });
  const e = umgebung();
  for (let i = 0; i < 9; i++) await worker.fetch(anfrage(gueltig(), { ip: "192.0.2.80" }), e);
  const stand = await e.ZAEHLER.get("id:gesamt").ctx.storage.get("tag");
  assert.equal(stand.anzahl, 5);
});

await test("rate limiter failure gives 503 and files nothing", async () => {
  aufrufe.length = 0;
  const kaputt = {
    idFromName: () => "x",
    get: () => ({
      zaehle: async () => {
        throw new Error("storage quota");
      },
    }),
  };
  const res = await worker.fetch(anfrage(gueltig()), umgebung({ ZAEHLER: kaputt }));
  assert.equal(res.status, 503);
  assert.equal(aufrufe.length, 0);
});

console.log(`\n${bestanden} passed, ${fehlgeschlagen} failed`);
process.exit(fehlgeschlagen ? 1 : 0);
