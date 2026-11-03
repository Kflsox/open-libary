"""Checks the translation tables in lang/ against the code (no server needed).

    python tests/check_translations.py

For every lang/<code>.json:
  - a JSON object of English text -> translation, no empty values
  - same {placeholders} and the same HTML tags on both sides
  - every literal tx("...") / N_("...") in seite.html and the Python modules has an entry
  - every text of the static markup in seite.html (text between tags, title, placeholder, aria-label, alt) has an
    entry, unless it reads the same in every language (names, units, symbols; NEUTRAL below)
Entries that nothing uses any more are listed as a note, not as an error (some keys come from data at run time).
Exit 1 on errors.
"""
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

HIER = Path(__file__).resolve().parent.parent
PH = re.compile(r"\{(\w+)\}")
TAG = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)")
CALL = re.compile(r"""\b(?:tx|N_)\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\$]|\\.)*)`)\s*[,)]""")
# static texts that are the same in every language
NEUTRAL = {"Open Libary", "BPM", "Kick", "Snare", "Clap", "Hat", "Perc", "Tom", "Rim", "808", "Loop", "One-Shot",
           "Loops", "One-Shots", "Percs", "Drums", "Genre", "Groove", "Vibe", "Pack", "Tempo", "Halftime", "Varispeed",
           "Original", "Pause", "Details", "Filter", "Name", "Freesound", "Downloads", "Splice", "Looperman", "ccMixter",
           "Internet Archive", "Freesound, Wikimedia Commons, ccMixter", "Liquid Glass", "?", "C", "F", "Fm",
           "Fm, F#min, am",
           "English", "Deutsch"}      # language names stay in their own language


def literal(g, i):
    if i == 1:
        g = g.replace("\\'", "'").replace('"', '\\"')
    elif i == 2:
        g = g.replace("\\`", "`").replace('"', '\\"').replace("\n", "\\n")
    try:
        return json.loads('"' + g + '"')
    except ValueError:
        return g


class Statisch(HTMLParser):
    """Text nodes and translatable attributes outside script and style, normalized like translatePage()."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tiefe_code = 0
        self.texte = set()
        self.html_saetze = []        # [depth, parts] for elements with data-tx-html
        self.roh = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.tiefe_code += 1
        for a, v in attrs:
            if a in ("title", "placeholder", "aria-label", "alt") and v:
                self.neu(v)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.tiefe_code -= 1

    def handle_data(self, data):
        if not self.tiefe_code:
            self.neu(data)

    def neu(self, s):
        s = re.sub(r"\s+", " ", s).strip()
        if s and re.search(r"[A-Za-zÄÖÜäöüß]", s) and "__" not in s:
            self.texte.add(s)


def html_saetze(text):
    """innerHTML of elements marked data-tx-html, normalized (they are translated as a whole)."""
    out = []
    for m in re.finditer(r"<(\w+)([^>]*\bdata-tx-html\b[^>]*)>(.*?)</\1>", text, re.S):
        out.append(re.sub(r"\s+", " ", m.group(3)).strip())
    return out


def main():
    seite = (HIER / "seite.html").read_text(encoding="utf-8")
    quellen = {"seite.html": seite}
    for p in sorted(HIER.glob("*.py")):
        quellen[p.name] = p.read_text(encoding="utf-8")
    schluessel = set()
    for text in quellen.values():
        for m in CALL.finditer(text):
            i = next(j for j, g in enumerate(m.groups()) if g is not None)
            schluessel.add(literal(m.group(i + 1), i))
    statisch = Statisch()
    statisch.feed(seite)
    saetze = html_saetze(seite)
    # text nodes inside a data-tx-html element are covered by the whole sentence
    innen = set()
    for s in saetze:
        p = Statisch()
        p.feed(s)
        innen |= p.texte
    noetig = (schluessel | (statisch.texte - innen) | set(saetze)) - NEUTRAL
    fehler = 0
    tabellen = sorted((HIER / "lang").glob("*.json"))
    if not tabellen:
        print("no lang/*.json")
        return 1
    for datei in tabellen:
        try:
            d = json.loads(datei.read_text(encoding="utf-8"))
        except ValueError as e:
            print(f"{datei.name}: not valid JSON ({e})")
            fehler += 1
            continue
        for k, v in d.items():
            if not isinstance(v, str) or not v.strip():
                print(f"{datei.name}: empty value for {k!r}")
                fehler += 1
            elif sorted(PH.findall(k)) != sorted(PH.findall(v)):
                print(f"{datei.name}: placeholders differ: {k!r} -> {v!r}")
                fehler += 1
            elif sorted(TAG.findall(k)) != sorted(TAG.findall(v)):
                print(f"{datei.name}: HTML tags differ: {k!r} -> {v!r}")
                fehler += 1
        fehlt = sorted(k for k in noetig if k not in d)
        for k in fehlt:
            print(f"{datei.name}: missing {k!r}")
        fehler += len(fehlt)
        unbenutzt = [k for k in d if k not in noetig]
        print(f"{datei.name}: {len(d)} entries, {len(fehlt)} missing, {len(unbenutzt)} not found in the code (may come "
              f"from data at run time)")
    print("OK" if not fehler else f"{fehler} error(s)")
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(main())
