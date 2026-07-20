"""
language_manager.py

Centralized language manager for CoActions.

This module is the single source of truth for which language is currently
selected in the app. Both the static UI translator (translate_text) and the
Gemini AI prompt-language logic can read from here instead of each keeping
their own notion of "the current language".

This file intentionally does NOT change any existing UI code or AI
functions in app.py - it just provides a small, reusable API that app.py
(or any other module) can adopt at its own pace.

Usage:
    from language_manager import get_current_language, set_current_language, SUPPORTED_LANGUAGES

    # Read the currently selected language
    lang = get_current_language()

    # Update the currently selected language (e.g. from a dropdown's on_change)
    set_current_language("Tamil")

Key-based UI translations (t()):
    from language_manager import t

    # Dot-separated key into translations/<lang>.json
    label = t("buttons.next_page")
"""

import json
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Supported languages
# ---------------------------------------------------------------------------
# English is treated as the default/base language.
LANGUAGE_ENGLISH = "English"
LANGUAGE_TAMIL = "Tamil"
LANGUAGE_HINDI = "Hindi"
LANGUAGE_MARATHI = "Marathi"

SUPPORTED_LANGUAGES = [
    LANGUAGE_ENGLISH,
    LANGUAGE_TAMIL,
    LANGUAGE_HINDI,
    LANGUAGE_MARATHI,
]

DEFAULT_LANGUAGE = LANGUAGE_ENGLISH

# Key used to store the selected language in st.session_state.
_SESSION_STATE_KEY = "current_language"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _ensure_initialized():
    """Make sure the session-state key exists before it's read/written.

    Safe to call as often as needed - it only sets the default the first
    time it's called for a given session.
    """
    if _SESSION_STATE_KEY not in st.session_state:
        st.session_state[_SESSION_STATE_KEY] = DEFAULT_LANGUAGE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_current_language():
    """Return the currently selected language.

    Falls back to DEFAULT_LANGUAGE ("English") if nothing has been set yet
    in st.session_state for this session.
    """
    _ensure_initialized()
    return st.session_state[_SESSION_STATE_KEY]


def set_current_language(language):
    """Set the currently selected language.

    Args:
        language: One of the values in SUPPORTED_LANGUAGES
                  ("English", "Tamil", "Hindi", "Marathi").

    Raises:
        ValueError: if `language` is not one of SUPPORTED_LANGUAGES.
    """
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"Unsupported language: {language!r}. "
            f"Supported languages are: {SUPPORTED_LANGUAGES}"
        )
    st.session_state[_SESSION_STATE_KEY] = language


def get_supported_languages():
    """Return the list of supported languages (copy-safe)."""
    return list(SUPPORTED_LANGUAGES)


# ---------------------------------------------------------------------------
# Key-based UI translations - t() / load_language() / set_language()
# ---------------------------------------------------------------------------
# app.py's own MODIFICATION comments describe this as: "t() is the new
# key-based translation helper (translations/<lang>.json via
# language_manager.py)", used for the common, mostly-static UI chrome
# (nav, home, about, help, footer/contact, buttons, labels, and
# success/warning/error messages) via dot-separated keys such as
# "buttons.next_page" or "messages.error.file_not_found". These three
# functions were missing from this file (only get_current_language /
# set_current_language / get_supported_languages were present), which is
# why `from language_manager import t` failed. Restored below, without
# altering any of the code above.

TRANSLATIONS_DIR = Path(__file__).parent / "translations"

# Maps the language labels used around the app to the translations/*.json
# filename code. Matches the same case-insensitive style as app.py's own
# ASSESSMENT_LANGUAGE_SUFFIX mapping, so "English (Default)" (the label
# app.py's language selector uses) resolves the same way "English" does.
_LANGUAGE_FILE_CODES = {
    "english (default)": "en",
    "english": "en",
    "tamil": "ta",
    "hindi": "hi",
    "marathi": "mr",
}

# In-memory cache of loaded translations JSON, keyed by file code, so each
# language file is only ever read from disk once per running process.
_translations_cache = {}


def _language_to_code(language):
    """Map a language label (however it's cased/labelled) to its
    translations/<code>.json file code. Falls back to 'en' for anything
    unrecognized."""
    return _LANGUAGE_FILE_CODES.get((language or "").strip().lower(), "en")


def _resolve_language():
    """Determine which language t() should translate into.

    Prefers st.session_state.language - the key the app's own language
    selector writes to (same one translate_text()/_t() read from) - so
    key-based and static-string translation stay in sync. Falls back to
    this module's own get_current_language() if that key isn't set.
    """
    lang = st.session_state.get("language")
    if lang:
        return lang
    return get_current_language()


def load_language(language=None):
    """Load and return the translations dict for `language` (or the
    currently selected language if not given) from
    translations/<code>.json. Falls back to translations/en.json if the
    requested language file doesn't exist, and to an empty dict if even
    that is missing - so a missing/incomplete translations file degrades
    gracefully instead of crashing the app. Cached per file code for the
    life of the running process.
    """
    if language is None:
        language = _resolve_language()
    code = _language_to_code(language)

    if code in _translations_cache:
        return _translations_cache[code]

    path = TRANSLATIONS_DIR / f"{code}.json"
    if not path.exists():
        path = TRANSLATIONS_DIR / "en.json"

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    _translations_cache[code] = data
    return data


def set_language(language):
    """Set the currently selected language for the key-based t() API.

    Unlike set_current_language(), this does not require `language` to be
    one of SUPPORTED_LANGUAGES - it accepts whatever label the app's
    language selector uses (e.g. "English (Default)"), storing it directly
    so t() and _resolve_language() can pick it up immediately.
    """
    st.session_state[_SESSION_STATE_KEY] = language
    st.session_state["language"] = language


def _lookup(data, dotted_key):
    """Walk a dot-separated key (e.g. 'buttons.next_page') through a
    nested translations dict. Returns None if any part of the path is
    missing or the final value isn't a string."""
    node = data
    for part in dotted_key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node if isinstance(node, str) else None


def t(key, default=None):
    """Translate a dot-separated UI key (e.g. "nav.home",
    "buttons.previous_page", "messages.error.file_not_found_full") using
    translations/<lang>.json for the currently selected language.

    Falls back to the English translations file if the key is missing in
    the selected language, then to `default`, then to the key itself -
    so a missing key never breaks the UI, it just shows the raw key.
    """
    value = _lookup(load_language(), key)
    if value is not None:
        return value

    if _resolve_language() != DEFAULT_LANGUAGE:
        value = _lookup(load_language(DEFAULT_LANGUAGE), key)
        if value is not None:
            return value

    return default if default is not None else key