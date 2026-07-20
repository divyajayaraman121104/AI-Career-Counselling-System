"""
translator.py

Reusable translation utility for the CoActions Career Guidance app.

Wraps the `deep-translator` library (Google Translate backend) to provide
a single, safe entry point - translate_text() - for converting app text
into one of the app's supported languages.

Supported languages:
    - English (Default)  -> no translation performed, original text returned
    - Tamil
    - Hindi
    - Marathi

Design notes:
    - translate_text() never raises. Any failure (missing dependency,
      network/API issue, unsupported language, empty input, etc.) falls
      back to returning the original, untranslated text so callers never
      have to wrap this in their own try/except.
    - This module only defines the translation utility - it does not
      touch any Streamlit UI/page code. Wiring it into the app is a
      separate step.

    - PERFORMANCE (caching): Streamlit reruns the entire script on every
      user interaction (every click, every keystroke on a form, etc.),
      and _t()/translate_text() is called for every piece of static UI
      text on every rerun. Without caching this means the same string
      gets re-sent to the Google Translate backend on literally every
      rerun, which is slow and wasteful - the translated result for a
      given (text, language) pair never changes.

      To fix this, the actual network call is wrapped in a cache keyed
      on (text, resolved language code, source language):
        - If Streamlit is available, `st.cache_data` is used. This cache
          is process-wide, so a string is translated at most once per
          language for the lifetime of the running app (shared across
          reruns AND across user sessions), and reruns after the first
          just hit the cache - no network call, no delay.
        - If Streamlit isn't available (e.g. this module is imported/
          tested outside a Streamlit app), an `functools.lru_cache`
          fallback provides the same "translate once, reuse after"
          behavior for a single process.

      Only the actual translation call is cached. All of the existing
      validation/fallback logic in translate_text() (empty text, English
      passthrough, missing dependency, unsupported language) still runs
      on every call as before - those are cheap, in-memory checks and
      involve no network/API activity, so there's nothing to gain by
      caching them, and keeping them outside the cache means a language
      switch, a newly-added string, etc. is always handled correctly.

      If the backend translation call fails, the failure is NOT cached
      (the cached function raises, so nothing gets stored) - so a
      transient network error on one rerun doesn't permanently "poison"
      the cache for that string; the next rerun will simply try again.
"""

import functools
import logging

try:
    from deep_translator import GoogleTranslator
    DEEP_TRANSLATOR_AVAILABLE = True
except Exception:
    DEEP_TRANSLATOR_AVAILABLE = False

try:
    import streamlit as st
    STREAMLIT_AVAILABLE = True
except Exception:
    STREAMLIT_AVAILABLE = False

translator_logger = logging.getLogger("coactions.translator")

# Default/source language used throughout the app's UI and AI-generated content.
DEFAULT_LANGUAGE = "English (Default)"

# Maps the language labels used in the app's UI to the ISO-639-1 language
# codes expected by deep-translator / Google Translate.
LANGUAGE_CODE_MAP = {
    "english": "en",
    "english (default)": "en",
    "tamil": "ta",
    "hindi": "hi",
    "marathi": "mr",
}

# Languages that are treated as "no-op" (source language) - text is
# returned as-is rather than sent to the translation backend.
ENGLISH_LABELS = {"english", "english (default)"}


def _resolve_language_code(language):
    """Normalize a UI language label (e.g. 'Tamil', 'english') into a
    deep-translator language code. Returns None if unsupported/unrecognized."""
    if not language or not isinstance(language, str):
        return None
    return LANGUAGE_CODE_MAP.get(language.strip().lower())


def _translate_uncached(text, target_code, source_language):
    """The actual call to the Google Translate backend via deep-translator.
    No caching happens here - caching is applied by the wrapper(s) below.
    Kept as a separate function so the cache decorator only ever wraps
    the real network call, not any of the validation/fallback logic."""
    translated = GoogleTranslator(source=source_language, target=target_code).translate(text)
    return translated if translated else text


if STREAMLIT_AVAILABLE:
    # Process-wide cache: a given (text, target_code, source_language)
    # combination is translated at most once for the life of the running
    # app, and every subsequent call (any rerun, any user session) is
    # served straight from cache with no network call.
    @st.cache_data(show_spinner=False, max_entries=10000)
    def _cached_translate(text, target_code, source_language):
        return _translate_uncached(text, target_code, source_language)
else:
    # Fallback for use outside a Streamlit runtime (e.g. unit tests):
    # same "translate once, reuse after" behavior, scoped to this process.
    @functools.lru_cache(maxsize=10000)
    def _cached_translate(text, target_code, source_language):
        return _translate_uncached(text, target_code, source_language)


def translate_text(text, target_language=DEFAULT_LANGUAGE, source_language="en"):
    """
    Translate `text` into `target_language`.

    Args:
        text: The source text to translate.
        target_language: One of "English (Default)" / "English", "Tamil",
            "Hindi", or "Marathi" (case-insensitive). Defaults to English.
        source_language: ISO-639-1 code of the source text. Defaults to
            "en", since all app content is authored in English.

    Returns:
        The translated text as a string. Falls back to the original
        `text` unchanged whenever:
            - `text` is empty/None,
            - `target_language` is English (or unspecified),
            - deep-translator is not installed,
            - `target_language` isn't one of the supported languages,
            - the translation call raises for any reason (network error,
              API/service error, rate limiting, etc.).
        This function is designed to never raise or crash the caller.

        Repeated calls with the same (text, target_language) combination
        are served from cache after the first call - see the module-level
        docstring for details.
    """
    # Nothing to translate.
    if text is None or text == "":
        return text

    # English is the source language - return the text unchanged.
    if not target_language or target_language.strip().lower() in ENGLISH_LABELS:
        return text

    if not DEEP_TRANSLATOR_AVAILABLE:
        translator_logger.warning(
            "deep-translator is not installed; returning original text."
        )
        return text

    target_code = _resolve_language_code(target_language)
    if not target_code:
        translator_logger.warning(
            f"Unsupported target language '{target_language}'; returning original text."
        )
        return text

    try:
        return _cached_translate(text, target_code, source_language)
    except Exception as exc:
        # Not cached - a transient failure here just means the next call
        # (e.g. next rerun) will try the network call again from scratch.
        translator_logger.warning(
            f"Translation to '{target_language}' failed: {exc}. Returning original text."
        )
        return text