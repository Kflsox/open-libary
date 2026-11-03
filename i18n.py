"""Interface language.

English is the source language: every text in the code is written in English. Other languages come from
lang/<code>.json, which maps each English text to its translation. The same file serves the page (seite.html gets it
injected) and the server (tx() below), so there is one place to translate.

Placeholders use {name}: tx("{n} files", n=3). A translation keeps the same placeholders in any order.
"""
import json
from pathlib import Path

LANGUAGES = {"en": "English", "de": "Deutsch"}   # code -> name in its own language
DEFAULT = "en"

_LANG_DIR = Path(__file__).resolve().parent / "lang"
_current = DEFAULT
_table = {}


def table(code):
    """Translation table for a language code ({} for English or when the file is missing or broken)."""
    if code == DEFAULT or code not in LANGUAGES:
        return {}
    try:
        data = json.loads((_LANG_DIR / f"{code}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v}


def normal(code):
    """Known language code or the default."""
    code = str(code or "").strip().lower()
    return code if code in LANGUAGES else DEFAULT


def set_language(code):
    """Switch the server's language. -> the code that is now active."""
    global _current, _table
    _current = normal(code)
    _table = table(_current)
    return _current


def current():
    return _current


def N_(text):
    """Marks a text for translation without translating it yet. For tables built at import time: the language can
    change while the server runs, so they keep the English text and the place that uses it calls tx(text)."""
    return text


def tx(text, **values):
    """Text in the current language; values fill its {placeholders}."""
    s = _table.get(text, text)
    if values:
        try:
            s = s.format(**values)
        except (KeyError, IndexError, ValueError):   # broken translation: fall back to the English text
            s = text.format(**values)
    return s
