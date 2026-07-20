import streamlit as st
import json
import difflib
from pathlib import Path
from datetime import datetime
import base64
from fpdf import FPDF
import tempfile
import os
import time      # MODIFICATION (503 retry): used for exponential backoff sleeps
import logging   # MODIFICATION (503 retry): used to log each retry attempt
from google import genai
from google.genai import types
from google.genai import errors as genai_errors  # MODIFICATION (503 retry): needed to detect ServerError/503 specifically

# MODIFICATION (i18n): translation utility for static UI text (nav, sidebar,
# buttons, labels, validation/error/success/warning messages, footer, etc).
# AI-generated content (career reports, chatbot replies, role breakdowns,
# roadmaps, etc.) is intentionally left untouched - only hardcoded UI text
# authored in this file is passed through translate_text().
from translator import translate_text

# MODIFICATION (i18n / static JSON translations): t() is the new key-based
# translation helper (translations/<lang>.json via language_manager.py).
# It is being rolled out for the common, mostly-static UI chrome (nav,
# home, about, help, footer/contact, buttons, labels, and
# success/warning/error messages). It is intentionally independent from
# _t()/translate_text() above - that runtime translator still covers every
# other page (assessment content, AI-generated reports, roadmaps, etc.)
# and is untouched.
from language_manager import t

# MODIFICATION (Vocational Careers data): structured data + pure filter
# helpers extracted from Vocational_Careers.pdf, kept in a separate module
# (vocational_careers_data.py) so the source data can be maintained
# independently of this file. Nothing here loads/parses the PDF itself at
# runtime - it's plain Python constants + functions, imported once at
# startup like any other module.
import vocational_careers_data as vocational_data


def _t(text):
    """Shorthand: translate a static UI string into the user's currently
    selected UI language (st.session_state.language). Safe to call before
    st.session_state.language is initialized - falls back to English.

    MODIFICATION (translation performance):
    - Page-level scoping: only the show_*() function for the CURRENT
      st.session_state.page ever runs (see the if/elif page dispatcher
      near the bottom of this file), so hidden pages' strings are never
      passed to _t() in the first place - nothing is preloaded/translated
      for pages the user hasn't navigated to.
    - Caching: translate_text() (translator.py) wraps the actual Google
      Translate network call in a process-wide st.cache_data cache keyed
      on the normalized (text, target_code, source_language). That means
      a given UI string is sent to the translation backend at most ONCE
      per language for the life of the running app - not once per
      Streamlit rerun (which happens on every button click / widget
      interaction) and not once per user session. Every later call for
      that same string+language, from any page render or any user, is
      served from memory. _t() intentionally does not add a second cache
      of its own on top of this - translate_text()'s cache already
      normalizes language labels (e.g. "Tamil" vs "tamil" vs
      "English (Default)" vs "English") to the right cache key, so a
      second, cruder cache here would only duplicate memory without
      avoiding any additional network calls.
    """
    language = st.session_state.get("language", "English (Default)")
    return translate_text(text, language)


# MODIFICATION (Gemini multilingual prompts): Gemini is now asked to
# generate every AI response directly in the student's selected language,
# instead of generating in English and translating the output afterwards.
# The selected language always comes from st.session_state.selected_language.
def _get_selected_language():
    """Return the language currently selected for Gemini AI responses.
    Driven by the same dropdown that controls static UI translation
    (st.session_state.language), so switching the language once switches
    both the static UI and every Gemini-generated response. Safe to call
    before st.session_state.language is initialized - falls back to
    English."""
    return st.session_state.get("language", "English (Default)") or "English (Default)"


def _gemini_language_instruction():
    """
    Build the language directive prepended to every Gemini prompt so the
    AI generates its entire response directly in the selected language.
    Only the language instruction changes - prompt content, schema, and
    output format requested elsewhere in each prompt stay exactly the same.
    """
    language = _get_selected_language().strip()
    if language.lower().startswith("english"):
        language = "English"
    return f"Generate the response only in {language}."


# MODIFICATION (translated assessment JSON): school/college/RIASEC
# question banks each ship one JSON file per language
# (e.g. school_questions.json / school_questions.ta.json / .hi.json /
# .mr.json). This maps the same st.session_state.language value used
# above to the filename suffix for those translated files. "" means the
# untranslated/default English file (school_questions.json,
# college_questions.json), which use no suffix at all - RIASEC is the
# exception and always has an explicit language code, including English
# ("riasec.en.json"), handled separately in load_riasec_questions().
ASSESSMENT_LANGUAGE_SUFFIX = {
    "english (default)": "",
    "english": "",
    "tamil": "ta",
    "hindi": "hi",
    "marathi": "mr",
}


def _get_assessment_language_code():
    """Return the filename suffix ('', 'ta', 'hi', 'mr') for the
    assessment JSON files matching the currently selected UI language
    (st.session_state.language). Unrecognised/unset languages fall back
    to '' (English)."""
    language = _get_selected_language().strip().lower()
    return ASSESSMENT_LANGUAGE_SUFFIX.get(language, "")


# MODIFICATION (i18n): AI-generated content (career reports, chatbot
# replies, role breakdowns, roadmaps, etc.) is no longer passed through any
# translation step after Gemini generates it - Gemini output is rendered
# and used as-is. Only static, hardcoded UI text authored in this file goes
# through translate_text() / _t() above.

# MODIFICATION (PDF charts): matplotlib is used to render the PDF report's
# charts (career-match bar chart, skills pie chart, readiness/skill-gap
# progress bars, roadmap timeline) to PNG images that get embedded into the
# FPDF reports. Guarded so a missing/broken matplotlib install never breaks
# PDF generation - chart sections are simply skipped (see MATPLOTLIB_AVAILABLE
# checks in the chart helper functions above generate_pdf_report()).
try:
    import matplotlib
    matplotlib.use("Agg")  # headless backend - no GUI/display needed on a server
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except Exception:
    MATPLOTLIB_AVAILABLE = False


# MODIFICATION (503 retry): dedicated logger for Gemini reliability events
# (retries, backoff waits, model fallback). Uses the standard `logging`
# module rather than `print()` so retry/backoff behaviour shows up in
# proper log output (with levels + timestamps) alongside the rest of the
# app's [Gemini DEBUG] print-based tracing already in this file.
logging.basicConfig(level=logging.INFO)
gemini_logger = logging.getLogger("coactions.gemini")

# MODIFICATION (503 retry): tunable retry/backoff/fallback constants.
# Kept at module level (not hardcoded inline) so they're easy to find and
# adjust without hunting through _GeminiModel.
GEMINI_MAX_RETRIES = 5                       # retry up to 5 times (6 attempts total) per model
GEMINI_BACKOFF_SECONDS = [1, 2, 4, 8, 16]    # exponential backoff schedule, one entry per retry
GEMINI_FALLBACK_MODEL_NAME = "gemini-2.5-flash"  # faster model to fall back to on persistent 503s


# MODIFICATION (dynamic State -> City location selection): single source of
# truth for every Indian State and Union Territory, each mapped to its top
# 3 major cities. This is DATA, not UI - the Welcome page's State/City
# selectboxes below are built by looping over this dict (no per-state
# if/elif branches, nothing hardcoded into the widgets themselves). Adding,
# removing, or re-ordering a state/city only ever requires editing this
# dict; the UI updates automatically. State/city values are intentionally
# left untranslated (same as the Grade dropdown) so downstream logic
# (AI prompts, PDF report, etc.) always sees consistent English location
# strings regardless of the selected UI language.
INDIA_STATE_CITIES = {
    "Andhra Pradesh": ["Visakhapatnam", "Vijayawada", "Guntur"],
    "Arunachal Pradesh": ["Itanagar", "Naharlagun", "Pasighat"],
    "Assam": ["Guwahati", "Dibrugarh", "Silchar"],
    "Bihar": ["Patna", "Gaya", "Bhagalpur"],
    "Chhattisgarh": ["Raipur", "Bhilai", "Bilaspur"],
    "Goa": ["Panaji", "Margao", "Vasco da Gama"],
    "Gujarat": ["Ahmedabad", "Surat", "Vadodara"],
    "Haryana": ["Gurugram", "Faridabad", "Panipat"],
    "Himachal Pradesh": ["Shimla", "Dharamshala", "Solan"],
    "Jharkhand": ["Ranchi", "Jamshedpur", "Dhanbad"],
    "Karnataka": ["Bengaluru", "Mysuru", "Mangaluru"],
    "Kerala": ["Kochi", "Thiruvananthapuram", "Kozhikode"],
    "Madhya Pradesh": ["Bhopal", "Indore", "Jabalpur"],
    "Maharashtra": ["Mumbai", "Pune", "Nagpur"],
    "Manipur": ["Imphal", "Thoubal", "Bishnupur"],
    "Meghalaya": ["Shillong", "Tura", "Jowai"],
    "Mizoram": ["Aizawl", "Lunglei", "Champhai"],
    "Nagaland": ["Kohima", "Dimapur", "Mokokchung"],
    "Odisha": ["Bhubaneswar", "Cuttack", "Rourkela"],
    "Punjab": ["Ludhiana", "Amritsar", "Jalandhar"],
    "Rajasthan": ["Jaipur", "Jodhpur", "Udaipur"],
    "Sikkim": ["Gangtok", "Namchi", "Gyalshing"],
    "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai"],
    "Telangana": ["Hyderabad", "Warangal", "Nizamabad"],
    "Tripura": ["Agartala", "Udaipur", "Dharmanagar"],
    "Uttar Pradesh": ["Lucknow", "Kanpur", "Noida"],
    "Uttarakhand": ["Dehradun", "Haridwar", "Nainital"],
    "West Bengal": ["Kolkata", "Howrah", "Siliguri"],
    "Andaman and Nicobar Islands": ["Port Blair", "Diglipur", "Rangat"],
    "Chandigarh": ["Chandigarh"],
    "Dadra and Nagar Haveli and Daman and Diu": ["Daman", "Diu", "Silvassa"],
    "Delhi": ["New Delhi", "Dwarka", "Rohini"],
    "Jammu and Kashmir": ["Srinagar", "Jammu", "Anantnag"],
    "Ladakh": ["Leh", "Kargil"],
    "Lakshadweep": ["Kavaratti"],
    "Puducherry": ["Puducherry", "Karaikal", "Yanam"],
}


# Page configuration
st.set_page_config(
    page_title="CoActions - Career Guidance",
    page_icon="logo123.png",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom CSS for Professional App with Background Image
st.markdown("""
<style>

    /* Background Image with Overlay */
    .stApp {
        background-image: url("https://images.unsplash.com/photo-1557683304-673a230ec87c?q=80&w=2029&auto=format&fit=crop");
        background-size: cover;
        background-position: center;
        background-attachment: fixed;
        background-repeat: no-repeat;
    }
    
    /* Dark Overlay for better readability */
    .stApp::before {
        content: "";
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: linear-gradient(135deg, rgba(0,0,0,0.5), rgba(0,0,0,0.3));
        z-index: -1;
    }
    
    /* Main Card - Glassmorphism Effect */
    .main-card {
        background: rgba(255, 255, 255, 0.94);
        backdrop-filter: blur(12px);
        border-radius: 28px;
        padding: 2rem;
        box-shadow: 0 25px 50px -12px rgba(0,0,0,0.3);
        border: 1px solid rgba(255,255,255,0.3);
        margin-bottom: 1rem;
    }
    
    /* App Title with Gradient */
    .app-title {
        font-size: 2.5rem;
        font-weight: 800;
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
    }
    
    /* Welcome Heading */
    .welcome-heading {
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
        font-size: 2rem;
        font-weight: 700;
        margin-bottom: 0.5rem;
        text-align: center;
    }
    
    /* User Cards - Frosted Glass */
    .user-card {
        box-sizing: border-box;
        background: rgba(255, 255, 255, 0.85);
        backdrop-filter: blur(8px);
        border-radius: 24px;
        padding: 1.8rem;
        text-align: center;
        transition: all 0.3s ease;
        border: 2px solid rgba(102, 126, 234, 0.3);
        margin: 0 0 var(--space-sm) 0;
        cursor: pointer;
    }
    .user-card:hover {
        transform: translateY(-6px);
        border-color: #667eea;
        background: rgba(255, 255, 255, 0.95);
        box-shadow: 0 20px 40px -12px rgba(102,126,234,0.4);
    }
    
    /* Modern Buttons */
    .stButton > button {
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        border: none;
        border-radius: 40px;
        padding: 0.7rem 1.8rem;
        font-weight: 600;
        transition: all 0.3s ease;
        width: 100%;
    }
    .stButton > button:hover {
        background: linear-gradient(135deg, #5a67d8, #6b46c1);
        transform: translateY(-2px);
        box-shadow: 0 10px 20px -5px rgba(102,126,234,0.4);
    }

    /* Top-nav Home/About/Contact/Help/AI Chat buttons - scoped so this
       never affects any other button in the app. Keeps labels on one
       line and shrinks padding/font so 5 buttons fit comfortably
       side-by-side. */
    .st-key-header_menu_row .stButton > button {
        padding: 0.55rem 0.5rem;
        font-size: 0.82rem;
        white-space: nowrap;
        min-width: 0;
    }
    @media (max-width: 768px) {
        .st-key-header_menu_row .stButton > button {
            padding: 0.5rem 0.3rem;
            font-size: 0.72rem;
        }
    }
    
    /* Question Cards */
    .question-card {
        box-sizing: border-box;
        background: rgba(247, 250, 252, 0.9);
        border-radius: 20px;
        padding: 1rem;
        margin: 0 0 var(--space-sm) 0;
        border-left: 5px solid #667eea;
        backdrop-filter: blur(4px);
    }
    
    /* Stream Cards - Gradient Cards (base look; sizing/spacing is owned
       by the .rec-card / .compare-card modifier classes applied in the
       markup, so there is a single source of truth for box model) */
    .stream-card-high, .stream-card-good, .stream-card-fair, .stream-card-potential {
        box-sizing: border-box;
        border-radius: 20px;
        padding: 1.5rem;
        text-align: center;
        transition: all 0.3s ease;
        cursor: pointer;
        color: white;
    }
    .stream-card-high { background: linear-gradient(135deg, #11998e, #38ef7d); }
    .stream-card-good { background: linear-gradient(135deg, #f2994a, #f2c94c); }
    .stream-card-fair { background: linear-gradient(135deg, #ff6b6b, #feca57); }
    .stream-card-potential { background: linear-gradient(135deg, #4facfe, #00f2fe); }

    .stream-card-high:hover, .stream-card-good:hover,
    .stream-card-fair:hover, .stream-card-potential:hover {
        transform: translateY(-8px);
        box-shadow: 0 20px 40px -12px rgba(0,0,0,0.3);
    }
    
    /* Score Bar */
    .score-bar {
        background: rgba(0,0,0,0.2);
        border-radius: 20px;
        height: 10px;
        margin: 0.5rem 0;
        overflow: hidden;
    }
    .score-fill {
        background: linear-gradient(90deg, #11998e, #38ef7d);
        border-radius: 20px;
        height: 100%;
        transition: width 1s ease;
    }
    
    /* Radio Buttons */
    .stRadio > div {
        background: rgba(255,255,255,0.9);
        padding: 0.8rem;
        border-radius: 20px;
    }
    
    /* Progress Bar */
    .progress-text {
        color: #667eea;
        font-size: 0.85rem;
        margin-top: 0.5rem;
        font-weight: 600;
    }
    
    /* Page Counter */
    .page-counter {
        background: linear-gradient(135deg, #667eea, #764ba2);
        border-radius: 20px;
        padding: 0.5rem 1rem;
        text-align: center;
        margin: 1rem 0;
        color: white;
        font-weight: 600;
    }
    
    /* User Type Indicator */
    .user-type-indicator {
        background: linear-gradient(135deg, #667eea, #764ba2);
        color: white;
        padding: 0.3rem 1rem;
        border-radius: 20px;
        display: inline-block;
        margin-bottom: 1rem;
        font-weight: 600;
    }
    
    /* Scrollbar */
    ::-webkit-scrollbar {
        width: 8px;
        height: 8px;
    }
    ::-webkit-scrollbar-track {
        background: rgba(255,255,255,0.1);
        border-radius: 10px;
    }
    ::-webkit-scrollbar-thumb {
        background: linear-gradient(135deg, #667eea, #764ba2);
        border-radius: 10px;
    }
    
    /* Print styles */
    @media print {
        .stButton, .stDownloadButton {
            display: none !important;
        }
        .main-card {
            background: white !important;
            box-shadow: none !important;
        }
        .stApp::before {
            display: none !important;
        }
    }

    /* ==================== FIXED DROPDOWN VISIBILITY - CRITICAL FIX ==================== */

    /* The "card" wrapper divs on every page are opened with one
       st.markdown('<div class="main-card">') call and closed with a
       separate later st.markdown('</div>') call. Streamlit renders each
       st.markdown() call as its own isolated DOM node, so that opening
       tag briefly renders as a standalone, contentless <div class="main-card">
       before the real content (which lives in later, sibling Streamlit
       elements) appears - showing up as an empty rounded/shadowed box.
       Hiding it only when genuinely empty leaves every populated card
       completely untouched. */
    .main-card:empty {
        display: none !important;
    }

    /* ==================== SHARED INPUT SIZING TOKENS ==================== */
    :root {
        --field-height: 48px;
        --field-border: 2px solid #1E88E5;
        --field-radius: 12px;
        --field-padding: 12px 15px;
        --field-font-size: 1rem;
    }

    /* Prevent ANY ancestor wrapper from clipping the field's border,
       regardless of which emotion-cache div Streamlit generates. This is
       what was making the bottom border disappear on some fields. */
    .stTextInput, .stTextInput > div, .stTextInput > div > div,
    .stNumberInput, .stNumberInput > div, .stNumberInput > div > div,
    .stSelectbox, .stSelectbox > div, .stSelectbox > div > div {
        overflow: visible !important;
    }

    /* SelectBox Container - Base styling */
    .stSelectbox > div {
        background: white !important;
        border-radius: var(--field-radius) !important;
    }
    
    /* The main select box input field */
    .stSelectbox div[data-baseweb="select"] {
        background: white !important;
        border: var(--field-border) !important;
        border-radius: var(--field-radius) !important;
        height: var(--field-height) !important;
        min-height: var(--field-height) !important;
        max-height: var(--field-height) !important;
        box-sizing: border-box !important;
        box-shadow: 0 2px 6px rgba(30, 136, 229, 0.08) !important;
        transition: all 0.25s ease !important;
        display: flex !important;
        align-items: center !important;
    }
    
    .stSelectbox div[data-baseweb="select"]:hover {
        border-color: #1565C0 !important;
        background: #E3F2FD !important;
    }

    .stSelectbox div[data-baseweb="select"]:focus-within {
        border-color: #1565C0 !important;
        box-shadow: 0 0 0 3px rgba(30, 136, 229, 0.2) !important;
    }
    
    /* THE MOST IMPORTANT FIX - Selected value text */
    .stSelectbox div[data-baseweb="select"] div {
        color: #1a1a2e !important;
        font-size: var(--field-font-size) !important;
        font-weight: 500 !important;
    }
    
    /* The value display span */
    .stSelectbox div[data-baseweb="select"] span {
        color: #1a1a2e !important;
        font-weight: 500 !important;
    }
    
    /* Dropdown arrow icon */
    .stSelectbox svg {
        fill: #1E88E5 !important;
    }
    
    /* Dropdown menu container (when opened) */
    div[data-baseweb="popover"] {
        background: white !important;
        border: 1px solid #1E88E5 !important;
        border-radius: var(--field-radius) !important;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15) !important;
    }
    
    /* Dropdown options list */
    ul[role="listbox"] {
        background: white !important;
        border-radius: var(--field-radius) !important;
    }
    
    /* Individual option items */
    li[role="option"] {
        color: #1a1a2e !important;
        background: white !important;
        padding: 10px 15px !important;
        font-size: 0.9rem !important;
    }
    
    /* Hover effect on options */
    li[role="option"]:hover {
        background: #E3F2FD !important;
        color: #1E88E5 !important;
    }
    
    /* Selected option in dropdown */
    li[role="option"][aria-selected="true"] {
        background: #1E88E5 !important;
        color: white !important;
    }
    
    /* ==================== INPUT FIELDS STYLING ==================== */

    /* BaseWeb wraps every text/number <input> in its own chrome div
       (border/background/shadow) BEFORE our CSS below ever touches the
       actual <input>. Left alone, that produces the "double box" /
       mismatched-corner look (a default grey/blue sliver peeking out
       around our custom border). Stripping it here means the only
       visible border/background/shadow on every field is the one we
       define explicitly below - text, number, and select all end up
       pixel-identical. */
    .stTextInput div[data-baseweb="base-input"],
    .stTextInput div[data-baseweb="input"],
    .stNumberInput div[data-baseweb="base-input"],
    .stNumberInput div[data-baseweb="input"] {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        height: var(--field-height) !important;
        box-sizing: border-box !important;
    }

    /* Text Input Fields - identical box model to .stSelectbox and the
       number input, so text inputs, number inputs, and dropdowns all
       render at the exact same height/border/radius in the same row. */
    .stTextInput > div > div > input {
        background: #ffffff !important;
        border: var(--field-border) !important;
        border-radius: var(--field-radius) !important;
        padding: var(--field-padding) !important;
        height: var(--field-height) !important;
        min-height: var(--field-height) !important;
        max-height: var(--field-height) !important;
        box-sizing: border-box !important;
        font-size: var(--field-font-size) !important;
        line-height: normal !important;
        color: #1a1a2e !important;
        box-shadow: 0 2px 6px rgba(30, 136, 229, 0.08) !important;
        transition: all 0.25s ease !important;
    }
    
    .stTextInput > div > div > input:focus {
        border-color: #1565C0 !important;
        background: #ffffff !important;
        box-shadow: 0 0 0 3px rgba(30, 136, 229, 0.2) !important;
        outline: none !important;
    }
    
    .stTextInput > div > div > input:hover {
        border-color: #1565C0 !important;
        background: #E3F2FD !important;
    }
    
    /* Number Input - the stepper container (field + −/+ buttons) is
       styled as ONE unified pill locked to --field-height, same as the
       text/select fields. overflow:hidden here only clips the buttons'
       square corners against the rounded container - it can no longer
       clip the border itself because every child is locked to
       height:100% + border-box, so nothing can grow past the
       container's own edge and force a clip anymore. */
    div[data-testid="stNumberInputContainer"] {
        background: #ffffff !important;
        border: var(--field-border) !important;
        border-radius: var(--field-radius) !important;
        height: var(--field-height) !important;
        min-height: var(--field-height) !important;
        max-height: var(--field-height) !important;
        box-sizing: border-box !important;
        overflow: hidden !important;
        display: flex !important;
        align-items: stretch !important;
        box-shadow: 0 2px 6px rgba(30, 136, 229, 0.08) !important;
        transition: all 0.25s ease !important;
    }
    div[data-testid="stNumberInputContainer"]:hover {
        border-color: #1565C0 !important;
    }
    div[data-testid="stNumberInputContainer"]:focus-within {
        border-color: #1565C0 !important;
        box-shadow: 0 0 0 3px rgba(30, 136, 229, 0.2) !important;
    }
    .stNumberInput > div > div > input {
        background: transparent !important;
        border: none !important;
        border-radius: 0 !important;
        padding: var(--field-padding) !important;
        height: 100% !important;
        box-sizing: border-box !important;
        font-size: var(--field-font-size) !important;
        line-height: normal !important;
        color: #1a1a2e !important;
        box-shadow: none !important;
    }
    .stNumberInput button {
        background: #ffffff !important;
        border: none !important;
        border-left: 1px solid #E3F2FD !important;
        height: 100% !important;
        box-sizing: border-box !important;
        color: #1E88E5 !important;
        transition: background 0.2s ease !important;
    }
    .stNumberInput button:hover {
        background: #E3F2FD !important;
    }
    
    /* Labels */
    .stTextInput label, 
    .stNumberInput label, 
    .stSelectbox label {
        color: #1565C0 !important;
        font-weight: 600 !important;
        font-size: 0.9rem !important;
        margin-bottom: 5px !important;
    }
    
    /* Placeholder text */
    .stTextInput input::placeholder,
    .stNumberInput input::placeholder {
        color: #90A4AE !important;
        font-size: 0.9rem !important;
    }

    /* Equal vertical rhythm between stacked fields in the same column so
       one field's box-shadow/border never visually merges with the next
       field's label. */
    div[data-testid="stTextInput"],
    div[data-testid="stNumberInput"],
    div[data-testid="stSelectbox"] {
        margin-bottom: 0.4rem !important;
    }

    /* ==================== SKILL CHIPS (Technical / Soft Skills) ==================== */
    .skill-chip-tech {
        display: inline-block;
        background: linear-gradient(135deg, #4facfe, #00f2fe);
        color: white;
        border-radius: 30px;
        padding: 0.5rem 1rem;
        margin: 0.3rem;
        font-size: 0.85rem;
        font-weight: 600;
        box-shadow: 0 4px 10px -4px rgba(79,172,254,0.5);
    }
    .skill-chip-soft {
        display: inline-block;
        background: linear-gradient(135deg, #f2994a, #f2c94c);
        color: white;
        border-radius: 30px;
        padding: 0.5rem 1rem;
        margin: 0.3rem;
        font-size: 0.85rem;
        font-weight: 600;
        box-shadow: 0 4px 10px -4px rgba(242,153,74,0.5);
    }

    /* ==================== CAREER OPPORTUNITY CARDS ==================== */
    .opportunity-card {
        box-sizing: border-box;
        background: rgba(255, 255, 255, 0.9);
        backdrop-filter: blur(6px);
        border-radius: 18px;
        padding: 1.2rem;
        margin: 0 0 var(--space-sm) 0;
        text-align: center;
        border: 1px solid rgba(102,126,234,0.25);
        box-shadow: 0 10px 25px -10px rgba(0,0,0,0.2);
        transition: all 0.25s ease;
        height: 100%;
    }
    .opportunity-card:hover {
        transform: translateY(-4px);
        box-shadow: 0 16px 32px -10px rgba(102,126,234,0.35);
        border-color: #667eea;
    }
    .opportunity-card .opp-icon {
        font-size: 1.6rem;
        margin-bottom: 0.3rem;
    }
    .opportunity-card .opp-label {
        font-size: 0.95rem;
        font-weight: 700;
        color: #2d2d44;
    }

    /* ==================== DESIGN SYSTEM TOKENS ==================== */
    :root {
        --radius-lg: 28px;
        --radius-md: 20px;
        --radius-sm: 12px;
        --space-xs: 0.5rem;
        --space-sm: 0.8rem;
        --space-md: 1.2rem;
        --space-lg: 1.8rem;
        --shadow-card: 0 10px 25px -10px rgba(0,0,0,0.18);
        --shadow-card-hover: 0 16px 32px -10px rgba(102,126,234,0.35);
        --border-soft: 1px solid rgba(102,126,234,0.2);
        --brand-gradient: linear-gradient(135deg, #667eea, #764ba2);
        --text-heading: #2d2d44;
        --text-body: #4a5568;
    }

    /* ==================== PREVIOUSLY UNSTYLED CLASSES ====================
       These classes were referenced throughout the app but had no matching
       CSS rule, causing default/unstyled browser text (wrong font size,
       weight, color, spacing) to appear next to properly styled elements.
       Styling them brings every page onto the same visual system. */
    .welcome-subheading {
        text-align: center;
        color: var(--text-body);
        font-size: 1.05rem;
        font-weight: 500;
        line-height: 1.5;
        margin: 0 0 var(--space-sm) 0;
    }
    .sub-message {
        text-align: center;
        color: var(--text-body);
        font-size: 1rem;
        font-weight: 500;
        margin: 0 0 var(--space-sm) 0;
    }
    .section-title {
        color: var(--text-heading);
        font-size: 1.25rem;
        font-weight: 700;
        margin: var(--space-sm) 0 var(--space-xs) 0;
    }
    /* Student Information heading + Language selector row - keeps the
       heading left-aligned and the language dropdown pinned to the far
       right, both vertically centered on the same row. */
    .st-key-student_info_header_row [data-testid="stHorizontalBlock"] {
        align-items: center;
    }
    .st-key-student_info_header_row .section-title {
        margin: 0;
    }
    .st-key-student_info_header_row .stSelectbox {
        margin-bottom: 0;
    }
    .st-key-home_banner_wrap {
        text-align: center;
        margin-bottom: 18px;
    }
    .st-key-home_banner_wrap img {
        max-width: 260px;
        width: 100%;
        height: auto;
        margin: 0 auto;
        display: block;
        object-fit: contain;
    }
    .student-info-card {
        background: rgba(102,126,234,0.08);
        border: var(--border-soft);
        border-radius: var(--radius-md);
        padding: var(--space-md);
        margin-bottom: var(--space-md);
        color: var(--text-heading);
        font-size: 1rem;
        line-height: 1.6;
        text-align: center;
    }
    .user-icon {
        font-size: 2.2rem;
        margin-bottom: var(--space-xs);
    }
    .question-text {
        font-weight: 700;
        color: var(--text-heading);
        font-size: 1.05rem;
        line-height: 1.5;
    }
    .stream-detail-card {
        background: rgba(255,255,255,0.92);
        backdrop-filter: blur(8px);
        border-radius: var(--radius-lg);
        padding: var(--space-lg);
        margin-bottom: var(--space-md);
        box-shadow: var(--shadow-card);
        border: 1px solid rgba(255,255,255,0.3);
    }

    /* ==================== REMOVE DEFAULT STREAMLIT CHROME ====================
       Streamlit's built-in top toolbar/decoration bar renders as a solid
       block above the page content by default. Against this app's custom
       background it shows up as a stray white rectangle sitting above/
       behind the logo and page title. Making it transparent removes that
       empty decorative box without touching any app content. */
    header[data-testid="stHeader"] {
        background: transparent !important;
        box-shadow: none !important;
    }
    div[data-testid="stDecoration"] {
        display: none !important;
    }
    div[data-testid="stToolbar"] {
        background: transparent !important;
    }
    div[data-testid="stImage"] {
        background: transparent !important;
        margin: 0 !important;
    }

    /* ==================== HEADING HIERARCHY ==================== */
    .main-card h3 {
        font-size: 1.1rem;
        font-weight: 700;
        color: var(--text-heading);
        margin: 0.3rem 0 0.6rem 0;
    }

    /* ==================== EQUAL-HEIGHT / ALIGNED CARD ROWS ====================
       Makes every st.columns() row stretch its columns to equal height, and
       keeps the button that follows a card pinned to the same position
       across all columns in the row - fixes uneven card heights and
       misaligned buttons in the Top-3 Recommendations, Stream Comparison,
       and every other card grid in the app. */
    div[data-testid="stHorizontalBlock"] {
        align-items: stretch;
        gap: var(--space-sm);
    }
    div[data-testid="column"] {
        display: flex;
        flex-direction: column;
    }
    div[data-testid="column"] > div {
        height: 100%;
    }
    div[data-testid="column"] .stButton {
        margin-top: auto;
    }

    /* Top-3 Recommendation cards: identical fixed height regardless of how
       long the AI-generated explanation text is, with clamped text so
       long content never overflows or breaks the card grid. */
    .rec-card {
        height: 300px;
        display: flex;
        flex-direction: column;
        margin: 0 0 var(--space-sm) 0;
    }
    .rec-card h3 {
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
        min-height: 2.6em;
    }
    .rec-card p {
        display: -webkit-box;
        -webkit-line-clamp: 5;
        -webkit-box-orient: vertical;
        overflow: hidden;
        flex: 1;
        margin: 0;
    }

    /* Stream Comparison cards (Strengths / Opportunities on the AI Analysis
       page): equal height regardless of how many bullet items the AI
       returns, without clipping any content. */
    .compare-card {
        min-height: 220px;
        display: flex;
        flex-direction: column;
        margin: 0 0 var(--space-sm) 0;
    }

    /* ==================== CARD GRIDS (Career Opportunities, Learning
       Resources, and similar item-card collections) ====================
       A real CSS Grid instead of Streamlit columns reused via modulo -
       every cell in a row is automatically the same width and height
       (CSS Grid's default align-items/justify-items is "stretch"), and
       items always flow left-to-right, top-to-bottom in true rows. */
    .card-grid {
        display: grid;
        gap: var(--space-sm);
        margin: 0 0 var(--space-sm) 0;
    }
    .card-grid-4 { grid-template-columns: repeat(4, 1fr); }
    .card-grid-3 { grid-template-columns: repeat(3, 1fr); }
    .card-grid .opportunity-card {
        margin: 0;
        min-height: 110px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
    }
    .card-grid .opp-label {
        display: -webkit-box;
        -webkit-line-clamp: 3;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }
    @media (max-width: 1024px) {
        .card-grid-4, .card-grid-3 { grid-template-columns: repeat(2, 1fr); }
    }
    @media (max-width: 600px) {
        .card-grid-4, .card-grid-3 { grid-template-columns: 1fr; }
    }

    /* ==================== PERSONALITY CHOICE CARDS ====================
       "Take Personality Assessment" / "Skip Personality Assessment": a
       fixed (not min) height ties both cards to the exact same top and
       bottom position regardless of description text length, so the two
       action buttons below them always start at the same vertical point. */
    .personality-choice-card {
        height: 350px;
        box-sizing: border-box;
        display: flex;
        flex-direction: column;
        justify-content: flex-start;
        align-items: stretch;
        overflow: hidden;
    }
    .personality-choice-card h3 {
        margin: 0.4rem 0;
    }
    .personality-choice-card p {
        margin: 0.3rem 0;
    }

    /* ==================== EXPANDERS, ALERTS, DIVIDERS, PROGRESS ==================== */
    [data-testid="stExpander"] {
        border-radius: var(--radius-md) !important;
        overflow: hidden;
        margin-bottom: var(--space-sm);
    }
    div[data-testid="stAlert"] {
        border-radius: var(--radius-sm) !important;
    }
    hr {
        margin: var(--space-md) 0 !important;
        border: none;
        border-top: 1px solid rgba(0,0,0,0.08);
    }
    .stProgress > div > div > div > div {
        background: var(--brand-gradient) !important;
        border-radius: 10px !important;
    }
    .stProgress > div > div {
        border-radius: 10px !important;
    }

    /* Tighter, more consistent top-level page padding */
    .block-container {
        padding-top: 2rem !important;
        padding-bottom: 2rem !important;
    }

    /* ==================== 12-MONTH LEARNING ROADMAP TIMELINE ==================== */
    .roadmap-overview-card {
        box-sizing: border-box;
        background: rgba(247, 250, 252, 0.9);
        border-radius: 20px;
        padding: 1.2rem;
        margin: 0 0 var(--space-md) 0;
        border-left: 5px solid #667eea;
        backdrop-filter: blur(4px);
    }
    .roadmap-timeline {
        position: relative;
        margin: 1.5rem 0;
        padding-left: 2.4rem;
    }
    .roadmap-timeline::before {
        content: "";
        position: absolute;
        left: 0.85rem;
        top: 0.4rem;
        bottom: 0.4rem;
        width: 3px;
        background: var(--brand-gradient);
        border-radius: 3px;
    }
    .roadmap-month {
        position: relative;
        margin-bottom: 1.4rem;
    }
    .roadmap-month-dot {
        position: absolute;
        left: -2.4rem;
        top: 0.3rem;
        width: 1.8rem;
        height: 1.8rem;
        border-radius: 50%;
        background: var(--brand-gradient);
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 0.8rem;
        font-weight: 700;
        box-shadow: 0 4px 10px -3px rgba(102,126,234,0.6);
    }
    .roadmap-month-card {
        box-sizing: border-box;
        background: rgba(255, 255, 255, 0.92);
        backdrop-filter: blur(6px);
        border-radius: 18px;
        padding: 1.1rem 1.3rem;
        border: 1px solid rgba(102,126,234,0.2);
        box-shadow: 0 10px 22px -12px rgba(0,0,0,0.18);
    }
    .roadmap-month-card h4 {
        margin: 0 0 0.6rem 0;
        color: #2d2d44;
        font-size: 1.05rem;
    }
    .roadmap-section-label {
        font-weight: 700;
        font-size: 0.85rem;
        color: #667eea;
        margin: 0.55rem 0 0.2rem 0;
    }
    .roadmap-month-card ul {
        margin: 0 0 0.2rem 0;
        padding-left: 1.2rem;
    }
    .roadmap-month-card li {
        font-size: 0.88rem;
        margin-bottom: 0.15rem;
    }
    .roadmap-cert-chip {
        display: inline-block;
        background: linear-gradient(135deg, #43e97b, #38f9d7);
        color: #12332a;
        border-radius: 30px;
        padding: 0.35rem 0.8rem;
        margin: 0.2rem 0.25rem 0.2rem 0;
        font-size: 0.78rem;
        font-weight: 700;
    }

    /* ==================== AI SKILL GAP ANALYSIS ==================== */
    .skillgap-readiness-card {
        box-sizing: border-box;
        background: rgba(247, 250, 252, 0.9);
        border-radius: 20px;
        padding: 1.2rem;
        margin: 0 0 var(--space-md) 0;
        border-left: 5px solid #667eea;
        backdrop-filter: blur(4px);
    }
    .skillgap-readiness-score {
        font-size: 2.2rem;
        font-weight: 800;
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
    }
    .skillgap-section-title {
        font-size: 1.15rem;
        font-weight: 800;
        color: #2d2d44;
        margin: 1.6rem 0 0.7rem 0;
    }
    .skillgap-card {
        box-sizing: border-box;
        background: rgba(255, 255, 255, 0.92);
        backdrop-filter: blur(6px);
        border-radius: 18px;
        padding: 1rem 1.2rem;
        margin-bottom: 0.8rem;
        border: 1px solid rgba(102,126,234,0.18);
        box-shadow: 0 10px 22px -14px rgba(0,0,0,0.18);
    }
    .skillgap-card-title {
        font-weight: 700;
        font-size: 0.98rem;
        color: #2d2d44;
        margin-bottom: 0.3rem;
    }
    .skillgap-card-explanation {
        font-size: 0.85rem;
        color: #4a4a5e;
        margin-top: 0.35rem;
    }
    .skillgap-bar-track {
        background: rgba(0,0,0,0.08);
        border-radius: 20px;
        height: 10px;
        margin: 0.4rem 0 0.2rem 0;
        overflow: hidden;
    }
    .skillgap-bar-fill-strength {
        background: linear-gradient(90deg, #11998e, #38ef7d);
        border-radius: 20px;
        height: 100%;
    }
    .skillgap-bar-fill-difficulty {
        background: linear-gradient(90deg, #f2994a, #eb3349);
        border-radius: 20px;
        height: 100%;
    }
    .skillgap-badge {
        display: inline-block;
        border-radius: 30px;
        padding: 0.3rem 0.75rem;
        margin: 0 0 0.4rem 0;
        font-size: 0.75rem;
        font-weight: 700;
        color: white;
    }
    .skillgap-badge-critical { background: linear-gradient(135deg, #eb3349, #f45c43); }
    .skillgap-badge-high { background: linear-gradient(135deg, #f2994a, #f2c94c); }
    .skillgap-badge-medium { background: linear-gradient(135deg, #4facfe, #00f2fe); }
    .skillgap-badge-easy { background: linear-gradient(135deg, #11998e, #38ef7d); }
    .skillgap-badge-moderate { background: linear-gradient(135deg, #4facfe, #00f2fe); }
    .skillgap-badge-hard { background: linear-gradient(135deg, #f2994a, #f2c94c); }
    .skillgap-badge-very-hard { background: linear-gradient(135deg, #eb3349, #f45c43); }
    .skillgap-priority-rank {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 1.8rem;
        height: 1.8rem;
        border-radius: 50%;
        background: var(--brand-gradient);
        color: white;
        font-weight: 800;
        font-size: 0.85rem;
        margin-right: 0.6rem;
        flex-shrink: 0;
    }
    .skillgap-order-timeline {
        position: relative;
        margin: 0.5rem 0 1rem 0;
        padding-left: 2.2rem;
    }
    .skillgap-order-timeline::before {
        content: "";
        position: absolute;
        left: 0.75rem;
        top: 0.3rem;
        bottom: 0.3rem;
        width: 3px;
        background: var(--brand-gradient);
        border-radius: 3px;
    }
    .skillgap-order-item {
        position: relative;
        margin-bottom: 0.9rem;
    }
    .skillgap-order-dot {
        position: absolute;
        left: -2.2rem;
        top: 0.15rem;
        width: 1.6rem;
        height: 1.6rem;
        border-radius: 50%;
        background: var(--brand-gradient);
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 0.75rem;
        font-weight: 700;
    }
    .skillgap-time-chip {
        display: inline-block;
        background: rgba(102,126,234,0.12);
        color: #4a4a8a;
        border-radius: 12px;
        padding: 0.3rem 0.7rem;
        margin: 0.15rem 0.3rem 0.15rem 0;
        font-size: 0.8rem;
        font-weight: 700;
    }

    /* ==================== VOCATIONAL CAREER AI DETAIL ====================
       Modern blue/cyan presentation for the AI-generated single-career
       breakdown (Career Overview, Eligibility, Skills, Salary, Job Demand,
       Certifications, Career Growth, Future Scope, Learning Roadmap, AI
       Career Advice) on the Vocational Careers page. Scoped entirely under
       "vocdetail-" classes so no other page/section is affected. Uses the
       same blue/cyan gradient (#4facfe -> #00f2fe) already used elsewhere
       in the app (skill chips, medium-priority badges) as its accent. */
    .vocdetail-hero {
        box-sizing: border-box;
        background: linear-gradient(135deg, #4facfe, #00f2fe);
        border-radius: var(--radius-lg);
        padding: 1.4rem 1.6rem;
        margin: 0 0 var(--space-md) 0;
        color: white;
        display: flex;
        align-items: center;
        gap: 1rem;
        box-shadow: 0 14px 30px -12px rgba(79,172,254,0.55);
    }
    .vocdetail-hero-icon {
        font-size: 2.2rem;
        width: 3.2rem;
        height: 3.2rem;
        display: flex;
        align-items: center;
        justify-content: center;
        background: rgba(255,255,255,0.22);
        border-radius: 50%;
        flex-shrink: 0;
    }
    .vocdetail-hero-title {
        font-size: 1.3rem;
        font-weight: 800;
        margin: 0 0 0.3rem 0;
    }
    .vocdetail-hero-badge {
        display: inline-block;
        background: rgba(255,255,255,0.25);
        border-radius: 20px;
        padding: 0.2rem 0.75rem;
        font-size: 0.78rem;
        font-weight: 700;
        letter-spacing: 0.02em;
    }

    /* Info-box cards (overview / salary / job demand / future scope / advice) */
    .vocdetail-grid-2 {
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: var(--space-sm);
        margin: 0 0 var(--space-sm) 0;
    }
    @media (max-width: 900px) {
        .vocdetail-grid-2 { grid-template-columns: 1fr; }
    }
    .vocdetail-card {
        box-sizing: border-box;
        background: rgba(255, 255, 255, 0.92);
        backdrop-filter: blur(6px);
        border-radius: var(--radius-md);
        padding: 1.1rem 1.2rem;
        border: 1px solid rgba(79,172,254,0.22);
        box-shadow: 0 10px 24px -14px rgba(0,0,0,0.18);
        height: 100%;
    }
    .vocdetail-card-header {
        display: flex;
        align-items: center;
        gap: 0.6rem;
        margin-bottom: 0.5rem;
    }
    .vocdetail-card-icon {
        width: 2.2rem;
        height: 2.2rem;
        min-width: 2.2rem;
        border-radius: 50%;
        background: linear-gradient(135deg, #4facfe, #00f2fe);
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 1.05rem;
        box-shadow: 0 6px 14px -6px rgba(79,172,254,0.6);
    }
    .vocdetail-card-title {
        font-weight: 800;
        font-size: 0.98rem;
        color: #2d2d44;
    }
    .vocdetail-card-body {
        font-size: 0.88rem;
        color: #4a4a5e;
        line-height: 1.55;
    }
    .vocdetail-card-body ul {
        margin: 0.2rem 0 0 1.1rem;
        padding: 0;
    }
    .vocdetail-card-body li {
        margin-bottom: 0.3rem;
    }

    /* Skill tags */
    .vocdetail-tag-wrap {
        display: flex;
        flex-wrap: wrap;
        gap: 0.4rem;
        margin-top: 0.2rem;
    }
    .vocdetail-tag {
        display: inline-block;
        background: linear-gradient(135deg, #4facfe, #00f2fe);
        color: white;
        border-radius: 30px;
        padding: 0.45rem 0.95rem;
        font-size: 0.82rem;
        font-weight: 700;
        box-shadow: 0 4px 10px -4px rgba(79,172,254,0.5);
    }

    /* List-style info cards (eligibility / certifications) as icon rows */
    .vocdetail-list-row {
        display: flex;
        align-items: flex-start;
        gap: 0.55rem;
        margin-bottom: 0.5rem;
        font-size: 0.88rem;
        color: #4a4a5e;
        line-height: 1.5;
    }
    .vocdetail-list-row:last-child {
        margin-bottom: 0;
    }
    .vocdetail-list-dot {
        width: 1.5rem;
        height: 1.5rem;
        min-width: 1.5rem;
        border-radius: 50%;
        background: rgba(79,172,254,0.15);
        color: #0091d5;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 0.75rem;
        font-weight: 800;
        margin-top: 0.05rem;
    }

    /* Career growth: horizontal stepper / progress indicator */
    .vocdetail-stepper {
        display: flex;
        align-items: flex-start;
        overflow-x: auto;
        padding: 0.4rem 0.1rem 0.6rem 0.1rem;
        gap: 0;
    }
    .vocdetail-step {
        display: flex;
        flex-direction: column;
        align-items: center;
        text-align: center;
        min-width: 108px;
        position: relative;
    }
    .vocdetail-step-circle {
        width: 2.4rem;
        height: 2.4rem;
        border-radius: 50%;
        background: linear-gradient(135deg, #4facfe, #00f2fe);
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: 800;
        font-size: 0.95rem;
        box-shadow: 0 6px 14px -6px rgba(79,172,254,0.6);
        z-index: 1;
    }
    .vocdetail-step-connector {
        position: absolute;
        top: 1.2rem;
        left: calc(-50% + 1.2rem);
        width: calc(100% - 2.4rem);
        height: 3px;
        background: linear-gradient(90deg, #4facfe, #00f2fe);
        z-index: 0;
    }
    .vocdetail-step-label {
        font-size: 0.78rem;
        font-weight: 700;
        color: #2d2d44;
        margin-top: 0.45rem;
        max-width: 100px;
    }

    /* Learning roadmap: vertical numbered timeline */
    .vocdetail-timeline {
        position: relative;
        margin: 0.3rem 0 0.2rem 0;
        padding-left: 2.3rem;
    }
    .vocdetail-timeline::before {
        content: "";
        position: absolute;
        left: 0.78rem;
        top: 0.3rem;
        bottom: 0.3rem;
        width: 3px;
        background: linear-gradient(180deg, #4facfe, #00f2fe);
        border-radius: 3px;
    }
    .vocdetail-timeline-item {
        position: relative;
        margin-bottom: 0.9rem;
        font-size: 0.88rem;
        color: #4a4a5e;
        line-height: 1.5;
    }
    .vocdetail-timeline-item:last-child {
        margin-bottom: 0;
    }
    .vocdetail-timeline-dot {
        position: absolute;
        left: -2.3rem;
        top: 0.05rem;
        width: 1.7rem;
        height: 1.7rem;
        border-radius: 50%;
        background: linear-gradient(135deg, #4facfe, #00f2fe);
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 0.78rem;
        font-weight: 800;
    }

    /* ==================== AI RESUME SUGGESTIONS ==================== */
    .resume-note-card {
        box-sizing: border-box;
        background: rgba(247, 250, 252, 0.9);
        border-radius: 20px;
        padding: 1.1rem 1.2rem;
        margin: 0 0 var(--space-md) 0;
        border-left: 5px solid #764ba2;
        backdrop-filter: blur(4px);
        font-size: 0.88rem;
    }
    .resume-section-title {
        font-size: 1.15rem;
        font-weight: 800;
        color: #2d2d44;
        margin: 1.6rem 0 0.7rem 0;
    }
    .resume-card {
        box-sizing: border-box;
        background: rgba(255, 255, 255, 0.92);
        backdrop-filter: blur(6px);
        border-radius: 18px;
        padding: 1rem 1.2rem;
        margin-bottom: 0.8rem;
        border: 1px solid rgba(118,75,162,0.18);
        box-shadow: 0 10px 22px -14px rgba(0,0,0,0.18);
    }
    .resume-card-title {
        font-weight: 700;
        font-size: 0.98rem;
        color: #2d2d44;
        margin-bottom: 0.3rem;
    }
    .resume-card-body {
        font-size: 0.88rem;
        color: #3a3a4e;
        margin-top: 0.2rem;
    }
    .resume-card-reason {
        font-size: 0.82rem;
        color: #6a6a7e;
        margin-top: 0.35rem;
        font-style: italic;
    }
    .resume-option-badge {
        display: inline-block;
        background: linear-gradient(135deg, #764ba2, #667eea);
        color: white;
        border-radius: 30px;
        padding: 0.25rem 0.7rem;
        margin: 0 0 0.4rem 0;
        font-size: 0.72rem;
        font-weight: 700;
    }
    .resume-skill-chip {
        display: inline-block;
        background: linear-gradient(135deg, #4facfe, #00f2fe);
        color: white;
        border-radius: 30px;
        padding: 0.45rem 0.9rem;
        margin: 0.25rem 0.3rem 0.25rem 0;
        font-size: 0.82rem;
        font-weight: 700;
    }
    .resume-cert-chip {
        display: inline-block;
        background: linear-gradient(135deg, #43e97b, #38f9d7);
        color: #12332a;
        border-radius: 30px;
        padding: 0.4rem 0.85rem;
        margin: 0.25rem 0.3rem 0.25rem 0;
        font-size: 0.8rem;
        font-weight: 700;
    }

    /* ==================== AI CAREER CHATBOT ==================== */
    .chatbot-intro-card {
        box-sizing: border-box;
        background: rgba(247, 250, 252, 0.9);
        border-radius: 20px;
        padding: 1rem 1.2rem;
        margin: 0 0 var(--space-md) 0;
        border-left: 5px solid #667eea;
        backdrop-filter: blur(4px);
        font-size: 0.88rem;
    }
    div[data-testid="stChatMessage"] {
        background: rgba(255, 255, 255, 0.85);
        border-radius: 18px;
        backdrop-filter: blur(6px);
        border: 1px solid rgba(102,126,234,0.15);
        box-shadow: 0 8px 18px -12px rgba(0,0,0,0.18);
    }
    .stChatInput textarea, div[data-testid="stChatInput"] textarea {
        border-radius: 20px !important;
    }
    .chatbot-suggested-label {
        font-weight: 700;
        font-size: 0.85rem;
        color: #667eea;
        margin: 0.6rem 0 0.4rem 0;
    }
    .st-key-chatbot_suggested_row .stButton > button {
        background: rgba(102,126,234,0.1);
        color: #4a4a8a;
        border: 1px solid rgba(102,126,234,0.3);
        font-weight: 600;
        font-size: 0.8rem;
        padding: 0.5rem 0.9rem;
        white-space: normal;
        height: auto;
    }
    .st-key-chatbot_suggested_row .stButton > button:hover {
        background: rgba(102,126,234,0.22);
        color: #2d2d44;
        transform: translateY(-2px);
        box-shadow: 0 8px 16px -8px rgba(102,126,234,0.4);
    }
    .st-key-chatbot_clear_row .stButton > button {
        background: linear-gradient(135deg, #eb3349, #f45c43);
    }
    .typing-indicator {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 0.3rem 0;
    }
    .typing-indicator span {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: #764ba2;
        animation: typing-bounce 1.2s infinite ease-in-out;
    }
    .typing-indicator span:nth-child(2) { animation-delay: 0.2s; }
    .typing-indicator span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes typing-bounce {
        0%, 60%, 100% { transform: translateY(0); opacity: 0.5; }
        30% { transform: translateY(-6px); opacity: 1; }
    }

    /* ==================== RESPONSIVE: TABLET / LAPTOP ==================== */
    @media (max-width: 1024px) {
        .app-title { font-size: 2.1rem; }
        .welcome-heading { font-size: 1.7rem; }
        .main-card { padding: 1.6rem; }
    }
    @media (max-width: 768px) {
        .main-card { padding: 1.2rem; border-radius: 20px; }
        .welcome-heading { font-size: 1.4rem; }
        .app-title { font-size: 1.8rem; }
        .rec-card { height: auto; min-height: 260px; }
        .rec-card p { -webkit-line-clamp: 6; }
    }

</style>
""", unsafe_allow_html=True)

# Initialize session state
if 'page' not in st.session_state:
    st.session_state.page = 'welcome'
if 'language' not in st.session_state:
    st.session_state.language = "English (Default)"
if 'user_type' not in st.session_state:
    st.session_state.user_type = None
if 'student_city' not in st.session_state:
    st.session_state.student_city = ""
if 'student_state' not in st.session_state:
    st.session_state.student_state = ""
if 'responses' not in st.session_state:
    st.session_state.responses = {}
if 'current_page' not in st.session_state:
    st.session_state.current_page = 0
if 'questions_list' not in st.session_state:
    st.session_state.questions_list = []
if 'categories_data' not in st.session_state:
    st.session_state.categories_data = {}
if 'recommended_categories' not in st.session_state:
    st.session_state.recommended_categories = []
if 'selected_stream' not in st.session_state:
    st.session_state.selected_stream = None
if 'selected_stream_data' not in st.session_state:
    st.session_state.selected_stream_data = None
if 'student_name' not in st.session_state:
    st.session_state.student_name = ""
if 'student_age' not in st.session_state:
    st.session_state.student_age = ""
if 'student_institution' not in st.session_state:
    st.session_state.student_institution = ""
if 'student_grade' not in st.session_state:
    st.session_state.student_grade = ""
if 'show_about' not in st.session_state:
    st.session_state.show_about = False
if 'show_contact' not in st.session_state:
    st.session_state.show_contact = False
if 'selected_career' not in st.session_state:
    st.session_state.selected_career = None
if 'show_career_detail' not in st.session_state:
    st.session_state.show_career_detail = False
if 'personality_responses' not in st.session_state:
    st.session_state.personality_responses = {}
if 'personality_current_index' not in st.session_state:
    st.session_state.personality_current_index = 0
if 'personality_questions' not in st.session_state:
    st.session_state.personality_questions = []
if 'personality_completed' not in st.session_state:
    st.session_state.personality_completed = False
if 'personality_pathway' not in st.session_state:
    st.session_state.personality_pathway = None
# ---- RIASEC AI Career Assessment state ----
# Entirely separate from the VAK-style personality/learning-style quiz
# above. riasec_scores holds ONLY the raw arithmetic Likert scores per
# Holland-code dimension (R/I/A/S/E/C) - no career/description/strength
# text is ever hardcoded here. All of that (riasec_result: personality
# type, description, top matching careers, match percentages, strengths,
# learning style, suitable work environment) is generated fresh by
# Gemini for every student, cached by response-fingerprint exactly like
# the other AI content in this app (see AI CONTENT CACHING POLICY below).
if 'riasec_questions' not in st.session_state:
    st.session_state.riasec_questions = []
if 'riasec_responses' not in st.session_state:
    st.session_state.riasec_responses = {}
if 'riasec_current_page' not in st.session_state:
    st.session_state.riasec_current_page = 0
if 'riasec_completed' not in st.session_state:
    st.session_state.riasec_completed = False
if 'riasec_scores' not in st.session_state:
    st.session_state.riasec_scores = None
if 'riasec_result' not in st.session_state:
    st.session_state.riasec_result = None
if 'riasec_result_status' not in st.session_state:
    st.session_state.riasec_result_status = None
if 'riasec_result_error' not in st.session_state:
    st.session_state.riasec_result_error = None
if 'riasec_result_fingerprint' not in st.session_state:
    st.session_state.riasec_result_fingerprint = None
if 'ai_recommendation' not in st.session_state:
    st.session_state.ai_recommendation = None
if 'ai_top_streams' not in st.session_state:
    st.session_state.ai_top_streams = None
if 'ai_analysis' not in st.session_state:
    st.session_state.ai_analysis = None
if 'ai_analysis_status' not in st.session_state:
    st.session_state.ai_analysis_status = None
if 'ai_deep_dive' not in st.session_state:
    st.session_state.ai_deep_dive = None
if 'ai_deep_dive_status' not in st.session_state:
    st.session_state.ai_deep_dive_status = None
if 'selected_career_role' not in st.session_state:
    st.session_state.selected_career_role = None
if 'ai_role_detail' not in st.session_state:
    st.session_state.ai_role_detail = None
if 'ai_role_detail_status' not in st.session_state:
    st.session_state.ai_role_detail_status = None
# ---- AI Help Center state ----
if 'help_return_page' not in st.session_state:
    st.session_state.help_return_page = 'welcome'
if 'ai_help_guide' not in st.session_state:
    st.session_state.ai_help_guide = None
if 'ai_help_guide_status' not in st.session_state:
    st.session_state.ai_help_guide_status = None
if 'help_search_query' not in st.session_state:
    st.session_state.help_search_query = ""
if 'help_search_answer' not in st.session_state:
    st.session_state.help_search_answer = None
if 'help_search_status' not in st.session_state:
    st.session_state.help_search_status = None
# ==================== AI CONTENT CACHING POLICY ====================
# Every piece of AI-generated content in this app is stored in
# st.session_state and is treated as a CACHE, not regenerated on every
# Streamlit rerun/page navigation. A fresh Gemini call is made ONLY when:
#   (a) the underlying questionnaire (and, where relevant, personality)
#       responses change, or
#   (b) the user selects a different career/stream/role.
# This is enforced by comparing a content-hash "fingerprint" of the
# relevant inputs (make_response_fingerprint) against the fingerprint
# stored alongside the cached result the last time it was generated - if
# they still match, the cached value in session_state is reused as-is and
# NO API call is made. Cached content + fingerprint pairs:
#   - ai_recommendation / ai_top_streams   <-> ai_recommendation_fingerprint
#   - ai_analysis (Strengths/Opportunities) <-> ai_analysis_fingerprint
#   - career_overview (+ related roles)     <-> career_overview_fingerprint
#   - ai_deep_dive (Skills, Education Path,
#     Certifications, Learning Resources,
#     Career Opportunities)                 <-> ai_deep_dive_fingerprint
#   - ai_role_detail (Career Detail page)   <-> ai_role_detail_fingerprint
# Fingerprints used to detect whether the underlying questionnaire/personality
# answers have changed - Gemini is only re-called when the relevant
# fingerprint no longer matches the cached one (caching layer below).
if 'ai_recommendation_fingerprint' not in st.session_state:
    st.session_state.ai_recommendation_fingerprint = None
if 'ai_analysis_fingerprint' not in st.session_state:
    st.session_state.ai_analysis_fingerprint = None
if 'ai_deep_dive_fingerprint' not in st.session_state:
    st.session_state.ai_deep_dive_fingerprint = None
if 'ai_role_detail_fingerprint' not in st.session_state:
    st.session_state.ai_role_detail_fingerprint = None
if 'gemini_response_raw' not in st.session_state:
    st.session_state.gemini_response_raw = None
if 'gemini_response_error' not in st.session_state:
    st.session_state.gemini_response_error = None
# Raw technical error/debug text for the other three Gemini calls, kept
# separate from the user-facing 'message' in *_status so the UI can show a
# friendly message while still offering the technical details inside an
# expandable debug section (never inline in the friendly message itself).
if 'ai_analysis_error' not in st.session_state:
    st.session_state.ai_analysis_error = None
if 'ai_deep_dive_error' not in st.session_state:
    st.session_state.ai_deep_dive_error = None
if 'ai_role_detail_error' not in st.session_state:
    st.session_state.ai_role_detail_error = None
if 'career_overview' not in st.session_state:
    st.session_state.career_overview = None
if 'career_overview_status' not in st.session_state:
    st.session_state.career_overview_status = None
if 'career_overview_error' not in st.session_state:
    st.session_state.career_overview_error = None
if 'career_overview_fingerprint' not in st.session_state:
    st.session_state.career_overview_fingerprint = None
if 'role_detail_return_page' not in st.session_state:
    st.session_state.role_detail_return_page = 'report'

# ---- 12-Month AI Learning Roadmap state ----
# roadmap_career_name holds whichever career the roadmap was requested for
# (either the selected stream's name, or a specific role name drilled into
# from the Career Detail page) - the roadmap is entirely AI-generated for
# this career, never a hardcoded/static plan.
if 'roadmap_career_name' not in st.session_state:
    st.session_state.roadmap_career_name = None
if 'roadmap_return_page' not in st.session_state:
    st.session_state.roadmap_return_page = 'report'
if 'learning_roadmap' not in st.session_state:
    st.session_state.learning_roadmap = None
if 'learning_roadmap_status' not in st.session_state:
    st.session_state.learning_roadmap_status = None
if 'learning_roadmap_error' not in st.session_state:
    st.session_state.learning_roadmap_error = None
if 'learning_roadmap_fingerprint' not in st.session_state:
    st.session_state.learning_roadmap_fingerprint = None

# ---- AI Skill Gap Analysis state ----
# skillgap_career_name holds whichever career the analysis was requested
# for (either the selected stream's name, or a specific role name drilled
# into from the Career Detail page) - the analysis is entirely
# AI-generated for this career (student's current abilities vs required
# industry skills), never a hardcoded/predefined skill list.
if 'skillgap_career_name' not in st.session_state:
    st.session_state.skillgap_career_name = None
if 'skillgap_return_page' not in st.session_state:
    st.session_state.skillgap_return_page = 'report'
if 'skill_gap_analysis' not in st.session_state:
    st.session_state.skill_gap_analysis = None
if 'skill_gap_analysis_status' not in st.session_state:
    st.session_state.skill_gap_analysis_status = None
if 'skill_gap_analysis_error' not in st.session_state:
    st.session_state.skill_gap_analysis_error = None
if 'skill_gap_analysis_fingerprint' not in st.session_state:
    st.session_state.skill_gap_analysis_fingerprint = None

# ---- AI Resume Suggestion state ----
# resume_career_name holds whichever career the suggestions were requested
# for. The suggestions are entirely AI-generated from the student's
# details, the selected career, whatever skills context is available
# (Career Detail's required_skills and/or the Skill Gap Analysis), and
# whatever roadmap context is available (the 12-Month Learning Roadmap) -
# never a static/templated resume or fixed suggestion list. This module
# only ever produces suggestions, never a complete resume.
if 'resume_career_name' not in st.session_state:
    st.session_state.resume_career_name = None
if 'resume_suggestions_return_page' not in st.session_state:
    st.session_state.resume_suggestions_return_page = 'report'
if 'resume_suggestions' not in st.session_state:
    st.session_state.resume_suggestions = None
if 'resume_suggestions_status' not in st.session_state:
    st.session_state.resume_suggestions_status = None
if 'resume_suggestions_error' not in st.session_state:
    st.session_state.resume_suggestions_error = None
if 'resume_suggestions_fingerprint' not in st.session_state:
    st.session_state.resume_suggestions_fingerprint = None

# ---- AI Career Chatbot state ----
# chatbot_messages holds the full conversation as a list of
# {"role": "user"/"assistant", "content": str} dicts, maintained purely in
# Streamlit session state (no external chat DB). Every assistant reply is
# generated live by Gemini for the specific conversation + student
# context - there is no predefined/scripted chatbot response anywhere.
if 'chatbot_messages' not in st.session_state:
    st.session_state.chatbot_messages = []
if 'chatbot_error' not in st.session_state:
    st.session_state.chatbot_error = None
if 'chatbot_suggested_questions' not in st.session_state:
    st.session_state.chatbot_suggested_questions = None
if 'chatbot_suggested_questions_fingerprint' not in st.session_state:
    st.session_state.chatbot_suggested_questions_fingerprint = None
if 'chatbot_return_page' not in st.session_state:
    st.session_state.chatbot_return_page = 'report'
if 'chatbot_pending_message' not in st.session_state:
    st.session_state.chatbot_pending_message = None

# Load JSON files
@st.cache_data
def load_json_file(file_path):
    """Load JSON file with error handling"""
    try:
        full_path = Path(file_path)
        if not full_path.exists():
            st.error(f"{t('messages.error.file_not_found')} {file_path}")
            return None
        with open(full_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Debug info
        total_questions = 0
        for cat in data.get('categories', []):
            # Check for both formats
            if 'questions' in cat:
                total_questions += len(cat['questions'])
            else:
                total_questions += len(cat.get('parent_questions', []))
                total_questions += len(cat.get('subfield_questions', []))
        
        print(f"Loaded {file_path}: {len(data.get('categories', []))} categories, {total_questions} questions")
        return data
    except Exception as e:
        st.error(f"{t('messages.error.error_loading')} {file_path}: {str(e)}")
        return None
        
def extract_questions_from_json(data):
    """Extract questions and categories from JSON format"""
    questions = []
    categories = {}
    
    if not data or 'categories' not in data:
        print("No categories found in data")
        return questions, categories
    
    for category in data.get('categories', []):
        cat_id = category.get('category_id')
        cat_name = category.get('category_name')
        cat_icon = category.get('icon', '📁')
        cat_color = category.get('color', '#6B7280')
        
        categories[cat_id] = {
            'id': cat_id,
            'name': cat_name,
            'icon': cat_icon,
            'color': cat_color,
            'score': 0,
            'question_count': 0,
            'subfield_scores': {}
        }
        
        # IMPORTANT: Use 'questions' key (not 'parent_questions' or 'subfield_questions')
        # This works for both school.json and college.json
        category_questions = category.get('questions', [])
        
        # Also handle legacy format (parent_questions + subfield_questions)
        if not category_questions:
            # Legacy format: combine parent and subfield questions
            parent_qs = category.get('parent_questions', [])
            subfield_qs = category.get('subfield_questions', [])
            category_questions = parent_qs + subfield_qs
        
        print(f"Loading {len(category_questions)} questions for {cat_name}")
        
        for q in category_questions:
            q_id = q.get('id')
            q_text = q.get('text')
            q_weight = q.get('weight', 1.0)
            q_type = q.get('type', 'parent')
            q_subfield = q.get('subfield')
            
            # Skip if missing required fields
            if not q_id or not q_text:
                print(f"Warning: Question missing id or text in {cat_name}")
                continue
            
            # Add to questions list
            questions.append({
                'id': q_id,
                'text': q_text,
                'category_id': cat_id,
                'category_name': cat_name,
                'weight': q_weight,
                'type': q_type,
                'subfield': q_subfield
            })
            
            # Initialize subfield tracking if needed
            if q_subfield and q_subfield != 'null' and q_subfield is not None:
                if q_subfield not in categories[cat_id]['subfield_scores']:
                    categories[cat_id]['subfield_scores'][q_subfield] = {
                        'score': 0,
                        'max_score': 0,
                        'count': 0
                    }
            
            # Increment question count
            categories[cat_id]['question_count'] += 1
    
    print(f"Total questions extracted: {len(questions)}")
    print(f"Total categories: {len(categories)}")
    
    return questions, categories


# MODIFICATION (translated assessment JSON): reusable loaders for the
# school / college / RIASEC question banks. Each one automatically picks
# the JSON file matching st.session_state.language (via
# _get_assessment_language_code()) and silently falls back to the
# English file if the translated file for that language doesn't exist
# on disk yet. Loading itself still goes through load_json_file(), so
# behaviour (caching, error handling, debug logging) is unchanged from
# before - only *which* filename gets loaded is now language-aware.
def load_school_questions():
    """Load the school assessment question bank JSON for the currently
    selected UI language, falling back to school_questions.json
    (English) if the translated file is missing."""
    lang_code = _get_assessment_language_code()
    if lang_code:
        localized_file = f"school_questions.{lang_code}.json"
        if Path(localized_file).exists():
            return load_json_file(localized_file)
    return load_json_file("school_questions.json")


def load_college_questions():
    """Load the college assessment question bank JSON for the currently
    selected UI language, falling back to college_questions.json
    (English) if the translated file is missing."""
    lang_code = _get_assessment_language_code()
    if lang_code:
        localized_file = f"college_questions.{lang_code}.json"
        if Path(localized_file).exists():
            return load_json_file(localized_file)
    return load_json_file("college_questions.json")


@st.cache_data
def _load_riasec_json_file(filename):
    """Read a RIASEC question-bank JSON file from disk by filename,
    falling back to riasec.en.json if that file doesn't exist. Cached
    per filename for the life of the running app."""
    path = Path(__file__).parent / filename
    if not path.exists():
        path = Path(__file__).parent / "riasec.en.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_riasec_questions():
    """Load the RIASEC question bank JSON for the currently selected UI
    language (riasec.en.json / .ta.json / .hi.json / .mr.json), falling
    back to riasec.en.json if the translated file is missing. Unlike
    school/college, RIASEC files always carry an explicit language code
    - English is 'en', never an unsuffixed filename."""
    lang_code = _get_assessment_language_code() or "en"
    return _load_riasec_json_file(f"riasec.{lang_code}.json")


# ==================== RIASEC AI CAREER ASSESSMENT ====================
# Holland's RIASEC model (Realistic, Investigative, Artistic, Social,
# Enterprising, Conventional) is a standard, published psychometric
# framework - the six dimension names/definitions below are that public
# framework, not app-specific hardcoded career data. What IS always
# AI-generated per student (never hardcoded) is: the resulting
# personality type label, its description, the matching careers list,
# match percentages, strengths, learning style, and suitable work
# environment - see generate_riasec_prompt / generate_ai_riasec_assessment.

RIASEC_LIKERT_OPTIONS = ["Strongly Dislike", "Dislike", "Neutral", "Like", "Strongly Like"]
RIASEC_LIKERT_SCORES = {
    "Strongly Dislike": 0,
    "Dislike": 1,
    "Neutral": 2,
    "Like": 3,
    "Strongly Like": 4,
}

RIASEC_DIMENSIONS = {
    "R": "Realistic",
    "I": "Investigative",
    "A": "Artistic",
    "S": "Social",
    "E": "Enterprising",
    "C": "Conventional",
}


def get_riasec_questions(user_type):
    """
    Return the fixed RIASEC Likert-scale questionnaire (5 items per
    dimension x 6 dimensions = 30 items), tagged with their Holland-code
    category so raw dimension scores can be totalled afterwards.

    This is the assessment INSTRUMENT itself (like a standard psychometric
    survey) - it is not a career-outcome mapping. Everything derived from
    the answers (personality type, careers, strengths, etc.) is generated
    fresh by Gemini in generate_ai_riasec_assessment(), never hardcoded.

    The question data itself lives in riasec.<lang>.json (ids, categories
    match what used to be hardcoded here; text is now loaded in whichever
    language is currently selected, via load_riasec_questions()).
    """
    data = load_riasec_questions()
    key = "school" if user_type == "school" else "college"
    questions = data["questions"][key]
    return [{**q, "options": RIASEC_LIKERT_OPTIONS} for q in questions]


def calculate_riasec_scores(responses, questions):
    """
    Pure arithmetic scoring: sums the Likert value of each answered
    question into its RIASEC dimension, then normalises to a 0-100
    percentage per dimension based on the maximum possible score for
    that dimension. Returns the ranked dimensions and a 2-3 letter
    Holland code from the top dimensions. No career/description text is
    produced here - that all comes from Gemini afterwards.
    """
    raw = {k: 0 for k in RIASEC_DIMENSIONS}
    counts = {k: 0 for k in RIASEC_DIMENSIONS}

    questions_by_id = {q['id']: q for q in questions}
    for q_id, answer in responses.items():
        q = questions_by_id.get(q_id)
        if not q or answer not in RIASEC_LIKERT_SCORES:
            continue
        cat = q['category']
        raw[cat] += RIASEC_LIKERT_SCORES[answer]
        counts[cat] += 1

    percentages = {}
    for cat, total in raw.items():
        max_possible = counts[cat] * 4  # 4 = top Likert score ("Strongly Like")
        percentages[cat] = round((total / max_possible) * 100) if max_possible > 0 else 0

    ranked = sorted(percentages.items(), key=lambda x: x[1], reverse=True)
    top_code = "".join(cat for cat, _ in ranked[:3] if percentages[cat] > 0)

    return {
        "raw": raw,
        "percentages": percentages,
        "ranked": ranked,  # list of (category_letter, percentage), highest first
        "ranked_named": [(RIASEC_DIMENSIONS[cat], pct) for cat, pct in ranked],
        "top_code": top_code or (ranked[0][0] if ranked else "R"),
    }


def generate_riasec_prompt(student_details, riasec_scores, user_type):
    """
    Build the prompt for the AI-powered RIASEC Career Assessment. Gemini
    must generate EVERYTHING about the result (personality type,
    description, top matching careers, match percentages, strengths,
    learning style, suitable work environment) fresh for this student -
    no hardcoded type-to-career mapping exists anywhere in this app.
    """
    name = student_details.get('name', 'The student')
    age = student_details.get('age', 'Not specified')
    grade = student_details.get('grade', 'Not specified')
    education_level = "School Student" if user_type == 'school' else "College Student"

    ranked_lines = "\n".join(
        f"- {RIASEC_DIMENSIONS[cat]} ({cat}): {pct}%"
        for cat, pct in riasec_scores['ranked']
    )

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: use SIMPLE, jargon-free language, short "
            "sentences, and everyday examples a teenager would understand. "
            "Explain any technical term in plain words. Frame matching careers "
            "and work environments in an encouraging, exploratory way rather "
            "than assuming specialised knowledge."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: use PROFESSIONAL, industry-appropriate "
            "language. Reference specific job roles, specialisations, tools, "
            "or industry context where relevant, and give more advanced, "
            "nuanced career guidance suited to someone entering the workforce."
        )

    prompt = f"""You are an expert occupational psychologist AI specialising in the RIASEC
(Holland Code) career interest model - Realistic, Investigative, Artistic,
Social, Enterprising, Conventional.

STUDENT INFORMATION
- Name: {name}
- Age: {age}
- Grade/Year: {grade}
- Education Level: {education_level}

RIASEC ASSESSMENT RESULTS (0-100 scale per dimension, from the student's own
Likert-scale answers to a 30-item RIASEC questionnaire)
{ranked_lines}

TASK
Based ONLY on the scores above, generate a completely fresh, personalized
RIASEC career assessment result for this student. Do NOT use any
pre-existing/manual RIASEC-to-career database - reason about the scores
yourself and generate everything dynamically. {depth_instruction}

Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly
this shape:

{{
  "riasec_code": "2-3 letter Holland code built from the student's strongest dimensions, e.g. IAS",
  "personality_type": "a short, descriptive personality-type title reflecting the dominant dimensions, e.g. 'The Analytical Creator'",
  "description": "2-4 sentences describing what this RIASEC profile means for this specific student, personalized using their actual scores",
  "top_matching_careers": [
    {{"career_name": "string", "match_percentage": 0, "why_it_fits": "one or two sentence explanation tied to this student's scores"}}
  ],
  "strengths": ["string", "string", "string"],
  "learning_style": "2-3 sentences on how this student likely learns best, derived from their RIASEC profile",
  "suitable_work_environment": "2-3 sentences describing the kind of work environment/culture that would suit this student"
}}

Requirements:
- top_matching_careers must contain 5 to 7 careers, ordered from highest to lowest match_percentage (integers 0-100), each genuinely justified by the scores above - not generic/copy-pasted across students.
- strengths must contain 4 to 6 concise bullet-point strings.
- Every string value must be a single line with no literal line breaks (use spaces instead).
- Output JSON only, nothing else."""

    return prompt


def _validate_riasec_schema(data):
    """
    Schema validator for the RIASEC assessment response, passed into
    generate_validated_json so a structurally-valid-but-incomplete
    response is treated the same as a JSON parse failure and triggers a
    single automatic retry instead of being silently accepted.
    """
    if not isinstance(data, dict):
        raise ValueError("RIASEC response is not a JSON object.")

    riasec_code = str(data.get("riasec_code", "")).strip()
    personality_type = str(data.get("personality_type", "")).strip()
    description = str(data.get("description", "")).strip()
    learning_style = str(data.get("learning_style", "")).strip()
    suitable_work_environment = str(data.get("suitable_work_environment", "")).strip()
    careers = data.get("top_matching_careers", [])
    strengths = data.get("strengths", [])

    if not personality_type or not description:
        raise ValueError("RIASEC response missing personality_type/description.")
    if not isinstance(careers, list) or len(careers) == 0:
        raise ValueError("RIASEC response missing top_matching_careers.")
    if not isinstance(strengths, list) or len(strengths) == 0:
        raise ValueError("RIASEC response missing strengths.")

    cleaned_careers = []
    for c in careers:
        if not isinstance(c, dict):
            continue
        career_name = str(c.get("career_name", "")).strip()
        if not career_name:
            continue
        try:
            match_percentage = int(round(float(c.get("match_percentage", 0))))
        except (TypeError, ValueError):
            match_percentage = 0
        match_percentage = max(0, min(100, match_percentage))
        cleaned_careers.append({
            "career_name": career_name,
            "match_percentage": match_percentage,
            "why_it_fits": str(c.get("why_it_fits", "")).strip(),
        })

    if not cleaned_careers:
        raise ValueError("RIASEC response had no usable career entries.")

    cleaned_careers.sort(key=lambda c: c["match_percentage"], reverse=True)

    return {
        "riasec_code": riasec_code,
        "personality_type": personality_type,
        "description": description,
        "top_matching_careers": cleaned_careers,
        "strengths": [str(s).strip() for s in strengths if str(s).strip()],
        "learning_style": learning_style,
        "suitable_work_environment": suitable_work_environment,
    }


def generate_ai_riasec_assessment(student_details, riasec_scores, user_type):
    """
    Call Gemini to produce the full RIASEC Career Assessment result and
    store the parsed result in st.session_state.riasec_result.

    Follows the exact same reliability pattern as generate_ai_analysis():
    generate_validated_json retries once on parse/schema failure, never
    calls json.loads() on unchecked text, and technical error detail is
    kept separate (riasec_result_error) from the friendly UI message.
    """
    prompt = generate_riasec_prompt(student_details, riasec_scores, user_type)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.riasec_result = None
        st.session_state.riasec_result_error = str(e)
        return {"status": "error", "message": _t("The RIASEC AI assessment is temporarily unavailable. Please try again later.")}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=2048,
            label="generate_ai_riasec_assessment",
            validator=_validate_riasec_schema,
        )

        if data is None:
            st.session_state.riasec_result = None
            st.session_state.riasec_result_error = (
                "Gemini's response was empty, invalid JSON, or missing required "
                "RIASEC fields, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": _t("Your RIASEC assessment could not be generated right now. Please try again later."),
            }

        st.session_state.riasec_result = data
        st.session_state.riasec_result_error = None
        return {"status": "success", "message": _t("RIASEC assessment generated.")}
    except Exception as e:
        st.session_state.riasec_result = None
        st.session_state.riasec_result_error = str(e)
        return {
            "status": "error",
            "message": _t("Something went wrong while generating your RIASEC assessment. Please try again later."),
        }


def calculate_results(responses, questions_list, categories):
    """Calculate weighted scores for each category"""
    # Reset scores
    for cat_id in categories:
        categories[cat_id]['score'] = 0
        categories[cat_id]['question_count'] = 0
    
    # Calculate weighted scores
    for q_id, score in responses.items():
        for q in questions_list:
            if q['id'] == q_id:
                cat_id = q['category_id']
                weight = q.get('weight', 1.0)
                weighted_score = int(score) * weight
                categories[cat_id]['score'] += weighted_score
                categories[cat_id]['question_count'] += 1
                break
    
    # Calculate percentages
    for cat_id in categories:
        cat = categories[cat_id]
        if cat['question_count'] > 0:
            # Each question max is 5, multiplied by weights
            # Calculate actual max possible based on weights
            total_weight = 0
            for q in questions_list:
                if q['category_id'] == cat_id:
                    total_weight += q.get('weight', 1.0)
            max_score = total_weight * 5
            
            if max_score > 0:
                cat['score'] = (cat['score'] / max_score) * 100
            else:
                cat['score'] = 0
        else:
            cat['score'] = 0
    
    # Sort categories by score and return top 3
    sorted_cats = sorted(categories.items(), key=lambda x: x[1]['score'], reverse=True)
    recommended = [cat[0] for cat in sorted_cats[:3]]
    
    return categories, recommended

# ==================== PDF REPORT CHART GENERATION ====================
# MODIFICATION (PDF charts): shared chart-building helpers used by both
# generate_pdf_report() and generate_role_detail_pdf() to render the
# report's data visualisations (career-match bar chart, skills pie chart,
# career-readiness progress bars, skill-gap progress bars, and the
# learning-roadmap timeline) as brand-colored PNGs, then embed them into
# the PDF inside the same rounded/bordered "card" style used for every
# other section. Nothing here changes any AI recommendation/report data -
# these functions only ever plot numbers that are already being rendered
# elsewhere in the PDF as text.

# Brand palette (kept in sync with the Dark Blue / Cyan / White tokens
# used throughout generate_pdf_report() and generate_role_detail_pdf()).
_CHART_DARK_BLUE = "#0D1B3E"
_CHART_CYAN = "#22D3EE"
_CHART_CYAN_SOFT = "#7DE6F5"
_CHART_TRACK = "#E7F6FA"      # light track/background for progress bars
_CHART_GRID = "#E3EEF2"
_CHART_LABEL = "#445C82"      # muted label text, matches LABEL_MUTED
_CHART_TEXT = "#0D1B3E"

# Difficulty / importance color buckets - dark blue for the most
# demanding/urgent items, cyan for the lightest, so the chart reads
# consistently with the rest of the brand-colored report.
_CHART_LEVEL_COLORS = {
    "high": _CHART_DARK_BLUE, "hard": _CHART_DARK_BLUE, "difficult": _CHART_DARK_BLUE,
    "medium": "#1C7FA8", "moderate": "#1C7FA8", "intermediate": "#1C7FA8",
    "low": _CHART_CYAN, "easy": _CHART_CYAN, "beginner": _CHART_CYAN,
}


def _chart_level_color(label):
    return _CHART_LEVEL_COLORS.get(str(label or "").strip().lower(), _CHART_CYAN)


# MODIFICATION (i18n / PDF Unicode fonts): the PDF report used to call
# pdf.set_font('helvetica', ...) everywhere and rely on the built-in
# core "Helvetica" font. Core Helvetica only covers Latin-1, so any
# Tamil, Hindi, or Marathi text (and non-Latin-1 characters in English
# text) raised: 'Character ... is outside the range of characters
# supported by the font "Helvetica"'.
#
# Fix: register real, modern Unicode font families per script - "Noto
# Sans" for English, "Noto Sans Tamil" for Tamil, "Noto Sans Devanagari"
# for Hindi & Marathi - auto-selected from the current UI language, then
# monkey-patch set_font on this pdf instance so every existing
# pdf.set_font('helvetica', ...) call throughout the report transparently
# resolves to the correct Noto family for the active language. No call
# site, layout, report content, colors, branding, or translation logic
# changes - only which font file backs "helvetica" changes.
#
# Each language keeps its own registered font family (rather than sharing
# one generic family name) so Tamil always renders with the Tamil-shaped
# glyphs of Noto Sans Tamil - never with the Devanagari or Latin font -
# and likewise for Hindi/Marathi and English.
#
# REQUIRED ASSET (place alongside app.py / logo.png), per family:
#   <Family>-Regular.ttf   e.g. NotoSans-Regular.ttf
# OPTIONAL, for true (non-faked) bold/italic instead of reusing Regular:
#   <Family>-Bold.ttf, <Family>-Italic.ttf, <Family>-BoldItalic.ttf
_PDF_FONT_FAMILY_BY_LANGUAGE = {
    "English (Default)": "NotoSans",
    "English": "NotoSans",
    "Hindi": "NotoSansDevanagari",
    "Marathi": "NotoSansDevanagari",
    "Tamil": "NotoSansTamil",
}
_PDF_FONT_STYLE_SUFFIX = {'': 'Regular', 'B': 'Bold', 'I': 'Italic', 'BI': 'BoldItalic'}


def _resolve_pdf_font_files(family_base):
    """Return {style: file_path} for the given font family base name
    (e.g. 'NotoSansTamil'), using real Bold/Italic/BoldItalic files where
    present and transparently falling back to the Regular file for any
    style whose dedicated file isn't shipped. Returns None if even the
    Regular file is missing."""
    regular_path = f"{family_base}-Regular.ttf"
    if not os.path.exists(regular_path):
        return None
    return {
        style: (f"{family_base}-{suffix}.ttf" if os.path.exists(f"{family_base}-{suffix}.ttf") else regular_path)
        for style, suffix in _PDF_FONT_STYLE_SUFFIX.items()
    }


def _register_pdf_font(pdf):
    lang = st.session_state.get("language", "English (Default)")
    # Always resolve to a Noto Sans variant - English included - never to
    # helvetica.ttf/built-in Helvetica, with NotoSans as the safety-net
    # both for unrecognized languages and if a script-specific family is
    # missing on disk.
    preferred_family = _PDF_FONT_FAMILY_BY_LANGUAGE.get(lang, "NotoSans")

    for family in (preferred_family, "NotoSans"):
        font_files = _resolve_pdf_font_files(family)
        if not font_files:
            continue
        try:
            for style, path in font_files.items():
                pdf.add_font(family, style, path)

            _base_set_font = pdf.set_font

            def _unicode_aware_set_font(fam, style='', size=0, *args, __family=family, **kwargs):
                if fam == 'helvetica':
                    fam = __family
                return _base_set_font(fam, style, size, *args, **kwargs)

            pdf.set_font = _unicode_aware_set_font
            pdf.set_font('helvetica', '', 12)

            # Enable HarfBuzz-based text shaping (fpdf2's optional
            # 'uharfbuzz' dependency) when available, so complex scripts -
            # Tamil vowel-sign reordering/conjuncts, Devanagari ligatures -
            # get correct glyph shaping and spacing instead of naive
            # one-glyph-per-codepoint placement. No-ops silently if
            # 'uharfbuzz' isn't installed; text still renders correctly via
            # Noto's own cmap, just without shaping refinements.
            try:
                pdf.set_text_shaping(True)
            except Exception:
                pass

            return
        except Exception:
            continue

    # Last-resort only: no Noto font file found on disk at all (assets not
    # deployed) - fall back to built-in Helvetica so the app doesn't hard
    # crash, though non-Latin-1 text will then fail again as before until
    # the required assets above are added.
    pdf.set_font('helvetica', '', 12)


# MODIFICATION (i18n): matplotlib's default font (DejaVu Sans) has no
# Devanagari (Hindi/Marathi) or Tamil glyphs, so translated chart text
# would render as missing-glyph boxes even though chart_* labels are now
# passed through _t(). Register the same local Noto TTFs used for the PDF
# body text with matplotlib's font manager (so chart labels match even if
# the fonts aren't installed system-wide), then pick the family for the
# *currently selected* language first, falling back through the other
# scripts and finally to matplotlib's default so English/Latin-script
# charts are unaffected. This only affects PNG chart images - it does not
# touch FPDF text.
def _configure_chart_font():
    if not MATPLOTLIB_AVAILABLE:
        return
    try:
        from matplotlib import font_manager

        for local_file in (
            "NotoSans-Regular.ttf",
            "NotoSansTamil-Regular.ttf",
            "NotoSansDevanagari-Regular.ttf",
        ):
            if os.path.exists(local_file):
                try:
                    font_manager.fontManager.addfont(local_file)
                except Exception:
                    pass

        available = {f.name for f in font_manager.fontManager.ttflist}
        lang = st.session_state.get("language", "English (Default)")
        lang_priority = {
            "Tamil": ["Noto Sans Tamil"],
            "Hindi": ["Noto Sans Devanagari"],
            "Marathi": ["Noto Sans Devanagari"],
        }
        # Script matching the active language wins first, then the other
        # Unicode fallbacks, so chart text always matches the language the
        # PDF was generated in rather than an arbitrary fixed order.
        ordered = lang_priority.get(lang, []) + [
            "Noto Sans", "Noto Sans Devanagari", "Noto Sans Tamil",
            "Mangal", "Nirmala UI", "Lohit Devanagari", "Lohit Tamil",
        ]
        for name in ordered:
            if name in available:
                plt.rcParams["font.family"] = name
                return
    except Exception:
        pass


def _new_chart_path():
    import tempfile
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    f.close()
    return f.name


def _style_chart_axes(ax, fig):
    """Common cosmetic cleanup shared by every chart: white background, no
    top/right/left spines, muted gridlines, brand-colored ticks."""
    _configure_chart_font()
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(_CHART_GRID)
    ax.tick_params(colors=_CHART_LABEL, labelsize=9)


def chart_top_career_matches(items, fig_w_in):
    """Horizontal bar chart of the top career/stream match percentages.
    items: list of (label, match_percentage) tuples, any order - this
    function sorts descending and keeps at most the top 7 so the chart
    stays readable. Returns (png_path, fig_height_in) or None if
    matplotlib isn't available or there's no usable data.
    """
    if not MATPLOTLIB_AVAILABLE or not items:
        return None
    items = [(str(label), float(pct)) for label, pct in items if str(label).strip()]
    if not items:
        return None
    items.sort(key=lambda x: x[1], reverse=True)
    items = items[:7]
    items = items[::-1]  # matplotlib barh draws bottom-up; reverse so #1 is on top

    labels = [lbl if len(lbl) <= 34 else lbl[:31] + "..." for lbl, _ in items]
    values = [pct for _, pct in items]
    colors = [_CHART_DARK_BLUE if i == len(items) - 1 else _CHART_CYAN for i in range(len(items))]

    fig_h_in = max(1.6, 0.5 * len(items) + 1.0)
    fig, ax = plt.subplots(figsize=(fig_w_in, fig_h_in))
    _style_chart_axes(ax, fig)

    bars = ax.barh(labels, values, color=colors, height=0.55, zorder=3)
    ax.set_xlim(0, 108)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.xaxis.grid(True, color=_CHART_GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for bar, val in zip(bars, values):
        ax.text(val + 1.5, bar.get_y() + bar.get_height() / 2, f"{val:.0f}%",
                 va="center", ha="left", fontsize=9, color=_CHART_TEXT, fontweight="bold")

    fig.tight_layout(pad=0.6)
    path = _new_chart_path()
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return path, fig_h_in


def chart_skills_pie(tech_count, soft_count, fig_w_in):
    """Pie chart comparing the number of Technical Skills vs Soft Skills
    listed for the recommended career. Returns (png_path, fig_height_in)
    or None if matplotlib isn't available or both counts are zero."""
    if not MATPLOTLIB_AVAILABLE or (tech_count <= 0 and soft_count <= 0):
        return None

    _configure_chart_font()
    fig_h_in = round(fig_w_in * 0.5, 2)
    fig, ax = plt.subplots(figsize=(fig_w_in, fig_h_in))
    fig.patch.set_facecolor("white")

    values = [tech_count, soft_count]
    labels = [f"{_t('Technical Skills')} ({tech_count})", f"{_t('Soft Skills')} ({soft_count})"]
    colors = [_CHART_DARK_BLUE, _CHART_CYAN]

    wedges, _texts, autotexts = ax.pie(
        values,
        colors=colors,
        autopct=lambda p: f"{p:.0f}%" if p > 0 else "",
        startangle=90,
        pctdistance=0.75,
        wedgeprops=dict(width=0.42, edgecolor="white", linewidth=2),
        textprops=dict(color="white", fontsize=10, fontweight="bold"),
    )
    ax.legend(wedges, labels, loc="center left", bbox_to_anchor=(1.0, 0.5),
              frameon=False, fontsize=9.5, labelcolor=_CHART_TEXT)
    ax.set_aspect("equal")

    fig.tight_layout(pad=0.6)
    path = _new_chart_path()
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return path, fig_h_in


def chart_career_readiness(overall_score, strengths, fig_w_in):
    """Progress-bar style chart for AI Skill Gap Analysis: one headline bar
    for the overall readiness score, then one bar per current-strength
    skill showing its proficiency score. strengths: list of
    (skill_name, proficiency_score). Returns (png_path, fig_height_in) or
    None if matplotlib isn't available / there's nothing to plot."""
    if not MATPLOTLIB_AVAILABLE:
        return None
    strengths = [(str(n), float(s)) for n, s in (strengths or []) if str(n).strip()]
    try:
        overall_score = float(overall_score)
    except (TypeError, ValueError):
        overall_score = None
    if overall_score is None and not strengths:
        return None

    rows = []
    if overall_score is not None:
        rows.append((_t("Overall Career Readiness"), overall_score, True))
    for name, score in strengths[:8]:
        label = name if len(name) <= 30 else name[:27] + "..."
        rows.append((label, max(0.0, min(100.0, score)), False))
    rows = rows[::-1]

    fig_h_in = max(1.6, 0.52 * len(rows) + 1.0)
    fig, ax = plt.subplots(figsize=(fig_w_in, fig_h_in))
    _style_chart_axes(ax, fig)

    labels = [r[0] for r in rows]
    values = [r[1] for r in rows]
    y_pos = range(len(rows))

    # Light "track" bars (full-length background) with the actual score
    # drawn on top, giving each row a classic progress-bar look.
    ax.barh(y_pos, [100] * len(rows), color=_CHART_TRACK, height=0.5, zorder=2)
    bar_colors = [_CHART_DARK_BLUE if r[2] else _CHART_CYAN for r in rows]
    bars = ax.barh(y_pos, values, color=bar_colors, height=0.5, zorder=3)

    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(labels)
    ax.set_xlim(0, 108)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.xaxis.grid(True, color=_CHART_GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)

    for bar, val, is_overall in zip(bars, values, [r[2] for r in rows]):
        ax.text(val + 1.5, bar.get_y() + bar.get_height() / 2, f"{val:.0f}%",
                 va="center", ha="left", fontsize=9,
                 color=_CHART_TEXT, fontweight="bold" if is_overall else "normal")

    fig.tight_layout(pad=0.6)
    path = _new_chart_path()
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return path, fig_h_in


def chart_skill_gap(items, fig_w_in):
    """Horizontal bar chart visualising the AI Skill Gap Analysis' learning
    difficulty per missing skill. items: list of
    (skill_name, difficulty_score, difficulty_label). Bars are colored by
    difficulty level (dark blue = hardest, cyan = easiest) so the chart
    doubles as a quick priority-reading aid. Returns (png_path,
    fig_height_in) or None."""
    if not MATPLOTLIB_AVAILABLE or not items:
        return None
    cleaned = []
    for name, score, level in items:
        name = str(name).strip()
        if not name:
            continue
        try:
            score = max(0.0, min(100.0, float(score)))
        except (TypeError, ValueError):
            continue
        cleaned.append((name, score, level))
    if not cleaned:
        return None
    cleaned = cleaned[:10][::-1]

    labels = [n if len(n) <= 30 else n[:27] + "..." for n, _, _ in cleaned]
    values = [s for _, s, _ in cleaned]
    colors = [_chart_level_color(lvl) for _, _, lvl in cleaned]

    fig_h_in = max(1.6, 0.5 * len(cleaned) + 1.0)
    fig, ax = plt.subplots(figsize=(fig_w_in, fig_h_in))
    _style_chart_axes(ax, fig)

    y_pos = range(len(cleaned))
    ax.barh(y_pos, [100] * len(cleaned), color=_CHART_TRACK, height=0.55, zorder=2)
    bars = ax.barh(y_pos, values, color=colors, height=0.55, zorder=3)
    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(labels)
    ax.set_xlim(0, 108)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel(_t("Learning Difficulty (%)"), color=_CHART_LABEL, fontsize=9)
    ax.xaxis.grid(True, color=_CHART_GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)

    for bar, val in zip(bars, values):
        ax.text(val + 1.5, bar.get_y() + bar.get_height() / 2, f"{val:.0f}%",
                 va="center", ha="left", fontsize=9, color=_CHART_TEXT, fontweight="bold")

    # Small legend explaining the color coding.
    from matplotlib.patches import Patch
    legend_handles = [
        Patch(facecolor=_CHART_DARK_BLUE, label=_t("High / Hard")),
        Patch(facecolor="#1C7FA8", label=_t("Medium")),
        Patch(facecolor=_CHART_CYAN, label=_t("Low / Easy")),
    ]
    ax.legend(handles=legend_handles, loc="lower right", frameon=False, fontsize=8, labelcolor=_CHART_TEXT)

    fig.tight_layout(pad=0.6)
    path = _new_chart_path()
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return path, fig_h_in


def chart_roadmap_timeline(months, fig_w_in):
    """Horizontal timeline visualising the 12-Month Learning Roadmap:
    one marker per month, connected by a line, labeled with the month's
    title underneath (rotated to avoid overlap). months: list of
    (month_number, month_title). Returns (png_path, fig_height_in) or
    None if matplotlib isn't available or there are no months."""
    if not MATPLOTLIB_AVAILABLE or not months:
        return None
    months = [(n, str(t)) for n, t in months if str(t).strip() or n]
    if not months:
        return None

    _configure_chart_font()
    n = len(months)
    xs = list(range(n))
    fig_h_in = 2.5
    fig, ax = plt.subplots(figsize=(fig_w_in, fig_h_in))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_yticks([])

    ax.plot(xs, [0] * n, color=_CHART_CYAN_SOFT, linewidth=3, zorder=1, solid_capstyle="round")
    colors = [_CHART_DARK_BLUE if i == 0 or i == n - 1 else _CHART_CYAN for i in range(n)]
    ax.scatter(xs, [0] * n, s=140, color=colors, zorder=3, edgecolors="white", linewidths=1.5)

    for i, (num, title) in enumerate(months):
        label = title if len(title) <= 16 else title[:14] + "..."
        ax.text(i, 0.16, f"M{num}", ha="center", va="bottom", fontsize=8.5,
                 fontweight="bold", color=_CHART_TEXT)
        ax.text(i, -0.16, label, ha="right", va="top", fontsize=7.5,
                 color=_CHART_LABEL, rotation=40, rotation_mode="anchor")

    ax.set_xlim(-0.6, n - 0.4)
    ax.set_ylim(-0.85, 0.4)

    fig.tight_layout(pad=0.6)
    path = _new_chart_path()
    fig.savefig(path, dpi=170)
    plt.close(fig)
    return path, fig_h_in


def draw_chart_card(pdf, ensure_space, draw_title_bar, content_width, CONTENT_MARGIN,
                     PDF_WHITE, CARD_BORDER, title, chart_result, fig_w_in):
    """Shared card renderer for every chart: a title bar (reusing the same
    draw_title_bar closure as every other section) followed by a rounded,
    bordered white card containing the chart image, sized so it can never
    overlap surrounding text or the footer (uses the same ensure_space
    page-break logic as every text/bullet/kv card).

    chart_result is the (img_path, fig_h_in) tuple returned by the
    chart_* functions above, generated with figure width fig_w_in - the
    same fig_w_in must be passed here so the image is placed at its
    native aspect ratio (no stretching/squashing).
    """
    if not chart_result:
        return
    img_path, fig_h_in = chart_result
    pad = 6
    inner_w_mm = content_width() - 2 * pad
    img_h_mm = inner_w_mm * (fig_h_in / fig_w_in)
    card_h = img_h_mm + 2 * pad
    ensure_space(9 + 4 + card_h + 8)
    draw_title_bar(title)
    x = CONTENT_MARGIN
    y = pdf.get_y()
    w = content_width()
    pdf.set_fill_color(*PDF_WHITE)
    pdf.set_draw_color(*CARD_BORDER)
    pdf.set_line_width(0.3)
    pdf.rect(x, y, w, card_h, style='DF', round_corners=True, corner_radius=2.2)
    pdf.image(img_path, x=x + pad, y=y + pad, w=inner_w_mm, h=img_h_mm)
    pdf.set_xy(x, y + card_h + 8)


def generate_pdf_report():
    """Generate PDF report for download with proper Unicode support"""
    
    from fpdf import FPDF
    import tempfile
    import os
    import re
    
    stream = st.session_state.get('selected_stream_data') or {}
    stream_name = stream.get('stream_name', 'Selected Stream')
    score_value = stream.get('match_percentage', 0)
    # MODIFICATION (i18n): translated display copy of the stream name for
    # every place it's rendered in the PDF; the raw `stream_name` is kept
    # untouched since it's also passed into generate_top10_related_roles()
    # below for AI lookups, which must stay in the original English.
    stream_name_t = _t(stream_name) if stream_name else stream_name

    recommendation = {
        "status": "success",
        "message": stream.get('explanation', 'AI recommendations have not been generated yet.'),
    }

    # MODIFICATION (PDF charts): source data for the "Top Career Match
    # Percentages" bar chart - prefer the RIASEC assessment's per-career
    # matches (richer, up to 7 careers) since that's the most granular
    # match data available; fall back to the AI's top-3 recommended
    # streams if RIASEC hasn't been completed this session. Either way,
    # this only reuses match_percentage values already generated and
    # shown elsewhere in the app - no scoring/recommendation logic here.
    riasec_result_for_chart = st.session_state.get('riasec_result') or {}
    top_matches_source = riasec_result_for_chart.get('top_matching_careers') or []
    if top_matches_source:
        top_match_items = [
            (c.get('career_name', ''), c.get('match_percentage', 0))
            for c in top_matches_source if c.get('career_name')
        ]
    else:
        top_streams_source = st.session_state.get('ai_top_streams') or []
        top_match_items = [
            (s.get('stream_name', ''), s.get('match_percentage', 0))
            for s in top_streams_source if s.get('stream_name')
        ]

    # ------------------------------------------------------------------
    # Shared design tokens for every content page (page 2 onward).
    # Dark Blue / Cyan / White theme, consistent with the cover page.
    # ------------------------------------------------------------------
    DARK_BLUE = (13, 27, 62)
    CYAN = (34, 211, 238)
    CYAN_SOFT = (125, 230, 245)
    PDF_WHITE = (255, 255, 255)
    BODY_TEXT = (30, 41, 63)
    LABEL_MUTED = (68, 92, 130)
    CARD_BORDER = (204, 234, 241)
    CONTENT_MARGIN = 14

    # Custom PDF class with Unicode support, a branded header bar, and a
    # branded footer (CoActions + page numbers) on every content page.
    class PDF(FPDF):
        def header(self):
            # Cover page (page 1) has its own full-page design; skip here.
            if self.page_no() == 1:
                return
            self.set_fill_color(*DARK_BLUE)
            self.rect(0, 0, self.w, 10, style='F')
            self.set_fill_color(*CYAN)
            self.rect(0, 10, self.w, 1.1, style='F')

            self.set_xy(CONTENT_MARGIN, 1.6)
            self.set_font('helvetica', 'B', 10.5)
            self.set_text_color(*PDF_WHITE)
            self.cell(90, 6.5, 'CoActions', align='L')

            self.set_xy(0, 2.2)
            self.set_font('helvetica', '', 8)
            self.set_text_color(180, 230, 240)
            self.cell(self.w - CONTENT_MARGIN, 6, _t('AI Career Guidance & Career Counselling System'), align='R')

            self.set_y(20)

        def footer(self):
            # Cover page (page 1) has its own footer tagline drawn manually,
            # so skip the generic footer there.
            if self.page_no() == 1:
                return
            self.set_draw_color(*CYAN)
            self.set_line_width(0.4)
            self.line(CONTENT_MARGIN, self.h - 18, self.w - CONTENT_MARGIN, self.h - 18)

            self.set_xy(CONTENT_MARGIN, self.h - 16)
            self.set_font('helvetica', 'B', 8)
            self.set_text_color(*DARK_BLUE)
            self.cell(100, 4.6, 'CoActions', align='L')

            self.set_xy(CONTENT_MARGIN, self.h - 11.4)
            self.set_font('helvetica', 'I', 7)
            self.set_text_color(*LABEL_MUTED)
            self.cell(100, 4.4, _t('AI Career Guidance & Career Counselling System'), align='L')

            self.set_xy(0, self.h - 13.6)
            self.set_font('helvetica', '', 8)
            self.set_text_color(*LABEL_MUTED)
            self.cell(self.w - CONTENT_MARGIN, 4.6, f"{_t('Page')} {self.page_no()} {_t('of')} {{nb}}", align='R')

    # ------------------------------------------------------------------
    # Cover page design: Dark Blue / Cyan / White corporate theme.
    # Centered logo, brand + system name, report title, and a student
    # info card (name, type, stream, generated date & time).
    # ------------------------------------------------------------------
    def draw_cover_page(pdf, student_name, student_type_label, career_stream_name):
        DARK_BLUE = (13, 27, 62)
        CYAN = (34, 211, 238)
        CYAN_SOFT = (125, 230, 245)
        WHITE = (255, 255, 255)
        NAVY_TEXT = (13, 27, 62)
        LABEL_GRAY = (90, 100, 130)
        DIVIDER_GRAY = (225, 228, 235)

        page_w = pdf.w
        page_h = pdf.h

        # Cover page must not be affected by the auto page-break logic
        # used elsewhere in the report.
        pdf.set_auto_page_break(False)
        pdf.add_page()

        # Full-page dark blue background
        pdf.set_fill_color(*DARK_BLUE)
        pdf.rect(0, 0, page_w, page_h, style='F')

        # Top and bottom cyan accent bars
        pdf.set_fill_color(*CYAN)
        pdf.rect(0, 0, page_w, 6, style='F')
        pdf.rect(0, page_h - 6, page_w, 6, style='F')

        # Inset cyan frame
        pdf.set_draw_color(*CYAN)
        pdf.set_line_width(0.6)
        margin = 10
        pdf.rect(margin, margin + 6, page_w - 2 * margin, page_h - 2 * margin - 12, style='D')

        # Logo, centered, on a white circular plate for contrast
        logo_size = 34
        logo_x = (page_w - logo_size) / 2
        logo_y = 32
        logo_path = "logo.png"

        if os.path.exists(logo_path):
            plate_r = logo_size / 2 + 6
            plate_cx = page_w / 2
            plate_cy = logo_y + logo_size / 2
            pdf.set_fill_color(*WHITE)
            pdf.ellipse(plate_cx - plate_r, plate_cy - plate_r, plate_r * 2, plate_r * 2, style='F')
            try:
                pdf.image(logo_path, x=logo_x, y=logo_y, w=logo_size, h=logo_size)
            except Exception:
                pass
            text_start_y = logo_y + logo_size + 16
        else:
            text_start_y = 50

        # Brand name
        pdf.set_font('helvetica', 'B', 28)
        pdf.set_text_color(*WHITE)
        pdf.set_y(text_start_y)
        pdf.cell(0, 12, "CoActions", align='C', ln=1)

        # System subtitle
        pdf.set_font('helvetica', '', 12.5)
        pdf.set_text_color(*CYAN_SOFT)
        pdf.ln(1)
        pdf.cell(0, 7, clean_text(_t("AI Career Guidance & Career Counselling System")), align='C', ln=1)

        # Divider
        pdf.ln(6)
        line_w = 46
        line_x = (page_w - line_w) / 2
        pdf.set_draw_color(*CYAN)
        pdf.set_line_width(0.5)
        pdf.line(line_x, pdf.get_y(), line_x + line_w, pdf.get_y())

        # Report title
        pdf.ln(8)
        pdf.set_font('helvetica', 'B', 18)
        pdf.set_text_color(*WHITE)
        pdf.cell(0, 10, clean_text(_t("Personalized Career Report")), align='C', ln=1)

        # Student info card
        card_w = page_w - 2 * margin - 24
        card_x = (page_w - card_w) / 2
        card_y = 150
        card_h = 70

        pdf.set_fill_color(*WHITE)
        pdf.rect(card_x, card_y, card_w, card_h, style='F', round_corners=True, corner_radius=4)

        # Cyan accent strip on the card's left edge
        pdf.set_fill_color(*CYAN)
        pdf.rect(card_x, card_y, 3, card_h, style='F')

        rows = [
            (_t("STUDENT NAME"), student_name or _t("Not Provided")),
            (_t("STUDENT TYPE"), student_type_label or _t("Not Provided")),
            (_t("SELECTED CAREER STREAM"), career_stream_name or _t("Not Selected")),
            (_t("REPORT GENERATED"), datetime.now().strftime('%d %b %Y, %I:%M %p')),
        ]

        row_h = card_h / len(rows)
        label_x = card_x + 12
        value_x = card_x + 75

        for i, (label, value) in enumerate(rows):
            row_y = card_y + i * row_h + row_h / 2 - 3.5

            pdf.set_xy(label_x, row_y)
            pdf.set_font('helvetica', 'B', 9)
            pdf.set_text_color(*LABEL_GRAY)
            pdf.cell(60, 6, clean_text(label), align='L')

            pdf.set_xy(value_x, row_y)
            pdf.set_font('helvetica', 'B', 11.5)
            pdf.set_text_color(*NAVY_TEXT)
            pdf.cell(card_w - (value_x - card_x) - 10, 6, clean_text(str(value)), align='L')

            if i < len(rows) - 1:
                pdf.set_draw_color(*DIVIDER_GRAY)
                pdf.set_line_width(0.2)
                pdf.line(card_x + 8, card_y + (i + 1) * row_h, card_x + card_w - 8, card_y + (i + 1) * row_h)

        # Footer tagline
        pdf.set_y(page_h - 22)
        pdf.set_font('helvetica', 'I', 9.5)
        pdf.set_text_color(*CYAN_SOFT)
        pdf.cell(0, 6, clean_text(_t("Empowering Students Through the Elevate Initiative")), align='C', ln=1)

        # Restore default auto page-break behaviour for the rest of the report.
        pdf.set_auto_page_break(True, margin=20)

    # Create PDF
    pdf = PDF()
    pdf.alias_nb_pages()  # enables "Page X of {nb}" in the footer

    # MODIFICATION (i18n): registers a font matching the selected UI
    # language (falls back to helvetica.ttf / built-in helvetica).
    _register_pdf_font(pdf)
    
    # Helper function to clean text (remove emojis and special chars)
    def clean_text(text):
        # Remove emojis and special characters that cause issues
        emoji_pattern = re.compile("["
            u"\U0001F600-\U0001F64F"  # emoticons
            u"\U0001F300-\U0001F5FF"  # symbols & pictographs
            u"\U0001F680-\U0001F6FF"  # transport & map symbols
            u"\U0001F1E0-\U0001F1FF"  # flags (iOS)
            u"\U00002702-\U000027B0"
            u"\U000024C2-\U0001F251"
            u"\U0001F900-\U0001F9FF"  # supplemental symbols
            u"\U0001FA70-\U0001FAFF"  # more emojis
            "]+", flags=re.UNICODE)
        text = emoji_pattern.sub(r'', text)
        # Replace bullet points with asterisks
        text = text.replace('•', '-').replace('●', '-').replace('○', '-')
        # MODIFICATION (i18n): previously this stripped every non-ASCII
        # character (text.encode('ascii','ignore').decode('ascii')), which
        # silently deleted translated Tamil/Hindi/Marathi text before it
        # ever reached the PDF. Non-ASCII characters are now kept - correct
        # rendering additionally requires a Unicode font whose glyph set
        # covers the target script to be embedded via pdf.add_font() below.
        return text.strip()
    
    # Helper function to add multi-cell text safely
    def safe_multi_cell(pdf, width, height, text, border=0, align='L'):
        clean = clean_text(text)
        pdf.multi_cell(width, height, clean, border, align)
    
    # Helper function to add cell text safely
    def safe_cell(pdf, width, height, text, border=0, ln=0, align='L'):
        clean = clean_text(text)
        pdf.cell(width, height, clean, border, ln, align)

    # ------------------------------------------------------------------
    # Layout helpers for content pages: colored title bars + rounded,
    # bordered information cards. All content pages share consistent
    # margins, spacing, and automatic page-break handling so cards never
    # get cut off or overlap the footer.
    # ------------------------------------------------------------------
    def content_width():
        return pdf.w - 2 * CONTENT_MARGIN

    def ensure_space(height_needed):
        """Force a page break if the upcoming block won't fit above the footer."""
        if pdf.get_y() + height_needed > pdf.h - pdf.b_margin:
            pdf.add_page()

    def draw_title_bar(title):
        ensure_space(14)
        w = content_width()
        x = CONTENT_MARGIN
        y = pdf.get_y()
        bar_h = 9
        pdf.set_fill_color(*DARK_BLUE)
        pdf.rect(x, y, w, bar_h, style='F', round_corners=True, corner_radius=1.6)
        pdf.set_fill_color(*CYAN)
        pdf.rect(x, y, 3, bar_h, style='F')
        pdf.set_xy(x + 6, y)
        pdf.set_font('helvetica', 'B', 11.5)
        pdf.set_text_color(*PDF_WHITE)
        pdf.cell(w - 10, bar_h, clean_text(title), align='L')
        pdf.set_xy(x, y + bar_h + 4)

    def draw_kv_card(title, pairs):
        """Section title bar + rounded card of label/value rows (e.g. Student Information)."""
        pad = 6
        row_h = 8.4
        card_h = row_h * len(pairs) + 2 * pad - 2
        ensure_space(9 + 4 + card_h + 8)
        draw_title_bar(title)
        x = CONTENT_MARGIN
        y = pdf.get_y()
        w = content_width()
        pdf.set_fill_color(*PDF_WHITE)
        pdf.set_draw_color(*CARD_BORDER)
        pdf.set_line_width(0.3)
        pdf.rect(x, y, w, card_h, style='DF', round_corners=True, corner_radius=2.2)

        label_w = 46
        for i, (k, v) in enumerate(pairs):
            row_y = y + pad + i * row_h
            pdf.set_xy(x + pad, row_y)
            pdf.set_font('helvetica', 'B', 8.6)
            pdf.set_text_color(*LABEL_MUTED)
            pdf.cell(label_w, 6, clean_text(str(k).upper()), align='L')
            pdf.set_xy(x + pad + label_w, row_y)
            pdf.set_font('helvetica', 'B', 10.5)
            pdf.set_text_color(*DARK_BLUE)
            pdf.cell(w - 2 * pad - label_w, 6, clean_text(str(v)), align='L')
            if i < len(pairs) - 1:
                pdf.set_draw_color(*CARD_BORDER)
                pdf.set_line_width(0.2)
                pdf.line(x + pad, row_y + row_h - 1.5, x + w - pad, row_y + row_h - 1.5)
        pdf.set_xy(x, y + card_h + 8)

    def draw_text_card(title, text):
        """Section title bar + rounded card containing a single paragraph."""
        body = clean_text(str(text)) if text not in (None, "") else clean_text(_t("Not available."))
        pad = 6
        inner_w = content_width() - 2 * pad
        line_h = 5.6
        pdf.set_font('helvetica', '', 10.5)
        text_h = pdf.multi_cell(inner_w, line_h, body, dry_run=True, output="HEIGHT")
        card_h = text_h + 2 * pad
        ensure_space(9 + 4 + card_h + 8)
        draw_title_bar(title)
        x = CONTENT_MARGIN
        y = pdf.get_y()
        w = content_width()
        pdf.set_fill_color(*PDF_WHITE)
        pdf.set_draw_color(*CARD_BORDER)
        pdf.set_line_width(0.3)
        pdf.rect(x, y, w, card_h, style='DF', round_corners=True, corner_radius=2.2)
        pdf.set_xy(x + pad, y + pad)
        pdf.set_font('helvetica', '', 10.5)
        pdf.set_text_color(*BODY_TEXT)
        pdf.multi_cell(inner_w, line_h, body, align='L')
        pdf.set_xy(x, y + card_h + 8)

    def draw_bullet_card(title, items):
        """Section title bar + rounded card containing a bulleted list."""
        items = [clean_text(str(i)) for i in (items or []) if str(i).strip()]
        if not items:
            items = [clean_text(_t("Not available."))]
        pad = 6
        bullet_indent = 5
        inner_w = content_width() - 2 * pad - bullet_indent
        line_h = 5.6
        pdf.set_font('helvetica', '', 10.5)
        heights = [pdf.multi_cell(inner_w, line_h, it, dry_run=True, output="HEIGHT") for it in items]
        gap = 2.2
        card_h = sum(heights) + gap * (len(items) - 1) + 2 * pad
        ensure_space(9 + 4 + card_h + 8)
        draw_title_bar(title)
        x = CONTENT_MARGIN
        y = pdf.get_y()
        w = content_width()
        pdf.set_fill_color(*PDF_WHITE)
        pdf.set_draw_color(*CARD_BORDER)
        pdf.set_line_width(0.3)
        pdf.rect(x, y, w, card_h, style='DF', round_corners=True, corner_radius=2.2)

        cy = y + pad
        for it, h in zip(items, heights):
            pdf.set_fill_color(*CYAN)
            pdf.ellipse(x + pad, cy + 1.6, 2, 2, style='F')
            pdf.set_xy(x + pad + bullet_indent, cy)
            pdf.set_font('helvetica', '', 10.5)
            pdf.set_text_color(*BODY_TEXT)
            pdf.multi_cell(inner_w, line_h, it, align='L')
            cy += h + gap
        pdf.set_xy(x, y + card_h + 8)

    def draw_section(title, body):
        """Route a section to a bullet card (list body) or text card (string body)."""
        if isinstance(body, list):
            draw_bullet_card(title, body)
        else:
            draw_text_card(title, body)

    # ---- Cover Page ----
    student_type_label = _t('School Student') if st.session_state.user_type == 'school' else _t('College Student')
    draw_cover_page(
        pdf,
        student_name=st.session_state.student_name,
        student_type_label=student_type_label,
        career_stream_name=stream_name_t,
    )

    # Start the report content on a fresh page after the cover, using the
    # shared content-page margins/auto-page-break configured above.
    pdf.set_margins(CONTENT_MARGIN, 20, CONTENT_MARGIN)
    pdf.set_auto_page_break(True, margin=24)
    pdf.add_page()

    # Student Information
    draw_kv_card(t("home.student_information"), [
        (_t("Name"), st.session_state.student_name),
        (t("labels.age"), st.session_state.student_age),
        (_t("Institution"), st.session_state.student_institution),
        (_t("City"), st.session_state.student_city),
        (_t("State"), st.session_state.student_state),
        (_t("Grade/Year"), st.session_state.student_grade),
        (_t("Assessment Type"), f"{_t('School Student') if st.session_state.user_type == 'school' else _t('College Student')} {_t('Pathway')}"),
        (_t("Date"), datetime.now().strftime('%Y-%m-%d')),
    ])

    # Selected Stream
    draw_kv_card(_t("Selected Stream"), [
        (_t("Stream"), stream_name_t),
        (_t("Match Score"), f"{score_value:.1f}%"),
    ])

    # MODIFICATION (PDF charts): Top Career Match Percentages bar chart -
    # visualises the same match_percentage values shown in the app's
    # "Top Matching Careers" / "Top 3 AI Career Recommendations" screens.
    # MODIFICATION (i18n): each career/stream label is translated for the
    # chart, since chart_top_career_matches() only plots what it's given.
    if top_match_items:
        top_match_items = [(_t(label), pct) for label, pct in top_match_items]
        chart_fig_w_in = (content_width() - 12) / 25.4  # card inner width, mm -> in
        draw_chart_card(
            pdf, ensure_space, draw_title_bar, content_width, CONTENT_MARGIN,
            PDF_WHITE, CARD_BORDER, _t("Top Career Match Percentages"),
            chart_top_career_matches(top_match_items, chart_fig_w_in),
            chart_fig_w_in,
        )

    # AI Recommendation
    # MODIFICATION (i18n): only the "AI Recommendation" label goes through
    # _t(); the message itself is Gemini-generated content and is already
    # produced directly in the selected language, so it is rendered as-is.
    draw_text_card(_t("AI Recommendation"), recommendation.get('message', 'Recommendations are not available yet.'))

    # AI Analysis - Strengths & Opportunities only (never Weaknesses/Threats)
    # MODIFICATION (i18n): translate the whole AI-generated dict once
    # (no longer needed - AI content is rendered as-is
    # display elsewhere in the app) so every string value inside it -
    # nested lists included - comes out translated.
    analysis = st.session_state.get('ai_analysis')
    if analysis:
        draw_bullet_card(_t("Strengths"), analysis.get('strengths', []))
        draw_bullet_card(_t("Opportunities"), analysis.get('opportunities', []))

    # Full AI deep-dive report
    deep_dive = st.session_state.get('ai_deep_dive')
    if deep_dive:
        draw_section(_t("Career Overview"), deep_dive.get("career_overview", ""))
        draw_section(_t("Future Scope"), deep_dive.get("future_scope", ""))
        draw_section(_t("Technical Skills"), deep_dive.get("technical_skills", []))
        draw_section(_t("Soft Skills"), deep_dive.get("soft_skills", []))

        # MODIFICATION (PDF charts): Technical Skills vs Soft Skills pie
        # chart - purely a visual count comparison of the two skill lists
        # already rendered as text/bullets above; no new data generated.
        tech_count = len(deep_dive.get("technical_skills", []) or [])
        soft_count = len(deep_dive.get("soft_skills", []) or [])
        if tech_count or soft_count:
            chart_fig_w_in = (content_width() - 12) / 25.4
            draw_chart_card(
                pdf, ensure_space, draw_title_bar, content_width, CONTENT_MARGIN,
                PDF_WHITE, CARD_BORDER, _t("Technical Skills vs Soft Skills"),
                chart_skills_pie(tech_count, soft_count, chart_fig_w_in),
                chart_fig_w_in,
            )

        draw_section(_t("Major Hiring Cities in India"), deep_dive.get("major_hiring_cities_india", []))
        draw_section(_t("Major Industry Hubs"), deep_dive.get("major_industry_hubs", []))
        draw_section(_t("Top Hiring Industries"), deep_dive.get("top_hiring_industries", []))

        # education_path is a structured object, shaped differently for
        # school vs college students - render each sub-field as its own
        # mini-section instead of dumping the raw dict.
        education_path = deep_dive.get("education_path", {}) or {}
        if st.session_state.user_type == 'school':
            draw_section(_t("Recommended Stream"), education_path.get("recommended_stream", ""))
            draw_section(_t("Undergraduate Degree"), education_path.get("undergraduate_degree", ""))
            draw_section(_t("Higher Studies"), education_path.get("higher_studies", ""))
            draw_section(_t("Certifications (Education Path)"), education_path.get("certifications", []))
        else:
            draw_section(_t("Higher Education Options"), education_path.get("higher_education_options", ""))
            draw_section(_t("Professional Certifications"), education_path.get("professional_certifications", []))
            draw_section(_t("Specializations"), education_path.get("specializations", []))
            draw_section(_t("Career Advancement"), education_path.get("career_advancement", ""))

        # learning_resources is also a structured object with 5 categories.
        learning_resources = deep_dive.get("learning_resources", {}) or {}
        draw_section(_t("Recommended Certifications"), learning_resources.get("recommended_certifications", []))
        draw_section(_t("Free Learning Resources"), learning_resources.get("free_learning_resources", []))
        draw_section(_t("Online Platforms"), learning_resources.get("online_platforms", []))
        draw_section(_t("Books"), learning_resources.get("books", []))
        draw_section(_t("Communities"), learning_resources.get("communities", []))

        draw_section(_t("Related Career Roles"), deep_dive.get("related_career_roles", []))

    # ==================================================================
    # MODIFICATION (Career Role Deep Dive / Top 10 Related Roles): if the
    # student has explored a specific career role in this session (Career
    # Detail generated for it), give that role its own dedicated,
    # richly-detailed section - entirely reusing content already
    # generated elsewhere in the app (Career Detail, 12-Month Roadmap, AI
    # Skill Gap Analysis) so no new AI calls happen and the recommendation
    # engine / app workflow is untouched. Otherwise, auto-generate a
    # Top 10 Related Career Roles overview for the recommended stream so
    # the report is never missing this content. Every block below uses
    # the same draw_* / ensure_space page-break helpers as the rest of
    # the report, so pages are created dynamically and nothing overlaps
    # or gets cut off; a page is only started right before content that
    # will immediately be drawn onto it, so no blank pages are produced.
    # ==================================================================
    explored_role_name = st.session_state.get('selected_career_role')
    explored_role_detail = st.session_state.get('ai_role_detail')
    # MODIFICATION (i18n): translated display copy of the role name, used
    # everywhere it's rendered below; not used for any session-state
    # lookups (those are keyed by fixed strings like 'ai_role_detail').
    explored_role_name_t = _t(explored_role_name) if explored_role_name else explored_role_name

    if explored_role_name and explored_role_detail:
        pdf.add_page()
        draw_title_bar(f"{_t('Career Role Deep Dive')}: {explored_role_name_t}")

        draw_text_card(_t("Career Overview"), explored_role_detail.get("career_description", ""))
        draw_bullet_card(_t("Required Skills"), explored_role_detail.get("required_skills", []))
        draw_text_card(
            _t("Salary Range & Future Scope"),
            f"{_t('Salary Range (India)')}: {explored_role_detail.get('salary_range_india') or _t('Not available')}\n\n"
            f"{_t('Future Scope')}: {explored_role_detail.get('future_demand') or explored_role_detail.get('future_job_growth') or _t('Not available')}",
        )
        draw_bullet_card(_t("Top Companies"), explored_role_detail.get("top_hiring_companies", []))

        # ---- Skill Gap Analysis sub-section (reuses cached data + the
        # existing progress-bar chart helpers, exactly as already shown
        # on the "AI Skill Gap Analysis" page for this role). ----
        role_skill_gap = st.session_state.get('skill_gap_analysis')
        draw_title_bar(f"{_t('Skill Gap Analysis')}: {explored_role_name_t}")
        if role_skill_gap:
            readiness_score = role_skill_gap.get('overall_readiness_score', 0) or 0
            draw_text_card(f"{_t('Overall Career Readiness')}: {readiness_score}%", role_skill_gap.get("readiness_summary", ""))

            readiness_strengths_for_chart = [
                (item.get('skill_name', ''), item.get('proficiency_score', 0))
                for item in role_skill_gap.get('current_strengths', []) if item.get('skill_name')
            ]
            chart_fig_w_in = (content_width() - 12) / 25.4
            draw_chart_card(
                pdf, ensure_space, draw_title_bar, content_width, CONTENT_MARGIN,
                PDF_WHITE, CARD_BORDER, _t("Career Readiness"),
                chart_career_readiness(readiness_score, readiness_strengths_for_chart, chart_fig_w_in),
                chart_fig_w_in,
            )

            draw_bullet_card(_t("Missing Skills"), [
                f"[{item.get('importance', '')}] {item.get('skill_name', '')}: {item.get('explanation', '')}"
                for item in role_skill_gap.get('missing_skills', [])
            ])

            skill_gap_items_for_chart = [
                (item.get('skill_name', ''), item.get('difficulty_score', 0), item.get('difficulty_label', ''))
                for item in role_skill_gap.get('learning_difficulty', []) if item.get('skill_name')
            ]
            if skill_gap_items_for_chart:
                chart_fig_w_in = (content_width() - 12) / 25.4
                draw_chart_card(
                    pdf, ensure_space, draw_title_bar, content_width, CONTENT_MARGIN,
                    PDF_WHITE, CARD_BORDER, _t("Skill Gap Progress Visualization"),
                    chart_skill_gap(skill_gap_items_for_chart, chart_fig_w_in),
                    chart_fig_w_in,
                )
        else:
            draw_text_card(
                _t("Skill Gap Analysis"),
                _t(
                    f"This section has not been generated yet in this session - visit "
                    f'"View AI Skill Gap Analysis for {explored_role_name}" first to include it here.'
                ),
            )

        # ---- Learning Roadmap sub-section (reuses cached data + the
        # existing timeline chart helper, exactly as already shown on the
        # "12-Month Learning Roadmap" page for this role). ----
        role_roadmap = st.session_state.get('learning_roadmap')
        draw_title_bar(f"{_t('Learning Roadmap')}: {explored_role_name_t}")
        certifications, resources = [], []
        if role_roadmap:
            draw_text_card(_t("Roadmap Overview"), role_roadmap.get("roadmap_overview", ""))

            roadmap_months_for_chart = [
                (m.get('month_number', ''), m.get('month_title', ''))
                for m in role_roadmap.get('months', [])
            ]
            if roadmap_months_for_chart:
                chart_fig_w_in = (content_width() - 12) / 25.4
                draw_chart_card(
                    pdf, ensure_space, draw_title_bar, content_width, CONTENT_MARGIN,
                    PDF_WHITE, CARD_BORDER, _t("Learning Roadmap Progress Timeline"),
                    chart_roadmap_timeline(roadmap_months_for_chart, chart_fig_w_in),
                    chart_fig_w_in,
                )

            draw_bullet_card(_t("Month-by-Month Plan"), [
                f"{_t('Month')} {m.get('month_number', '')}: {m.get('month_title', '')}"
                for m in role_roadmap.get('months', [])
            ])

            # Certifications & Learning Resources are aggregated (deduped,
            # order-preserved) from the roadmap already generated for
            # this specific role - genuinely role-specific, no new AI call.
            seen_certs, seen_res = set(), set()
            for m in role_roadmap.get('months', []):
                for c in m.get('certifications', []) or []:
                    if c and c not in seen_certs:
                        seen_certs.add(c)
                        certifications.append(c)
                for r in m.get('free_resources', []) or []:
                    if r and r not in seen_res:
                        seen_res.add(r)
                        resources.append(r)
        else:
            draw_text_card(
                _t("Learning Roadmap"),
                _t(
                    f"This section has not been generated yet in this session - visit "
                    f'"View 12-Month Roadmap for {explored_role_name}" first to include it here.'
                ),
            )

        # Fall back to the stream-level learning resources (already
        # generated as part of the AI deep-dive) if the roadmap didn't
        # yield any certifications/resources of its own.
        if not certifications:
            certifications = (deep_dive.get("learning_resources", {}) or {}).get("recommended_certifications", []) if deep_dive else []
        if not resources:
            lr = (deep_dive.get("learning_resources", {}) or {}) if deep_dive else {}
            resources = (lr.get("free_learning_resources", []) or []) + (lr.get("online_platforms", []) or [])

        draw_bullet_card(_t("Certifications"), certifications)
        draw_bullet_card(_t("Learning Resources"), resources)

    else:
        # ---- Top 10 Related Career Roles (auto-generated for the
        # recommended stream since no specific role has been explored). ----
        pdf.add_page()
        draw_title_bar(f"{_t('Top 10 Related Career Roles')}: {stream_name_t}")

        student_details_for_roles = {"name": st.session_state.get('student_name', '')}
        top10_roles = generate_top10_related_roles(
            student_details_for_roles, stream_name, st.session_state.get('user_type', 'college')
        )

        if top10_roles:
            for i, role in enumerate(top10_roles, start=1):
                draw_title_bar(f"{i}. {role.get('role_name', '')}")
                draw_text_card(_t("Overview"), role.get("short_description", ""))
                draw_text_card(
                    _t("Salary Range & Future Scope"),
                    f"{_t('Salary Range (India)')}: {role.get('salary_range_india') or _t('Not available')}\n\n"
                    f"{_t('Future Scope')}: {role.get('future_scope') or _t('Not available')}",
                )
                draw_bullet_card(_t("Required Skills"), role.get("required_skills", []))
        else:
            # Graceful fallback: reuse whichever plain role-name list is
            # already cached from the AI deep-dive, so this section is
            # never blank even if the extra AI call above is unavailable.
            fallback_names = (st.session_state.get('ai_deep_dive') or {}).get("related_career_roles", [])
            draw_bullet_card(
                _t("Related Career Roles"),
                [_t(n) for n in fallback_names] if fallback_names else [_t("Related career role details are not available yet for this stream.")],
            )

    # Save to temporary file
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
    pdf.output(temp_file.name)
    temp_file.close()
    
    return temp_file.name


def generate_role_detail_pdf(role_name, stream_name):
    """
    Generate a single downloadable PDF for the Career Detail page that
    bundles together every AI-generated section relevant to this specific
    role: the Career Detail breakdown itself, the 12-Month Learning
    Roadmap, the AI Skill Gap Analysis, and the AI Resume Suggestions -
    whichever of those have already been generated for this role in the
    current session (nothing is regenerated here; this only renders
    what's already cached in st.session_state, exactly as shown on-screen).

    Visual design matches generate_pdf_report(): a Dark Blue / Cyan /
    White brand theme with a branded header/footer (page numbers +
    "CoActions - AI Career Guidance & Career Counselling System"),
    colored title bars for every section heading, and rounded, bordered
    information cards for every block of content. No report data is
    changed here - only presentation.
    """
    import tempfile
    import re

    detail = st.session_state.get('ai_role_detail')
    roadmap = st.session_state.get('learning_roadmap')
    skill_gap = st.session_state.get('skill_gap_analysis')
    resume = st.session_state.get('resume_suggestions')

    # MODIFICATION (i18n): translate the AI-generated content dicts once
    # (no longer needed - AI content is rendered as-is
    # display elsewhere in the app), and build translated display copies
    # of role_name/stream_name - both parameters are only ever used for
    # display in this function, never for a session-state lookup key.
    role_name = _t(role_name) if role_name else role_name
    stream_name = _t(stream_name) if stream_name else stream_name

    # ------------------------------------------------------------------
    # Shared design tokens - identical brand palette to generate_pdf_report()
    # ------------------------------------------------------------------
    DARK_BLUE = (13, 27, 62)
    CYAN = (34, 211, 238)
    CYAN_SOFT = (125, 230, 245)
    PDF_WHITE = (255, 255, 255)
    BODY_TEXT = (30, 41, 63)
    LABEL_MUTED = (68, 92, 130)
    CARD_BORDER = (204, 234, 241)
    NAVY_TEXT = (13, 27, 62)
    CONTENT_MARGIN = 14

    class PDF(FPDF):
        def header(self):
            self.set_fill_color(*DARK_BLUE)
            self.rect(0, 0, self.w, 10, style='F')
            self.set_fill_color(*CYAN)
            self.rect(0, 10, self.w, 1.1, style='F')

            self.set_xy(CONTENT_MARGIN, 1.6)
            self.set_font('helvetica', 'B', 10.5)
            self.set_text_color(*PDF_WHITE)
            self.cell(90, 6.5, 'CoActions', align='L')

            self.set_xy(0, 2.2)
            self.set_font('helvetica', '', 8)
            self.set_text_color(180, 230, 240)
            self.cell(self.w - CONTENT_MARGIN, 6, _t('AI Career Guidance & Career Counselling System'), align='R')

            self.set_y(20)

        def footer(self):
            self.set_draw_color(*CYAN)
            self.set_line_width(0.4)
            self.line(CONTENT_MARGIN, self.h - 18, self.w - CONTENT_MARGIN, self.h - 18)

            self.set_xy(CONTENT_MARGIN, self.h - 16)
            self.set_font('helvetica', '', 8)
            self.set_text_color(*LABEL_MUTED)
            self.cell(self.w - 2 * CONTENT_MARGIN, 4.6,
                      f"CoActions  -  {_t('AI Career Guidance & Career Counselling System')}", align='L')

            self.set_xy(0, self.h - 16)
            self.set_font('helvetica', '', 8)
            self.set_text_color(*LABEL_MUTED)
            self.cell(self.w - CONTENT_MARGIN, 4.6, f"{_t('Page')} {self.page_no()} {_t('of')} {{nb}}", align='R')

    pdf = PDF()
    pdf.alias_nb_pages()  # enables "Page X of {nb}" in the footer
    pdf.set_margins(CONTENT_MARGIN, 20, CONTENT_MARGIN)
    pdf.set_auto_page_break(True, margin=24)
    pdf.add_page()

    # MODIFICATION (i18n): registers a font matching the selected UI
    # language (falls back to helvetica.ttf / built-in helvetica).
    _register_pdf_font(pdf)

    def clean_text(text):
        emoji_pattern = re.compile("["
            u"\U0001F600-\U0001F64F"
            u"\U0001F300-\U0001F5FF"
            u"\U0001F680-\U0001F6FF"
            u"\U0001F1E0-\U0001F1FF"
            u"\U00002702-\U000027B0"
            u"\U000024C2-\U0001F251"
            u"\U0001F900-\U0001F9FF"
            u"\U0001FA70-\U0001FAFF"
            "]+", flags=re.UNICODE)
        text = emoji_pattern.sub(r'', str(text))
        text = text.replace('•', '-').replace('●', '-').replace('○', '-')
        # MODIFICATION (i18n): kept non-ASCII characters instead of
        # stripping them - see matching note in generate_pdf_report().
        return text.strip()

    # ------------------------------------------------------------------
    # Layout helpers - same visual language as generate_pdf_report():
    # colored title bars + rounded, bordered cards, with page-break-aware
    # spacing so nothing overlaps the header or footer.
    # ------------------------------------------------------------------
    def content_width():
        return pdf.w - 2 * CONTENT_MARGIN

    def ensure_space(height_needed):
        if pdf.get_y() + height_needed > pdf.h - pdf.b_margin:
            pdf.add_page()

    def draw_title_bar(title, big=False):
        ensure_space(16 if big else 14)
        w = content_width()
        x = CONTENT_MARGIN
        y = pdf.get_y()
        bar_h = 11 if big else 9
        pdf.set_fill_color(*DARK_BLUE)
        pdf.rect(x, y, w, bar_h, style='F', round_corners=True, corner_radius=1.6)
        pdf.set_fill_color(*CYAN)
        pdf.rect(x, y, 3, bar_h, style='F')
        pdf.set_xy(x + 6, y)
        pdf.set_font('helvetica', 'B', 13 if big else 11.5)
        pdf.set_text_color(*PDF_WHITE)
        pdf.cell(w - 10, bar_h, clean_text(title), align='L')
        pdf.set_xy(x, y + bar_h + 4)

    def draw_month_divider(month_num, month_title):
        """Lighter cyan sub-heading marking the start of one month's block,
        visually nested under the main 'Learning Roadmap' title bar."""
        ensure_space(12)
        w = content_width()
        x = CONTENT_MARGIN
        y = pdf.get_y()
        bar_h = 7.5
        pdf.set_fill_color(*CYAN)
        pdf.rect(x, y, w, bar_h, style='F', round_corners=True, corner_radius=1.4)
        pdf.set_xy(x + 5, y)
        pdf.set_font('helvetica', 'B', 10)
        pdf.set_text_color(*DARK_BLUE)
        pdf.cell(w - 10, bar_h, clean_text(f"{_t('MONTH')} {month_num}  -  {month_title}"), align='L')
        pdf.set_xy(x, y + bar_h + 3)

    def draw_kv_card(title, pairs, big_title=False):
        pad = 6
        row_h = 8.4
        card_h = row_h * len(pairs) + 2 * pad - 2
        ensure_space((11 if big_title else 9) + 4 + card_h + 8)
        draw_title_bar(title, big=big_title)
        x = CONTENT_MARGIN
        y = pdf.get_y()
        w = content_width()
        pdf.set_fill_color(*PDF_WHITE)
        pdf.set_draw_color(*CARD_BORDER)
        pdf.set_line_width(0.3)
        pdf.rect(x, y, w, card_h, style='DF', round_corners=True, corner_radius=2.2)

        label_w = 58
        for i, (k, v) in enumerate(pairs):
            row_y = y + pad + i * row_h
            pdf.set_xy(x + pad, row_y)
            pdf.set_font('helvetica', 'B', 8.6)
            pdf.set_text_color(*LABEL_MUTED)
            pdf.cell(label_w, 6, clean_text(str(k).upper()), align='L')
            pdf.set_xy(x + pad + label_w, row_y)
            pdf.set_font('helvetica', 'B', 10.5)
            pdf.set_text_color(*DARK_BLUE)
            pdf.cell(w - 2 * pad - label_w, 6, clean_text(str(v)), align='L')
            if i < len(pairs) - 1:
                pdf.set_draw_color(*CARD_BORDER)
                pdf.set_line_width(0.2)
                pdf.line(x + pad, row_y + row_h - 1.5, x + w - pad, row_y + row_h - 1.5)
        pdf.set_xy(x, y + card_h + 8)

    def draw_text_card(title, text):
        body = clean_text(str(text)) if text not in (None, "") else clean_text(_t("Not available."))
        pad = 6
        inner_w = content_width() - 2 * pad
        line_h = 5.6
        pdf.set_font('helvetica', '', 10.5)
        text_h = pdf.multi_cell(inner_w, line_h, body, dry_run=True, output="HEIGHT")
        card_h = text_h + 2 * pad
        ensure_space(9 + 4 + card_h + 8)
        draw_title_bar(title)
        x = CONTENT_MARGIN
        y = pdf.get_y()
        w = content_width()
        pdf.set_fill_color(*PDF_WHITE)
        pdf.set_draw_color(*CARD_BORDER)
        pdf.set_line_width(0.3)
        pdf.rect(x, y, w, card_h, style='DF', round_corners=True, corner_radius=2.2)
        pdf.set_xy(x + pad, y + pad)
        pdf.set_font('helvetica', '', 10.5)
        pdf.set_text_color(*BODY_TEXT)
        pdf.multi_cell(inner_w, line_h, body, align='L')
        pdf.set_xy(x, y + card_h + 8)

    def draw_bullet_card(title, items, with_title_bar=True):
        items = [clean_text(str(i)) for i in (items or []) if str(i).strip()]
        if not items:
            items = [clean_text(_t("Not available."))]
        pad = 6
        bullet_indent = 5
        inner_w = content_width() - 2 * pad - bullet_indent
        line_h = 5.6
        pdf.set_font('helvetica', '', 10.5)
        heights = [pdf.multi_cell(inner_w, line_h, it, dry_run=True, output="HEIGHT") for it in items]
        gap = 2.2
        card_h = sum(heights) + gap * (len(items) - 1) + 2 * pad
        ensure_space((9 + 4 if with_title_bar else 0) + card_h + 8)
        if with_title_bar:
            draw_title_bar(title)
        x = CONTENT_MARGIN
        y = pdf.get_y()
        w = content_width()
        pdf.set_fill_color(*PDF_WHITE)
        pdf.set_draw_color(*CARD_BORDER)
        pdf.set_line_width(0.3)
        pdf.rect(x, y, w, card_h, style='DF', round_corners=True, corner_radius=2.2)

        cy = y + pad
        for it, h in zip(items, heights):
            pdf.set_fill_color(*CYAN)
            pdf.ellipse(x + pad, cy + 1.6, 2, 2, style='F')
            pdf.set_xy(x + pad + bullet_indent, cy)
            pdf.set_font('helvetica', '', 10.5)
            pdf.set_text_color(*BODY_TEXT)
            pdf.multi_cell(inner_w, line_h, it, align='L')
            cy += h + gap
        pdf.set_xy(x, y + card_h + 8)

    def draw_section(title, body):
        if isinstance(body, list):
            draw_bullet_card(title, body)
        else:
            draw_text_card(title, body)

    def draw_note_card(message):
        """Rounded, cyan-bordered info card for 'not generated yet' notices."""
        pad = 6
        inner_w = content_width() - 2 * pad
        line_h = 5.6
        pdf.set_font('helvetica', 'I', 10)
        text_h = pdf.multi_cell(inner_w, line_h, clean_text(message), dry_run=True, output="HEIGHT")
        card_h = text_h + 2 * pad
        ensure_space(card_h + 8)
        x = CONTENT_MARGIN
        y = pdf.get_y()
        w = content_width()
        pdf.set_fill_color(*PDF_WHITE)
        pdf.set_draw_color(*CYAN)
        pdf.set_line_width(0.4)
        pdf.rect(x, y, w, card_h, style='DF', round_corners=True, corner_radius=2.2)
        pdf.set_fill_color(*CYAN)
        pdf.rect(x, y, 3, card_h, style='F')
        pdf.set_xy(x + pad, y + pad)
        pdf.set_font('helvetica', 'I', 10)
        pdf.set_text_color(*LABEL_MUTED)
        pdf.multi_cell(inner_w, line_h, clean_text(message), align='L')
        pdf.set_xy(x, y + card_h + 8)

    def not_generated_note(section_label, page_hint):
        draw_note_card(
            _t(f'{section_label} has not been generated yet in this session - visit "{page_hint}" first to include it here.')
        )

    # ---- Header: report title + student info card ----
    draw_kv_card(
        f"CoActions {_t('Career Detail Report')}",
        [
            (_t("Name"), st.session_state.get('student_name', '')),
            (_t("Grade / Year"), st.session_state.get('student_grade', '')),
            (_t("Stream"), stream_name),
            (_t("Career Role"), role_name),
            (_t("Date"), datetime.now().strftime('%Y-%m-%d')),
        ],
        big_title=True,
    )

    # ---- Section 1: Career Detail ----
    draw_title_bar(f"{_t('Career Detail')}: {role_name}", big=True)
    if detail:
        draw_section(_t("Career Description"), detail.get("career_description", ""))
        draw_section(_t("Salary Range (India)"), detail.get("salary_range_india", ""))
        draw_section(_t("Educational Requirements"), detail.get("educational_requirements", []))
        draw_section(_t("Required Skills"), detail.get("required_skills", []))
        draw_section(_t("Job Responsibilities"), detail.get("job_responsibilities", []))
        draw_section(_t("Job Growth"), detail.get("future_job_growth", ""))
        draw_section(_t("Industry Outlook"), detail.get("industry_outlook", ""))
        draw_section(_t("Top Hiring Companies"), detail.get("top_hiring_companies", []))
        draw_section(_t("Future Demand"), detail.get("future_demand", ""))
    else:
        not_generated_note(_t("Career Detail"), _t("Career Detail"))

    # ---- Section 2: 12-Month Learning Roadmap ----
    pdf.add_page()
    draw_title_bar(f"{_t('12-Month Learning Roadmap')}: {role_name}", big=True)
    if roadmap:
        level_label = _t("Beginner Level") if st.session_state.user_type == 'school' else _t("Advanced / Industry Level")
        draw_section(f"{_t('Roadmap Overview')} ({level_label})", roadmap.get("roadmap_overview", ""))

        # MODIFICATION (PDF charts): Learning Roadmap Progress Timeline -
        # a horizontal timeline of the same months detailed below, giving
        # a one-glance overview before the month-by-month breakdown.
        roadmap_months_for_chart = [
            (m.get('month_number', ''), m.get('month_title', ''))
            for m in roadmap.get('months', [])
        ]
        if roadmap_months_for_chart:
            chart_fig_w_in = (content_width() - 12) / 25.4
            draw_chart_card(
                pdf, ensure_space, draw_title_bar, content_width, CONTENT_MARGIN,
                PDF_WHITE, CARD_BORDER, _t("Learning Roadmap Progress Timeline"),
                chart_roadmap_timeline(roadmap_months_for_chart, chart_fig_w_in),
                chart_fig_w_in,
            )

        for month in roadmap.get('months', []):
            month_num = month.get('month_number', '')
            draw_month_divider(month_num, month.get('month_title', ''))
            draw_section(_t("Skills to Learn"), month.get("skills_to_learn", []))
            draw_section(_t("Topics"), month.get("topics", []))
            draw_section(_t("Practice Activities"), month.get("practice_activities", []))
            draw_section(_t("Mini Projects"), month.get("mini_projects", []))
            draw_section(_t("Recommended Free Resources"), month.get("free_resources", []))
            draw_section(_t("Certifications"), month.get("certifications", []))
    else:
        not_generated_note(_t("The 12-Month Learning Roadmap"), f"{_t('View 12-Month Roadmap for')} {role_name}")

    # ---- Section 3: AI Skill Gap Analysis ----
    pdf.add_page()
    draw_title_bar(f"{_t('AI Skill Gap Analysis')}: {role_name}", big=True)
    if skill_gap:
        readiness_score = skill_gap.get('overall_readiness_score', 0) or 0
        draw_text_card(f"{_t('Overall Career Readiness')}: {readiness_score}%", skill_gap.get("readiness_summary", ""))

        # MODIFICATION (PDF charts): Progress Bars for Career Readiness -
        # visualises the same overall_readiness_score above plus each
        # current-strength's proficiency_score (both already shown as
        # text below) as brand-colored progress bars.
        readiness_strengths_for_chart = [
            (item.get('skill_name', ''), item.get('proficiency_score', 0))
            for item in skill_gap.get('current_strengths', []) if item.get('skill_name')
        ]
        chart_fig_w_in = (content_width() - 12) / 25.4
        draw_chart_card(
            pdf, ensure_space, draw_title_bar, content_width, CONTENT_MARGIN,
            PDF_WHITE, CARD_BORDER, _t("Career Readiness"),
            chart_career_readiness(readiness_score, readiness_strengths_for_chart, chart_fig_w_in),
            chart_fig_w_in,
        )

        draw_bullet_card(_t("Current Strengths"), [
            f"{item.get('skill_name', '')} ({item.get('proficiency_score', 0)}%): {item.get('explanation', '')}"
            for item in skill_gap.get('current_strengths', [])
        ])
        draw_bullet_card(_t("Missing Skills"), [
            f"[{item.get('importance', '')}] {item.get('skill_name', '')}: {item.get('explanation', '')}"
            for item in skill_gap.get('missing_skills', [])
        ])
        draw_bullet_card(_t("Priority Skills"), [
            f"#{item.get('priority_rank', '')} {item.get('skill_name', '')}: {item.get('reason', '')}"
            for item in skill_gap.get('priority_skills', [])
        ])
        draw_bullet_card(_t("Learning Difficulty"), [
            f"{item.get('skill_name', '')} ({item.get('difficulty_label', '')}, {item.get('difficulty_score', 0)}%): {item.get('reason', '')}"
            for item in skill_gap.get('learning_difficulty', [])
        ])

        # MODIFICATION (PDF charts): Skill Gap Progress Visualization -
        # visualises the same learning_difficulty scores above (per
        # missing skill) as a color-coded bar chart, doubling as a
        # quick priority-reading aid (darker = harder/higher priority).
        skill_gap_items_for_chart = [
            (item.get('skill_name', ''), item.get('difficulty_score', 0), item.get('difficulty_label', ''))
            for item in skill_gap.get('learning_difficulty', []) if item.get('skill_name')
        ]
        if skill_gap_items_for_chart:
            chart_fig_w_in = (content_width() - 12) / 25.4
            draw_chart_card(
                pdf, ensure_space, draw_title_bar, content_width, CONTENT_MARGIN,
                PDF_WHITE, CARD_BORDER, _t("Skill Gap Progress Visualization"),
                chart_skill_gap(skill_gap_items_for_chart, chart_fig_w_in),
                chart_fig_w_in,
            )

        draw_bullet_card(_t("Recommended Learning Order"), [
            f"{item.get('order', '')}. {item.get('skill_name', '')}: {item.get('rationale', '')}"
            for item in skill_gap.get('recommended_learning_order', [])
        ])
        draw_bullet_card(_t("Estimated Learning Time"), [
            f"{item.get('skill_name', '')}: {item.get('estimated_duration', '')} ({item.get('weekly_commitment', '')})"
            for item in skill_gap.get('estimated_learning_time', [])
        ])
    else:
        not_generated_note(_t("The AI Skill Gap Analysis"), f"{_t('View AI Skill Gap Analysis for')} {role_name}")

    # ---- Section 4: AI Resume Suggestions ----
    pdf.add_page()
    draw_title_bar(f"{_t('AI Resume Suggestions')}: {role_name}", big=True)
    if resume:
        draw_bullet_card(_t("Resume Headline Options"), [
            f"{_t('Option')} {idx}: {headline}"
            for idx, headline in enumerate(resume.get('resume_headline', []), start=1)
        ])
        draw_bullet_card(_t("Career Objective Options"), [
            f"{_t('Option')} {idx}: {objective}"
            for idx, objective in enumerate(resume.get('career_objective', []), start=1)
        ])
        draw_bullet_card(_t("Key Skills to Feature"), [
            f"{item.get('skill_name', '')}: {item.get('reason', '')}"
            for item in resume.get('key_skills', [])
        ])
        draw_bullet_card(_t("Projects to Include"), [
            f"{item.get('project_title', '')}: {item.get('description', '')} ({item.get('relevance', '')})"
            for item in resume.get('projects_to_include', [])
        ])
        draw_bullet_card(_t("Certifications"), [
            f"{item.get('certification_name', '')}: {item.get('reason', '')}"
            for item in resume.get('certifications', [])
        ])
        draw_bullet_card(_t("Achievements to Pursue/Highlight"), [
            f"{item.get('suggestion', '')}: {item.get('reason', '')}"
            for item in resume.get('achievements', [])
        ])
        draw_bullet_card(_t("Portfolio Suggestions"), [
            f"{item.get('suggestion', '')} ({item.get('platform_or_format', '')}): {item.get('reason', '')}"
            for item in resume.get('portfolio_suggestions', [])
        ])
        draw_bullet_card(_t("Internship Suggestions"), [
            f"{item.get('internship_type', '')}: {item.get('reason', '')}"
            for item in resume.get('internship_suggestions', [])
        ])
    else:
        not_generated_note(_t("AI Resume Suggestions"), f"{_t('Get AI Resume Suggestions for')} {role_name}")

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
    pdf.output(temp_file.name)
    temp_file.close()

    return temp_file.name


# ==================== COMPLETE STREAM DETAILS DATABASE ====================
# This covers ALL possible streams from both school and college JSON files

# ==================== GEMINI API CONFIGURATION ====================

class GeminiConfigError(Exception):
    """Raised when the Gemini API key/client cannot be configured."""
    pass


class GeminiConnectionError(Exception):
    """Raised when a call to the Gemini API fails at runtime."""
    pass


class GeminiOverloadedError(GeminiConnectionError):
    """
    MODIFICATION (503 retry): raised only when the Gemini API kept
    returning HTTP 503 (UNAVAILABLE / "high demand") even after every
    retry attempt (and, if applicable, after trying the fallback model
    too). Deliberately a SEPARATE exception type from generic
    GeminiConnectionError / other errors so callers could special-case
    "the service is overloaded, try later" messaging if they ever want
    to - today it's still caught by the existing `except Exception`
    blocks throughout this file and turned into a friendly message,
    exactly like any other failure.
    """
    pass


def _get_gemini_api_key():
    """
    Safely fetch the Gemini API key.
    Priority: st.secrets["GEMINI_API_KEY"] -> environment variable GEMINI_API_KEY.
    Returns None if not configured anywhere. The key is never hardcoded
    and never written to logs or the UI.
    """
    try:
        if "GEMINI_API_KEY" in st.secrets:
            key = st.secrets["GEMINI_API_KEY"]
            if key:
                return key
    except Exception:
        # st.secrets raises if no secrets.toml exists at all - that's fine, fall back to env var
        pass
    return os.environ.get("GEMINI_API_KEY")


class _GeminiModel:
    """
    Thin adapter around the new `google-genai` SDK's `client.models`
    surface that preserves the `model.generate_content(prompt,
    generation_config=...)` call shape the rest of this file already
    uses. This keeps every call site below (and `safe_generate_json_content`'s
    dict-based `generation_config`) unchanged, while routing the actual
    request through `client.models.generate_content(model=..., contents=...,
    config=types.GenerateContentConfig(...))` under the hood.
    """

    def __init__(self, client: "genai.Client", model_name: str, fallback_model_name: str = GEMINI_FALLBACK_MODEL_NAME):
        self._client = client
        self._model_name = model_name
        # MODIFICATION (503 retry): remember the fallback model name so
        # generate_content() can automatically switch to it if the primary
        # model keeps returning 503s. If the primary model IS already the
        # fallback model, there's nothing to fall back to (handled below).
        self._fallback_model_name = fallback_model_name

    def _build_config(self, generation_config):
        """
        MODIFICATION (503 retry): pulled the config-building logic out of
        generate_content() into its own helper, unchanged in behaviour,
        so generate_content() can call it once per retry/model-switch
        without duplicating this block. No functional change here.
        """
        config = None
        if generation_config:
            cfg_kwargs = {}
            if "max_output_tokens" in generation_config:
                cfg_kwargs["max_output_tokens"] = generation_config["max_output_tokens"]
            if generation_config.get("response_mime_type"):
                cfg_kwargs["response_mime_type"] = generation_config["response_mime_type"]
            if "thinking_config" in generation_config:
                tc = generation_config["thinking_config"] or {}
                cfg_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_budget=tc.get("thinking_budget", 0)
                )
            config = types.GenerateContentConfig(**cfg_kwargs)
        return config

    def _call_with_retry(self, model_name, prompt, config):
        """
        MODIFICATION (503 retry): calls client.models.generate_content()
        for a SINGLE model_name, retrying only on HTTP 503
        (google.genai.errors.ServerError with .code == 503) with
        exponential backoff (1, 2, 4, 8, 16 seconds), up to
        GEMINI_MAX_RETRIES retries (6 attempts total).

        Any other exception (ClientError such as auth failure, invalid
        request, quota exceeded; or a ServerError that is NOT 503) is
        re-raised immediately on the first occurrence - it is never
        retried or suppressed, per requirement to only handle 503s.

        Returns the raw SDK response on success. Raises the last 503
        ServerError if every attempt for this model_name was exhausted.
        """
        last_error = None
        for attempt in range(1, GEMINI_MAX_RETRIES + 2):  # attempt 1 = first try, then up to 5 retries
            try:
                return self._client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
            except genai_errors.ServerError as e:
                # Only handle HTTP 503 (UNAVAILABLE / high demand). Any
                # other server error code (500, 502, etc.) is NOT what
                # this retry logic is for - raise it immediately.
                if getattr(e, "code", None) != 503:
                    raise

                last_error = e
                if attempt > GEMINI_MAX_RETRIES:
                    # All retries for this model exhausted.
                    gemini_logger.warning(
                        "[Gemini] Model '%s' still returning 503 after %d retries. Giving up on this model.",
                        model_name, GEMINI_MAX_RETRIES,
                    )
                    break

                wait_seconds = GEMINI_BACKOFF_SECONDS[attempt - 1]
                gemini_logger.warning(
                    "[Gemini] 503 UNAVAILABLE from model '%s' (attempt %d/%d). "
                    "Retrying in %d second(s)...",
                    model_name, attempt, GEMINI_MAX_RETRIES + 1, wait_seconds,
                )
                time.sleep(wait_seconds)
            # NOTE: every other exception type (ClientError - 401/403/429/400,
            # or any non-genai exception) is intentionally NOT caught here,
            # so it propagates straight up to the caller unmodified, exactly
            # as before this change.

        # Exhausted all retries for model_name with repeated 503s.
        raise last_error

    def generate_content(self, prompt, generation_config=None):
        config = self._build_config(generation_config)

        # MODIFICATION (503 retry): first try the primary model with
        # retry + exponential backoff on 503s.
        try:
            return self._call_with_retry(self._model_name, prompt, config)
        except genai_errors.ServerError as primary_error:
            if getattr(primary_error, "code", None) != 503:
                raise  # not a 503 - never retried in the first place, just re-raise

            # MODIFICATION (503 retry): primary model kept returning 503
            # after every retry - automatically fall back to a faster
            # model (e.g. gemini-2.5-flash) and try again, if the primary
            # model isn't already the fallback model.
            if self._fallback_model_name and self._fallback_model_name != self._model_name:
                gemini_logger.warning(
                    "[Gemini] Falling back from model '%s' to '%s' after repeated 503 errors.",
                    self._model_name, self._fallback_model_name,
                )
                try:
                    return self._call_with_retry(self._fallback_model_name, prompt, config)
                except genai_errors.ServerError as fallback_error:
                    if getattr(fallback_error, "code", None) != 503:
                        raise
                    gemini_logger.error(
                        "[Gemini] Fallback model '%s' also returned 503 after retries. Giving up.",
                        self._fallback_model_name,
                    )
                    raise GeminiOverloadedError(
                        "The Gemini API is currently experiencing high demand and did not "
                        "recover after several automatic retries (including a fallback "
                        "model). Please try again in a few minutes."
                    ) from fallback_error

            # No distinct fallback model available - surface a friendly error.
            raise GeminiOverloadedError(
                "The Gemini API is currently experiencing high demand and did not "
                "recover after several automatic retries. Please try again in a "
                "few minutes."
            ) from primary_error


@st.cache_resource(show_spinner=False)
def _build_gemini_client(api_key: str, model_name: str):
    """
    Actually construct the Gemini client. Cached on BOTH api_key and
    model_name so that editing secrets.toml / the GEMINI_API_KEY env var
    and re-running the app automatically picks up the new key instead of
    silently reusing a client built with the old one.
    """
    client = genai.Client(api_key=api_key)
    return _GeminiModel(client, model_name)


def get_gemini_client(model_name: str = "gemini-2.5-flash"):
    """
    Build (and transparently cache) a reusable Gemini client.

    This is the single place in the app that talks to the `google-genai`
    SDK for configuration - all future Gemini calls should reuse the
    model instance returned here instead of calling
    genai.Client()/client.models.generate_content() directly.

    The underlying client is cached via st.cache_resource, keyed on the
    actual api_key + model_name, so changing the key in secrets.toml (or
    the GEMINI_API_KEY env var) and rerunning Streamlit automatically
    builds a fresh client instead of reusing a stale one.

    Raises:
        GeminiConfigError: if no API key is configured, or the SDK
            fails to initialize for any reason (invalid key format,
            SDK/network issues at configure-time, etc).

    Returns:
        _GeminiModel: a ready-to-use Gemini client adapter wrapping
        genai.Client.
    """
    api_key = _get_gemini_api_key()
    if not api_key:
        raise GeminiConfigError(
            "Gemini API key not found. Set it via Streamlit secrets "
            "(st.secrets['GEMINI_API_KEY']) or the GEMINI_API_KEY "
            "environment variable."
        )

    try:
        return _build_gemini_client(api_key, model_name)
    except Exception as e:
        raise GeminiConfigError(f"Failed to initialize the Gemini client: {str(e)}")


def check_gemini_connection(model_name: str = "gemini-2.5-flash"):
    """
    Verify that a working Gemini API connection can be established.

    This performs a lightweight call to confirm the API key is valid and
    the service is reachable. It does NOT generate any career
    recommendations - it only validates connectivity for diagnostic /
    setup purposes.

    Returns:
        dict: {"connected": bool, "message": str}
    """
    try:
        model = get_gemini_client(model_name)
    except GeminiConfigError as e:
        return {"connected": False, "message": str(e)}

    try:
        response = model.generate_content("Reply with the single word: OK")
        reply_text = (getattr(response, "text", "") or "").strip()
        if reply_text:
            return {"connected": True, "message": _t("Gemini API connection successful.")}
        return {
            "connected": False,
            "message": _t("Gemini API responded but returned no content. "
                        "The connection may be unstable."),
        }
    except Exception as e:
        return {
            "connected": False,
            "message": f"Gemini API call failed: {str(e)}",
        }


def make_response_fingerprint(*parts):
    """
    Build a stable fingerprint string from the given parts (questionnaire
    responses, personality responses, user_type, stream name, role name,
    etc). Used to decide whether a cached Gemini response can be reused or
    whether the underlying inputs changed and a fresh call is needed.
    """
    import hashlib
    normalized = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def safe_generate_json_content(model, prompt, max_output_tokens=2048):
    """
    Call Gemini with JSON response mode, falling back to a plain call if the
    `google-genai` SDK/model in use doesn't support a given config option
    (e.g. an older/different Gemini model that rejects response_mime_type
    or thinking_config).

    ROOT CAUSE OF "Unterminated string... response looks cut off mid-JSON":
    -------------------------------------------------------------------
    The model used by default is "gemini-2.5-flash", which is a *thinking*
    model. Gemini 2.5 models spend part of their max_output_tokens budget
    on an internal "thinking"/reasoning pass BEFORE emitting the visible
    answer text. That hidden thinking output is counted against the same
    max_output_tokens cap as the visible JSON answer.

    With max_output_tokens set as low as 1536-3072 (see the various call
    sites in this file), the thinking pass alone can consume most or all
    of that budget, leaving only a few hundred tokens (or fewer) for the
    actual JSON answer. The model then gets cut off mid-stream by the
    token limit, producing exactly the symptom reported: a response that
    *looks* like valid JSON at the start but ends abruptly with an
    unterminated string/object (response.candidates[0].finish_reason ==
    "MAX_TOKENS").

    This is NOT a markdown-fence problem and NOT a "Gemini returned plain
    text" problem - the existing ```json fence-stripping and json.loads()
    calls were already correct. The response text itself was genuinely
    incomplete JSON because it got truncated before parsing ever happened.

    THE FIX (two parts, both required):
    1. Explicitly disable the thinking budget via `thinking_config` so the
       full max_output_tokens budget is available for the visible JSON
       answer instead of being silently eaten by hidden reasoning tokens.
       This is tried first, with graceful fallbacks if the installed
       SDK/model doesn't support `thinking_config`.
    2. Surface the *real* reason for truncation (finish_reason) via the
       new `_log_gemini_debug_info()` helper below, called from every
       generate_ai_* function BEFORE the JSON is parsed - so any future
       failure is diagnosable from logs instead of guesswork.
    """
    base_config = {
        "max_output_tokens": max_output_tokens,
        "response_mime_type": "application/json",
    }

    # Attempt 1: JSON mode + thinking budget disabled - the real fix, since
    # it leaves the full max_output_tokens budget free for the JSON output
    # instead of letting hidden "thinking" tokens eat into it.
    try:
        return model.generate_content(
            prompt,
            generation_config={
                **base_config,
                "thinking_config": {"thinking_budget": 0},
            },
        )
    except Exception as e:
        print(f"[Gemini DEBUG] thinking_config not supported by this SDK/model "
              f"({e}); falling back to JSON mode without thinking_config.")

    # Attempt 2: JSON mode without thinking_config (older SDKs that don't
    # recognise the kwarg at all - same behaviour as before this fix).
    try:
        return model.generate_content(prompt, generation_config=base_config)
    except Exception as e:
        print(f"[Gemini DEBUG] response_mime_type=application/json not "
              f"supported ({e}); falling back to a plain text call.")

    # Attempt 3: plain call, no JSON mode (oldest SDKs). Markdown-fence
    # stripping in each parser below still handles this case.
    return model.generate_content(
        prompt,
        generation_config={"max_output_tokens": max_output_tokens},
    )


def _strip_markdown_json(response_text):
    """
    Remove markdown code-fence formatting (```json ... ``` or plain ``` ... ```)
    that Gemini sometimes wraps its JSON output in, even when JSON response
    mode is requested. Always strips surrounding whitespace first/last.

    This is intentionally a pure string transform with no parsing - it never
    raises, and it is safe to call on any text (including empty strings or
    text that isn't fenced at all, which is returned unchanged but
    whitespace-stripped).
    """
    cleaned = (response_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        # Drop a leading language hint like "json\n"
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return cleaned


def _safe_json_loads(cleaned_text, label):
    """
    The ONLY place in this file allowed to call json.loads() on Gemini
    output. Never call json.loads() directly on unchecked AI text anywhere
    else - always go through this function (or generate_validated_json
    below, which uses it).

    Validates the text first (non-empty, looks like a JSON object/array),
    then attempts to parse it. Catches json.JSONDecodeError specifically
    (rather than a bare except) so we never accidentally swallow unrelated
    bugs, logs the raw text that failed to parse for debugging, and returns
    None on any failure instead of raising - so a bad AI response can never
    crash the app.

    Returns:
        dict | list | None: the parsed JSON value, or None if the text was
        empty, structurally invalid, or not valid JSON.
    """
    if not cleaned_text:
        print(f"[Gemini DEBUG] [{label}] _safe_json_loads: empty text after cleaning - nothing to parse.")
        return None

    # Cheap structural pre-check before we even attempt to parse: a valid
    # JSON object/array must start with { or [. This catches "Gemini
    # returned plain prose instead of JSON" early, with a clear log line,
    # rather than letting json.loads() raise a more confusing error.
    if cleaned_text[0] not in "{[":
        print(f"[Gemini DEBUG] [{label}] _safe_json_loads: text does not start "
              f"with '{{' or '[' - this looks like plain text, not JSON. "
              f"First 200 chars: {cleaned_text[:200]!r}")
        return None

    try:
        return json.loads(cleaned_text, strict=False)
    except json.JSONDecodeError as e:
        print(f"[Gemini DEBUG] [{label}] _safe_json_loads: JSONDecodeError: {e}")
        print(f"[Gemini DEBUG] [{label}] raw text that failed to parse:\n{cleaned_text}")
        return None


def generate_validated_json(model, prompt, max_output_tokens, label, validator=None):
    """
    The single, shared "ask Gemini for JSON and get back a trustworthy
    Python object" pipeline. Every generate_ai_* function below should call
    this instead of hand-rolling its own generate -> strip -> json.loads()
    sequence.

    Pipeline:
        1. Call Gemini with JSON response mode (response_mime_type=
           "application/json") via safe_generate_json_content - this is the
           "always request structured JSON output" requirement; it's not
           optional/best-effort, every call site goes through it.
        2. Log the raw response (finish_reason, length, truncation check)
           for debugging, BEFORE any parsing is attempted.
        3. Strip markdown code fences if present (_strip_markdown_json).
        4. Validate + parse via _safe_json_loads - this NEVER calls
           json.loads() on unchecked text; it pre-validates structure and
           catches JSONDecodeError internally.
        5. If a `validator` callable was provided, run it on the parsed
           data. The validator is responsible for checking the response
           matches the EXPECTED SCHEMA (required fields present, right
           types/shape) while ignoring any optional/extra fields, and
           must either return the cleaned/normalised result or raise
           ValueError if the schema doesn't match. A schema mismatch is
           treated exactly like a JSON parse failure below - it triggers
           a regeneration, not a partially-displayed result.
        6. If parsing OR schema validation failed, retry the ENTIRE Gemini
           call exactly once (fresh request - a different sample from the
           model often succeeds even when the first one was truncated,
           malformed, or incomplete). The retry goes through the exact
           same log -> strip -> validate -> schema-check steps.
        7. If the retry also fails, return None. Callers MUST treat None as
           "could not get a usable, schema-valid response" and show a
           friendly error instead of proceeding - they must never fall
           back to displaying the raw/partially-parsed text.

    Returns:
        tuple[Any | None, str]: (validated_result_or_None, last_raw_response_text)
        When `validator` is given and succeeds, the first element is
        whatever the validator returned (its normalised result) rather
        than the raw parsed JSON. The raw text is always returned (even on
        failure) so callers can continue to store it in session_state for
        debugging, exactly as before.
    """
    # MODIFICATION (Gemini multilingual prompts): prepend the selected
    # language instruction before sending the prompt to Gemini, so the
    # response is generated directly in that language. Only the language
    # instruction is added - the rest of the prompt (schema/output format)
    # is untouched.
    prompt = f"{_gemini_language_instruction()}\n\n{prompt}"

    last_response_text = ""

    for attempt in (1, 2):
        attempt_label = f"{label} (attempt {attempt}/2)"
        response = safe_generate_json_content(model, prompt, max_output_tokens=max_output_tokens)
        response_text = (getattr(response, "text", "") or "").strip()
        last_response_text = response_text

        _log_gemini_debug_info(attempt_label, response, response_text)

        if not response_text:
            print(f"[Gemini DEBUG] [{attempt_label}] Gemini returned an empty response.")
            if attempt == 1:
                print(f"[Gemini DEBUG] [{label}] Retrying once with a fresh Gemini call...")
            continue

        cleaned = _strip_markdown_json(response_text)
        data = _safe_json_loads(cleaned, attempt_label)

        if data is None:
            print(f"[Gemini DEBUG] [{attempt_label}] JSON parsing failed.")
            if attempt == 1:
                print(f"[Gemini DEBUG] [{label}] Retrying once with a fresh Gemini call...")
            continue

        if validator is not None:
            try:
                validated = validator(data)
            except ValueError as schema_error:
                print(f"[Gemini DEBUG] [{attempt_label}] Schema validation failed: {schema_error}")
                if attempt == 1:
                    print(f"[Gemini DEBUG] [{label}] Response was valid JSON but did not match "
                          f"the expected schema - retrying once with a fresh Gemini call...")
                continue
            data = validated

        if attempt == 2:
            print(f"[Gemini DEBUG] [{label}] Succeeded on retry (attempt 2/2).")
        return data, response_text

    print(f"[Gemini DEBUG] [{label}] Both attempts failed to produce valid JSON. Giving up.")
    return None, last_response_text


def _log_gemini_debug_info(label, response, response_text):
    """
    Print full diagnostic info about a raw Gemini response BEFORE it is
    parsed as JSON. This is the "print the raw response" + truncation
    detection requested for debugging the parse-failure pipeline.

    Logs (to stdout / Streamlit's server console - never shown to the end
    user, so existing UI behaviour is unchanged):
      - which generation step this came from (label)
      - finish_reason for the first candidate, e.g. "STOP" (normal),
        "MAX_TOKENS" (truncated - the real bug), "SAFETY", "RECITATION".
      - the full raw text Gemini returned, exactly as received.
      - whether the text looks like markdown-fenced JSON, and whether it
        looks truncated (doesn't end in a closing brace/bracket/fence).
    """
    finish_reason = None
    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            raw_finish_reason = getattr(candidates[0], "finish_reason", None)
            # The new google-genai SDK returns a FinishReason enum (e.g.
            # FinishReason.STOP) rather than a plain string/int - normalize
            # to its bare name (e.g. "STOP") so the STOP/MAX_TOKENS string
            # comparisons below keep working exactly as before.
            finish_reason = getattr(raw_finish_reason, "name", raw_finish_reason)
    except Exception as e:
        finish_reason = f"<could not read finish_reason: {e}>"

    stripped = (response_text or "").strip()
    looks_fenced = stripped.startswith("```")
    looks_truncated = bool(stripped) and stripped[-1] not in "}]`"

    print(f"\n[Gemini DEBUG] ===== {label} =====")
    print(f"[Gemini DEBUG] finish_reason: {finish_reason}")
    print(f"[Gemini DEBUG] response length: {len(stripped)} chars")
    print(f"[Gemini DEBUG] markdown-fenced JSON: {looks_fenced}")
    print(f"[Gemini DEBUG] looks truncated (doesn't end in }}/]/`): {looks_truncated}")
    print(f"[Gemini DEBUG] raw response text:\n{response_text}")
    print(f"[Gemini DEBUG] ===== end {label} =====\n")

    if str(finish_reason) not in ("STOP", "1", "None"):
        print(f"[Gemini DEBUG] WARNING: finish_reason={finish_reason} - if this "
              f"is MAX_TOKENS, the response was cut off before completion. "
              f"Consider raising max_output_tokens for this call.")


# ==================== AI HELP CENTER (GEMINI-POWERED, NO STATIC CONTENT) ====================
# Everything shown on the Help page - the overview, the school-vs-college
# explanation, the step-by-step walkthrough, the FAQ, the tips, and answers
# to free-text search questions - is generated live by Gemini. Nothing here
# is a hardcoded string, JSON manual, or static template; this section only
# describes the app's CURRENT structure/flow to Gemini so it can write the
# guide itself. If new pages/features are added to `APP_STRUCTURE_CONTEXT`
# below, the AI guide automatically reflects them on the next generation -
# no manual help-text edits required.

APP_STRUCTURE_CONTEXT = """
APP NAME: AI Career Guidance Platform (built by CoActions)

WHAT IT DOES: The AI Career Guidance Platform is a Streamlit web app,
built by CoActions, that uses Google's Gemini AI to give students
personalized career guidance. There are no static/fixed career lists -
every recommendation, analysis, and report is generated live by AI based
on what the individual student answers.

WHO USES IT: Students in two groups -
  - School Students: Classes/Grades 9th, 10th, 11th, 12th.
  - College/Undergraduate Students: 1st, 2nd, 3rd, 4th Year College.
The questionnaire and AI prompts adapt automatically depending on which
group the student belongs to (school students get guidance framed around
subject streams and higher-education choices; college students get
guidance framed around specialization, internships, and job readiness).

FULL APP FLOW (in order):
1. Registration - student enters Full Name, Age, City, State, Institution,
   and Current Grade/Year.
2. The app automatically detects Student Type (School vs College) from the
   selected grade/year.
3. Career Questionnaire - the student answers a series of interest/aptitude
   questions relevant to their student type.
4. Personality Assessment (optional) - the student may choose to take an
   additional personality assessment for deeper insight, or skip it.
5. Top 3 AI Career Recommendations - Gemini analyzes all responses and
   generates the top 3 best-fit career streams with match percentages and
   reasoning.
6. The student selects one recommended career stream to explore further.
7. AI Analysis - Gemini generates a personalized Strengths & Opportunities
   analysis for the student based on their answers.
8. Career Overview - AI-generated overview of what that career involves.
9. Technical Skills - AI-generated list of technical skills needed.
10. Soft Skills - AI-generated list of soft skills needed.
11. Hiring Cities - AI-generated list of Indian cities with strong hiring
    activity for that career.
12. Industry Hubs - AI-generated list of industry hub regions for that
    career.
13. Education Path - AI-generated recommended education path/degrees.
14. Certifications - AI-generated relevant certifications.
15. Free Learning Resources - AI-generated free resources to start
    learning.
16. Related Career Roles - AI-generated list of related/adjacent roles the
    student can also explore.
17. Detailed Career Information - the student can click into any related
    role to see AI-generated detailed information about it.
18. AI Career Report - the student can generate and download a premium,
    multi-page PDF career report summarizing everything (charts, SWOT
    analysis, skill-gap analysis, and a learning roadmap), plus an offline
    AI Learning Roadmap with stage-by-stage checkboxes they can track
    progress against.

OTHER NAVIGATION: Home (restarts the assessment), About (what the AI
Career Guidance Platform and CoActions are), Contact (support
email/website), Help (this AI-powered help center).

IMPORTANT CONTEXT FOR YOUR ANSWERS: Every recommendation, analysis, skill
list, roadmap, and report in this app is generated dynamically by Gemini AI
based on the student's own answers - nothing is a fixed/static lookup
table. Make that clear where relevant. Keep answers encouraging, clear, and
written for a student audience (avoid overly technical jargon). Do not
invent features that are not listed above.
"""


def _build_help_guide_prompt():
    """
    Build the prompt asking Gemini to generate the FULL Help Center guide
    (overview, audience, school-vs-college distinction, step-by-step guide,
    FAQ, and tips) as a single structured JSON object, based on the current
    app structure described in APP_STRUCTURE_CONTEXT.
    """
    return f"""You are the in-app AI assistant for the AI Career Guidance
Platform (built by CoActions). Using ONLY the app structure described
below, write a friendly, clear, student-facing Help Guide.

{APP_STRUCTURE_CONTEXT}

Return ONLY a single JSON object (no markdown fences, no extra text) with
EXACTLY this shape:
{{
  "app_overview": "<2-4 sentences explaining what the AI Career Guidance Platform does>",
  "who_can_use": "<1-3 sentences on who this app is for>",
  "school_vs_college": "<2-4 sentences clearly explaining the difference between how School Students and College Students use the app>",
  "steps": [
    {{"step_number": 1, "title": "<short step title>", "description": "<1-2 sentence explanation of this step>"}}
    ... one entry for EVERY step in the FULL APP FLOW above, in order, same count (18 steps) ...
  ],
  "faqs": [
    {{"question": "<question>", "answer": "<clear 1-3 sentence answer>"}}
    ... at least 8 FAQ entries, covering things like: how recommendations are generated, whether the assessment can be retaken, whether the personality assessment is mandatory, what happens if it's skipped, whether recommendations are truly AI-generated, whether both school and college students can use the app, whether certifications/roadmaps are personalized, and how career roles are selected ...
  ],
  "tips": [
    "<short actionable tip>"
    ... at least 6 tips for getting the most useful AI recommendations (e.g. answering honestly, completing all questions, not rushing, reviewing all recommendations, exploring multiple paths, revisiting the assessment after gaining new skills) ...
  ]
}}

Do not wrap the JSON in markdown code fences. Do not include any text
before or after the JSON object."""


def _validate_help_guide_json(data):
    """
    Validate + normalise the JSON Gemini returns for the full Help Guide.
    Same pattern as the other validators in this file: raises ValueError on
    a missing/malformed shape (which generate_validated_json treats as a
    failure and retries once), otherwise returns a cleaned dict.
    """
    if not isinstance(data, dict):
        raise ValueError("Help guide response was not a JSON object.")

    app_overview = str(data.get("app_overview", "")).strip()
    who_can_use = str(data.get("who_can_use", "")).strip()
    school_vs_college = str(data.get("school_vs_college", "")).strip()
    if not app_overview or not who_can_use or not school_vs_college:
        raise ValueError("Help guide response is missing overview/audience text.")

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or len(raw_steps) < 5:
        raise ValueError("Help guide response did not contain a usable step-by-step guide.")
    steps = []
    for i, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        description = str(item.get("description", "")).strip()
        if not title:
            continue
        try:
            step_number = int(item.get("step_number", i))
        except (TypeError, ValueError):
            step_number = i
        steps.append({"step_number": step_number, "title": title, "description": description})
    if len(steps) < 5:
        raise ValueError("Help guide response had too few valid steps.")

    raw_faqs = data.get("faqs")
    if not isinstance(raw_faqs, list) or len(raw_faqs) < 3:
        raise ValueError("Help guide response did not contain a usable FAQ section.")
    faqs = []
    for item in raw_faqs:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        answer = str(item.get("answer", "")).strip()
        if question and answer:
            faqs.append({"question": question, "answer": answer})
    if len(faqs) < 3:
        raise ValueError("Help guide response had too few valid FAQ entries.")

    raw_tips = data.get("tips")
    if not isinstance(raw_tips, list):
        raise ValueError("Help guide response did not contain a tips list.")
    tips = [str(t).strip() for t in raw_tips if str(t).strip()]
    if len(tips) < 3:
        raise ValueError("Help guide response had too few valid tips.")

    return {
        "app_overview": app_overview,
        "who_can_use": who_can_use,
        "school_vs_college": school_vs_college,
        "steps": steps,
        "faqs": faqs,
        "tips": tips,
    }


def generate_ai_help_guide(force_refresh=False):
    """
    Generate (or reuse the cached) full AI Help Guide.

    Follows the same caching policy as the rest of the app's AI content:
    the guide is generated once and reused across reruns/navigation so
    opening the Help page repeatedly does not repeatedly call Gemini. Pass
    force_refresh=True (from the "Regenerate Guide" button) to force a
    fresh Gemini call.
    """
    if not force_refresh and st.session_state.get("ai_help_guide"):
        return {"status": "success"}

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.ai_help_guide = None
        st.session_state.ai_help_guide_status = str(e)
        return {"status": "error", "message": str(e)}

    prompt = _build_help_guide_prompt()
    data, _raw = generate_validated_json(
        model,
        prompt,
        max_output_tokens=4096,
        label="AI Help Guide",
        validator=_validate_help_guide_json,
    )

    if data is None:
        st.session_state.ai_help_guide = None
        st.session_state.ai_help_guide_status = _t(
            "The AI Help Guide is temporarily unavailable. Please try again in a moment."
        )
        return {"status": "error", "message": st.session_state.ai_help_guide_status}

    st.session_state.ai_help_guide = data
    st.session_state.ai_help_guide_status = None
    return {"status": "success"}


def generate_help_search_answer(user_question):
    """
    Answer a free-text question typed into the Help page search box, using
    Gemini and the same app-structure context as the full guide. This is a
    plain-text (not JSON) call since the output is a single conversational
    answer, not structured data.
    """
    user_question = (user_question or "").strip()
    if not user_question:
        return {"status": "error", "message": _t("Please type a question first.")}

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        return {"status": "error", "message": str(e)}

    prompt = f"""You are the in-app AI assistant for the AI Career Guidance
Platform (built by CoActions). Using ONLY the app structure described below,
answer the student's question clearly and concisely (2-5 sentences, plain
text, no markdown headers, no JSON).

{APP_STRUCTURE_CONTEXT}

STUDENT QUESTION: {user_question}

Answer the question directly. If the question is unrelated to using this
app, politely say you can only help with questions about using the AI
Career Guidance Platform."""

    # MODIFICATION (Gemini multilingual prompts): prepend the selected
    # language instruction before sending the prompt to Gemini.
    prompt = f"{_gemini_language_instruction()}\n\n{prompt}"

    try:
        response = model.generate_content(
            prompt,
            generation_config={
                "max_output_tokens": 1024,
                "thinking_config": {"thinking_budget": 0},
            },
        )
        answer_text = (getattr(response, "text", "") or "").strip()
    except Exception as e:
        return {"status": "error", "message": f"{_t('Gemini call failed:')} {str(e)}"}

    if not answer_text:
        return {"status": "error", "message": _t("AI did not return an answer. Please try again.")}

    return {"status": "success", "answer": answer_text}


# ==================== AI PROMPT BUILDER ====================

def generate_ai_prompt(student_details, questionnaire_responses, personality_responses, user_type):
    """
    Build the complete, dynamic prompt that will be sent to Gemini.

    This function ONLY assembles prompt text - it does not call the
    Gemini API itself.

    Args:
        student_details (dict): Student registration info. Expected keys
            include 'name', 'age', 'institution', 'city', 'state', 'grade'.
        questionnaire_responses (dict): Raw {question_id: rating} answers
            from the career questionnaire.
        personality_responses (dict): Raw {question_id: answer} answers
            from the optional personality assessment (may be empty).
        user_type (str): Either 'school' or 'college'.

    Returns:
        str: A complete prompt instructing Gemini to analyse the
        student's interests, aptitude, learning style, and career
        preferences, and to return ONLY the top 3 recommended career
        streams as a small, strict JSON payload (stream_name,
        match_percentage, reason per stream) - nothing else. This is the
        FIRST of several Gemini calls in the app; all other details
        (skills, salary, companies, roadmap, certifications, education
        path, future scope, resources, strengths/opportunities analysis)
        are deliberately deferred to later, separate API calls
        (generate_ai_analysis / generate_ai_deep_dive /
        generate_ai_role_detail) so this first request stays small, fast,
        and reliable.
    """
    name = student_details.get('name', 'The student')
    age = student_details.get('age', 'Not specified')
    grade = student_details.get('grade', 'Not specified')
    education_level = "School Student" if user_type == 'school' else "College Student"
    grade_label = "Grade" if user_type == 'school' else "Year"

    questionnaire_responses = questionnaire_responses or {}
    personality_responses = personality_responses or {}

    # ---- OPTIMIZATION 1: compact interest summary instead of raw Q&A ----
    # Gemini doesn't need every individual question/answer pair (often
    # ~25 separate "- Question N: answer" lines) - it only needs the
    # *aggregate signal* those answers produce: how strongly the student
    # leans toward each interest category. `calculate_results()` already
    # turns the raw answers into a 0-100 score per category and stores it
    # in st.session_state.categories_data once the questionnaire is
    # submitted, so we reuse that instead of resending raw answers. This
    # collapses ~25 lines of raw Q&A into a handful of "Category: score"
    # lines carrying the same decision-relevant information.
    categories_data = st.session_state.get('categories_data') or {}
    scored_categories = sorted(
        (
            (cat.get('name') or cat.get('category_name') or cat_id, round(cat.get('score', 0)))
            for cat_id, cat in categories_data.items()
            if cat.get('question_count', 0) > 0
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    if scored_categories:
        # OPTIMIZATION 2: cap to the top categories. Only the
        # highest-scoring categories can plausibly drive a "top 3"
        # recommendation - low-scoring ones add tokens without adding
        # decision-relevant signal, so the tail is dropped.
        interest_lines = "\n".join(f"{cat_name}: {score}" for cat_name, score in scored_categories[:6])
    elif questionnaire_responses:
        # Fallback if scores aren't available yet - still avoid dumping
        # every raw answer; just note that responses exist.
        interest_lines = f"{len(questionnaire_responses)} questionnaire questions answered (scores unavailable)."
    else:
        interest_lines = "No questionnaire responses provided."

    # ---- FIXED STREAM LIST: recommendations must come from the app's own
    # general career-stream categories (loaded from school_questions.json /
    # college_questions.json), never AI-invented hybrid/niche titles like
    # "Digital Marketing & Communications". `scored_categories` above
    # already holds every general stream defined for this student's
    # questionnaire (typically 16), ranked by interest score - reuse the
    # FULL list (not just the top 6) so Gemini knows the complete valid
    # option space, even for streams that scored lower. ----
    allowed_streams = [cat_name for cat_name, _ in scored_categories]
    if allowed_streams:
        allowed_streams_lines = "\n".join(f"- {name}" for name in allowed_streams)
        stream_constraint = f"""ALLOWED STREAMS (choose your top 3 ONLY from this exact list - do not invent, combine, rename, or rephrase any stream; copy the stream_name text exactly as written below):
{allowed_streams_lines}"""
    else:
        # Defensive fallback - should not normally happen, since
        # categories_data is always populated before this page is reached.
        stream_constraint = "No fixed stream list is available for this student - recommend general, broad career streams only."

    # ---- OPTIMIZATION 3: compact personality summary instead of raw Q&A ----
    # The learning-style personality quiz has been replaced by the RIASEC
    # assessment, so personality_pathway is always None now and this
    # simply falls back to "Not taken (optional)." below. Left in place
    # (rather than removed) since other generate_ai_* prompt functions
    # still reference this same session_state key defensively.
    pathway = st.session_state.get('personality_pathway')
    if pathway:
        traits = "\n".join(f"- {t}" for t in pathway.get('strengths', [])[:4])
        personality_summary = f"{pathway.get('title', 'N/A')} ({pathway.get('match_percentage', 0)}% match)"
        if traits:
            personality_summary += f"\nTop Personality Traits:\n{traits}"
    elif personality_responses:
        personality_summary = f"{len(personality_responses)} personality questions answered (summary unavailable)."
    else:
        personality_summary = "Not taken (optional)."

    # ---- OPTIMIZATION 4: one short, non-repeated depth instruction ----
    # The original spelled out similar guidance (adapt response, language
    # level, scope) across several sentences per user_type. Reduced to a
    # single line carrying only the information that actually changes
    # model behaviour. NOTE: this now only controls the LANGUAGE/tone of
    # the "reason" text - the stream_name itself is always constrained to
    # the ALLOWED STREAMS list above, for both school and college students.
    if user_type == 'school':
        depth_instruction = "School student: use simple, jargon-free language in the reason text."
    else:
        depth_instruction = "College student: use professional, industry-appropriate language in the reason text."

    # ---- OPTIMIZATION 5: removed duplicated/repeated instructions ----
    # The original stated "JSON only, no markdown/preamble" twice,
    # repeated the "don't generate skills/salary/etc." instruction across
    # two separate paragraphs, and explained internal app architecture
    # (deferred follow-up calls) that Gemini has no use for. All of that
    # is removed - the single OUTPUT FORMAT line below is now the only
    # statement of what to return.
    prompt = f"""You are an expert career counsellor AI. Recommend the TOP 3 career streams for this student, ranked strongest to weakest match, based on the summary below.

STUDENT: {name}, age {age}, {education_level} ({grade_label} {grade})

INTEREST SCORES (0-100)
{interest_lines}

{stream_constraint}

PERSONALITY/LEARNING STYLE
{personality_summary}

{depth_instruction}

Return ONLY this JSON (no markdown, no commentary), exactly 3 items, each reason <=15 words, single-line strings, stream_name copied exactly from the ALLOWED STREAMS list above:
{{"recommendations": [{{"stream_name": "string", "match_percentage": 60-99, "reason": "string"}}]}}"""

    return prompt




# ==================== JSON RESPONSE PARSING HELPER ====================

def _parse_top3_json(data, allowed_streams=None):
    """
    Validate that an already-parsed JSON value (dict or list, as returned
    by generate_validated_json / _safe_json_loads) matches the EXPECTED
    SCHEMA for the first/lightweight Gemini call, and normalise it into a
    list of exactly-3 recommendation dicts: {"stream_name": str,
    "match_percentage": int, "explanation": str}.

    Used as the `validator` passed into generate_validated_json, so if this
    raises ValueError, generate_validated_json treats it the same as a
    malformed/incomplete response and automatically requests ONE
    regeneration from Gemini - the caller never sees or displays a
    partially-parsed result.

    Expected schema per recommendation entry: {"stream_name": <non-empty
    string>, "match_percentage": <number>, "reason": <non-empty string>}.
    ("explanation" is also accepted as a fallback key name, for backward
    compatibility with prompt revisions.) Any OTHER fields Gemini might
    add are simply ignored - this validator only enforces the 3 fields it
    actually needs.

    If `allowed_streams` is given (the fixed list of general stream names
    from categories_data), every returned stream_name is snapped to the
    matching entry in that list (case-insensitive exact match, falling
    back to a close-match lookup for minor casing/whitespace drift) so the
    UI never displays an AI-invented stream name that isn't one of the
    app's own general streams. Entries that can't be matched to any
    allowed stream are dropped rather than shown as-is.

    NOTE: this function does NOT call json.loads() or touch raw text at
    all - by the time data reaches here it has already been safely parsed
    upstream. It only checks/normalises *shape*.

    Raises:
        ValueError: if `recommendations` is missing, not a list, empty, or
            does not contain at least 3 entries with all required fields
            present and non-empty (after stream-name matching, when
            `allowed_streams` is provided). This is treated as an
            "incomplete response" and triggers a regeneration.
    """
    recommendations = data.get("recommendations") if isinstance(data, dict) else data

    if not isinstance(recommendations, list) or len(recommendations) == 0:
        raise ValueError("Gemini response did not contain a recommendations list.")

    # Build a case-insensitive lookup so a minor casing/whitespace
    # difference from Gemini still snaps to the canonical stream name.
    allowed_lookup = {}
    if allowed_streams:
        allowed_lookup = {name.strip().lower(): name for name in allowed_streams}

    parsed = []
    seen_streams = set()
    for item in recommendations:
        if not isinstance(item, dict):
            continue

        stream_name = str(item.get("stream_name", "")).strip()
        # "reason" is the field requested in the lightweight first prompt;
        # "explanation" is accepted as a fallback for compatibility.
        reason = str(item.get("reason") or item.get("explanation") or "").strip()
        try:
            match_percentage = int(round(float(item.get("match_percentage", 0))))
        except (TypeError, ValueError):
            match_percentage = 0
        match_percentage = max(0, min(100, match_percentage))

        # Required fields: stream_name and reason must both be present and
        # non-empty. Optional/extra fields (anything else Gemini might add)
        # are simply ignored rather than causing a failure.
        if not stream_name or not reason:
            continue

        if allowed_lookup:
            canonical = allowed_lookup.get(stream_name.strip().lower())
            if canonical is None:
                # Try a fuzzy match for minor drift (e.g. trailing
                # punctuation) before giving up on this entry entirely.
                close = difflib.get_close_matches(
                    stream_name.strip().lower(), allowed_lookup.keys(), n=1, cutoff=0.8
                )
                canonical = allowed_lookup.get(close[0]) if close else None
            if canonical is None:
                # Not one of the app's general streams - drop it rather
                # than show an AI-invented stream name.
                continue
            stream_name = canonical

        if stream_name in seen_streams:
            continue
        seen_streams.add(stream_name)

        parsed.append({
            "stream_name": stream_name,
            "match_percentage": match_percentage,
            "explanation": reason,
        })

    if len(parsed) < 3:
        raise ValueError(
            f"Incomplete recommendations response: expected 3 complete "
            f"entries (stream_name + match_percentage + reason) matching "
            f"the allowed stream list, got {len(parsed)} valid out of "
            f"{len(recommendations)} returned."
        )

    return parsed[:3]


# ==================== AI RECOMMENDATION (GEMINI COMMUNICATION) ====================

def generate_ai_recommendation(student_details, questionnaire_responses, personality_responses, user_type):
    """
    Send the dynamically built prompt to Gemini, parse its JSON response into
    the Top 3 recommended career streams, and store the structured result in
    session state.

    This function is responsible ONLY for the Streamlit <-> Gemini
    communication: building the prompt, calling the API, parsing the
    response, and storing the result. It deliberately does NOT render any
    UI - that is handled by show_recommendation().

    Args:
        student_details (dict): Student registration info.
        questionnaire_responses (dict): Raw career questionnaire responses.
        personality_responses (dict): Raw personality assessment responses.
        user_type (str): Either 'school' or 'college'.

    Returns:
        dict: A status payload, e.g.
            {"status": "success", "message": _t("...")} or
            {"status": "error", "message": _t("...")}
        The actual AI-generated Top 3 recommendations (if any) are stored
        separately in st.session_state.ai_top_streams as a list of
        {"stream_name", "match_percentage", "explanation"} dicts - this is
        the ONLY source of recommendation data; no manual/static data is
        ever used.
    """
    prompt = generate_ai_prompt(student_details, questionnaire_responses, personality_responses, user_type)

    # Same fixed general-stream list used inside generate_ai_prompt() to
    # build the ALLOWED STREAMS instruction - reused here so the validator
    # can snap/reject stream names against the exact same list, keeping
    # the prompt and the validation in sync.
    categories_data = st.session_state.get('categories_data') or {}
    allowed_streams = [
        cat.get('name') or cat.get('category_name') or cat_id
        for cat_id, cat in categories_data.items()
        if cat.get('question_count', 0) > 0
    ]

    def _validator(data):
        return _parse_top3_json(data, allowed_streams=allowed_streams)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.gemini_response_raw = None
        st.session_state.gemini_response_error = str(e)
        st.session_state.ai_top_streams = None
        return {
            "status": "error",
            "message": _t("AI recommendations are temporarily unavailable "
                        "(Gemini API is not configured). Please try again later."),
        }

    try:
        # `validator=_parse_top3_json` makes schema validation part of the
        # retry loop itself: if Gemini returns syntactically valid JSON
        # that is nonetheless missing recommendations / required fields
        # (an "incomplete response"), generate_validated_json treats that
        # exactly like a parse failure and automatically regenerates once
        # before giving up - so st.session_state.ai_top_streams is only
        # ever assigned a fully-validated list of exactly 3 entries, never
        # a partially-parsed or incomplete result.
        #
        # max_output_tokens is intentionally small (512) because this
        # first-pass prompt now asks for ONLY 3 short fields per stream
        # (stream_name, match_percentage, a one-line reason) - no skills,
        # salary, companies, roadmap, certifications, education path,
        # future scope, resources, or analysis. Keeping the budget tight
        # keeps this first request fast and leaves no room for the model
        # to drift into the longer sections that are deferred to later
        # calls (generate_ai_analysis / generate_ai_deep_dive /
        # generate_ai_role_detail).
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=512,
            label="generate_ai_recommendation",
            validator=_validator,
        )

        # Always store the raw response for debugging - not displayed in
        # the UI - regardless of whether parsing ultimately succeeded.
        st.session_state.gemini_response_raw = response_text or None

        if data is None:
            truncated_hint = (
                " (response looks cut off mid-JSON - likely hit the output "
                "token limit; try increasing max_output_tokens)"
                if response_text and not response_text.rstrip().endswith("}")
                else ""
            )
            st.session_state.ai_top_streams = None
            st.session_state.gemini_response_error = (
                "Could not get valid AI recommendations: Gemini's response "
                f"was empty, invalid JSON, or incomplete (missing the "
                f"recommendations list or required fields), even after a "
                f"retry{truncated_hint}."
            )
            return {
                "status": "error",
                "message": _t("AI recommendations could not be generated right now. "
                            "Please try again later."),
            }

        # `data` here is ALREADY the fully-validated, normalised list of
        # exactly 3 {"stream_name", "match_percentage", "explanation"}
        # dicts produced by _parse_top3_json inside generate_validated_json
        # - it is safe to store and display directly.
        st.session_state.ai_top_streams = data
        st.session_state.gemini_response_error = None
        return {
            "status": "success",
            "message": _t("Your personalized AI recommendations have been generated."),
        }
    except Exception as e:
        st.session_state.gemini_response_raw = None
        st.session_state.gemini_response_error = str(e)
        st.session_state.ai_top_streams = None
        return {
            "status": "error",
            "message": _t("Something went wrong while contacting the AI recommendation "
                        "service. Please try again later."),
        }


# ==================== AI ANALYSIS (STRENGTHS + OPPORTUNITIES ONLY) ====================

def generate_ai_analysis_prompt(student_details, questionnaire_responses, personality_responses, stream, user_type):
    """
    Build the prompt for the AI Analysis page. Gemini must return ONLY
    Strengths and Opportunities (never Weaknesses or Threats), personalized
    to the student's details, questionnaire responses, personality
    responses, and the stream they selected.
    """
    name = student_details.get('name', 'The student')
    age = student_details.get('age', 'Not specified')
    institution = student_details.get('institution', 'Not specified')
    grade = student_details.get('grade', 'Not specified')
    education_level = "School Student" if user_type == 'school' else "College Student"

    questionnaire_responses = questionnaire_responses or {}
    personality_responses = personality_responses or {}

    # Reuse the same compact interest-score summary approach used in
    # generate_ai_prompt(), rather than dumping raw Q&A pairs, so this
    # stays token-efficient while still carrying the same decision signal.
    categories_data = st.session_state.get('categories_data') or {}
    scored_categories = sorted(
        (
            (cat.get('name') or cat.get('category_name') or cat_id, round(cat.get('score', 0)))
            for cat_id, cat in categories_data.items()
            if cat.get('question_count', 0) > 0
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    if scored_categories:
        questionnaire_lines = "\n".join(f"{cat_name}: {score}" for cat_name, score in scored_categories[:6])
    elif questionnaire_responses:
        questionnaire_lines = f"{len(questionnaire_responses)} questionnaire questions answered (scores unavailable)."
    else:
        questionnaire_lines = "No questionnaire responses provided."

    # Compact personality summary, same pattern as generate_ai_prompt().
    pathway = st.session_state.get('personality_pathway')
    if pathway:
        traits = "\n".join(f"- {t}" for t in pathway.get('strengths', [])[:4])
        personality_summary = f"{pathway.get('title', 'N/A')} ({pathway.get('match_percentage', 0)}% match)"
        if traits:
            personality_summary += f"\nTop Personality Traits:\n{traits}"
    elif personality_responses:
        personality_summary = f"{len(personality_responses)} personality questions answered (summary unavailable)."
    else:
        personality_summary = "Not taken (optional)."

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: use SIMPLE, jargon-free language with short "
            "sentences and everyday vocabulary; explain every concept simply; keep "
            "strengths/opportunities framed around beginner-friendly stream selection "
            "rather than specific job roles."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: use PROFESSIONAL, industry-appropriate "
            "language; where relevant, reference specific industry roles, "
            "specializations, skills, tools, or market context for advanced career "
            "guidance."
        )

    prompt = f"""You are an expert career counsellor AI. The student below selected the
career stream "{stream}" from your earlier recommendations.

STUDENT INFORMATION
- Name: {name}
- Age: {age}
- Institution: {institution}
- Grade/Year: {grade}
- Education Level: {education_level}

CAREER QUESTIONNAIRE RESPONSES (INTEREST SCORES 0-100)
{questionnaire_lines}

PERSONALITY / LEARNING STYLE
{personality_summary}

TASK
Generate a personalized AI Analysis for this student about the "{stream}" stream,
based on their student information, questionnaire responses, and personality
profile above. The analysis must include EXACTLY two sections:
1. Strengths - personal strengths/traits this student shows (from their
   responses) that support success in this stream.
2. Opportunities - opportunities this stream and the student's profile open up
   for them (growth areas, future possibilities, advantages).

Do NOT include Weaknesses or Threats in any form - they must be completely
omitted. {depth_instruction}

Do NOT use any pre-existing/manual data - generate everything freshly for
this student.

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "strengths": ["string", "string", "string"],
  "opportunities": ["string", "string", "string"]
}}

Provide 3 to 5 concise bullet-point style strings in each array. Every string value must be a single line with no literal line breaks (use spaces instead). Output JSON only."""

    return prompt


def _validate_analysis_schema(data):
    """
    Schema validator for the AI Analysis response, passed into
    generate_validated_json so a structurally-valid-but-incomplete response
    (e.g. missing or empty strengths/opportunities) is treated the same as
    a JSON parse failure and triggers the SAME single automatic retry,
    instead of being silently accepted or only caught after the fact.

    Raises ValueError (caught by generate_validated_json) if the schema
    doesn't match; otherwise returns the normalised {"strengths": [...],
    "opportunities": [...]} dict.
    """
    strengths = data.get("strengths", []) if isinstance(data, dict) else []
    opportunities = data.get("opportunities", []) if isinstance(data, dict) else []

    if not isinstance(strengths, list) or not isinstance(opportunities, list) \
            or len(strengths) == 0 or len(opportunities) == 0:
        raise ValueError("AI Analysis response missing strengths/opportunities.")

    return {
        "strengths": [str(s).strip() for s in strengths if str(s).strip()],
        "opportunities": [str(o).strip() for o in opportunities if str(o).strip()],
    }


def generate_ai_analysis(student_details, questionnaire_responses, personality_responses, stream, user_type):
    """
    Call Gemini to produce the Strengths/Opportunities AI Analysis and store
    the parsed result in st.session_state.ai_analysis.

    Personalized using student details, questionnaire responses,
    personality responses, and the selected career stream.

    RELIABILITY: uses generate_validated_json, which already (a) retries
    the Gemini call exactly ONCE if JSON parsing OR schema validation
    fails - never more, so there is no risk of an infinite retry loop -
    and (b) never calls json.loads() on unchecked text. The technical
    failure detail is stored separately in st.session_state.ai_analysis_error
    for an expandable debug section; the message returned to the UI is
    always a short, friendly, non-technical sentence.
    """
    prompt = generate_ai_analysis_prompt(student_details, questionnaire_responses, personality_responses, stream, user_type)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.ai_analysis = None
        st.session_state.ai_analysis_error = str(e)
        return {"status": "error", "message": _t("AI Analysis is temporarily unavailable. Please try again later.")}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=1536,
            label="generate_ai_analysis",
            validator=_validate_analysis_schema,
        )

        if data is None:
            st.session_state.ai_analysis = None
            st.session_state.ai_analysis_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "strengths/opportunities, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": _t("AI Analysis could not be generated right now. Please try again later."),
            }

        st.session_state.ai_analysis = data
        st.session_state.ai_analysis_error = None
        return {"status": "success", "message": _t("AI Analysis generated.")}
    except Exception as e:
        st.session_state.ai_analysis = None
        st.session_state.ai_analysis_error = str(e)
        return {
            "status": "error",
            "message": _t("Something went wrong while generating your AI Analysis. Please try again later."),
        }


# ==================== CAREER OVERVIEW (AI-GENERATED, SHOWN ON AI ANALYSIS PAGE) ====================

def generate_career_overview_prompt(student_details, questionnaire_responses, personality_responses, stream, user_type):
    """
    Build the prompt for the Career Overview card shown right after the
    Strengths/Opportunities AI Analysis. Entirely AI-generated - no
    hardcoded or JSON-file-based career descriptions of any kind.
    """
    name = student_details.get('name', 'The student')
    age = student_details.get('age', 'Not specified')
    grade = student_details.get('grade', 'Not specified')
    education_level = "School Student" if user_type == 'school' else "College Student"

    questionnaire_responses = questionnaire_responses or {}
    personality_responses = personality_responses or {}

    categories_data = st.session_state.get('categories_data') or {}
    scored_categories = sorted(
        (
            (cat.get('name') or cat.get('category_name') or cat_id, round(cat.get('score', 0)))
            for cat_id, cat in categories_data.items()
            if cat.get('question_count', 0) > 0
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    if scored_categories:
        questionnaire_lines = "\n".join(f"{cat_name}: {score}" for cat_name, score in scored_categories[:6])
    elif questionnaire_responses:
        questionnaire_lines = f"{len(questionnaire_responses)} questionnaire questions answered (scores unavailable)."
    else:
        questionnaire_lines = "No questionnaire responses provided."

    pathway = st.session_state.get('personality_pathway')
    if pathway:
        traits = "\n".join(f"- {t}" for t in pathway.get('strengths', [])[:4])
        personality_summary = f"{pathway.get('title', 'N/A')} ({pathway.get('match_percentage', 0)}% match)"
        if traits:
            personality_summary += f"\nTop Personality Traits:\n{traits}"
    elif personality_responses:
        personality_summary = f"{len(personality_responses)} personality questions answered (summary unavailable)."
    else:
        personality_summary = "Not taken (optional)."

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: use SIMPLE, jargon-free, everyday language "
            "with short sentences; explain every concept simply, as if introducing "
            "the career for the very first time."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: use PROFESSIONAL, industry-level language; "
            "reference real specializations, tools, and current market context "
            "where relevant."
        )

    prompt = f"""You are an expert career counsellor AI. The student below selected the
career stream "{stream}" from your earlier recommendations, and has already
received a Strengths/Opportunities analysis for it.

STUDENT INFORMATION
- Name: {name}
- Age: {age}
- Education Level: {education_level} (Grade/Year: {grade})

CAREER QUESTIONNAIRE RESPONSES (INTEREST SCORES 0-100)
{questionnaire_lines}

PERSONALITY / LEARNING STYLE
{personality_summary}

TASK
Generate a complete, fresh Career Overview for "{stream}" personalized to this
student. Do NOT use any pre-existing/manual database or JSON file of career
descriptions - generate everything freshly for this student. {depth_instruction}

Generate EXACTLY these six sections:
1. career_description - what this career/stream actually involves, in a clear paragraph.
2. why_matches - specifically why this career matches THIS student, referencing their interest scores and/or personality traits above.
3. daily_responsibilities - a list of typical day-to-day responsibilities/activities in this career.
4. future_scope - a short paragraph on the growth/demand outlook for this career.
5. career_growth - a short paragraph or list describing how a person typically grows/advances in this career over time.
6. related_career_roles - a list of 2 to 4 GROUPS of related career roles within/adjacent to "{stream}" that this student could explore, dynamically generated (do NOT use any predefined/static list). Each group is an object with:
   - category: a short, natural category name for the group (e.g. "Technology & Engineering", "Research & Analytics") - generate a category name that actually fits this stream, not a fixed template.
   - roles: a list of 4 to 6 specific job-title roles in that category this student could pursue.

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "career_description": "string",
  "why_matches": "string",
  "daily_responsibilities": ["string", "..."],
  "future_scope": "string",
  "career_growth": "string",
  "related_career_roles": [
    {{"category": "string", "roles": ["string", "..."]}}
  ]
}}

Every string value must be a single line with no literal line breaks (use
spaces instead). Output JSON only."""

    return prompt


def _validate_career_overview_schema(data):
    """
    Schema validator for the Career Overview response, passed into
    generate_validated_json so an incomplete response triggers the same
    bounded single automatic retry as a JSON parse failure.
    """
    required_keys = [
        "career_description", "why_matches", "daily_responsibilities",
        "future_scope", "career_growth", "related_career_roles",
    ]
    if not isinstance(data, dict) or not all(k in data for k in required_keys):
        raise ValueError("Career Overview response missing one or more required sections.")

    responsibilities = data.get("daily_responsibilities", [])
    if not isinstance(responsibilities, list) or len(responsibilities) == 0:
        raise ValueError("Career Overview response missing daily_responsibilities.")

    groups = data.get("related_career_roles", [])
    if not isinstance(groups, list) or len(groups) == 0:
        raise ValueError("Career Overview response missing related_career_roles groups.")
    cleaned_groups = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        category = str(g.get("category", "")).strip()
        roles = [str(r).strip() for r in (g.get("roles") or []) if str(r).strip()]
        if category and roles:
            cleaned_groups.append({"category": category, "roles": roles})
    if not cleaned_groups:
        raise ValueError("Career Overview response has no valid related_career_roles groups.")

    data["daily_responsibilities"] = [str(r).strip() for r in responsibilities if str(r).strip()]
    data["related_career_roles"] = cleaned_groups
    return data


def generate_career_overview(student_details, questionnaire_responses, personality_responses, stream, user_type):
    """
    Call Gemini to produce the Career Overview card and store the parsed
    result in st.session_state.career_overview.

    Personalized using student details, questionnaire responses,
    personality responses, and the selected career stream - entirely
    AI-generated, no JSON files or hardcoded descriptions.

    RELIABILITY: same pattern as generate_ai_analysis / generate_ai_deep_dive -
    generate_validated_json retries once on parse/schema failure, technical
    detail goes to career_overview_error, and the UI message stays short and
    friendly.
    """
    prompt = generate_career_overview_prompt(student_details, questionnaire_responses, personality_responses, stream, user_type)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.career_overview = None
        st.session_state.career_overview_error = str(e)
        return {"status": "error", "message": _t("Career Overview is temporarily unavailable. Please try again later.")}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=3072,
            label="generate_career_overview",
            validator=_validate_career_overview_schema,
        )

        if data is None:
            st.session_state.career_overview = None
            st.session_state.career_overview_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "required sections, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": _t("Career Overview could not be generated right now. Please try again later."),
            }

        st.session_state.career_overview = data
        st.session_state.career_overview_error = None
        return {"status": "success", "message": _t("Career Overview generated.")}
    except Exception as e:
        st.session_state.career_overview = None
        st.session_state.career_overview_error = str(e)
        return {
            "status": "error",
            "message": _t("Something went wrong while generating the Career Overview. Please try again later."),
        }


# ==================== DEEP-DIVE CAREER REPORT (FULL AI REPORT) ====================

def generate_ai_deep_dive_prompt(student_details, questionnaire_responses, stream, user_type):
    """
    Build the prompt for the full post-analysis career report. Entirely
    AI-generated - no manual/static data of any kind.
    """
    name = student_details.get('name', 'The student')
    education_level = "School Student" if user_type == 'school' else "College Student"

    questionnaire_responses = questionnaire_responses or {}

    # Compact interest-score summary (same pattern as the other AI calls)
    # so skills/opportunities are personalized to the student's actual
    # questionnaire signal rather than raw, token-heavy Q&A pairs.
    categories_data = st.session_state.get('categories_data') or {}
    scored_categories = sorted(
        (
            (cat.get('name') or cat.get('category_name') or cat_id, round(cat.get('score', 0)))
            for cat_id, cat in categories_data.items()
            if cat.get('question_count', 0) > 0
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    if scored_categories:
        questionnaire_lines = "\n".join(f"{cat_name}: {score}" for cat_name, score in scored_categories[:6])
    elif questionnaire_responses:
        questionnaire_lines = f"{len(questionnaire_responses)} questionnaire questions answered (scores unavailable)."
    else:
        questionnaire_lines = "No questionnaire responses provided."

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: use SIMPLE language throughout; keep all "
            "sections BEGINNER-FRIENDLY and focused on foundational next steps "
            "(subjects to study, basic awareness of the field, stream selection) "
            "rather than deep professional detail; explain every concept simply."
        )
        education_path_instruction = """9. education_path - an OBJECT (not a string) describing this student's education roadmap into "{stream}", with EXACTLY these keys, each a short, simple, personalized string:
   - recommended_stream: the school stream (e.g. Science/Commerce/Arts/Vocational) and subject combination recommended for this student.
   - undergraduate_degree: the undergraduate degree(s) this student should pursue after school for this career.
   - higher_studies: postgraduate/higher-studies options relevant to this career, explained simply.
   - certifications: a list of 3 to 5 beginner-friendly certifications/courses a school student could start with.""".replace("{stream}", stream)
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: use PROFESSIONAL, industry-appropriate "
            "language; keep all sections at an INDUSTRY/PROFESSIONAL level of "
            "depth, name specific industry roles and SPECIALIZATIONS, and give "
            "ADVANCED CAREER GUIDANCE referencing real-world skills, tools, "
            "certifications, and the current job market."
        )
        education_path_instruction = """9. education_path - an OBJECT (not a string) describing this student's education/career roadmap into "{stream}", with EXACTLY these keys, each a professional, industry-level string:
   - higher_education_options: postgraduate/advanced degree options relevant to this career (e.g. specific Master's programs, MBA routes, doctoral paths where relevant).
   - professional_certifications: a list of 4 to 6 industry-recognized professional certifications for this career.
   - specializations: a list of 3 to 6 specific specializations/tracks within this career this student could pursue.
   - career_advancement: a short paragraph describing typical career advancement/promotion path in this field.""".replace("{stream}", stream)

    prompt = f"""You are an expert career counsellor AI generating a complete career report
for the stream "{stream}" for the student below.

STUDENT INFORMATION
- Name: {name}
- Education Level: {education_level}

CAREER QUESTIONNAIRE RESPONSES (INTEREST SCORES 0-100)
{questionnaire_lines}

{depth_instruction}

Do NOT use any pre-existing/manual database, JSON file, or predefined/static
list of any kind (education paths, certifications, or otherwise) - generate
every field freshly and specifically for this student, their student type,
and their selected career. Do NOT include a "Software Skills" section,
field, or category under any name - it must not appear anywhere in your
output.

Generate EXACTLY these sections, each personalized to the student:
1. career_overview - a short paragraph describing what this career stream involves.
2. related_career_roles - a list of 5 to 8 specific job-title roles within this stream that this student could pursue (e.g. specific roles, not subfields).
3. future_scope - a short paragraph on growth/demand outlook for this stream.
4. technical_skills - a list of AT LEAST 8 specific technical skills required for this career, personalized to this student's questionnaire responses and student type.
5. soft_skills - a list of AT LEAST 8 soft skills required for this career, personalized to this student's questionnaire responses and student type.
6. major_hiring_cities_india - a list of 5 to 8 major Indian cities where this stream has strong hiring demand.
7. major_industry_hubs - a list of 4 to 6 major industry hubs/clusters in India for this career (e.g. specific tech parks, industrial corridors, or regional clusters known for this field).
8. top_hiring_industries - a list of 5 to 8 specific industries/sectors that actively hire for this career.
{education_path_instruction}
10. learning_resources - an OBJECT (not a list) with EXACTLY these keys, each personalized to "{stream}" and this student:
   - recommended_certifications: a list of 4 to 6 certifications worth pursuing for this specific career (generated fresh - do not reuse any predefined/static certification list).
   - free_learning_resources: a list of 4 to 6 specific FREE resources (e.g. named free courses, tutorials, YouTube channels, open courseware) relevant to this career.
   - online_platforms: a list of 4 to 6 named online learning platforms well-suited to learning this career's skills.
   - books: a list of 3 to 5 specific recommended books (title and author) relevant to this career.
   - communities: a list of 3 to 5 specific communities, forums, or professional networks (e.g. named subreddits, Discord/Slack communities, professional associations) relevant to this career.

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "career_overview": "string",
  "related_career_roles": ["string", "..."],
  "future_scope": "string",
  "technical_skills": ["string", "..." (at least 8 items)],
  "soft_skills": ["string", "..." (at least 8 items)],
  "major_hiring_cities_india": ["string", "..."],
  "major_industry_hubs": ["string", "..."],
  "top_hiring_industries": ["string", "..."],
  "education_path": {{ ... see keys above for this student's type ... }},
  "learning_resources": {{
    "recommended_certifications": ["string", "..."],
    "free_learning_resources": ["string", "..."],
    "online_platforms": ["string", "..."],
    "books": ["string", "..."],
    "communities": ["string", "..."]
  }}
}}

Every string value must be a single line with no literal line breaks (use
spaces instead). Output JSON only."""

    return prompt


def _validate_deep_dive_schema(data, user_type='school'):
    """
    Schema validator for the deep-dive report, passed into
    generate_validated_json so a response missing required sections, or
    with too few technical/soft skills, triggers the same bounded single
    automatic retry as a JSON parse failure, rather than only being caught
    after the fact with no retry.

    education_path and learning_resources are now nested objects (not a
    string/flat list) - this validates their required sub-keys too, with
    the expected education_path keys depending on user_type (school vs
    college), since the prompt asks for a different roadmap shape for each.
    """
    required_keys = [
        "career_overview", "related_career_roles", "future_scope", "technical_skills",
        "soft_skills", "major_hiring_cities_india", "major_industry_hubs",
        "top_hiring_industries", "education_path", "learning_resources",
    ]
    if not isinstance(data, dict) or not all(k in data for k in required_keys):
        raise ValueError("Deep-dive response missing one or more required sections.")

    technical_skills = data.get("technical_skills", [])
    soft_skills = data.get("soft_skills", [])
    if not isinstance(technical_skills, list) or not isinstance(soft_skills, list) \
            or len(technical_skills) < 8 or len(soft_skills) < 8:
        raise ValueError("Deep-dive response has fewer than 8 technical_skills or soft_skills.")

    education_path = data.get("education_path")
    if not isinstance(education_path, dict):
        raise ValueError("education_path must be an object.")
    if user_type == 'school':
        required_edu_keys = ["recommended_stream", "undergraduate_degree", "higher_studies", "certifications"]
    else:
        required_edu_keys = ["higher_education_options", "professional_certifications", "specializations", "career_advancement"]
    if not all(k in education_path for k in required_edu_keys):
        raise ValueError(f"education_path missing required keys for user_type={user_type}.")

    learning_resources = data.get("learning_resources")
    required_lr_keys = ["recommended_certifications", "free_learning_resources", "online_platforms", "books", "communities"]
    if not isinstance(learning_resources, dict) or not all(k in learning_resources for k in required_lr_keys):
        raise ValueError("learning_resources missing required keys.")

    # Explicitly drop any "software_skills" field if the model adds it anyway -
    # it must never be displayed, under any name.
    data.pop("software_skills", None)
    return data



def generate_ai_deep_dive(student_details, questionnaire_responses, stream, user_type):
    """
    Call Gemini to produce the full deep-dive career report and store the
    parsed result in st.session_state.ai_deep_dive.

    RELIABILITY: uses generate_validated_json, which retries the Gemini
    call exactly ONCE if JSON parsing OR schema validation fails (capped -
    no infinite retry loop) and never calls json.loads() on unchecked
    text. The technical failure detail is stored separately in
    st.session_state.ai_deep_dive_error for an expandable debug section;
    the message returned to the UI is always a short, friendly sentence.

    NOTE: this function definition was previously missing entirely (the
    docstring/body below were accidentally left nested inside
    generate_ai_deep_dive_prompt, which never called `return prompt` and
    therefore generate_ai_deep_dive(...) didn't exist as a callable - it
    would have raised NameError the first time show_report() tried to call
    it). Fixed as part of restructuring the JSON response handling.
    """
    prompt = generate_ai_deep_dive_prompt(student_details, questionnaire_responses, stream, user_type)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.ai_deep_dive = None
        st.session_state.ai_deep_dive_error = str(e)
        return {"status": "error", "message": _t("The AI career report is temporarily unavailable. Please try again later.")}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=5120,
            label="generate_ai_deep_dive",
            validator=lambda d: _validate_deep_dive_schema(d, user_type=user_type),
        )

        if data is None:
            st.session_state.ai_deep_dive = None
            st.session_state.ai_deep_dive_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "one or more required sections, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": _t("The full AI report could not be generated right now. Please try again later."),
            }

        st.session_state.ai_deep_dive = data
        st.session_state.ai_deep_dive_error = None
        return {"status": "success", "message": _t("Deep-dive report generated.")}
    except Exception as e:
        st.session_state.ai_deep_dive = None
        st.session_state.ai_deep_dive_error = str(e)
        return {
            "status": "error",
            "message": _t("Something went wrong while generating your AI report. Please try again later."),
        }


# ==================== CAREER ROLE DETAIL (FULLY DYNAMIC) ====================

def generate_ai_role_detail_prompt(student_details, role_name, stream, user_type):
    """
    Build the prompt for a deep-dive on a single career role the student
    clicked on. Entirely AI-generated - no hardcoded role data.
    """
    name = student_details.get('name', 'The student')
    education_level = "School Student" if user_type == 'school' else "College Student"

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: use SIMPLE, beginner-friendly language; "
            "explain concepts simply; frame this role as an early, beginner-level "
            "introduction rather than deep professional detail."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: use PROFESSIONAL, industry-appropriate "
            "language; mention relevant specializations within this role and give "
            "ADVANCED CAREER GUIDANCE at an industry/professional level of depth."
        )

    prompt = f"""You are an expert career counsellor AI. The student below clicked on the
specific career role "{role_name}" (within the broader stream "{stream}").

STUDENT INFORMATION
- Name: {name}
- Education Level: {education_level}

{depth_instruction}

Do NOT use any pre-existing/manual role database - generate every field
freshly and specifically for the role "{role_name}" in the Indian job market.

Generate EXACTLY these sections for this role:
1. career_description - a paragraph describing what someone in this role actually does.
2. salary_range_india - the typical salary range in India (entry to experienced) as a string, in INR (e.g. lakhs per annum).
3. educational_requirements - a list of typical educational qualifications needed for this role.
4. job_responsibilities - a list of typical day-to-day responsibilities.
5. required_skills - a list of skills (technical and soft) required for this role.
6. future_job_growth - a short paragraph on the CURRENT growth trend/trajectory for this role (how the role has been growing recently).
7. industry_outlook - a short paragraph on the broader industry context this role sits in.
8. top_hiring_companies - a list of types/examples of companies in India that hire for this role.
9. future_demand - a short paragraph specifically forecasting FUTURE demand for this role over the next several years (distinct from future_job_growth - focus on forward-looking demand, emerging trends, and long-term outlook).

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "career_description": "string",
  "salary_range_india": "string",
  "educational_requirements": ["string", "..."],
  "job_responsibilities": ["string", "..."],
  "required_skills": ["string", "..."],
  "future_job_growth": "string",
  "industry_outlook": "string",
  "top_hiring_companies": ["string", "..."],
  "future_demand": "string"
}}

Every string value must be a single line with no literal line breaks (use spaces instead). Output JSON only."""

    return prompt


def _validate_role_detail_schema(data):
    """
    Schema validator for the career-role detail response, passed into
    generate_validated_json so a response missing required sections
    triggers the same bounded single automatic retry as a JSON parse
    failure.
    """
    required_keys = [
        "career_description", "salary_range_india", "educational_requirements",
        "job_responsibilities", "required_skills", "future_job_growth",
        "industry_outlook", "top_hiring_companies", "future_demand",
    ]
    if not isinstance(data, dict) or not all(k in data for k in required_keys):
        raise ValueError("Role detail response missing one or more required sections.")
    return data


def generate_ai_role_detail(student_details, role_name, stream, user_type):
    """
    Call Gemini to produce the dynamic career role detail and store the
    parsed result in st.session_state.ai_role_detail.

    RELIABILITY: uses generate_validated_json, which retries the Gemini
    call exactly ONCE if JSON parsing OR schema validation fails (capped -
    no infinite retry loop) and never calls json.loads() on unchecked
    text. The technical failure detail is stored separately in
    st.session_state.ai_role_detail_error for an expandable debug section;
    the message returned to the UI is always a short, friendly sentence.
    """
    prompt = generate_ai_role_detail_prompt(student_details, role_name, stream, user_type)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.ai_role_detail = None
        st.session_state.ai_role_detail_error = str(e)
        return {"status": "error", "message": _t("This role breakdown is temporarily unavailable. Please try again later.")}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=2560,
            label="generate_ai_role_detail",
            validator=_validate_role_detail_schema,
        )

        if data is None:
            st.session_state.ai_role_detail = None
            st.session_state.ai_role_detail_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "one or more required sections, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": _t("This role breakdown could not be generated right now. Please try again later."),
            }

        st.session_state.ai_role_detail = data
        st.session_state.ai_role_detail_error = None
        return {"status": "success", "message": _t("Role detail generated.")}
    except Exception as e:
        st.session_state.ai_role_detail = None
        st.session_state.ai_role_detail_error = str(e)
        return {
            "status": "error",
            "message": _t("Something went wrong while generating this role breakdown. Please try again later."),
        }


def generate_top10_related_roles_prompt(student_details, stream, user_type):
    """
    Build the prompt for the "Top 10 Related Career Roles" overview used in
    the AI Career Report PDF when the student has not yet clicked into a
    specific career role in this session. Entirely AI-generated - no
    hardcoded/static role database of any kind.
    """
    name = student_details.get('name', 'The student')
    education_level = "School Student" if user_type == 'school' else "College Student"

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: use SIMPLE, beginner-friendly language "
            "when describing each role."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: use PROFESSIONAL, industry-appropriate "
            "language when describing each role."
        )

    prompt = f"""You are an expert career counsellor AI. The student below has been
recommended the career stream "{stream}" but has not yet explored a
specific role within it.

STUDENT INFORMATION
- Name: {name}
- Education Level: {education_level}

{depth_instruction}

Do NOT use any pre-existing/manual role database - generate every field
freshly and specifically for the Indian job market.

Generate EXACTLY 10 specific, distinct job-title roles within/adjacent to
the stream "{stream}" that this student could pursue. For EACH role,
generate:
1. role_name - a specific job title (not a subfield or category).
2. short_description - a 1-2 sentence description of what someone in this role does.
3. salary_range_india - the typical salary range in India (entry to experienced) as a string, in INR (e.g. lakhs per annum).
4. future_scope - a short 1-2 sentence forecast of future demand/growth for this role.
5. required_skills - a list of 4 to 8 key skills (technical and/or soft) required for this role.

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "related_roles": [
    {{
      "role_name": "string",
      "short_description": "string",
      "salary_range_india": "string",
      "future_scope": "string",
      "required_skills": ["string", "..."]
    }}
  ]
}}

The "related_roles" array must contain EXACTLY 10 items. Every string value
must be a single line with no literal line breaks (use spaces instead).
Output JSON only."""

    return prompt


def _validate_top10_related_roles_schema(data):
    """
    Schema validator for the Top 10 Related Career Roles response, passed
    into generate_validated_json so a response missing roles or required
    per-role fields triggers the same bounded single automatic retry as a
    JSON parse failure.
    """
    if not isinstance(data, dict):
        raise ValueError("Top 10 related roles response is not a JSON object.")
    roles = data.get("related_roles")
    if not isinstance(roles, list) or not roles:
        raise ValueError("Top 10 related roles response missing a non-empty related_roles list.")

    required_keys = ["role_name", "short_description", "salary_range_india", "future_scope", "required_skills"]
    cleaned = []
    for role in roles:
        if not isinstance(role, dict) or not str(role.get("role_name", "")).strip():
            continue
        if not all(k in role for k in required_keys):
            continue
        cleaned.append(role)

    if not cleaned:
        raise ValueError("Top 10 related roles response has no valid role entries.")

    data["related_roles"] = cleaned[:10]
    return data


def generate_top10_related_roles(student_details, stream, user_type):
    """
    Call Gemini to produce the Top 10 Related Career Roles overview for
    the AI Career Report PDF, and cache the parsed result in
    st.session_state.ai_top10_related_roles (keyed by stream, so it is
    only regenerated when the recommended stream changes within a
    session - repeated PDF downloads reuse the cached result).

    RELIABILITY: uses generate_validated_json, which retries the Gemini
    call exactly ONCE if JSON parsing OR schema validation fails. On any
    failure this returns None and the caller falls back to a simpler
    name-only list so the PDF is never blocked or broken by this being
    unavailable.
    """
    cache_key = f"ai_top10_related_roles::{stream}::{user_type}"
    cached = st.session_state.get('ai_top10_related_roles')
    if isinstance(cached, dict) and cached.get('_cache_key') == cache_key:
        return cached.get('related_roles')

    prompt = generate_top10_related_roles_prompt(student_details, stream, user_type)

    try:
        model = get_gemini_client()
    except GeminiConfigError:
        return None

    try:
        data, _response_text = generate_validated_json(
            model, prompt, max_output_tokens=2560,
            label="generate_top10_related_roles",
            validator=_validate_top10_related_roles_schema,
        )
        if data is None:
            return None
        roles = data.get("related_roles", [])
        st.session_state.ai_top10_related_roles = {"_cache_key": cache_key, "related_roles": roles}
        return roles
    except Exception:
        return None


def generate_learning_roadmap_prompt(student_details, questionnaire_responses, personality_pathway, career_name, user_type):
    """
    Build the prompt for a personalized 12-Month Learning Roadmap for the
    student's selected career. Entirely AI-generated - no hardcoded/static
    roadmap templates or JSON databases of any kind.

    Inputs woven into the prompt (per the workflow: Student -> Select
    Recommended Career -> 12-Month Roadmap):
      - Student Details (name, education level, institution, location)
      - Student Type (school -> beginner depth, college -> advanced depth)
      - Selected Career (career_name)
      - Questionnaire Responses (compact interest-score summary)
      - Personality Assessment / learning-style pathway, if available
    """
    name = student_details.get('name', 'The student')
    education_level = "School Student" if user_type == 'school' else "College Student"
    grade = student_details.get('grade', '')

    questionnaire_responses = questionnaire_responses or {}
    categories_data = st.session_state.get('categories_data') or {}
    scored_categories = sorted(
        (
            (cat.get('name') or cat.get('category_name') or cat_id, round(cat.get('score', 0)))
            for cat_id, cat in categories_data.items()
            if cat.get('question_count', 0) > 0
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    if scored_categories:
        questionnaire_lines = "\n".join(f"{cat_name}: {score}" for cat_name, score in scored_categories[:6])
    elif questionnaire_responses:
        questionnaire_lines = f"{len(questionnaire_responses)} questionnaire questions answered (scores unavailable)."
    else:
        questionnaire_lines = "No questionnaire responses provided."

    if personality_pathway:
        personality_lines = (
            f"Learning Style: {personality_pathway.get('title', 'Unknown')} "
            f"({personality_pathway.get('match_percentage', '')}% match) - "
            f"{personality_pathway.get('description', '')}"
        )
    else:
        personality_lines = "No personality/learning-style assessment available - do not assume a learning style."

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT (BEGINNER LEVEL ROADMAP): every month must "
            "start from foundational, beginner-friendly ground - simple language, "
            "no assumed prior expertise, light/introductory tools, and a gentle "
            "month-over-month progression. Skills, topics, and projects should be "
            "appropriate for someone with little to no prior exposure to this field."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT (ADVANCED / INDUSTRY-LEVEL ROADMAP): use "
            "professional, industry-appropriate language throughout - assume the "
            "student can grasp technical concepts quickly, reference real "
            "industry tools/frameworks/practices, and progress toward "
            "job-ready, portfolio-worthy competence by month 12."
        )

    prompt = f"""You are an expert career counsellor and curriculum-design AI. Build a
personalized 12-MONTH LEARNING ROADMAP for the student below, who has
selected the career "{career_name}".

STUDENT DETAILS
- Name: {name}
- Education Level: {education_level}
- Grade/Year: {grade}
- Institution: {student_details.get('institution', '')}
- Location: {student_details.get('city', '')}, {student_details.get('state', '')}

STUDENT TYPE: {user_type.upper()}
SELECTED CAREER: {career_name}

CAREER QUESTIONNAIRE RESPONSES (INTEREST SCORES 0-100)
{questionnaire_lines}

PERSONALITY / LEARNING-STYLE ASSESSMENT
{personality_lines}

{depth_instruction}

Do NOT use any pre-existing/manual roadmap template, JSON file, or
predefined/static curriculum of any kind - generate every month's content
freshly and specifically for "{career_name}", this student's type, their
questionnaire signal, and their learning style (if given). The roadmap must
show clear month-over-month PROGRESSION (each month should build on the
previous one, moving from fundamentals toward proficiency).

Generate EXACTLY 12 monthly entries (month_number 1 through 12). For EVERY
single month, include ALL of these fields:
- month_title: a short theme/title for that month (e.g. "Programming Foundations").
- skills_to_learn: a list of 3 to 5 specific skills to learn that month.
- topics: a list of 3 to 6 specific topics/concepts covered that month.
- practice_activities: a list of 2 to 4 hands-on practice activities/exercises for that month.
- mini_projects: a list of 1 to 3 small mini-projects to build that month, applying that month's skills.
- free_resources: a list of 2 to 4 specific FREE learning resources (named courses, tutorials, YouTube channels, documentation, or open courseware) relevant to that month's content.
- certifications: a list of 0 to 2 relevant certifications for that month IF applicable to that month's content; use an empty list if none apply that month - do not force a certification into every month.

Also generate:
- roadmap_overview: a short paragraph (2-4 sentences) summarizing the overall roadmap philosophy/progression for "{career_name}" and how it's tailored to this student.

Keep every list item SHORT (a phrase, not a full sentence) so the roadmap
stays scannable. Every string value must be a single line with no literal
line breaks (use spaces instead).

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "career_name": "{career_name}",
  "roadmap_overview": "string",
  "months": [
    {{
      "month_number": 1,
      "month_title": "string",
      "skills_to_learn": ["string", "..."],
      "topics": ["string", "..."],
      "practice_activities": ["string", "..."],
      "mini_projects": ["string", "..."],
      "free_resources": ["string", "..."],
      "certifications": ["string", "..."]
    }}
    ... (continue for all 12 months, month_number 1 through 12, in order)
  ]
}}

Output JSON only."""

    return prompt


def _validate_learning_roadmap_schema(data):
    """
    Schema validator for the 12-Month Learning Roadmap response, passed
    into generate_validated_json so a response missing required sections,
    or missing any of the 12 months, triggers the same bounded single
    automatic retry as a JSON parse failure.
    """
    if not isinstance(data, dict):
        raise ValueError("Learning roadmap response is not a JSON object.")

    if "roadmap_overview" not in data:
        raise ValueError("Learning roadmap response missing 'roadmap_overview'.")

    months = data.get("months")
    if not isinstance(months, list) or len(months) != 12:
        raise ValueError("Learning roadmap response must contain exactly 12 months.")

    required_month_keys = [
        "month_number", "month_title", "skills_to_learn", "topics",
        "practice_activities", "mini_projects", "free_resources", "certifications",
    ]
    for month in months:
        if not isinstance(month, dict) or not all(k in month for k in required_month_keys):
            raise ValueError("A month entry in the learning roadmap is missing one or more required fields.")

    # Normalize month ordering by month_number so display always renders
    # Month 1 -> Month 12 in order regardless of the order Gemini returned.
    try:
        data["months"] = sorted(months, key=lambda m: int(m.get("month_number", 0)))
    except (TypeError, ValueError):
        pass

    return data


def generate_ai_learning_roadmap(student_details, questionnaire_responses, personality_pathway, career_name, user_type):
    """
    Call Gemini to produce the dynamic 12-Month Learning Roadmap and store
    the parsed result in st.session_state.learning_roadmap.

    RELIABILITY: uses generate_validated_json, which retries the Gemini
    call exactly ONCE if JSON parsing OR schema validation fails (capped -
    no infinite retry loop) and never calls json.loads() on unchecked
    text. The technical failure detail is stored separately in
    st.session_state.learning_roadmap_error for an expandable debug
    section; the message returned to the UI is always a short, friendly
    sentence.
    """
    prompt = generate_learning_roadmap_prompt(
        student_details, questionnaire_responses, personality_pathway, career_name, user_type,
    )

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.learning_roadmap = None
        st.session_state.learning_roadmap_error = str(e)
        return {"status": "error", "message": _t("The learning roadmap is temporarily unavailable. Please try again later.")}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=8192,
            label="generate_ai_learning_roadmap",
            validator=_validate_learning_roadmap_schema,
        )

        if data is None:
            st.session_state.learning_roadmap = None
            st.session_state.learning_roadmap_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "one or more required months/sections, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": _t("This learning roadmap could not be generated right now. Please try again later."),
            }

        st.session_state.learning_roadmap = data
        st.session_state.learning_roadmap_error = None
        return {"status": "success", "message": _t("Learning roadmap generated.")}
    except Exception as e:
        st.session_state.learning_roadmap = None
        st.session_state.learning_roadmap_error = str(e)
        return {
            "status": "error",
            "message": _t("Something went wrong while generating this learning roadmap. Please try again later."),
        }


def generate_skill_gap_prompt(student_details, questionnaire_responses, personality_pathway, ai_analysis, career_name, user_type):
    """
    Build the prompt for an AI SKILL GAP ANALYSIS for the student's
    selected career. Entirely AI-generated - Gemini itself must decide
    which current abilities the student likely has and which industry
    skills the career requires; NO predefined/static skill list, database,
    or taxonomy of any kind is used anywhere in this module.

    Inputs woven into the prompt (Student -> Select Career -> Skill Gap
    Analysis):
      - Student Details (name, education level, institution, location)
      - Student Type (school -> foundational framing, college -> industry framing)
      - Selected Career (career_name)
      - Questionnaire Responses (compact interest-score summary) as a proxy
        signal for the student's current inclinations/abilities
      - Personality Assessment / learning-style pathway, if available
      - Previously AI-generated Strengths (from the AI Analysis step), if
        available, as an additional signal of the student's current
        abilities
    """
    name = student_details.get('name', 'The student')
    education_level = "School Student" if user_type == 'school' else "College Student"
    grade = student_details.get('grade', '')

    questionnaire_responses = questionnaire_responses or {}
    categories_data = st.session_state.get('categories_data') or {}
    scored_categories = sorted(
        (
            (cat.get('name') or cat.get('category_name') or cat_id, round(cat.get('score', 0)))
            for cat_id, cat in categories_data.items()
            if cat.get('question_count', 0) > 0
        ),
        key=lambda x: x[1],
        reverse=True,
    )
    if scored_categories:
        questionnaire_lines = "\n".join(f"{cat_name}: {score}" for cat_name, score in scored_categories[:6])
    elif questionnaire_responses:
        questionnaire_lines = f"{len(questionnaire_responses)} questionnaire questions answered (scores unavailable)."
    else:
        questionnaire_lines = "No questionnaire responses provided."

    if personality_pathway:
        personality_lines = (
            f"Learning Style: {personality_pathway.get('title', 'Unknown')} "
            f"({personality_pathway.get('match_percentage', '')}% match) - "
            f"{personality_pathway.get('description', '')}"
        )
    else:
        personality_lines = "No personality/learning-style assessment available - do not assume a learning style."

    ai_analysis = ai_analysis or {}
    prior_strengths = ai_analysis.get('strengths') or []
    if prior_strengths:
        prior_strengths_lines = "\n".join(f"- {s}" for s in prior_strengths[:6])
    else:
        prior_strengths_lines = "None available."

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: frame current abilities in terms of "
            "foundational aptitude, interests, and transferable habits (not "
            "professional experience they don't have). Missing/required "
            "skills should be described at a level a school student can "
            "realistically start building now."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: frame current abilities against "
            "real, industry-standard skill expectations for this career, "
            "using professional terminology and job-ready benchmarks."
        )

    prompt = f"""You are an expert industry skills-assessment AI. Perform an AI SKILL GAP
ANALYSIS for the student below against the career "{career_name}", by
comparing (a) the student's CURRENT ABILITIES, inferred from the signals
given below, against (b) the REQUIRED INDUSTRY SKILLS for that career,
which you must determine yourself from your own knowledge of the
industry - do NOT use any predefined, static, or pre-existing skill list,
JSON file, or database of any kind. Infer both sides freshly and
specifically for this student and this exact career.

STUDENT DETAILS
- Name: {name}
- Education Level: {education_level}
- Grade/Year: {grade}
- Institution: {student_details.get('institution', '')}
- Location: {student_details.get('city', '')}, {student_details.get('state', '')}

STUDENT TYPE: {user_type.upper()}
SELECTED CAREER: {career_name}

CAREER QUESTIONNAIRE RESPONSES (INTEREST SCORES 0-100, current-abilities signal)
{questionnaire_lines}

PERSONALITY / LEARNING-STYLE ASSESSMENT
{personality_lines}

PREVIOUSLY IDENTIFIED PERSONAL STRENGTHS (current-abilities signal)
{prior_strengths_lines}

{depth_instruction}

Generate ALL of the following sections, entirely freshly for
"{career_name}" and this student - every skill named, every score, and
every explanation must be generated by you based on your own reasoning,
never copied from a fixed list:

1. overall_readiness_score: an integer 0-100 estimating how ready this
   student currently is for "{career_name}" overall.
2. readiness_summary: a short paragraph (2-4 sentences) summarizing the
   student's overall skill-gap picture for this career.
3. current_strengths: 3 to 6 skills/abilities the student ALREADY shows
   signs of possessing that are relevant to "{career_name}". Each item needs:
   - skill_name (short)
   - proficiency_score: integer 0-100 estimating their current level in that skill
   - explanation: 1 short sentence on why this is a strength for this student
4. missing_skills: 4 to 8 industry-required skills for "{career_name}" that
   the student does NOT yet show clear signs of having. Each item needs:
   - skill_name (short)
   - importance: one of "Critical", "High", "Medium" - how essential this skill is for the career
   - explanation: 1 short sentence on why this skill matters for this career
5. priority_skills: rank the 3 to 6 MOST urgent skills to develop first,
   drawn from missing_skills. Each item needs:
   - priority_rank: integer starting at 1 (1 = most urgent)
   - skill_name (must match a skill_name from missing_skills)
   - reason: 1 short sentence on why it's high priority
6. learning_difficulty: for EVERY skill listed in missing_skills, estimate
   how hard it will be for THIS student to learn. Each item needs:
   - skill_name (must match a skill_name from missing_skills)
   - difficulty_label: one of "Easy", "Moderate", "Hard", "Very Hard"
   - difficulty_score: integer 0-100 (higher = harder)
   - reason: 1 short sentence explaining the difficulty estimate
7. recommended_learning_order: sequence ALL skills from missing_skills into
   a logical learning order (which to learn first, second, etc). Each item needs:
   - order: integer starting at 1
   - skill_name (must match a skill_name from missing_skills)
   - rationale: 1 short sentence on why it belongs at this point in the sequence
8. estimated_learning_time: for EVERY skill in missing_skills, estimate how
   long it will realistically take this student to learn it. Each item needs:
   - skill_name (must match a skill_name from missing_skills)
   - estimated_duration: short phrase, e.g. "3-4 weeks" or "2 months"
   - weekly_commitment: short phrase, e.g. "4-5 hrs/week"

Keep every explanation/reason/rationale to ONE short sentence so the
results stay scannable. Every string value must be a single line with no
literal line breaks (use spaces instead). Every skill_name referenced in
priority_skills, learning_difficulty, recommended_learning_order, and
estimated_learning_time MUST exactly match a skill_name that appears in
missing_skills.

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "career_name": "{career_name}",
  "overall_readiness_score": 0,
  "readiness_summary": "string",
  "current_strengths": [
    {{"skill_name": "string", "proficiency_score": 0, "explanation": "string"}}
    ... (3 to 6 items)
  ],
  "missing_skills": [
    {{"skill_name": "string", "importance": "Critical", "explanation": "string"}}
    ... (4 to 8 items)
  ],
  "priority_skills": [
    {{"priority_rank": 1, "skill_name": "string", "reason": "string"}}
    ... (3 to 6 items)
  ],
  "learning_difficulty": [
    {{"skill_name": "string", "difficulty_label": "Moderate", "difficulty_score": 0, "reason": "string"}}
    ... (one entry per missing_skills item)
  ],
  "recommended_learning_order": [
    {{"order": 1, "skill_name": "string", "rationale": "string"}}
    ... (one entry per missing_skills item)
  ],
  "estimated_learning_time": [
    {{"skill_name": "string", "estimated_duration": "string", "weekly_commitment": "string"}}
    ... (one entry per missing_skills item)
  ]
}}

Output JSON only."""

    return prompt


def _validate_skill_gap_schema(data):
    """
    Schema validator for the AI Skill Gap Analysis response, passed into
    generate_validated_json so a response missing required sections
    triggers the same bounded single automatic retry as a JSON parse
    failure.
    """
    if not isinstance(data, dict):
        raise ValueError("Skill gap analysis response is not a JSON object.")

    required_top_keys = [
        "overall_readiness_score", "readiness_summary", "current_strengths",
        "missing_skills", "priority_skills", "learning_difficulty",
        "recommended_learning_order", "estimated_learning_time",
    ]
    for key in required_top_keys:
        if key not in data:
            raise ValueError(f"Skill gap analysis response missing '{key}'.")

    list_fields = [
        "current_strengths", "missing_skills", "priority_skills",
        "learning_difficulty", "recommended_learning_order", "estimated_learning_time",
    ]
    for field in list_fields:
        if not isinstance(data.get(field), list) or len(data.get(field)) == 0:
            raise ValueError(f"Skill gap analysis response has an empty/invalid '{field}' list.")

    required_item_keys = {
        "current_strengths": ["skill_name", "proficiency_score", "explanation"],
        "missing_skills": ["skill_name", "importance", "explanation"],
        "priority_skills": ["priority_rank", "skill_name", "reason"],
        "learning_difficulty": ["skill_name", "difficulty_label", "difficulty_score", "reason"],
        "recommended_learning_order": ["order", "skill_name", "rationale"],
        "estimated_learning_time": ["skill_name", "estimated_duration", "weekly_commitment"],
    }
    for field, keys in required_item_keys.items():
        for item in data.get(field, []):
            if not isinstance(item, dict) or not all(k in item for k in keys):
                raise ValueError(f"An item in '{field}' is missing one or more required fields.")

    # Normalize ordering so display always renders in a sensible sequence
    # regardless of the order Gemini returned them in.
    try:
        data["priority_skills"] = sorted(
            data["priority_skills"], key=lambda x: int(x.get("priority_rank", 0))
        )
    except (TypeError, ValueError):
        pass
    try:
        data["recommended_learning_order"] = sorted(
            data["recommended_learning_order"], key=lambda x: int(x.get("order", 0))
        )
    except (TypeError, ValueError):
        pass
    try:
        data["current_strengths"] = sorted(
            data["current_strengths"], key=lambda x: int(x.get("proficiency_score", 0)), reverse=True
        )
    except (TypeError, ValueError):
        pass

    return data


def generate_ai_skill_gap_analysis(student_details, questionnaire_responses, personality_pathway, ai_analysis, career_name, user_type):
    """
    Call Gemini to produce the dynamic AI Skill Gap Analysis (Current
    Strengths, Missing Skills, Priority Skills, Learning Difficulty,
    Recommended Learning Order, Estimated Learning Time) and store the
    parsed result in st.session_state.skill_gap_analysis.

    RELIABILITY: uses generate_validated_json, which retries the Gemini
    call exactly ONCE if JSON parsing OR schema validation fails (capped -
    no infinite retry loop) and never calls json.loads() on unchecked
    text. The technical failure detail is stored separately in
    st.session_state.skill_gap_analysis_error for an expandable debug
    section; the message returned to the UI is always a short, friendly
    sentence.
    """
    prompt = generate_skill_gap_prompt(
        student_details, questionnaire_responses, personality_pathway, ai_analysis, career_name, user_type,
    )

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.skill_gap_analysis = None
        st.session_state.skill_gap_analysis_error = str(e)
        return {"status": "error", "message": _t("The skill gap analysis is temporarily unavailable. Please try again later.")}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=8192,
            label="generate_ai_skill_gap_analysis",
            validator=_validate_skill_gap_schema,
        )

        if data is None:
            st.session_state.skill_gap_analysis = None
            st.session_state.skill_gap_analysis_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "one or more required sections, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": _t("This skill gap analysis could not be generated right now. Please try again later."),
            }

        st.session_state.skill_gap_analysis = data
        st.session_state.skill_gap_analysis_error = None
        return {"status": "success", "message": _t("Skill gap analysis generated.")}
    except Exception as e:
        st.session_state.skill_gap_analysis = None
        st.session_state.skill_gap_analysis_error = str(e)
        return {
            "status": "error",
            "message": _t("Something went wrong while generating this skill gap analysis. Please try again later."),
        }


def generate_resume_suggestions_prompt(student_details, career_name, user_type, skills_context, roadmap_context):
    """
    Build the prompt for AI RESUME SUGGESTIONS for the student's selected
    career. Entirely AI-generated - Gemini must produce personalized
    SUGGESTIONS only (never a complete/fake resume, never fabricated
    work history), grounded in the student's details, the selected
    career, and whatever real skills/roadmap context is available from
    earlier steps in the app.

    Inputs woven into the prompt (Student -> Recommended Career -> Skills
    -> Roadmap -> AI Resume Suggestions):
      - Student Details (name, education level, institution, location)
      - Recommended Career (career_name)
      - Skills context (from Career Detail's required_skills and/or the
        Skill Gap Analysis's current strengths / missing skills), if
        available
      - Roadmap context (skills-per-month summary from the 12-Month
        Learning Roadmap), if available
    """
    name = student_details.get('name', 'The student')
    education_level = "School Student" if user_type == 'school' else "College Student"
    grade = student_details.get('grade', '')

    skills_lines = skills_context if skills_context else "No prior skills data available - infer typical relevant skills yourself."
    roadmap_lines = roadmap_context if roadmap_context else "No learning roadmap generated yet - do not assume specific completed roadmap milestones."

    if user_type == 'school':
        depth_instruction = (
            "This is a SCHOOL STUDENT: suggestions should suit a beginner's "
            "resume/portfolio profile - school-level projects, clubs, "
            "competitions, and entry-level exposure. Do NOT suggest "
            "professional work experience or paid internships that would "
            "be unrealistic for a school student; favor school/community "
            "projects, junior competitions, online micro-courses, and "
            "beginner-friendly virtual internships or volunteering instead."
        )
    else:
        depth_instruction = (
            "This is a COLLEGE STUDENT: suggestions should suit a "
            "job-ready, industry-facing resume/portfolio profile - "
            "real-world project ideas, recognized certifications, "
            "internship types, and achievement framing that a recruiter "
            "in this field would find credible."
        )

    prompt = f"""You are an expert resume-strategy AI for students. Generate PERSONALIZED
RESUME SUGGESTIONS ONLY for the student below, based on their profile and
selected career "{career_name}". Do NOT write a complete resume, do NOT
invent fake work history, fake companies, or fake accomplishments the
student hasn't done - only generate forward-looking, realistic
SUGGESTIONS the student can act on. Everything must be freshly generated
by you for this exact student and career - do not use any fixed/static
suggestion list or template of any kind.

STUDENT DETAILS
- Name: {name}
- Education Level: {education_level}
- Grade/Year: {grade}
- Institution: {student_details.get('institution', '')}
- Location: {student_details.get('city', '')}, {student_details.get('state', '')}

RECOMMENDED CAREER: {career_name}
STUDENT TYPE: {user_type.upper()}

SKILLS CONTEXT (from this student's Career Detail / Skill Gap Analysis, if available)
{skills_lines}

ROADMAP CONTEXT (from this student's 12-Month Learning Roadmap, if available)
{roadmap_lines}

{depth_instruction}

Generate ALL of the following sections as SUGGESTIONS ONLY (never a
finished resume, never fabricated history):

1. resume_headline: 2 to 3 alternative short resume headline/title lines
   (each under 12 words) the student could use, tailored to "{career_name}".
2. career_objective: 2 to 3 alternative short career objective/summary
   statements (each 1-2 sentences) tailored to this student and career.
3. key_skills: 5 to 8 skills the student should feature on their resume
   for this career. Each item needs:
   - skill_name (short)
   - reason: 1 short sentence on why this skill should be featured
4. projects_to_include: 3 to 5 project ideas the student could build and
   list on their resume/portfolio for this career. Each item needs:
   - project_title (short)
   - description: 1-2 sentence description of what the project would involve
   - relevance: 1 short sentence on why it strengthens their resume for this career
5. certifications: 3 to 5 suggested certifications relevant to this career
   (real, recognizable certification names/providers where possible).
   Each item needs:
   - certification_name
   - reason: 1 short sentence on why it's valuable for this career
6. achievements: 3 to 5 suggestions for achievements/recognitions the
   student could pursue or highlight (e.g. competitions, hackathons,
   olympiads, leadership roles) framed as forward-looking suggestions, not
   fabricated claims. Each item needs:
   - suggestion (short)
   - reason: 1 short sentence on why it would strengthen their resume
7. portfolio_suggestions: 3 to 5 suggestions for what to include in an
   online portfolio (e.g. GitHub, personal website, Behance, LinkedIn
   featured section) for this career. Each item needs:
   - suggestion (short)
   - platform_or_format: short phrase naming a suitable platform/format
   - reason: 1 short sentence on why it helps
8. internship_suggestions: 3 to 5 suggestions for the TYPE of internships
   or entry-level/volunteer opportunities the student should look for
   (roles/domains, not specific real companies). Each item needs:
   - internship_type (short)
   - reason: 1 short sentence on why this type of internship fits their profile and career

Keep every reason/description to ONE short sentence so results stay
scannable. Every string value must be a single line with no literal line
breaks (use spaces instead).

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "career_name": "{career_name}",
  "resume_headline": ["string", "string"],
  "career_objective": ["string", "string"],
  "key_skills": [
    {{"skill_name": "string", "reason": "string"}}
    ... (5 to 8 items)
  ],
  "projects_to_include": [
    {{"project_title": "string", "description": "string", "relevance": "string"}}
    ... (3 to 5 items)
  ],
  "certifications": [
    {{"certification_name": "string", "reason": "string"}}
    ... (3 to 5 items)
  ],
  "achievements": [
    {{"suggestion": "string", "reason": "string"}}
    ... (3 to 5 items)
  ],
  "portfolio_suggestions": [
    {{"suggestion": "string", "platform_or_format": "string", "reason": "string"}}
    ... (3 to 5 items)
  ],
  "internship_suggestions": [
    {{"internship_type": "string", "reason": "string"}}
    ... (3 to 5 items)
  ]
}}

Output JSON only."""

    return prompt


def _validate_resume_suggestions_schema(data):
    """
    Schema validator for the AI Resume Suggestions response, passed into
    generate_validated_json so a response missing required sections
    triggers the same bounded single automatic retry as a JSON parse
    failure.
    """
    if not isinstance(data, dict):
        raise ValueError("Resume suggestions response is not a JSON object.")

    required_list_fields = {
        "resume_headline": None,
        "career_objective": None,
        "key_skills": ["skill_name", "reason"],
        "projects_to_include": ["project_title", "description", "relevance"],
        "certifications": ["certification_name", "reason"],
        "achievements": ["suggestion", "reason"],
        "portfolio_suggestions": ["suggestion", "platform_or_format", "reason"],
        "internship_suggestions": ["internship_type", "reason"],
    }

    for field in required_list_fields:
        if field not in data:
            raise ValueError(f"Resume suggestions response missing '{field}'.")
        if not isinstance(data.get(field), list) or len(data.get(field)) == 0:
            raise ValueError(f"Resume suggestions response has an empty/invalid '{field}' list.")

    for field, keys in required_list_fields.items():
        if keys is None:
            for item in data.get(field, []):
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(f"An item in '{field}' is not a valid non-empty string.")
        else:
            for item in data.get(field, []):
                if not isinstance(item, dict) or not all(k in item for k in keys):
                    raise ValueError(f"An item in '{field}' is missing one or more required fields.")

    return data


def generate_ai_resume_suggestions(student_details, career_name, user_type, skills_context, roadmap_context):
    """
    Call Gemini to produce the dynamic AI Resume Suggestions (Headline,
    Career Objective, Key Skills, Projects to Include, Certifications,
    Achievements, Portfolio Suggestions, Internship Suggestions) and store
    the parsed result in st.session_state.resume_suggestions.

    RELIABILITY: uses generate_validated_json, which retries the Gemini
    call exactly ONCE if JSON parsing OR schema validation fails (capped -
    no infinite retry loop) and never calls json.loads() on unchecked
    text. The technical failure detail is stored separately in
    st.session_state.resume_suggestions_error for an expandable debug
    section; the message returned to the UI is always a short, friendly
    sentence.
    """
    prompt = generate_resume_suggestions_prompt(
        student_details, career_name, user_type, skills_context, roadmap_context,
    )

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.resume_suggestions = None
        st.session_state.resume_suggestions_error = str(e)
        return {"status": "error", "message": _t("Resume suggestions are temporarily unavailable. Please try again later.")}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=8192,
            label="generate_ai_resume_suggestions",
            validator=_validate_resume_suggestions_schema,
        )

        if data is None:
            st.session_state.resume_suggestions = None
            st.session_state.resume_suggestions_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "one or more required sections, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": _t("These resume suggestions could not be generated right now. Please try again later."),
            }

        st.session_state.resume_suggestions = data
        st.session_state.resume_suggestions_error = None
        return {"status": "success", "message": _t("Resume suggestions generated.")}
    except Exception as e:
        st.session_state.resume_suggestions = None
        st.session_state.resume_suggestions_error = str(e)
        return {
            "status": "error",
            "message": _t("Something went wrong while generating these resume suggestions. Please try again later."),
        }


CHATBOT_TOPIC_CATEGORIES = """
- Career Recommendations
- Career Analysis
- Learning Roadmap
- Skills
- Education
- Higher Studies
- Job Roles
- Certifications
- Resume
- Interview Preparation
"""


def _build_chatbot_context():
    """
    Gather whatever real, already-generated personalization context exists
    in st.session_state (student details, selected career, AI Analysis,
    Career Overview / Deep Dive skills & education path, Skill Gap
    Analysis, 12-Month Learning Roadmap, Resume Suggestions) and format it
    into a compact text block so the chatbot's answers are grounded in
    THIS student's actual journey through the app rather than generic
    advice. Any section not yet generated is simply omitted - the prompt
    instructs Gemini to answer generally in that case.
    """
    parts = []

    name = st.session_state.get('student_name')
    if name:
        user_type = st.session_state.get('user_type')
        education_level = "School Student" if user_type == 'school' else "College Student"
        parts.append(
            f"Student: {name}, {education_level}, Grade/Year: {st.session_state.get('student_grade', '')}, "
            f"Institution: {st.session_state.get('student_institution', '')}, "
            f"Location: {st.session_state.get('student_city', '')}, {st.session_state.get('student_state', '')}"
        )

    stream = st.session_state.get('selected_stream_data')
    if stream:
        parts.append(
            f"Selected/Recommended Career: {stream.get('stream_name', '')} "
            f"(Match: {stream.get('match_percentage', '')}%)"
        )

    ai_analysis = st.session_state.get('ai_analysis')
    if ai_analysis and ai_analysis.get('strengths'):
        parts.append("Identified Personal Strengths: " + ", ".join(ai_analysis.get('strengths', [])[:6]))

    deep_dive = st.session_state.get('ai_deep_dive')
    if deep_dive:
        if deep_dive.get('technical_skills'):
            parts.append("Technical Skills for this career: " + ", ".join(deep_dive.get('technical_skills', [])[:10]))
        if deep_dive.get('soft_skills'):
            parts.append("Soft Skills for this career: " + ", ".join(deep_dive.get('soft_skills', [])[:10]))
        education_path = deep_dive.get('education_path') or {}
        if education_path:
            edu_bits = [f"{k}: {v}" for k, v in education_path.items() if v and isinstance(v, str)]
            if edu_bits:
                parts.append("Education Path: " + "; ".join(edu_bits[:6]))
        if deep_dive.get('related_career_roles'):
            parts.append("Related Job Roles: " + ", ".join(deep_dive.get('related_career_roles', [])[:8]))

    skill_gap = st.session_state.get('skill_gap_analysis')
    if skill_gap:
        if skill_gap.get('missing_skills'):
            parts.append(
                "Skills Currently Being Developed (Skill Gap Analysis): "
                + ", ".join(s.get('skill_name', '') for s in skill_gap.get('missing_skills', [])[:8])
            )
        if skill_gap.get('overall_readiness_score') is not None:
            parts.append(f"Overall Career Readiness Score: {skill_gap.get('overall_readiness_score')}%")

    roadmap = st.session_state.get('learning_roadmap')
    if roadmap and roadmap.get('months'):
        month_titles = [f"M{m.get('month_number')}: {m.get('month_title', '')}" for m in roadmap.get('months', [])[:12]]
        parts.append("12-Month Learning Roadmap outline: " + "; ".join(month_titles))

    resume = st.session_state.get('resume_suggestions')
    if resume:
        if resume.get('resume_headline'):
            parts.append("Suggested Resume Headline(s): " + " | ".join(resume.get('resume_headline', [])[:2]))
        if resume.get('key_skills'):
            parts.append(
                "Resume Key Skills to Feature: "
                + ", ".join(s.get('skill_name', '') for s in resume.get('key_skills', [])[:6])
            )

    return "\n".join(parts)


def generate_chatbot_suggested_questions(context_text):
    """
    Ask Gemini for a short set of suggested starter questions the student
    can tap, spanning the chatbot's supported topic categories and
    personalized to this student's context when available. Plain-text
    call (one question per line) - not JSON - since the output is a
    simple list of short strings.
    """
    try:
        model = get_gemini_client()
    except GeminiConfigError:
        return []

    context_block = context_text if context_text else "No personalized context available yet - suggest general-purpose starter questions."

    prompt = f"""You are generating SUGGESTED STARTER QUESTIONS for an AI career chatbot
inside the AI Career Guidance Platform (built by CoActions). The chatbot answers questions about:
{CHATBOT_TOPIC_CATEGORIES}
STUDENT CONTEXT:
{context_block}

Generate exactly 6 short, natural starter questions a student like this
would want to tap on to ask the chatbot. Cover a VARIETY of the topic
categories above (not all the same category), and personalize them to
the student context where possible (e.g. mention their selected career
by name if given). Each question must be under 12 words.

Respond with ONLY the 6 questions, one per line, no numbering, no
markdown, no preamble."""

    # MODIFICATION (Gemini multilingual prompts): prepend the selected
    # language instruction before sending the prompt to Gemini.
    prompt = f"{_gemini_language_instruction()}\n\n{prompt}"

    try:
        response = model.generate_content(
            prompt,
            generation_config={
                "max_output_tokens": 512,
                "thinking_config": {"thinking_budget": 0},
            },
        )
        text = (getattr(response, "text", "") or "").strip()
    except Exception:
        return []

    if not text:
        return []

    questions = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("-•*0123456789.) ").strip()
        if cleaned:
            questions.append(cleaned)

    return questions[:6]


def generate_chatbot_reply(user_message, context_text, conversation_history):
    """
    Generate the AI Career Chatbot's reply to `user_message` using Gemini,
    grounded in the student's app-usage context and the ongoing
    conversation history maintained in st.session_state. Plain-text call
    (conversational answer, not JSON). Every reply is generated fresh by
    Gemini for this exact conversation - there is no predefined/scripted
    response anywhere in this function.
    """
    user_message = (user_message or "").strip()
    if not user_message:
        return {"status": "error", "message": _t("Please type a question first.")}

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        return {"status": "error", "message": str(e)}

    context_block = context_text if context_text else "No personalized context available yet - answer generally and helpfully."

    # Keep the last several turns only, to keep the prompt compact while
    # still giving Gemini real multi-turn conversational memory.
    recent_history = conversation_history[-10:]
    history_lines = "\n".join(
        f"{'Student' if turn['role'] == 'user' else 'AI Career Assistant'}: {turn['content']}"
        for turn in recent_history
    )

    prompt = f"""You are the AI Career Chatbot inside the AI Career Guidance
Platform (built by CoActions). You help students with questions about:
{CHATBOT_TOPIC_CATEGORIES}
Answer conversationally, clearly, and encouragingly (plain text, no
markdown headers, no JSON). Keep answers focused and reasonably concise
(roughly 2-6 sentences, longer only if the question genuinely needs a
list or steps). Ground your answer in the student's own context below
where relevant, and be specific to their selected career rather than
generic when their context provides one. If the student asks something
completely unrelated to careers, education, skills, or this platform,
politely redirect them to ask a career-related question instead. Every
answer must be generated freshly for this exact question - never reuse a
generic canned response.

STUDENT CONTEXT:
{context_block}

CONVERSATION SO FAR:
{history_lines}

Respond as the AI Career Assistant, replying to the student's most recent
message above. Output ONLY your reply text."""

    # MODIFICATION (Gemini multilingual prompts): prepend the selected
    # language instruction before sending the prompt to Gemini.
    prompt = f"{_gemini_language_instruction()}\n\n{prompt}"

    try:
        response = model.generate_content(
            prompt,
            generation_config={
                "max_output_tokens": 1024,
                "thinking_config": {"thinking_budget": 0},
            },
        )
        answer_text = (getattr(response, "text", "") or "").strip()
    except Exception as e:
        return {"status": "error", "message": f"Something went wrong while getting a response. Please try again. ({str(e)})"}

    if not answer_text:
        return {"status": "error", "message": _t("The AI didn't return an answer. Please try again.")}

    return {"status": "success", "answer": answer_text}


def get_user_type_from_grade(grade):
    """Determine user type based on grade/year"""
    school_grades = ["9th", "10th", "11th", "12th"]
    college_grades = ["1st Year College", "2nd Year College", "3rd Year College", "4th Year College"]
    
    if grade in school_grades:
        return "school"
    elif grade in college_grades:
        return "college"
    else:
        return "school"

# Header with menu
def show_home_banner(image_path="banner.png"):
    """
    Display the professional home-page illustration, centered, with a
    constrained max-width so it scales cleanly on both desktop and mobile
    without stretching or distorting. Replaces the old emoji decoration.
    Fails gracefully (renders nothing) if the image file is missing, so a
    missing asset never crashes the app.
    """
    if not os.path.isfile(image_path):
        return
    try:
        with st.container(key="home_banner_wrap"):
            st.image(image_path, use_container_width=True)
    except Exception:
        # Fail silently - the illustration is decorative, not functional.
        pass


def show_header():
    col1, col2 = st.columns([1.3, 1.7])
    with col1:
        st.image("logo.png", width=200)
    with col2:
        with st.container(key="header_menu_row"):
            menu_col1, menu_col2, menu_col3, menu_col4, menu_col5, menu_col6 = st.columns(6)
            with menu_col1:
                if st.button(t("nav.home"), key="menu_home", use_container_width=True):
                    # Reset all session state for home
                    for key in list(st.session_state.keys()):
                        if key not in ['page', 'show_about', 'show_contact']:
                            del st.session_state[key]
                    st.session_state.page = 'welcome'
                    st.session_state.show_about = False
                    st.session_state.show_contact = False
                    st.rerun()
            with menu_col2:
                if st.button(t("nav.about"), key="menu_about", use_container_width=True):
                    st.session_state.show_about = True
                    st.session_state.show_contact = False
            with menu_col3:
                if st.button(t("nav.contact"), key="menu_contact", use_container_width=True):
                    st.session_state.show_contact = True
                    st.session_state.show_about = False
            with menu_col4:
                if st.button(t("nav.help"), key="menu_help", use_container_width=True):
                    # Remember which page we came from so "Back" can return the
                    # user to exactly where they were - Help never resets or
                    # touches any other assessment/session data.
                    if st.session_state.page != 'help':
                        st.session_state.help_return_page = st.session_state.page
                    st.session_state.page = 'help'
                    st.rerun()
            with menu_col5:
                if st.button(t("nav.ai_chat"), key="menu_chatbot", use_container_width=True):
                    # Same "remember where we came from" pattern as Help -
                    # the chatbot never resets or touches any other
                    # assessment/session data either.
                    if st.session_state.page != 'career_chatbot':
                        st.session_state.chatbot_return_page = st.session_state.page
                    st.session_state.page = 'career_chatbot'
                    st.rerun()
            with menu_col6:
                # MODIFICATION (Vocational Careers nav item): new top-nav
                # button, styled identically to the other nav buttons
                # (use_container_width=True, no custom CSS added). Uses the
                # exact same session_state.page navigation pattern as
                # Help/AI Chat above - no new Streamlit page, no
                # st.switch_page(), just a session_state.page value the
                # existing if/elif dispatcher in main() renders inline in
                # the same content area. Remembers the return page so the
                # eventual "Back" button on the section can restore
                # wherever the user came from, without touching any other
                # assessment/session data.
                if st.button(_t("Vocational Careers"), key="menu_vocational", use_container_width=True):
                    if st.session_state.page != 'vocational_careers':
                        st.session_state.vocational_careers_return_page = st.session_state.page
                    st.session_state.page = 'vocational_careers'
                    st.rerun()

# MODIFICATION (Vocational Careers search filters): static reference data
# for the new State / City / Vocational Category dropdowns on the
# Vocational Careers section. City options cascade off the selected
# state (same "India-specific cascading state/city dropdown" pattern
# used elsewhere). This is UI scaffolding only - no career data is
# fetched or loaded from these selections yet.
VOCATIONAL_STATE_CITIES = {
    "Andhra Pradesh": ["Visakhapatnam", "Vijayawada", "Guntur", "Tirupati"],
    "Arunachal Pradesh": ["Itanagar", "Naharlagun", "Pasighat"],
    "Assam": ["Guwahati", "Dibrugarh", "Silchar", "Jorhat"],
    "Bihar": ["Patna", "Gaya", "Bhagalpur", "Muzaffarpur"],
    "Chhattisgarh": ["Raipur", "Bhilai", "Bilaspur", "Durg"],
    "Goa": ["Panaji", "Margao", "Vasco da Gama"],
    "Gujarat": ["Ahmedabad", "Surat", "Vadodara", "Rajkot"],
    "Haryana": ["Gurugram", "Faridabad", "Panipat", "Ambala"],
    "Himachal Pradesh": ["Shimla", "Dharamshala", "Solan"],
    "Jharkhand": ["Ranchi", "Jamshedpur", "Dhanbad", "Bokaro"],
    "Karnataka": ["Bengaluru", "Mysuru", "Mangaluru", "Hubballi"],
    "Kerala": ["Kochi", "Thiruvananthapuram", "Kozhikode", "Thrissur"],
    "Madhya Pradesh": ["Bhopal", "Indore", "Jabalpur", "Gwalior"],
    "Maharashtra": ["Mumbai", "Pune", "Nagpur", "Nashik"],
    "Manipur": ["Imphal"],
    "Meghalaya": ["Shillong"],
    "Mizoram": ["Aizawl"],
    "Nagaland": ["Kohima", "Dimapur"],
    "Odisha": ["Bhubaneswar", "Cuttack", "Rourkela"],
    "Punjab": ["Ludhiana", "Amritsar", "Jalandhar", "Patiala"],
    "Rajasthan": ["Jaipur", "Jodhpur", "Udaipur", "Kota"],
    "Sikkim": ["Gangtok"],
    "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai", "Tiruchirappalli"],
    "Telangana": ["Hyderabad", "Warangal", "Nizamabad"],
    "Tripura": ["Agartala"],
    "Uttar Pradesh": ["Lucknow", "Kanpur", "Noida", "Varanasi"],
    "Uttarakhand": ["Dehradun", "Haridwar", "Nainital"],
    "West Bengal": ["Kolkata", "Howrah", "Siliguri", "Durgapur"],
    "Delhi": ["New Delhi"],
}

VOCATIONAL_CATEGORIES = [
    "ITI Trades",
    "Polytechnic Diploma",
    "Apprenticeships (NAPS/NATS)",
    "Paramedical & Healthcare Support",
    "Culinary & Hospitality",
    "Beauty & Wellness",
    "Agriculture & Allied Trades",
    "IT & Digital Skill Certifications",
]

# MODIFICATION (Vocational Careers -> relevant-data loading): this UI's
# VOCATIONAL_CATEGORIES (training-route based) predates and is kept
# unchanged; vocational_data's categories (industry-based, from
# Vocational_Careers.pdf) are a different taxonomy. This map is the only
# place that translates one into the other, so exactly one (small) slice
# of vocational_data is pulled per search - never the whole dataset.
VOCATIONAL_CATEGORY_TO_DATA_KEYS = {
    "ITI Trades": [
        "construction_infrastructure", "manufacturing_mechanical",
        "electrical_electronics", "automobile",
    ],
    "Polytechnic Diploma": [
        "manufacturing_mechanical", "electrical_electronics",
        "construction_infrastructure", "automobile",
    ],
    "Apprenticeships (NAPS/NATS)": [
        "manufacturing_mechanical", "construction_infrastructure",
        "automobile", "electrical_electronics",
    ],
    "Paramedical & Healthcare Support": ["healthcare"],
    "Culinary & Hospitality": ["hospitality_tourism"],
    "Beauty & Wellness": ["beauty_wellness"],
    "Agriculture & Allied Trades": ["agriculture", "food_processing"],
    "IT & Digital Skill Certifications": [
        "information_technology", "media_creative",
    ],
}


def get_relevant_vocational_context(state, city, category):
    """
    Build a small, ready-to-use context for the user's State/City/Category
    selection - pulling ONLY the matching slice of vocational_data, never
    the entire dataset.

    Performance notes:
    - vocational_data.filter_careers() is called with an explicit
      `categories=` list resolved from VOCATIONAL_CATEGORY_TO_DATA_KEYS, so
      only that category's careers are read out of the module (typically
      ~6-12 of the 103 total entries), not the full career list.
    - State/city are used only to add brief location framing (does the
      selected city already appear as a known training/employment hub in
      the source document); no per-state/city dataset exists or is loaded,
      since the source document doesn't break careers down by state.
    - Returns plain data (dict) - no Gemini/network call happens here. This
      keeps the "load relevant info" step cheap and separate from the
      (separate, cache-checked) AI-generation step that will consume it.
    """
    no_selection = (
        not category
        or category == "Select Vocational Category"
        or category not in VOCATIONAL_CATEGORY_TO_DATA_KEYS
    )
    if no_selection:
        return None

    data_keys = VOCATIONAL_CATEGORY_TO_DATA_KEYS[category]

    # Only the relevant category slice is pulled from vocational_data -
    # filter_careers() narrows to `data_keys` internally rather than this
    # function iterating the full flat list itself.
    matches = vocational_data.filter_careers(categories=data_keys)
    career_names = sorted({m["career"] for m in matches})

    city_is_known_hub = bool(city) and city in vocational_data.TRAINING_EMPLOYMENT_CITIES

    return {
        "state": state or "",
        "city": city or "",
        "category": category,
        "city_is_known_training_hub": city_is_known_hub,
        "careers": career_names,
        "eligibility_options": vocational_data.ELIGIBILITY_INFO["minimum_qualification_options"],
        "age_range": vocational_data.ELIGIBILITY_INFO["age_range"],
        "salary_bands": vocational_data.SALARY_BANDS,
        "career_growth_path": vocational_data.CAREER_GROWTH_PATH,
    }


def _build_vocational_ai_context_text(context):
    """
    Render the (already-filtered) context dict from
    get_relevant_vocational_context() into a compact plain-text block
    suitable for dropping into a Gemini prompt. Kept separate from prompt
    construction itself so future AI features (overview, roadmap, etc.)
    can all reuse this same compact block instead of re-serializing the
    full dataset each time.
    """
    if not context:
        return ""

    hub_line = (
        f"{context['city']} is a known training/employment hub for this field."
        if context["city_is_known_training_hub"]
        else f"No specific data on {context['city']} - treat it as a general Indian city for this field."
    )

    lines = [
        f"Category: {context['category']}",
        f"Location: {context['city']}, {context['state']}",
        hub_line,
        f"Relevant careers: {', '.join(context['careers'])}",
        f"Eligibility routes: {'; '.join(context['eligibility_options'])}",
        f"Typical age range: {context['age_range']['min']}-{context['age_range']['max']} ({context['age_range']['note']})",
        "Salary bands: " + "; ".join(
            f"{b['experience']}: {b['salary_range']}" for b in context["salary_bands"]
        ),
        "Career growth path: " + " -> ".join(context["career_growth_path"]),
    ]
    return "\n".join(lines)


def _build_vocational_career_ai_context_text(context):
    """
    Render the single-career context dict from
    vocational_data.get_career_detail_context() into a compact plain-text
    block for a Gemini prompt. Mirrors _build_vocational_ai_context_text
    above, but scoped to exactly ONE career - this is the only text that
    gets sent to Gemini for a career detail request, never the full
    vocational_careers_data module.
    """
    if not context:
        return ""

    lines = [
        f"Career: {context['career']}",
        f"Category: {context['category_label']}",
        "Eligibility routes: " + "; ".join(context["eligibility_options"]),
        f"Typical age range: {context['age_range']['min']}-{context['age_range']['max']} ({context['age_note']})",
        "Core technical skill areas: " + ", ".join(context["technical_skills"]),
        "Core soft skill areas: " + ", ".join(context["soft_skills"]),
        "Typical training institutes: " + ", ".join(context["training_institutes"]),
        f"Typical course duration: {context['course_duration']['min']} - {context['course_duration']['max']} ({context['course_duration']['note']})",
        "Recognised certifications: " + ", ".join(context["certifications"]),
        "Salary bands: " + "; ".join(
            f"{b['experience']}: {b['salary_range']}" for b in context["salary_bands"]
        ),
        "Typical career growth path: " + " -> ".join(context["career_growth_path"]),
        "Common employment sectors: " + ", ".join(context["employment_sectors"]),
        "Relevant government portals/schemes: " + ", ".join(context["government_portals"]),
    ]
    return "\n".join(lines)


def generate_ai_vocational_career_prompt(career_name, context_text):
    """
    Build the prompt sent to Gemini for a single, specifically SELECTED
    vocational career. Only `context_text` (built from
    vocational_data.get_career_detail_context() for this one career) is
    included - the full vocational_careers_data module/document is never
    sent.
    """
    prompt = f"""You are an expert vocational-career counsellor AI for students in India.
The student has selected ONE specific vocational career to learn more about.

SELECTED VOCATIONAL CAREER DATA (use this as your factual grounding - do not
invent data that contradicts it, but you may elaborate naturally on top of it):
{context_text}

Generate EXACTLY these 12 sections for the career "{career_name}":
1. career_overview - a complete description explaining what someone in this vocational career actually does day-to-day and where they typically work.
2. eligibility - a list of the eligibility requirements (minimum qualification, age range, any prerequisites) to enter this specific career.
3. skills - a list of the specific technical and soft skills required for this career (tailored to this career, not a generic list).
4. salary - a paragraph summarizing realistic salary expectations in India for this career, from fresher to experienced/self-employed, in INR.
5. job_demand - a paragraph on the CURRENT job market demand for this specific career in India.
6. industries - a list of the specific industries/sectors that hire for this career (e.g. Construction Companies, Manufacturing, MSMEs, Self-Employment), tailored to this career.
7. government_opportunities - a list of government schemes, portals, PSU/government-department jobs, subsidies, or apprenticeship programs relevant to entering or growing in this specific career.
8. certifications - a list of the certifications/courses relevant to this specific career.
9. career_growth - a list of career growth stages/promotion path specific to this career (e.g. Trainee -> ... -> Business Owner).
10. future_scope - a paragraph forecasting the FUTURE outlook for this career over the next 5-10 years (automation risk, emerging opportunities, industry trends).
11. learning_roadmap - a list of ordered steps a student should take to start and progress in this career (training, certification, practical experience, specialization).
12. ai_career_advice - a short, personalized paragraph of practical advice for a student considering this specific vocational career.

Do NOT use any other vocational career's data. Every fact must be specific to "{career_name}".

OUTPUT FORMAT - respond with ONLY valid JSON, no markdown fences, no preamble:

{{
  "career_overview": "string",
  "eligibility": ["string", "..."],
  "skills": ["string", "..."],
  "salary": "string",
  "job_demand": "string",
  "industries": ["string", "..."],
  "government_opportunities": ["string", "..."],
  "certifications": ["string", "..."],
  "career_growth": ["string", "..."],
  "future_scope": "string",
  "learning_roadmap": ["string", "..."],
  "ai_career_advice": "string"
}}

Every string value must be a single line with no literal line breaks (use spaces instead). Output JSON only."""

    # MODIFICATION (Vocational Careers i18n integration): prepend the same
    # selected-language directive every other Gemini prompt in this app
    # uses, so vocational career AI breakdowns are generated NATIVELY in
    # the student's selected language (Tamil/Hindi/Marathi/English)
    # instead of always coming back in English. Nothing else about the
    # prompt changes, and the response is never translated afterwards -
    # consistent with the rest of the app's "generate in-language, don't
    # post-translate AI output" approach.
    prompt = f"{_gemini_language_instruction()}\n\n{prompt}"

    return prompt


def _validate_vocational_career_schema(data):
    """
    Schema validator for the vocational career detail response, passed
    into generate_validated_json so a response missing required sections
    triggers the same bounded single automatic retry as a JSON parse
    failure.
    """
    required_keys = [
        "career_overview", "eligibility", "skills", "salary", "job_demand",
        "industries", "government_opportunities",
        "certifications", "career_growth", "future_scope",
        "learning_roadmap", "ai_career_advice",
    ]
    if not isinstance(data, dict) or not all(k in data for k in required_keys):
        raise ValueError("Vocational career detail response missing one or more required sections.")
    return data


def generate_ai_vocational_career_detail(career_name):
    """
    Call Gemini to produce the AI-generated detail for ONE selected
    vocational career and store the parsed result in
    st.session_state.vocational_career_detail.

    Only the compact single-career context built via
    vocational_data.get_career_detail_context() /
    _build_vocational_career_ai_context_text() is sent to Gemini - never
    the full vocational_careers_data module/document.

    RELIABILITY: uses generate_validated_json, which retries the Gemini
    call exactly ONCE if JSON parsing OR schema validation fails, and
    never calls json.loads() on unchecked text. The technical failure
    detail is stored separately in
    st.session_state.vocational_career_detail_error for an expandable
    debug section; the message returned to the UI is always a short,
    friendly sentence.
    """
    context = vocational_data.get_career_detail_context(career_name)
    if context is None:
        st.session_state.vocational_career_detail = None
        st.session_state.vocational_career_detail_error = (
            f"'{career_name}' was not found in the vocational careers data."
        )
        return {"status": "error", "message": _t("This career could not be found. Please pick another one.")}

    context_text = _build_vocational_career_ai_context_text(context)
    prompt = generate_ai_vocational_career_prompt(career_name, context_text)

    try:
        model = get_gemini_client()
    except GeminiConfigError as e:
        st.session_state.vocational_career_detail = None
        st.session_state.vocational_career_detail_error = str(e)
        return {"status": "error", "message": _t("This career breakdown is temporarily unavailable. Please try again later.")}

    try:
        data, response_text = generate_validated_json(
            model, prompt, max_output_tokens=2560,
            label="generate_ai_vocational_career_detail",
            validator=_validate_vocational_career_schema,
        )

        if data is None:
            st.session_state.vocational_career_detail = None
            st.session_state.vocational_career_detail_error = (
                "Gemini's response was empty, invalid JSON, or missing "
                "one or more required sections, even after one automatic retry."
            )
            return {
                "status": "error",
                "message": _t("This career breakdown could not be generated right now. Please try again later."),
            }

        st.session_state.vocational_career_detail = data
        st.session_state.vocational_career_detail_error = None
        return {"status": "success", "message": _t("Career detail generated.")}
    except Exception as e:
        st.session_state.vocational_career_detail = None
        st.session_state.vocational_career_detail_error = str(e)
        return {
            "status": "error",
            "message": _t("Something went wrong while generating this career breakdown. Please try again later."),
        }


def show_vocational_careers():
    """
    Vocational Careers section. Rendered inline in the existing content
    area via the normal session_state.page dispatcher in main() - not a
    separate Streamlit page/st.switch_page(). Reuses the same visual
    language as the other sections (show_header(), 'main-card' /
    'section-title' classes already defined in the app's <style> block,
    and the same inline-styled info-card look used on the Help page) so
    no new CSS is introduced and the rest of the UI is untouched. All
    text goes through _t() so it follows st.session_state.language like
    every other page - the language selector on the Welcome page keeps
    working exactly as before.
    """
    show_header()

    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    st.markdown(f'<h1 class="welcome-heading">{_t("Vocational Careers")}</h1>', unsafe_allow_html=True)
    st.markdown(
        f'<p class="welcome-subheading">{_t("Skill-based career paths that lead directly to employment - no long degree required")}</p>',
        unsafe_allow_html=True,
    )
    st.markdown('<hr>', unsafe_allow_html=True)

    # ---- Search filters: State / City / Vocational Category + Search ----
    # MODIFICATION (Vocational Careers search UI): captures the user's
    # selections into session_state only. Intentionally does NOT call any
    # AI/data-fetching function yet - no career listings are loaded here,
    # per requirements. The Search button just stores what was searched
    # for and shows an acknowledgement placeholder.
    st.markdown(f'<h3 class="section-title">{_t("Search Vocational Careers")}</h3>', unsafe_allow_html=True)

    filter_col1, filter_col2, filter_col3 = st.columns(3)
    with filter_col1:
        state_options = ["Select State"] + list(VOCATIONAL_STATE_CITIES.keys())
        vocational_state = st.selectbox(
            _t("State"),
            state_options,
            key="vocational_state",
            # MODIFICATION (Vocational Careers i18n integration): translate
            # the dropdown options themselves (placeholder + state names),
            # not just the field label - mirrors the Vocational Category
            # dropdown below, which already used format_func=_t.
            format_func=_t,
        )
    with filter_col2:
        city_choices = VOCATIONAL_STATE_CITIES.get(vocational_state, [])
        city_options = ["Select City"] + city_choices
        vocational_city = st.selectbox(
            _t("City"),
            city_options,
            key="vocational_city",
            format_func=_t,
        )
    with filter_col3:
        category_options = ["Select Vocational Category"] + VOCATIONAL_CATEGORIES
        vocational_category = st.selectbox(
            _t("Vocational Category"),
            category_options,
            key="vocational_category",
            format_func=_t,
        )

    search_clicked = st.button(_t("Search"), key="vocational_search_btn", type="primary", use_container_width=True)
    if search_clicked:
        st.session_state.vocational_search_submitted = True

    if st.session_state.get("vocational_search_submitted"):
        # MODIFICATION (Vocational Careers -> relevant-data loading):
        # fingerprint the current State/City/Category selection and only
        # rebuild the filtered context when the selection actually changed.
        # This avoids re-filtering vocational_data (and, once the AI step
        # is wired in, avoids re-calling Gemini) on every Streamlit rerun
        # triggered by unrelated widget interactions on this page.
        selection_fingerprint = make_response_fingerprint(
            vocational_state, vocational_city, vocational_category
        )
        if st.session_state.get("vocational_context_fingerprint") != selection_fingerprint:
            st.session_state.vocational_prepared_context = get_relevant_vocational_context(
                vocational_state, vocational_city, vocational_category
            )
            st.session_state.vocational_context_fingerprint = selection_fingerprint

        prepared_context = st.session_state.vocational_prepared_context

        if prepared_context is None:
            st.warning(_t("Please select a Vocational Category to continue."))
        else:
            st.info(
                _t("Found")
                + f" {len(prepared_context['careers'])} "
                + _t("relevant vocational careers for")
                + f" {_t(vocational_category)}. "
                + _t("Select one below to get its full AI-generated breakdown.")
            )

            # ---- Career selection: user picks ONE specific career. Only
            # that single career's compact context (never the whole
            # dataset) is what will be sent to Gemini. ----
            careers_found = prepared_context["careers"]
            for row_start in range(0, len(careers_found), 3):
                row_careers = careers_found[row_start:row_start + 3]
                row_cols = st.columns(3)
                for j, career_name in enumerate(row_careers):
                    c_idx = row_start + j
                    with row_cols[j]:
                        if st.button(_t(career_name), key=f"vocational_career_{c_idx}", use_container_width=True):
                            st.session_state.selected_vocational_career = career_name
                            st.session_state.vocational_career_detail = None
                            st.session_state.vocational_career_detail_fingerprint = None
                            st.rerun()

    # ---- Selected career: AI-generated detail (Career Overview,
    # Eligibility, Skills, Salary, Job Demand, Certifications, Career
    # Growth, Future Scope, Industries, Government Opportunities, Learning
    # Roadmap, AI Career Advice). Only the selected career's own data is
    # ever sent to Gemini - see generate_ai_vocational_career_detail() /
    # get_career_detail_context(). ----
    selected_career = st.session_state.get("selected_vocational_career")
    if selected_career:
        st.markdown('<hr>', unsafe_allow_html=True)

        # MODIFICATION (modern AI detail UI): hero card shows the selected
        # career + its category as a pill badge, in the blue/cyan design
        # used throughout the rest of this detail section. Category label
        # comes from the same pure-data lookup already used to build the
        # Gemini context (no extra AI call).
        _career_ctx_for_hero = vocational_data.get_career_detail_context(selected_career)
        _category_label_for_hero = _career_ctx_for_hero["category_label"] if _career_ctx_for_hero else ""
        st.markdown(
            f'<div class="vocdetail-hero">'
            f'<div class="vocdetail-hero-icon">🧭</div>'
            f'<div>'
            f'<div class="vocdetail-hero-title">{_t(selected_career)}</div>'
            + (f'<span class="vocdetail-hero-badge">{_t(_category_label_for_hero)}</span>' if _category_label_for_hero else "")
            + '</div></div>',
            unsafe_allow_html=True,
        )

        current_fp = make_response_fingerprint(selected_career)
        if (
            st.session_state.get("vocational_career_detail") is None
            or st.session_state.get("vocational_career_detail_fingerprint") != current_fp
        ):
            with st.spinner(f"{_t('Generating AI breakdown for')} {_t(selected_career)}..."):
                result = generate_ai_vocational_career_detail(selected_career)
            st.session_state.vocational_career_detail_status = result
            st.session_state.vocational_career_detail_fingerprint = current_fp

        detail = st.session_state.get("vocational_career_detail")

        if not detail:
            msg = (st.session_state.get("vocational_career_detail_status") or {}).get(
                "message", _t("This career breakdown could not be generated right now.")
            )
            st.markdown(f"""
            <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
                <p>⚠️ {msg}</p>
            </div>
            """, unsafe_allow_html=True)
            if st.session_state.get("vocational_career_detail_error"):
                with st.expander(_t("Technical details (for debugging)")):
                    st.code(st.session_state.vocational_career_detail_error)
            if st.button(t("buttons.retry"), key="vocational_career_retry_btn", use_container_width=True):
                st.session_state.vocational_career_detail = None
                st.rerun()
        else:
            # MODIFICATION (collapsible detail sections): each section is
            # now its own st.expander, stacked in a single column. This is
            # the "question-card" content pattern already used for the
            # Career Overview expander elsewhere in the app (see
            # show_career_overview()), so no new CSS is introduced. A
            # single-column stack (no side-by-side st.columns) keeps this
            # readable and responsive on narrow/mobile viewports. Every
            # value below still comes straight from `detail` (the Gemini
            # response for this one career) - no new data is invented here.
            def vocdetail_text_section(title, icon, text, expanded=False):
                with st.expander(f"{icon} {_t(title)}", expanded=expanded):
                    st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
                    st.markdown(text or "")
                    st.markdown('</div>', unsafe_allow_html=True)

            def vocdetail_list_section(title, icon, items, expanded=False):
                with st.expander(f"{icon} {_t(title)}", expanded=expanded):
                    st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
                    for item in (items or []):
                        st.markdown(f"- {item}")
                    st.markdown('</div>', unsafe_allow_html=True)

            # ---- The 8 requested sections, in order, each collapsible ----
            vocdetail_text_section("Complete Description", "📘", detail.get("career_overview", ""), expanded=True)
            vocdetail_list_section("Required Skills", "🛠️", detail.get("skills", []))
            vocdetail_list_section("Eligibility", "🎓", detail.get("eligibility", []))
            vocdetail_text_section("Salary", "💰", detail.get("salary", ""))
            vocdetail_list_section("Industries", "🏭", detail.get("industries", []))
            vocdetail_list_section("Government Opportunities", "🏛️", detail.get("government_opportunities", []))
            vocdetail_list_section("Certifications", "📜", detail.get("certifications", []))
            vocdetail_text_section("Future Scope", "🔮", detail.get("future_scope", ""))

            # ---- Additional insights (kept from the previous version,
            # collapsed by default so the 8 requested sections stay the
            # primary focus) ----
            vocdetail_text_section("Job Demand", "📊", detail.get("job_demand", ""))
            vocdetail_list_section("Career Growth", "📈", detail.get("career_growth", []))
            vocdetail_list_section("Learning Roadmap", "🗺️", detail.get("learning_roadmap", []))
            vocdetail_text_section("AI Career Advice", "🤖", detail.get("ai_career_advice", ""))

        if st.button(_t("Clear selection"), key="vocational_career_clear_btn", use_container_width=True):
            st.session_state.selected_vocational_career = None
            st.session_state.vocational_career_detail = None
            st.session_state.vocational_career_detail_fingerprint = None
            st.rerun()

    st.markdown('<hr>', unsafe_allow_html=True)

    st.markdown(f'<h3 class="section-title">{_t("Explore Vocational & Skill-Based Paths")}</h3>', unsafe_allow_html=True)

    vocational_tracks = [
        ("🔧", "ITI Trades", "Government/private Industrial Training Institute courses (electrician, fitter, welder, mechanic) leading to certified, job-ready skills in 1-2 years."),
        ("🏭", "Polytechnic Diploma", "3-year diploma in engineering fields (mechanical, civil, electrical, computer) after 10th - a fast, affordable route into technical jobs or lateral entry to engineering degrees."),
        ("🎓", "Apprenticeships (NAPS/NATS)", "Earn-while-you-learn training with companies under India's National Apprenticeship schemes, combining stipend, hands-on work, and a recognized certificate."),
        ("🩺", "Paramedical & Healthcare Support", "Short-term diplomas for lab technician, nursing assistant, radiology technician, and other in-demand healthcare support roles."),
        ("🍳", "Culinary & Hospitality", "Hotel management diplomas, chef training, and hospitality certifications for careers in restaurants, hotels, and catering."),
        ("💇", "Beauty & Wellness", "Certified courses in cosmetology, hairstyling, and wellness therapy - popular self-employment and salon-industry pathways."),
        ("🌾", "Agriculture & Allied Trades", "Diplomas and certifications in agriculture, horticulture, and dairy/animal husbandry for rural and agri-business careers."),
        ("💻", "IT & Digital Skill Certifications", "NSDC-aligned short courses in web support, digital marketing, and computer hardware - quick entry into the IT services job market."),
    ]

    cards_html_parts = ['<div style="display:grid; grid-template-columns:repeat(2, 1fr); gap:0.9rem;">']
    for icon, title, description in vocational_tracks:
        cards_html_parts.append(
            '<div style="background:#F8F9FF; border-left:4px solid #667eea; border-radius:16px; '
            'padding:1.1rem 1.3rem;">'
            f'<p style="margin:0 0 0.35rem 0; font-weight:700; font-size:1.02rem;">{icon} {_t(title)}</p>'
            f'<p style="margin:0; color:#444; font-size:0.92rem; line-height:1.5;">{_t(description)}</p>'
            '</div>'
        )
    cards_html_parts.append('</div>')
    st.markdown("".join(cards_html_parts), unsafe_allow_html=True)

    st.markdown(
        f'<p style="margin-top:1.2rem; color:#555;">{_t("Not sure if a vocational path fits you? Take the career assessment to get a personalized recommendation alongside these options.")}</p>',
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button(t("buttons.back"), key="vocational_careers_back_btn", use_container_width=True):
        st.session_state.page = st.session_state.get("vocational_careers_return_page") or "welcome"
        st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_help():
    """
    AI-powered Help Center. Every word of content here (overview, audience,
    step-by-step guide, FAQ, tips, and search answers) is generated live by
    Gemini via generate_ai_help_guide() / generate_help_search_answer() -
    nothing on this page is hardcoded text or a static JSON manual. Does
    not touch or reset any other part of the app's session state.
    """
    show_header()

    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    st.markdown(f'<h1 class="welcome-heading">{t("help.title")}</h1>', unsafe_allow_html=True)
    st.markdown(
        '<p class="welcome-subheading">Your personal AI-generated guide to using the AI Career Guidance Platform</p>',
        unsafe_allow_html=True,
    )
    st.markdown('<hr>', unsafe_allow_html=True)

    st.markdown(f'<h3 class="section-title">{t("help.ask_ai_heading")}</h3>', unsafe_allow_html=True)
    search_col1, search_col2 = st.columns([4, 1])
    with search_col1:
        query = st.text_input(
            t("labels.help_search"),
            value=st.session_state.help_search_query,
            placeholder=t("labels.help_search_placeholder"),
            key="help_search_input",
            label_visibility="collapsed",
        )
    with search_col2:
        ask_clicked = st.button(t("buttons.ask_ai"), key="help_ask_btn", use_container_width=True)

    if ask_clicked:
        st.session_state.help_search_query = query
        with st.spinner(t("help.thinking_spinner")):
            result = generate_help_search_answer(query)
        if result["status"] == "success":
            st.session_state.help_search_answer = result["answer"]
            st.session_state.help_search_status = None
        else:
            st.session_state.help_search_answer = None
            st.session_state.help_search_status = result["message"]

    if st.session_state.help_search_status:
        st.error(st.session_state.help_search_status)
    if st.session_state.help_search_answer:
        st.markdown(
            "<div style=\"background:#EEF2FF; border-left:4px solid #667eea; border-radius:14px; padding:1.2rem; margin-top:0.5rem;\">"
            "<p style=\"margin:0;\"><strong>AI Answer:</strong></p>"
            "<p style=\"margin:0.5rem 0 0 0;\">" + st.session_state.help_search_answer + "</p>"
            "</div>",
            unsafe_allow_html=True,
        )

    st.markdown('<hr>', unsafe_allow_html=True)

    if not st.session_state.ai_help_guide:
        with st.spinner(t("help.generating_spinner")):
            generate_ai_help_guide()

    regen_col1, regen_col2 = st.columns([5, 1])
    with regen_col2:
        if st.button(t("buttons.regenerate"), key="help_regenerate_btn", use_container_width=True):
            with st.spinner(t("help.regenerating_spinner")):
                generate_ai_help_guide(force_refresh=True)
            st.rerun()

    guide = st.session_state.ai_help_guide

    if not guide:
        st.error(st.session_state.ai_help_guide_status or t("messages.error.help_guide_unavailable"))
    else:
        st.markdown(f'<h3 class="section-title">{t("help.about_app_heading")}</h3>', unsafe_allow_html=True)
        st.markdown(
            "<div style=\"background:#FFF8F0; border-radius:20px; padding:1.5rem; margin-bottom:1rem;\">"
            "<p><strong>What AI-Career Counselling System does:</strong> " + guide['app_overview'] + "</p>"
            "<p><strong>Who it's for:</strong> " + guide['who_can_use'] + "</p>"
            "<p><strong>School vs. College students:</strong> " + guide['school_vs_college'] + "</p>"
            "</div>",
            unsafe_allow_html=True,
        )

        st.markdown(f'<h3 class="section-title">{t("help.steps_heading")}</h3>', unsafe_allow_html=True)
        steps_html_parts = ['<div style="background:#F8F9FF; border-radius:20px; padding:1.5rem;">']
        total_steps = len(guide["steps"])
        for i, step in enumerate(guide["steps"]):
            steps_html_parts.append(
                '<div style="display:flex; align-items:flex-start; margin-bottom:0.9rem;">'
                '<div style="min-width:36px; height:36px; border-radius:50%; background:linear-gradient(135deg,#667eea,#764ba2); '
                'color:white; display:flex; align-items:center; justify-content:center; font-weight:700; margin-right:0.9rem;">'
                + str(step['step_number']) + '</div>'
                '<div><p style="margin:0; font-weight:700;">' + step['title'] + '</p>'
                '<p style="margin:0.15rem 0 0 0; color:#444;">' + step['description'] + '</p></div></div>'
            )
            if i < total_steps - 1:
                steps_html_parts.append(
                    '<div style="margin:0 0 0.9rem 17px; border-left:2px dashed #a3a3d1; height:12px;"></div>'
                )
        steps_html_parts.append('</div>')
        st.markdown("".join(steps_html_parts), unsafe_allow_html=True)

        st.markdown(f'<h3 class="section-title">{t("help.tips_heading")}</h3>', unsafe_allow_html=True)
        tips_html_parts = ['<div style="background:#F0FFF4; border-radius:20px; padding:1.5rem;"><ul style="margin:0; padding-left:1.2rem;">']
        for tip in guide["tips"]:
            tips_html_parts.append('<li style="margin-bottom:0.4rem;">' + tip + '</li>')
        tips_html_parts.append('</ul></div>')
        st.markdown("".join(tips_html_parts), unsafe_allow_html=True)

        st.markdown(f'<h3 class="section-title">{t("help.faq_heading")}</h3>', unsafe_allow_html=True)
        for faq in guide["faqs"]:
            with st.expander(faq["question"]):
                st.write(faq["answer"])

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button(t("buttons.back"), key="help_back_btn", use_container_width=True):
        st.session_state.page = st.session_state.help_return_page or "welcome"
        st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_welcome():
    show_header()
    
    if st.session_state.show_about:
        st.markdown('<div class="main-card">', unsafe_allow_html=True)

        st.markdown("""
        <style>
            .about-hero {
                text-align: center;
                padding: 0.5rem 0 1.5rem 0;
            }
            .about-hero h1 {
                background: linear-gradient(135deg, #667eea, #764ba2);
                -webkit-background-clip: text;
                background-clip: text;
                color: transparent;
                font-size: 2.1rem;
                font-weight: 800;
                margin-bottom: 0.4rem;
            }
            .about-hero p {
                color: #4a5568;
                font-size: 1.05rem;
                font-weight: 500;
                max-width: 640px;
                margin: 0 auto;
            }
            .about-section {
                background: rgba(255,255,255,0.9);
                border-radius: 22px;
                padding: 1.6rem 1.8rem;
                margin-bottom: 1.2rem;
                box-shadow: 0 10px 25px -12px rgba(0,0,0,0.15);
                border: 1px solid rgba(102,126,234,0.15);
            }
            .about-section h2 {
                font-size: 1.35rem;
                font-weight: 800;
                color: #2d2d44;
                margin: 0 0 0.7rem 0;
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }
            .about-section p {
                color: #333;
                font-size: 1rem;
                line-height: 1.65;
                margin: 0 0 0.7rem 0;
            }
            .about-section p:last-child { margin-bottom: 0; }
            .about-badge {
                display: inline-block;
                background: linear-gradient(135deg, #667eea, #764ba2);
                color: #fff;
                font-weight: 700;
                font-size: 0.78rem;
                letter-spacing: 0.02em;
                padding: 0.25rem 0.9rem;
                border-radius: 20px;
                margin-bottom: 0.9rem;
            }
            .about-list {
                list-style: none;
                margin: 0;
                padding: 0;
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                gap: 0.6rem;
            }
            .about-list li {
                background: #F8F9FF;
                border-left: 4px solid #667eea;
                border-radius: 12px;
                padding: 0.65rem 0.9rem;
                font-size: 0.95rem;
                font-weight: 600;
                color: #2d2d44;
                line-height: 1.4;
            }
            .about-list.why li {
                border-left-color: #11998e;
                background: #F0FBF9;
            }
            @media (max-width: 700px) {
                .about-list { grid-template-columns: 1fr; }
            }
            .about-mission {
                background: linear-gradient(135deg, #667eea, #764ba2);
                border-radius: 22px;
                padding: 1.8rem;
                margin-bottom: 1.2rem;
                text-align: center;
                color: #fff;
                box-shadow: 0 15px 30px -12px rgba(102,126,234,0.5);
            }
            .about-mission h2 {
                font-size: 1.3rem;
                font-weight: 800;
                margin: 0 0 0.6rem 0;
            }
            .about-mission p {
                font-size: 1.02rem;
                line-height: 1.6;
                margin: 0;
                opacity: 0.97;
            }
            .about-commit {
                background: #FFF8F0;
                border-radius: 22px;
                padding: 1.6rem 1.8rem;
                border-left: 5px solid #E67E22;
            }
        </style>
        """, unsafe_allow_html=True)

        st.markdown(f"""
        <div class="about-hero">
            <h1>🌍 {t("about.title")}</h1>
            <p>{t("about.hero_subtitle")}</p>
        </div>

        <div class="about-section">
            <h2>🌍 {t("about.title")}</h2>
            <p>{t("about.intro_paragraph_1")}</p>
            <p>{t("about.intro_paragraph_2")}</p>
        </div>

        <div class="about-section">
            <span class="about-badge">{t("about.badge_flagship")}</span>
            <h2>🚀 {t("about.elevate_title")}</h2>
            <p>{t("about.elevate_paragraph_1")}</p>
            <p>{t("about.elevate_paragraph_2")}</p>
        </div>

        <div class="about-mission">
            <h2>🎯 {t("about.mission_title")}</h2>
            <p>{t("about.mission_text")}</p>
        </div>

        <div class="about-section">
            <h2>💡 {t("about.what_we_do_title")}</h2>
            <ul class="about-list">
                <li>🎓 {t("about.item_ai_career")}</li>
                <li>💻 {t("about.item_tech")}</li>
                <li>🌱 {t("about.item_youth")}</li>
                <li>🤝 {t("about.item_internship")}</li>
                <li>📊 {t("about.item_data")}</li>
                <li>🏫 {t("about.item_education")}</li>
                <li>🌍 {t("about.item_community")}</li>
            </ul>
        </div>

        <div class="about-section">
            <h2>🌟 {t("about.why_title")}</h2>
            <ul class="about-list why">
                <li>{t("about.why_item_1")}</li>
                <li>{t("about.why_item_2")}</li>
                <li>{t("about.why_item_3")}</li>
                <li>{t("about.why_item_4")}</li>
                <li>{t("about.why_item_5")}</li>
                <li>{t("about.why_item_6")}</li>
                <li>{t("about.why_item_7")}</li>
            </ul>
        </div>

        <div class="about-section">
            <h2>📈 {t("about.impact_title")}</h2>
            <p>{t("about.impact_text")}</p>
        </div>

        <div class="about-commit">
            <h2 style="margin-top:0; color:#D35400; font-size:1.3rem; font-weight:800;">🤝 {t("about.commitment_title")}</h2>
            <p style="margin:0; color:#333; line-height:1.65;">{t("about.commitment_text")}</p>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button(t("buttons.back_to_home"), key="back_to_home_about", use_container_width=True):
            st.session_state.show_about = False
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return
    
    if st.session_state.show_contact:
        st.markdown('<div class="main-card">', unsafe_allow_html=True)
        st.markdown(f'<h1 class="welcome-heading">📞 {t("footer.title")}</h1>', unsafe_allow_html=True)
        st.markdown(f"""
        <div style="background:#FFF8F0; border-radius:20px; padding:1.5rem;">
            <p>📧 <strong>{t("footer.email_label")}</strong> <a href="mailto:elevateall2020@gmail.com">elevateall2020@gmail.com</a></p>
            <p>🌐 <strong>{t("footer.website_label")}</strong> <a href="https://coactionsinfotech.org/" target="_blank">https://coactionsinfotech.org/</a></p>
            <p>💬 <strong>{t("footer.support_label")}</strong> {t("footer.support_hours")}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.button(t("buttons.back_to_home"), key="back_to_home_contact"):
            st.session_state.show_contact = False
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return
    
    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    st.markdown(f'<h1 class="welcome-heading">{t("home.welcome_heading")}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="welcome-subheading">{t("home.welcome_subheading")}</p>', unsafe_allow_html=True)
    show_home_banner()
    st.markdown('<hr>', unsafe_allow_html=True)
    
    with st.container(key="student_info_header_row"):
        header_col, lang_col = st.columns([4, 1])
        with header_col:
            st.markdown(f'<h3 class="section-title">📝 {t("home.student_information")}</h3>', unsafe_allow_html=True)
        with lang_col:
            language_options = ["English (Default)", "Tamil", "Hindi", "Marathi"]
            current_language = st.session_state.get("language", "English (Default)")
            if current_language not in language_options:
                current_language = "English (Default)"
            st.session_state.language = st.selectbox(
                t("labels.language"),
                language_options,
                index=language_options.index(current_language),
                key="language_selector",
                label_visibility="collapsed"
            )

    col1, col2 = st.columns(2)
    with col1:
        student_name = st.text_input(t("labels.full_name"), placeholder=t("labels.full_name_placeholder"), key="welcome_name")
        student_age = st.number_input(t("labels.age"), min_value=10, max_value=100, step=1, key="welcome_age")

        # MODIFICATION (dynamic State -> City location selection): State is
        # built by looping over INDIA_STATE_CITIES (all 28 States + 8 Union
        # Territories) - nothing state-specific is hardcoded into the
        # widget itself, so adding/removing a state only ever means editing
        # that dict. Sorted alphabetically for easy scanning in the dropdown.
        state_options = ["Select State"] + sorted(INDIA_STATE_CITIES.keys())
        student_state = st.selectbox(
            t("labels.state"),
            state_options,
            key="welcome_state"
        )
    with col2:
        student_institution = st.text_input(t("labels.institution"), placeholder=t("labels.institution_placeholder"), key="welcome_institution")
        student_grade = st.selectbox(t("labels.grade"), 
                                      ["Select Grade", "9th", "10th", "11th", "12th", "1st Year College", "2nd Year College", "3rd Year College", "4th Year College"],
                                      key="welcome_grade")

        # MODIFICATION (dynamic State -> City location selection): City
        # options cascade off whichever state was just picked above - the
        # top-3-cities list is looked up from INDIA_STATE_CITIES, never
        # hardcoded per state here. The widget's key is derived from the
        # selected state so Streamlit always mounts a fresh selectbox (reset
        # to "Select City") the moment the state changes, instead of
        # carrying over a city selection that no longer belongs to the
        # newly selected state.
        city_choices = INDIA_STATE_CITIES.get(student_state, [])
        city_options = ["Select City"] + city_choices
        student_city = st.selectbox(
            t("labels.city"),
            city_options,
            key=f"welcome_city_{student_state}",
            disabled=(student_state == "Select State"),
        )
    
    st.markdown('<hr>', unsafe_allow_html=True)
    
    # Define grade lists
    school_grades_list = ["9th", "10th", "11th", "12th"]
    college_grades_list = ["1st Year College", "2nd Year College", "3rd Year College", "4th Year College"]
    
    if st.button(t("home.start_journey"), type="primary", use_container_width=True, key="start_journey"):
        if (student_name and student_grade != "Select Grade"
                and student_state != "Select State" and student_city != "Select City"):
            st.session_state.student_name = student_name
            st.session_state.student_age = student_age
            st.session_state.student_city = student_city
            st.session_state.student_state = student_state
            st.session_state.student_institution = student_institution
            st.session_state.student_grade = student_grade
            
            if student_grade in school_grades_list:
                st.session_state.user_type = "school"
            elif student_grade in college_grades_list:
                st.session_state.user_type = "college"
            else:
                st.session_state.user_type = "school"
            
            st.session_state.page = 'load_assessment'
            st.rerun()
        elif not student_name:
            st.warning(t("messages.warning.enter_name"))
        elif student_state == "Select State":
            st.warning(t("messages.warning.select_state"))
        elif student_city == "Select City":
            st.warning(t("messages.warning.enter_city"))
        elif student_grade == "Select Grade":
            st.warning(t("messages.warning.select_grade"))
    
    st.markdown('</div>', unsafe_allow_html=True)

# Load Assessment Page
def show_load_assessment():
    with st.spinner(_t("Loading your personalized assessment...")):
        # Load appropriate JSON file based on user type, in the currently
        # selected UI language (falls back to English automatically if a
        # translated file is missing).
        if st.session_state.user_type == 'school':
            assessment_label = 'school_questions.json'
            data = load_school_questions()
        else:
            assessment_label = 'college_questions.json'
            data = load_college_questions()
        
        if data:
            questions, categories = extract_questions_from_json(data)
            st.session_state.questions_list = questions
            st.session_state.categories_data = categories
            st.session_state.responses = {}
            st.session_state.current_page = 0
            st.session_state.selected_stream = None
            st.session_state.recommended_categories = []
            st.session_state.page = 'assessment'
            st.rerun()
        else:
            st.error(f"{assessment_label} {t('messages.error.file_not_found_full')}")
            if st.button(t("buttons.go_back")):
                st.session_state.page = 'welcome'
                st.rerun()

# Assessment Page - 10 questions per page
def show_assessment():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    
    # Show user type indicator
    user_type_display = _t("School Student") if st.session_state.user_type == 'school' else _t("College Student")
    st.markdown(f'<div class="user-type-indicator">🎓 {user_type_display} {_t("Pathway")}</div>', unsafe_allow_html=True)
    
    if not st.session_state.questions_list:
        st.error(t("messages.error.no_questions_loaded"))
        if st.button(t("buttons.go_back")):
            st.session_state.page = 'welcome'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return
    
    total_questions = len(st.session_state.questions_list)
    questions_per_page = 10
    total_pages = (total_questions + questions_per_page - 1) // questions_per_page
    current_page = st.session_state.current_page
    start_idx = current_page * questions_per_page
    end_idx = min(start_idx + questions_per_page, total_questions)
    page_questions = st.session_state.questions_list[start_idx:end_idx]
    
    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.8rem;">{_t("Career Assessment")}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message" style="font-size:1.1rem;">{_t("Rate each statement honestly to get accurate career recommendations")}</p>', unsafe_allow_html=True)
    
    # Page counter
    st.markdown(f'<div class="page-counter">📄 {_t("Page")} {current_page + 1} {_t("of")} {total_pages}</div>', unsafe_allow_html=True)
    
    # Display questions for current page
    # NOTE: q['text'] and q['category_name'] come straight from the
    # language-specific JSON loaded by load_school_questions() /
    # load_college_questions() (e.g. school_questions.ta.json), so they
    # are already in the selected language. They are shown as-is here
    # (not passed through _t()) to avoid re-translating already-localized
    # text. _t() is reserved for the static, English-authored UI strings
    # on this page (headings, labels, buttons).
    for i, q in enumerate(page_questions):
        question_number = start_idx + i + 1
        st.markdown(f"""
        <div style="background: #FEF9F0; border-radius: 20px; padding: 1.2rem; margin: 1rem 0; border-left: 5px solid #E67E22; box-shadow: 0 2px 8px rgba(0,0,0,0.05);">
            <div style="font-weight: 700; color: #2E7D32; margin-bottom: 1rem; font-size: 1.2rem;">{question_number}. {q['text']}</div>
            <p style="color: #1565C0; font-size: 0.85rem; margin-bottom: 0.5rem;">{t("labels.category")} {q['category_name']}</p>
        </div>
        """, unsafe_allow_html=True)
        
        # Radio button with NO default selection (index=None)
        rating = st.radio(
            t("labels.select_answer"),
            options=[1, 2, 3, 4, 5],
            format_func=lambda x: {1: f"1 - {t('labels.strongly_disagree')}", 2: f"2 - {t('labels.disagree')}", 3: f"3 - {t('labels.neutral')}", 4: f"4 - {t('labels.agree')}", 5: f"5 - {t('labels.strongly_agree')}"}[x],
            horizontal=True,
            key=f"q_{q['id']}_{question_number}",
            index=0  # This prevents any default selection
        )
        
        # Only store if user made a selection
        if rating is not None:
            st.session_state.responses[q['id']] = rating
        
        st.markdown("<br>", unsafe_allow_html=True)
    
    # Calculate answered questions count
    answered_count = sum(1 for q in st.session_state.questions_list[:end_idx] 
                         if st.session_state.responses.get(q['id']) is not None)
    progress = answered_count / total_questions if total_questions > 0 else 0
    st.progress(progress)
    st.markdown(f'<p class="progress-text" style="font-size:0.9rem;">📊 {_t("Progress:")} {answered_count}/{total_questions} {_t("questions answered")} ({int(progress*100)}%)</p>', unsafe_allow_html=True)
    
    # Navigation buttons
    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if current_page > 0:
            if st.button(t("buttons.previous_page"), key="career_prev_page"):
                st.session_state.current_page -= 1
                st.rerun()
    
    with col3:
        # Check if all questions on current page are answered
        current_page_answered = all(
            st.session_state.responses.get(q['id']) is not None 
            for q in page_questions
        )
        
        if end_idx < total_questions:
            if current_page_answered:
                if st.button(t("buttons.next_page"), key="career_next_page", type="primary"):
                    st.session_state.current_page += 1
                    st.rerun()
            else:
                # Show disabled button with warning
                st.button(t("buttons.next_page"), key="career_next_page_disabled", disabled=True)
                st.warning(f"⚠️ {t('messages.warning.answer_all_prefix')} {len(page_questions)} {t('messages.warning.questions_on_page')}")
        else:
            # Last page - check if all questions are answered
            all_answered = all(
                st.session_state.responses.get(q['id']) is not None 
                for q in st.session_state.questions_list
            )
            if all_answered:
                if st.button(t("buttons.submit_get_results"), type="primary", key="submit_career_results"):
                    # Calculate career assessment results
                    st.session_state.categories_data, st.session_state.recommended_categories = calculate_results(
                        st.session_state.responses, st.session_state.questions_list, st.session_state.categories_data
                    )
                    # Go to RIASEC assessment choice (OPTIONAL assessment)
                    st.session_state.page = 'riasec_choice'
                    st.rerun()
            else:
                unanswered = total_questions - answered_count
                st.button(t("buttons.submit_get_results"), key="submit_career_results_disabled", disabled=True)
                st.warning(f"⚠️ {t('messages.warning.answer_more_prefix')} {unanswered} {t('messages.warning.more_questions_before_submit')}")
    
    st.markdown('</div>', unsafe_allow_html=True)

def show_riasec_choice():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    
    st.markdown(f'''
    <div class="student-info-card">
        👋 {_t("Welcome,")} <strong>{st.session_state.student_name}</strong><br>
        📍 {st.session_state.student_city} | 🎂 {_t("Age:")} {st.session_state.student_age} | 📚 {st.session_state.student_grade}
    </div>
    ''', unsafe_allow_html=True)
    
    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.8rem;">🧭 {_t("Optional: RIASEC Career Assessment")}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="welcome-subheading">⚠️ <strong>{_t("Note: Your career assessment is already complete. This is optional.")}</strong></p>', unsafe_allow_html=True)
    
    # Add Back button to go to career assessment
    col_back1, col_back2, col_back3 = st.columns([1, 2, 1])
    with col_back1:
        if st.button(t("buttons.back_short"), key="back_to_career_assessment", use_container_width=True):
            st.session_state.page = 'assessment'
            st.rerun()
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    # ============ DETAILS SHOWN DIRECTLY (NO EXPANDER) ============
    st.markdown(f"""
    <div style="background: #E8F5E9; border-radius: 16px; padding: 1rem; margin: 1rem 0; border-left: 5px solid #1565C0;">
        <strong style="color: #1565C0; font-size: 1.1rem;">🔍 {_t("What does the RIASEC assessment include?")}</strong><br><br>
        {_t("The AI-powered RIASEC (Holland Code) assessment identifies your career personality type by evaluating your interest across six dimensions:")}
        <ul>
            <li><strong>{_t("Realistic")}</strong>: {_t("Hands-on, mechanical, and practical activities")}</li>
            <li><strong>{_t("Investigative")}</strong>: {_t("Analytical, scientific, and research-driven thinking")}</li>
            <li><strong>{_t("Artistic")}</strong>: {_t("Creative, expressive, and original work")}</li>
            <li><strong>{_t("Social")}</strong>: {_t("Helping, teaching, and working with people")}</li>
            <li><strong>{_t("Enterprising")}</strong>: {_t("Leading, persuading, and business-minded thinking")}</li>
            <li><strong>{_t("Conventional")}</strong>: {_t("Organised, detail-oriented, and structured work")}</li>
        </ul>
        {_t("Based on your responses, our AI will generate your career personality type, top-matching careers, strengths, learning style, and suitable work environment - personalized just for you!")}
    </div>
    """, unsafe_allow_html=True)
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    # Display assessment options
    st.markdown(f'<p style="text-align: center; font-size: 1.1rem; font-weight: 500;">{_t("Choose an option below:")}</p>', unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown(f'''
        <div class="user-card personality-choice-card">
            <div>
                <div class="user-icon">🧭</div>
                <h3>{_t("Take RIASEC Assessment")}</h3>
                <p>{_t("Take our AI-powered RIASEC (Holland Code) assessment to discover your career personality type and best-matching careers.")}</p>
                <p style="margin-top:10px; font-size:0.8rem; color:#1E88E5;">⏱️ {_t("Takes about 10-15 minutes")}</p>
                <p style="font-size:0.8rem; color:#1E88E5;">📊 {_t("30 questions")}</p>
                <p style="margin-top:10px; font-size:0.8rem; color:#1565C0;">🤖 {_t("Fully AI-generated results")}</p>
            </div>
        </div>
        ''', unsafe_allow_html=True)

        if st.button(t("buttons.take_riasec_rocket"), key="take_riasec_btn", use_container_width=True):
            st.session_state.riasec_questions = get_riasec_questions(st.session_state.user_type)
            st.session_state.riasec_responses = {}
            st.session_state.riasec_current_page = 0
            st.session_state.riasec_completed = False
            st.session_state.riasec_scores = None
            st.session_state.riasec_result = None
            st.session_state.riasec_result_fingerprint = None
            st.session_state.page = 'riasec_assessment'
            st.rerun()

    with col2:
        st.markdown(f'''
        <div class="user-card personality-choice-card">
            <div>
                <div class="user-icon">⏭️</div>
                <h3>{_t("Skip RIASEC Assessment")}</h3>
                <p>{_t("Skip the RIASEC test and go directly to your career stream comparison.")}</p>
                <p style="margin-top:10px; font-size:0.8rem; color:#1E88E5;">⚡ {_t("Continue directly")}</p>
                <p style="font-size:0.8rem; color:#1E88E5;">📊 {_t("View your career recommendations")}</p>
                <p style="margin-top:10px; font-size:0.8rem; color:#1565C0;">🎯 {_t("Your career assessment results are ready!")}</p>
            </div>
        </div>
        ''', unsafe_allow_html=True)
        
        if st.button(t("buttons.skip_riasec"), key="skip_riasec_btn", use_container_width=True):
            st.session_state.page = 'recommendation'
            st.rerun()
    
    st.markdown('</div>', unsafe_allow_html=True)

def show_riasec_assessment():
    """
    Paginated 30-question RIASEC Likert questionnaire, mirroring the
    structure of the previous learning-style assessment page. On submit,
    only the raw per-dimension scores are computed locally
    (calculate_riasec_scores) - the actual RIASEC result (type, careers,
    strengths, etc.) is generated by Gemini on the results page, never here.
    """
    col_home, col_spacer = st.columns([1, 11])
    with col_home:
        if st.button(t("nav.home"), key="riasec_home", use_container_width=True):
            for key in list(st.session_state.keys()):
                if key not in ['page']:
                    del st.session_state[key]
            st.session_state.page = 'welcome'
            st.rerun()

    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    if not st.session_state.riasec_questions:
        st.session_state.riasec_questions = get_riasec_questions(st.session_state.user_type)

    questions_per_page = 10
    total_questions = len(st.session_state.riasec_questions)
    total_pages = (total_questions + questions_per_page - 1) // questions_per_page
    current_page = st.session_state.riasec_current_page
    start_idx = current_page * questions_per_page
    end_idx = min(start_idx + questions_per_page, total_questions)
    page_questions = st.session_state.riasec_questions[start_idx:end_idx]

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.5rem;">🧭 {_t("RIASEC Career Assessment")}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message">{_t("Page")} {current_page + 1} {_t("of")} {total_pages} • {_t("How much would you enjoy each activity?")}</p>', unsafe_allow_html=True)

    for i, q in enumerate(page_questions):
        q_index = start_idx + i
        # NOTE: q['text'] comes straight from the language-specific JSON
        # loaded by load_riasec_questions() (e.g. riasec.ta.json), so it's
        # already in the selected language - shown as-is, not passed
        # through _t(), to avoid re-translating already-localized text.
        # The Likert options below (q['options']) ARE still passed through
        # _t() because they come from the hardcoded English
        # RIASEC_LIKERT_OPTIONS constant, not from the JSON file.
        st.markdown(f"""
        <div class="question-card">
            <div class="question-text">{q_index + 1}. {q['text']}</div>
        </div>
        """, unsafe_allow_html=True)

        current_value = st.session_state.riasec_responses.get(q['id'], None)

        # NOTE: `options=q['options']` and the returned `answer` value are
        # kept exactly as-is (untranslated English strings) so scoring in
        # calculate_riasec_scores() - which matches against
        # RIASEC_LIKERT_SCORES - is completely unaffected. Only the
        # on-screen label of each option is translated via format_func;
        # Streamlit still returns the original English option as `answer`.
        answer = st.radio(
            t("labels.select_answer"),
            options=q['options'],
            format_func=lambda opt: _t(opt),
            horizontal=True,
            key=f"riasec_q_{q['id']}_{q_index}",
            index=None
        )

        if answer is not None:
            st.session_state.riasec_responses[q['id']] = answer

        st.markdown("<br>", unsafe_allow_html=True)

    answered_count = sum(1 for q in st.session_state.riasec_questions[:end_idx]
                         if st.session_state.riasec_responses.get(q['id']) is not None)
    progress = answered_count / total_questions if total_questions > 0 else 0
    st.progress(progress)
    st.markdown(f'<p class="progress-text">📊 {_t("Progress:")} {answered_count}/{total_questions} {_t("questions answered")} ({int(progress*100)}%)</p>', unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])
    with col1:
        if current_page > 0:
            if st.button(t("buttons.previous_page"), key="riasec_prev_page", use_container_width=True):
                st.session_state.riasec_current_page -= 1
                st.rerun()

    with col3:
        current_page_answered = all(
            st.session_state.riasec_responses.get(q['id']) is not None
            for q in page_questions
        )

        if end_idx < total_questions:
            if current_page_answered:
                if st.button(t("buttons.next_page"), key="riasec_next_page", type="primary", use_container_width=True):
                    st.session_state.riasec_current_page += 1
                    st.rerun()
            else:
                st.button(t("buttons.next_page"), key="riasec_next_page_disabled", use_container_width=True, disabled=True)
                st.warning(f"⚠️ {t('messages.warning.answer_all_prefix')} {len(page_questions)} {t('messages.warning.questions_on_page')}")
        else:
            all_answered = all(
                st.session_state.riasec_responses.get(q['id']) is not None
                for q in st.session_state.riasec_questions
            )
            if all_answered:
                if st.button(t("buttons.submit_see_results"), key="riasec_submit", type="primary", use_container_width=True):
                    valid_responses = {k: v for k, v in st.session_state.riasec_responses.items() if v is not None}
                    st.session_state.riasec_scores = calculate_riasec_scores(
                        valid_responses, st.session_state.riasec_questions
                    )
                    st.session_state.riasec_completed = True
                    st.session_state.riasec_result = None
                    st.session_state.riasec_result_fingerprint = None
                    st.session_state.page = 'riasec_result'
                    st.rerun()
            else:
                unanswered = total_questions - answered_count
                st.button(t("buttons.submit_see_results"), key="riasec_submit_disabled", type="primary", use_container_width=True, disabled=True)
                st.warning(f"⚠️ {t('messages.warning.answer_more_prefix')} {unanswered} {t('messages.warning.more_questions_before_submit')}")

    st.markdown("---")
    col_skip1, col_skip2, col_skip3 = st.columns([1, 2, 1])
    with col_skip2:
        if st.button(t("buttons.skip_for_now"), key="riasec_skip", use_container_width=True):
            st.session_state.page = 'recommendation'
            st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_riasec_result():
    """
    Calls Gemini (cached by response-fingerprint, same policy as every
    other AI content in this app) to generate the full RIASEC result, then
    renders: RIASEC code + personality type, description, top matching
    careers with match percentages, strengths, learning style, and
    suitable work environment - all AI-generated, nothing hardcoded.
    """
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.8rem;">🧭 {_t("Your RIASEC Career Assessment")}</h1>', unsafe_allow_html=True)

    if not st.session_state.riasec_scores:
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {_t("No RIASEC responses found yet. Please take the assessment first.")}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.button(t("buttons.take_riasec_compass"), use_container_width=True):
            st.session_state.riasec_questions = get_riasec_questions(st.session_state.user_type)
            st.session_state.riasec_responses = {}
            st.session_state.riasec_current_page = 0
            st.session_state.page = 'riasec_assessment'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'grade': st.session_state.student_grade,
    }

    # Cache by fingerprint: only call Gemini again if the RIASEC answers or
    # user_type actually changed since the last call.
    current_fp = make_response_fingerprint(
        st.session_state.riasec_responses,
        st.session_state.user_type,
    )
    if st.session_state.riasec_result is None or st.session_state.riasec_result_fingerprint != current_fp:
        with st.spinner(_t("Analysing your RIASEC responses with AI...")):
            result = generate_ai_riasec_assessment(
                student_details,
                st.session_state.riasec_scores,
                st.session_state.user_type,
            )
        st.session_state.riasec_result_status = result
        st.session_state.riasec_result_fingerprint = current_fp
    else:
        result = st.session_state.riasec_result_status

    riasec_result = st.session_state.riasec_result

    if not riasec_result:
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {(result or {}).get('message', _t('Your RIASEC assessment is not available right now.'))}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('riasec_result_error'):
            with st.expander(_t("Technical details (for debugging)")):
                st.code(st.session_state.riasec_result_error)
        if st.button(t("buttons.retry_ai_analysis"), key="riasec_retry", use_container_width=True):
            st.session_state.riasec_result = None
            st.session_state.riasec_result_fingerprint = None
            st.rerun()
    else:
        st.markdown(f"""
        <div class="stream-detail-card">
            <div style="text-align:center;">
                <div style="font-size:2rem; opacity:0.85; font-weight:600;">{_t("RIASEC Code:")} {riasec_result.get('riasec_code', '')}</div>
                <h1 style="color:#D35400; margin:0.5rem 0;">{riasec_result.get('personality_type', '')}</h1>
            </div>
        </div>
        """, unsafe_allow_html=True)

        st.markdown(f"<p>{riasec_result.get('description', '')}</p>", unsafe_allow_html=True)

        # ---- Dimension score bars (raw scores, from calculate_riasec_scores) ----
        st.markdown(f"**{_t('📊 Your RIASEC Dimension Scores')}**")
        for cat, pct in st.session_state.riasec_scores['ranked']:
            st.markdown(f"""
            <div style="margin-bottom:0.6rem;">
                <div style="display:flex; justify-content:space-between; font-size:0.9rem;">
                    <span>{_t(RIASEC_DIMENSIONS[cat])}</span><span>{pct}%</span>
                </div>
                <div class="score-bar">
                    <div class="score-fill" style="width:{pct}%;"></div>
                </div>
            </div>
            """, unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ---- Top Matching Careers ----
        st.markdown(f"**{_t('🎯 Top Matching Careers')}**")
        careers = riasec_result.get('top_matching_careers', [])
        for row_start in range(0, len(careers), 2):
            row_careers = careers[row_start:row_start + 2]
            row_cols = st.columns(len(row_careers))
            for j, career in enumerate(row_careers):
                with row_cols[j]:
                    st.markdown(f"""
                    <div class="stream-card-good rec-card">
                        <h3 style="margin:0.4rem 0;">{career['career_name']}</h3>
                        <div class="score-bar">
                            <div class="score-fill" style="width:{career['match_percentage']}%;"></div>
                        </div>
                        <div style="font-size:1.1rem; font-weight:700; margin:0.4rem 0;">{career['match_percentage']}% {_t("Match")}</div>
                        <p style="font-size:0.85rem; line-height:1.4; text-align:left;">{career.get('why_it_fits', '')}</p>
                    </div>
                    """, unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)

        # ---- Strengths ----
        st.markdown(f"**{_t('✨ Your Key Strengths')}**")
        for strength in riasec_result.get('strengths', []):
            st.markdown(f"- {strength}")

        st.markdown("<br>", unsafe_allow_html=True)

        # ---- Learning Style & Work Environment ----
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown('<div class="question-card" style="text-align:left; height:100%;">', unsafe_allow_html=True)
            st.markdown(f"**{_t('📚 Learning Style')}**")
            st.markdown(riasec_result.get('learning_style', ''))
            st.markdown('</div>', unsafe_allow_html=True)
        with col_b:
            st.markdown('<div class="question-card" style="text-align:left; height:100%;">', unsafe_allow_html=True)
            st.markdown(f"**{_t('🏢 Suitable Work Environment')}**")
            st.markdown(riasec_result.get('suitable_work_environment', ''))
            st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    with col1:
        if st.button(t("buttons.retake_riasec"), key="riasec_retake", use_container_width=True):
            st.session_state.riasec_questions = get_riasec_questions(st.session_state.user_type)
            st.session_state.riasec_responses = {}
            st.session_state.riasec_current_page = 0
            st.session_state.riasec_completed = False
            st.session_state.riasec_scores = None
            st.session_state.riasec_result = None
            st.session_state.riasec_result_fingerprint = None
            st.session_state.page = 'riasec_assessment'
            st.rerun()
    with col2:
        if st.button(t("buttons.continue_arrow"), key="riasec_continue", type="primary", use_container_width=True):
            st.session_state.page = 'recommendation'
            st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


# Report Page
# ==================== AI RECOMMENDATION PAGE (PLACEHOLDER) ====================
def show_recommendation():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.8rem;">🎯 {_t("Your Top 3 AI Career Recommendations")}</h1>', unsafe_allow_html=True)

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    # Cache by fingerprint: only call Gemini again if the questionnaire /
    # personality answers / user type actually changed since the last call.
    current_fp = make_response_fingerprint(
        st.session_state.responses,
        st.session_state.personality_responses,
        st.session_state.user_type,
    )
    if st.session_state.ai_recommendation is None or st.session_state.ai_recommendation_fingerprint != current_fp:
        with st.spinner(_t("Analysing your responses with AI...")):
            recommendation = generate_ai_recommendation(
                student_details,
                st.session_state.responses,
                st.session_state.personality_responses,
                st.session_state.user_type,
            )
        st.session_state.ai_recommendation = recommendation
        st.session_state.ai_recommendation_fingerprint = current_fp
    else:
        recommendation = st.session_state.ai_recommendation

    streams = st.session_state.ai_top_streams

    if recommendation.get("status") != "success" or not streams:
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {recommendation.get('message', _t('AI recommendations are not available right now.'))}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('gemini_response_error'):
            with st.expander(_t("Technical details (for debugging)")):
                st.code(st.session_state.gemini_response_error)
        if st.button(t("buttons.retry_ai_analysis"), use_container_width=True):
            st.session_state.ai_recommendation = None
            st.session_state.ai_top_streams = None
            st.rerun()
    else:
        card_styles = ["stream-card-high", "stream-card-good", "stream-card-fair"]
        cols = st.columns(3)
        for idx, stream in enumerate(streams[:3]):
            display_stream = stream
            style = card_styles[idx % len(card_styles)]
            with cols[idx]:
                st.markdown(f"""
                <div class="{style} rec-card">
                    <div style="font-size:0.85rem; opacity:0.85; font-weight:600;">#{idx + 1} {_t("RECOMMENDED")}</div>
                    <h3 style="margin:0.4rem 0;">{display_stream['stream_name']}</h3>
                    <div class="score-bar">
                        <div class="score-fill" style="width:{stream['match_percentage']}%;"></div>
                    </div>
                    <div style="font-size:1.1rem; font-weight:700; margin:0.4rem 0;">{stream['match_percentage']}% {_t("Match")}</div>
                    <p style="font-size:0.85rem; line-height:1.4; text-align:left;">{display_stream['explanation']}</p>
                </div>
                """, unsafe_allow_html=True)
                if st.button(t("buttons.explore"), key=f"explore_{idx}", use_container_width=True):
                    st.session_state.selected_stream = stream['stream_name']
                    st.session_state.selected_stream_data = stream
                    st.session_state.ai_analysis = None
                    st.session_state.career_overview = None
                    st.session_state.ai_deep_dive = None
                    st.session_state.page = 'ai_analysis'
                    st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    with col1:
        if st.button(t("buttons.back_to_assessment"), use_container_width=True):
            st.session_state.page = 'assessment'
            st.rerun()
    with col2:
        if streams and st.button(t("buttons.continue_report"), type="primary", use_container_width=True):
            if not st.session_state.get('selected_stream_data'):
                st.session_state.selected_stream = streams[0]['stream_name']
                st.session_state.selected_stream_data = streams[0]
            st.session_state.ai_analysis = None
            st.session_state.career_overview = None
            st.session_state.ai_deep_dive = None
            st.session_state.page = 'ai_analysis'
            st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_ai_analysis():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    stream = st.session_state.get('selected_stream_data')
    if not stream:
        st.warning(t("messages.warning.no_stream_selected"))
        if st.button(t("buttons.back_to_recommendations"), use_container_width=True):
            st.session_state.page = 'recommendation'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.8rem;">🧠 {_t("AI Analysis")}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message" style="text-align:center;">{_t(stream["stream_name"])}</p>', unsafe_allow_html=True)

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    # CACHING POLICY: AI Analysis and Career Overview (below) share this same
    # fingerprint and are both stored in st.session_state (ai_analysis,
    # career_overview). Gemini is called again ONLY when the questionnaire
    # (+ personality) responses change or a different career/stream is
    # selected - `personality_responses` is included because it's part of
    # the same pre-recommendation questionnaire/assessment step, not a
    # separate trigger. Simply re-rendering this page never re-calls Gemini.
    current_fp = make_response_fingerprint(
        st.session_state.responses,
        st.session_state.personality_responses,
        st.session_state.user_type,
        stream['stream_name'],
    )
    if st.session_state.ai_analysis is None or st.session_state.ai_analysis_fingerprint != current_fp:
        with st.spinner(_t("Generating your personalized AI Analysis...")):
            result = generate_ai_analysis(
                student_details,
                st.session_state.responses,
                st.session_state.personality_responses,
                stream['stream_name'],
                st.session_state.user_type,
            )
        st.session_state.ai_analysis_status = result
        st.session_state.ai_analysis_fingerprint = current_fp

    analysis = st.session_state.ai_analysis

    if not analysis:
        msg = (st.session_state.ai_analysis_status or {}).get(
            'message', _t('AI Analysis could not be generated right now.')
        )
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {msg}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('ai_analysis_error'):
            with st.expander(_t("Technical details (for debugging)")):
                st.code(st.session_state.ai_analysis_error)
        if st.button(t("buttons.retry_ai_analysis"), use_container_width=True):
            st.session_state.ai_analysis = None
            st.rerun()
    else:
        display_analysis = analysis
        col1, col2 = st.columns(2)
        with col1:
            st.markdown('<div class="stream-card-high compare-card" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown(f'<h3>💪 {_t("Strengths")}</h3>', unsafe_allow_html=True)
            for s in display_analysis['strengths']:
                st.markdown(f"- {s}")
            st.markdown('</div>', unsafe_allow_html=True)
        with col2:
            st.markdown('<div class="stream-card-potential compare-card" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown(f'<h3>🚀 {_t("Opportunities")}</h3>', unsafe_allow_html=True)
            for o in display_analysis['opportunities']:
                st.markdown(f"- {o}")
            st.markdown('</div>', unsafe_allow_html=True)

        # ---- Career Overview: generated automatically once the AI Analysis
        # above has completed successfully, cached by the same fingerprint
        # approach, shown in a modern expandable card. ----
        if st.session_state.career_overview is None or st.session_state.career_overview_fingerprint != current_fp:
            with st.spinner(_t("Generating your Career Overview...")):
                overview_result = generate_career_overview(
                    student_details,
                    st.session_state.responses,
                    st.session_state.personality_responses,
                    stream['stream_name'],
                    st.session_state.user_type,
                )
            st.session_state.career_overview_status = overview_result
            st.session_state.career_overview_fingerprint = current_fp

        overview = st.session_state.career_overview

        st.markdown("<br>", unsafe_allow_html=True)

        if not overview:
            ov_msg = (st.session_state.career_overview_status or {}).get(
                'message', _t('Career Overview could not be generated right now.')
            )
            st.markdown(f"""
            <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
                <p>⚠️ {ov_msg}</p>
            </div>
            """, unsafe_allow_html=True)
            if st.session_state.get('career_overview_error'):
                with st.expander(_t("Technical details (for debugging)")):
                    st.code(st.session_state.career_overview_error)
            if st.button(t("buttons.retry_career_overview"), use_container_width=True):
                st.session_state.career_overview = None
                st.rerun()
        else:
            display_overview = overview
            with st.expander(f"📘 {_t('Career Overview')} — {_t(stream['stream_name'])}", expanded=True):
                st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
                st.markdown(f"**{_t('📖 Career Description')}**")
                st.markdown(display_overview.get("career_description", ""))
                st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
                st.markdown(f"**{_t('🎯 Why This Career Matches You')}**")
                st.markdown(display_overview.get("why_matches", ""))
                st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
                st.markdown(f"**{_t('🗓️ Daily Responsibilities')}**")
                for item in display_overview.get("daily_responsibilities", []):
                    st.markdown(f"- {item}")
                st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
                st.markdown(f"**{_t('📈 Future Scope')}**")
                st.markdown(display_overview.get("future_scope", ""))
                st.markdown('</div>', unsafe_allow_html=True)

                st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
                st.markdown(f"**{_t('🌱 Career Growth')}**")
                st.markdown(display_overview.get("career_growth", ""))
                st.markdown('</div>', unsafe_allow_html=True)

            # ---- Related Career Roles: grouped by AI-generated category,
            # shown below the Career Overview. Clicking a role opens the
            # full AI Career Detail page for that role. ----
            role_groups = overview.get("related_career_roles", [])
            if role_groups:
                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
                st.markdown(f"**{_t('🧭 Related Career Roles')}**")
                st.markdown(_t("Click a role to see its full AI-generated Career Detail page."))
                st.markdown('</div>', unsafe_allow_html=True)

                for g_idx, group in enumerate(role_groups):
                    category = group.get("category", "")
                    roles = group.get("roles", [])
                    if not roles:
                        continue
                    st.markdown(f"##### {_t(category)}")
                    for row_start in range(0, len(roles), 3):
                        row_roles = roles[row_start:row_start + 3]
                        row_cols = st.columns(3)
                        for j, role in enumerate(row_roles):
                            r_idx = row_start + j
                            with row_cols[j]:
                                if st.button(_t(role), key=f"ov_role_{g_idx}_{r_idx}", use_container_width=True):
                                    st.session_state.selected_career_role = role
                                    st.session_state.ai_role_detail = None
                                    st.session_state.role_detail_return_page = 'ai_analysis'
                                    st.session_state.page = 'role_detail'
                                    st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)

    col1, col2 = st.columns(2)
    with col1:
        if st.button(t("buttons.back_to_recommendations"), use_container_width=True):
            st.session_state.page = 'recommendation'
            st.rerun()
    with col2:
        if analysis and st.button(t("buttons.continue_full_report"), type="primary", use_container_width=True):
            st.session_state.ai_deep_dive = None
            st.session_state.page = 'report'
            st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_role_detail():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    role_name = st.session_state.get('selected_career_role')
    stream = st.session_state.get('selected_stream_data')

    if not role_name or not stream:
        st.warning(t("messages.warning.no_role_selected"))
        if st.button(t("buttons.back_to_report"), use_container_width=True):
            st.session_state.page = 'report'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.7rem;">🧭 {_t(role_name)}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message" style="text-align:center;">{_t("within")} {_t(stream["stream_name"])}</p>', unsafe_allow_html=True)

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    # CACHING POLICY: this cached Career Detail is regenerated ONLY if the
    # questionnaire responses change or a different career role/stream is
    # selected - never on every rerun/navigation. `responses` was
    # previously missing here, so a questionnaire retake did NOT invalidate
    # an already-cached role detail; now it does, matching the same policy
    # used for AI Analysis / Career Overview / the deep-dive report below.
    current_fp = make_response_fingerprint(
        st.session_state.responses,
        st.session_state.user_type,
        stream['stream_name'],
        role_name,
    )
    if st.session_state.ai_role_detail is None or st.session_state.ai_role_detail_fingerprint != current_fp:
        with st.spinner(f"{_t('Generating AI breakdown for')} {_t(role_name)}..."):
            result = generate_ai_role_detail(
                student_details,
                role_name,
                stream['stream_name'],
                st.session_state.user_type,
            )
        st.session_state.ai_role_detail_status = result
        st.session_state.ai_role_detail_fingerprint = current_fp

    detail = st.session_state.ai_role_detail

    st.markdown("<hr>", unsafe_allow_html=True)

    if not detail:
        msg = (st.session_state.ai_role_detail_status or {}).get(
            'message', _t('This role breakdown could not be generated right now.')
        )
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {msg}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('ai_role_detail_error'):
            with st.expander(_t("Technical details (for debugging)")):
                st.code(st.session_state.ai_role_detail_error)
        if st.button(t("buttons.retry"), use_container_width=True):
            st.session_state.ai_role_detail = None
            st.rerun()
    else:
        def section(title, icon, body):
            st.markdown(f'<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown(f"**{icon} {_t(title)}**")
            display_body = body
            if isinstance(display_body, list):
                for item in display_body:
                    st.markdown(f"- {item}")
            else:
                st.markdown(display_body)
            st.markdown('</div>', unsafe_allow_html=True)

        section("Career Description", "📘", detail.get("career_description", ""))
        section("Salary Range (India)", "💰", detail.get("salary_range_india", ""))
        section("Educational Requirements", "🎓", detail.get("educational_requirements", []))
        section("Required Skills", "🛠️", detail.get("required_skills", []))
        section("Job Responsibilities", "🧩", detail.get("job_responsibilities", []))
        section("Job Growth", "📈", detail.get("future_job_growth", ""))
        section("Industry Outlook", "🏭", detail.get("industry_outlook", ""))
        section("Top Hiring Companies", "🏢", detail.get("top_hiring_companies", []))
        section("Future Demand", "🔮", detail.get("future_demand", ""))

    if detail:
        if st.button(f"🗓️ {_t('View 12-Month Roadmap for')} {_t(role_name)}", type="primary", use_container_width=True):
            st.session_state.roadmap_career_name = role_name
            st.session_state.learning_roadmap = None
            st.session_state.roadmap_return_page = 'role_detail'
            st.session_state.page = 'learning_roadmap'
            st.rerun()

        if st.button(f"🧭 {_t('View AI Skill Gap Analysis for')} {_t(role_name)}", use_container_width=True):
            st.session_state.skillgap_career_name = role_name
            st.session_state.skill_gap_analysis = None
            st.session_state.skillgap_return_page = 'role_detail'
            st.session_state.page = 'skill_gap'
            st.rerun()

        if st.button(f"📄 {_t('Get AI Resume Suggestions for')} {_t(role_name)}", use_container_width=True):
            st.session_state.resume_career_name = role_name
            st.session_state.resume_suggestions = None
            st.session_state.resume_suggestions_return_page = 'role_detail'
            st.session_state.page = 'resume_suggestions'
            st.rerun()

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button(t("buttons.download_full_pdf"), use_container_width=True):
            with st.spinner(_t("Generating your PDF...")):
                try:
                    pdf_path = generate_role_detail_pdf(role_name, stream['stream_name'])
                    with open(pdf_path, "rb") as pdf_file:
                        pdf_data = pdf_file.read()
                    safe_role_name = "".join(c if c.isalnum() else "_" for c in role_name)
                    st.download_button(
                        label=_t("💾 Save PDF"),
                        data=pdf_data,
                        file_name=f"{st.session_state.student_name}_{safe_role_name}_career_detail.pdf",
                        mime="application/pdf",
                        key="download_role_detail_pdf"
                    )
                    os.unlink(pdf_path)
                    st.success(t("messages.success.pdf_generated_full"))
                except Exception as e:
                    st.error(f"{t('messages.error.pdf_generation')} {str(e)}")

    st.markdown("<hr>", unsafe_allow_html=True)

    if st.button(t("buttons.back_arrow"), use_container_width=True):
        st.session_state.page = st.session_state.get('role_detail_return_page') or 'report'
        st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_learning_roadmap():
    """
    Student -> Select Recommended Career -> AI-generated 12-Month Learning
    Roadmap, displayed as a vertical timeline. Entirely Gemini-generated
    (no JSON template / hardcoded roadmap of any kind) using student
    details, student type, the selected career, questionnaire responses,
    and the personality/learning-style assessment when available. Cached
    in st.session_state.learning_roadmap and only regenerated when the
    underlying inputs (career or questionnaire responses) change.
    """
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    career_name = st.session_state.get('roadmap_career_name')

    if not career_name:
        st.warning(t("messages.warning.no_career_roadmap"))
        if st.button(t("buttons.back_to_report"), use_container_width=True):
            st.session_state.page = 'report'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.6rem;">🗓️ {_t("12-Month Learning Roadmap")}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message" style="text-align:center;">{_t("for")} {_t(career_name)}</p>', unsafe_allow_html=True)

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    # CACHING POLICY: regenerated ONLY if the questionnaire responses,
    # student type, personality pathway, or the selected career changes -
    # never on every rerun/navigation, matching the same caching pattern
    # used for AI Analysis / Career Overview / Career Detail above.
    current_fp = make_response_fingerprint(
        st.session_state.responses,
        st.session_state.user_type,
        st.session_state.get('personality_pathway'),
        career_name,
    )
    if st.session_state.learning_roadmap is None or st.session_state.learning_roadmap_fingerprint != current_fp:
        with st.spinner(f"{_t('Generating your personalized 12-month roadmap for')} {_t(career_name)}..."):
            result = generate_ai_learning_roadmap(
                student_details,
                st.session_state.responses,
                st.session_state.get('personality_pathway'),
                career_name,
                st.session_state.user_type,
            )
        st.session_state.learning_roadmap_status = result
        st.session_state.learning_roadmap_fingerprint = current_fp

    roadmap = st.session_state.learning_roadmap

    st.markdown("<hr>", unsafe_allow_html=True)

    if not roadmap:
        msg = (st.session_state.learning_roadmap_status or {}).get(
            'message', _t('This learning roadmap could not be generated right now.')
        )
        error_html = (
            f'<div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">'
            f'<p>⚠️ {msg}</p>'
            f'</div>'
        )
        st.markdown(error_html, unsafe_allow_html=True)
        if st.session_state.get('learning_roadmap_error'):
            with st.expander(_t("Technical details (for debugging)")):
                st.code(st.session_state.learning_roadmap_error)
        if st.button(t("buttons.retry"), use_container_width=True):
            st.session_state.learning_roadmap = None
            st.rerun()
    else:
        display_roadmap = roadmap
        level_label = _t("Beginner Level") if st.session_state.user_type == 'school' else _t("Advanced / Industry Level")
        overview_html = (
            f'<div class="roadmap-overview-card">'
            f'<strong>📌 {_t("Roadmap Level")}:</strong> {level_label}<br><br>'
            f'<p>{display_roadmap.get("roadmap_overview", "")}</p>'
            f'</div>'
        )
        st.markdown(overview_html, unsafe_allow_html=True)

        st.markdown('<div class="roadmap-timeline">', unsafe_allow_html=True)
        for month in display_roadmap.get('months', []):
            month_num = month.get('month_number', '')

            def bullet_list(items):
                items = items or []
                if not items:
                    return f"<li><em>{_t('None this month')}</em></li>"
                return "".join(f"<li>{item}</li>" for item in items)

            certs = month.get('certifications', []) or []
            cert_html = (
                "".join(f'<span class="roadmap-cert-chip">📜 {c}</span>' for c in certs)
                if certs else f"<em>{_t('No certification this month')}</em>"
            )

            month_html = (
                f'<div class="roadmap-month">'
                f'<div class="roadmap-month-dot">{month_num}</div>'
                f'<div class="roadmap-month-card">'
                f'<h4>{_t("Month")} {month_num}: {month.get("month_title", "")}</h4>'
                f'<div class="roadmap-section-label">🧠 {_t("Skills to Learn")}</div>'
                f'<ul>{bullet_list(month.get("skills_to_learn"))}</ul>'
                f'<div class="roadmap-section-label">📖 {_t("Topics")}</div>'
                f'<ul>{bullet_list(month.get("topics"))}</ul>'
                f'<div class="roadmap-section-label">✍️ {_t("Practice Activities")}</div>'
                f'<ul>{bullet_list(month.get("practice_activities"))}</ul>'
                f'<div class="roadmap-section-label">🛠️ {_t("Mini Projects")}</div>'
                f'<ul>{bullet_list(month.get("mini_projects"))}</ul>'
                f'<div class="roadmap-section-label">🆓 {_t("Recommended Free Resources")}</div>'
                f'<ul>{bullet_list(month.get("free_resources"))}</ul>'
                f'<div class="roadmap-section-label">📜 {_t("Certifications")}</div>'
                f'<div>{cert_html}</div>'
                f'</div>'
                f'</div>'
            )
            st.markdown(month_html, unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)

    if st.button(t("buttons.back_arrow"), use_container_width=True):
        st.session_state.page = st.session_state.get('roadmap_return_page') or 'report'
        st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_skill_gap_analysis():
    """
    Student -> Select Career -> AI Skill Gap Analysis, displayed with
    progress bars and cards. Entirely Gemini-generated (no predefined/
    static skill list of any kind) by comparing the student's current
    abilities (questionnaire signal, personality/learning-style, and any
    prior AI-identified strengths) against the industry skills Gemini
    itself determines are required for the selected career. Cached in
    st.session_state.skill_gap_analysis and only regenerated when the
    underlying inputs (career or questionnaire responses) change.
    """
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    career_name = st.session_state.get('skillgap_career_name')

    if not career_name:
        st.warning(t("messages.warning.no_career_skill_gap"))
        if st.button(t("buttons.back_to_report"), use_container_width=True):
            st.session_state.page = 'report'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.6rem;">🧭 {_t("AI Skill Gap Analysis")}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message" style="text-align:center;">{_t("for")} {_t(career_name)}</p>', unsafe_allow_html=True)

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    # CACHING POLICY: regenerated ONLY if the questionnaire responses,
    # student type, personality pathway, or the selected career changes -
    # never on every rerun/navigation, matching the same caching pattern
    # used for the 12-Month Learning Roadmap above.
    current_fp = make_response_fingerprint(
        st.session_state.responses,
        st.session_state.user_type,
        st.session_state.get('personality_pathway'),
        career_name,
    )
    if st.session_state.skill_gap_analysis is None or st.session_state.skill_gap_analysis_fingerprint != current_fp:
        with st.spinner(f"{_t('Analyzing your skill gap for')} {_t(career_name)}..."):
            result = generate_ai_skill_gap_analysis(
                student_details,
                st.session_state.responses,
                st.session_state.get('personality_pathway'),
                st.session_state.get('ai_analysis'),
                career_name,
                st.session_state.user_type,
            )
        st.session_state.skill_gap_analysis_status = result
        st.session_state.skill_gap_analysis_fingerprint = current_fp

    analysis = st.session_state.skill_gap_analysis

    st.markdown("<hr>", unsafe_allow_html=True)

    if not analysis:
        msg = (st.session_state.skill_gap_analysis_status or {}).get(
            'message', _t('This skill gap analysis could not be generated right now.')
        )
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {msg}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('skill_gap_analysis_error'):
            with st.expander(_t("Technical details (for debugging)")):
                st.code(st.session_state.skill_gap_analysis_error)
        if st.button(t("buttons.retry"), use_container_width=True):
            st.session_state.skill_gap_analysis = None
            st.rerun()
    else:
        # ---- Overall Readiness ----
        readiness_score = analysis.get('overall_readiness_score', 0) or 0
        st.markdown(
            f'<div class="skillgap-readiness-card">'
            f'<span class="skillgap-readiness-score">{readiness_score}%</span> '
            f'<strong>{_t("Overall Career Readiness")}</strong>'
            f'<p style="margin-top:0.5rem;">{analysis.get("readiness_summary", "")}</p>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.progress(max(0, min(100, int(readiness_score))) / 100)

        # ---- Current Strengths ----
        st.markdown(f'<div class="skillgap-section-title">💪 {_t("Current Strengths")}</div>', unsafe_allow_html=True)
        for item in analysis.get('current_strengths', []):
            score = max(0, min(100, int(item.get('proficiency_score', 0) or 0)))
            display_item = item
            st.markdown(
                f'<div class="skillgap-card">'
                f'<div class="skillgap-card-title">{display_item.get("skill_name", "")} — {score}%</div>'
                f'<div class="skillgap-bar-track"><div class="skillgap-bar-fill-strength" style="width:{score}%;"></div></div>'
                f'<div class="skillgap-card-explanation">{display_item.get("explanation", "")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Missing Skills ----
        st.markdown(f'<div class="skillgap-section-title">🧩 {_t("Missing Skills")}</div>', unsafe_allow_html=True)
        importance_class_map = {"critical": "critical", "high": "high", "medium": "medium"}
        for item in analysis.get('missing_skills', []):
            importance = (item.get('importance') or 'Medium')
            badge_class = importance_class_map.get(importance.strip().lower(), 'medium')
            display_item = item
            display_importance = _t(importance)
            st.markdown(
                f'<div class="skillgap-card">'
                f'<span class="skillgap-badge skillgap-badge-{badge_class}">{display_importance}</span>'
                f'<div class="skillgap-card-title">{display_item.get("skill_name", "")}</div>'
                f'<div class="skillgap-card-explanation">{display_item.get("explanation", "")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Priority Skills ----
        st.markdown(f'<div class="skillgap-section-title">🎯 {_t("Priority Skills")}</div>', unsafe_allow_html=True)
        for item in analysis.get('priority_skills', []):
            display_item = item
            st.markdown(
                f'<div class="skillgap-card" style="display:flex; align-items:flex-start;">'
                f'<span class="skillgap-priority-rank">{item.get("priority_rank", "")}</span>'
                f'<div>'
                f'<div class="skillgap-card-title">{display_item.get("skill_name", "")}</div>'
                f'<div class="skillgap-card-explanation">{display_item.get("reason", "")}</div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Learning Difficulty ----
        st.markdown(f'<div class="skillgap-section-title">⚙️ {_t("Learning Difficulty")}</div>', unsafe_allow_html=True)
        difficulty_class_map = {
            "easy": "easy", "moderate": "moderate", "hard": "hard", "very hard": "very-hard",
        }
        for item in analysis.get('learning_difficulty', []):
            diff_score = max(0, min(100, int(item.get('difficulty_score', 0) or 0)))
            label = item.get('difficulty_label', 'Moderate')
            badge_class = difficulty_class_map.get(label.strip().lower(), 'moderate')
            display_item = item
            display_label = _t(label)
            st.markdown(
                f'<div class="skillgap-card">'
                f'<span class="skillgap-badge skillgap-badge-{badge_class}">{display_label}</span>'
                f'<div class="skillgap-card-title">{display_item.get("skill_name", "")}</div>'
                f'<div class="skillgap-bar-track"><div class="skillgap-bar-fill-difficulty" style="width:{diff_score}%;"></div></div>'
                f'<div class="skillgap-card-explanation">{display_item.get("reason", "")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Recommended Learning Order ----
        st.markdown(f'<div class="skillgap-section-title">🗺️ {_t("Recommended Learning Order")}</div>', unsafe_allow_html=True)
        order_html = ['<div class="skillgap-order-timeline">']
        for item in analysis.get('recommended_learning_order', []):
            display_item = item
            order_html.append(
                f'<div class="skillgap-order-item">'
                f'<div class="skillgap-order-dot">{item.get("order", "")}</div>'
                f'<div class="skillgap-card" style="margin-bottom:0;">'
                f'<div class="skillgap-card-title">{display_item.get("skill_name", "")}</div>'
                f'<div class="skillgap-card-explanation">{display_item.get("rationale", "")}</div>'
                f'</div>'
                f'</div>'
            )
        order_html.append('</div>')
        st.markdown("".join(order_html), unsafe_allow_html=True)

        # ---- Estimated Learning Time ----
        st.markdown(f'<div class="skillgap-section-title">⏱️ {_t("Estimated Learning Time")}</div>', unsafe_allow_html=True)
        for item in analysis.get('estimated_learning_time', []):
            display_item = item
            st.markdown(
                f'<div class="skillgap-card">'
                f'<div class="skillgap-card-title">{display_item.get("skill_name", "")}</div>'
                f'<span class="skillgap-time-chip">⏳ {display_item.get("estimated_duration", "")}</span>'
                f'<span class="skillgap-time-chip">📅 {display_item.get("weekly_commitment", "")}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("<hr>", unsafe_allow_html=True)

    if st.button(t("buttons.back_arrow"), use_container_width=True):
        st.session_state.page = st.session_state.get('skillgap_return_page') or 'report'
        st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def _build_resume_context_strings():
    """
    Gather whatever real skills/roadmap context already exists in
    st.session_state from earlier steps (Career Detail's required_skills,
    the Skill Gap Analysis, and/or the 12-Month Learning Roadmap) and
    format it into short text blocks to feed into the resume suggestions
    prompt. Returns (skills_context, roadmap_context) strings, either of
    which may be empty if that data hasn't been generated yet - the
    prompt itself handles the "not available" case.
    """
    skills_parts = []

    role_detail = st.session_state.get('ai_role_detail')
    if role_detail and role_detail.get('required_skills'):
        skills_parts.append(
            "Required Skills (from Career Detail): "
            + ", ".join(role_detail.get('required_skills', [])[:10])
        )

    skill_gap = st.session_state.get('skill_gap_analysis')
    if skill_gap:
        strengths = [s.get('skill_name', '') for s in skill_gap.get('current_strengths', [])]
        missing = [s.get('skill_name', '') for s in skill_gap.get('missing_skills', [])]
        if strengths:
            skills_parts.append("Current Strengths (from Skill Gap Analysis): " + ", ".join(strengths[:8]))
        if missing:
            skills_parts.append("Skills Being Developed (from Skill Gap Analysis): " + ", ".join(missing[:8]))

    skills_context = "\n".join(skills_parts)

    roadmap_parts = []
    roadmap = st.session_state.get('learning_roadmap')
    if roadmap and roadmap.get('months'):
        for month in roadmap.get('months', [])[:12]:
            month_skills = month.get('skills_to_learn') or []
            if month_skills:
                roadmap_parts.append(
                    f"Month {month.get('month_number', '')} ({month.get('month_title', '')}): "
                    + ", ".join(month_skills)
                )
    roadmap_context = "\n".join(roadmap_parts)

    return skills_context, roadmap_context


def show_resume_suggestions():
    """
    Student -> Recommended Career -> Skills -> Roadmap -> AI Resume
    Suggestions, displayed as cards. Entirely Gemini-generated - produces
    SUGGESTIONS only (headline options, objective options, and
    recommendation cards for skills/projects/certifications/achievements/
    portfolio/internships), never a complete or fabricated resume. Cached
    in st.session_state.resume_suggestions and only regenerated when the
    underlying inputs (career or available skills/roadmap context) change.
    """
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    career_name = st.session_state.get('resume_career_name')

    if not career_name:
        st.warning(t("messages.warning.no_career_resume"))
        if st.button(t("buttons.back_to_report"), use_container_width=True):
            st.session_state.page = 'report'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.6rem;">📄 {_t("AI Resume Suggestions")}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message" style="text-align:center;">{_t("for")} {_t(career_name)}</p>', unsafe_allow_html=True)
    st.markdown(
        '<div class="resume-note-card">✨ These are personalized <strong>suggestions</strong> to guide '
        'what you build and include on your resume - not a finished resume. Use them as a checklist '
        'while you build your real experience.</div>',
        unsafe_allow_html=True,
    )

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    skills_context, roadmap_context = _build_resume_context_strings()

    # CACHING POLICY: regenerated ONLY if the selected career or the
    # available skills/roadmap context changes - never on every
    # rerun/navigation, matching the same caching pattern used elsewhere
    # in the app.
    current_fp = make_response_fingerprint(
        st.session_state.user_type,
        career_name,
        skills_context,
        roadmap_context,
    )
    if st.session_state.resume_suggestions is None or st.session_state.resume_suggestions_fingerprint != current_fp:
        with st.spinner(f"{_t('Generating personalized resume suggestions for')} {_t(career_name)}..."):
            result = generate_ai_resume_suggestions(
                student_details,
                career_name,
                st.session_state.user_type,
                skills_context,
                roadmap_context,
            )
        st.session_state.resume_suggestions_status = result
        st.session_state.resume_suggestions_fingerprint = current_fp

    suggestions = st.session_state.resume_suggestions

    st.markdown("<hr>", unsafe_allow_html=True)

    if not suggestions:
        msg = (st.session_state.resume_suggestions_status or {}).get(
            'message', _t('These resume suggestions could not be generated right now.')
        )
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {msg}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('resume_suggestions_error'):
            with st.expander(_t("Technical details (for debugging)")):
                st.code(st.session_state.resume_suggestions_error)
        if st.button(t("buttons.retry"), use_container_width=True):
            st.session_state.resume_suggestions = None
            st.rerun()
    else:
        # ---- Resume Headline options ----
        st.markdown(f'<div class="resume-section-title">🏷️ {_t("Resume Headline")}</div>', unsafe_allow_html=True)
        for idx, headline in enumerate(suggestions.get('resume_headline', []), start=1):
            st.markdown(
                f'<div class="resume-card">'
                f'<span class="resume-option-badge">{_t("Option")} {idx}</span>'
                f'<div class="resume-card-body">{headline}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Career Objective options ----
        st.markdown(f'<div class="resume-section-title">🎯 {_t("Career Objective")}</div>', unsafe_allow_html=True)
        for idx, objective in enumerate(suggestions.get('career_objective', []), start=1):
            st.markdown(
                f'<div class="resume-card">'
                f'<span class="resume-option-badge">{_t("Option")} {idx}</span>'
                f'<div class="resume-card-body">{objective}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Key Skills ----
        st.markdown(f'<div class="resume-section-title">🛠️ {_t("Key Skills to Feature")}</div>', unsafe_allow_html=True)
        chips_html = "".join(
            f'<span class="resume-skill-chip">{item.get("skill_name", "")}</span>'
            for item in suggestions.get('key_skills', [])
        )
        st.markdown(f'<div class="resume-card">{chips_html}</div>', unsafe_allow_html=True)
        for item in suggestions.get('key_skills', []):
            st.markdown(
                f'<div class="resume-card">'
                f'<div class="resume-card-title">{item.get("skill_name", "")}</div>'
                f'<div class="resume-card-reason">{item.get("reason", "")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Projects to Include ----
        st.markdown(f'<div class="resume-section-title">🧪 {_t("Projects to Include")}</div>', unsafe_allow_html=True)
        for item in suggestions.get('projects_to_include', []):
            st.markdown(
                f'<div class="resume-card">'
                f'<div class="resume-card-title">{item.get("project_title", "")}</div>'
                f'<div class="resume-card-body">{item.get("description", "")}</div>'
                f'<div class="resume-card-reason">{item.get("relevance", "")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Certifications ----
        st.markdown(f'<div class="resume-section-title">📜 {_t("Certifications")}</div>', unsafe_allow_html=True)
        cert_chips_html = "".join(
            f'<span class="resume-cert-chip">📜 {item.get("certification_name", "")}</span>'
            for item in suggestions.get('certifications', [])
        )
        st.markdown(f'<div class="resume-card">{cert_chips_html}</div>', unsafe_allow_html=True)
        for item in suggestions.get('certifications', []):
            st.markdown(
                f'<div class="resume-card">'
                f'<div class="resume-card-title">{item.get("certification_name", "")}</div>'
                f'<div class="resume-card-reason">{item.get("reason", "")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Achievements ----
        st.markdown(f'<div class="resume-section-title">🏆 {_t("Achievements to Pursue/Highlight")}</div>', unsafe_allow_html=True)
        for item in suggestions.get('achievements', []):
            st.markdown(
                f'<div class="resume-card">'
                f'<div class="resume-card-title">{item.get("suggestion", "")}</div>'
                f'<div class="resume-card-reason">{item.get("reason", "")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Portfolio Suggestions ----
        st.markdown(f'<div class="resume-section-title">💼 {_t("Portfolio Suggestions")}</div>', unsafe_allow_html=True)
        for item in suggestions.get('portfolio_suggestions', []):
            st.markdown(
                f'<div class="resume-card">'
                f'<div class="resume-card-title">{item.get("suggestion", "")}</div>'
                f'<span class="resume-option-badge">{item.get("platform_or_format", "")}</span>'
                f'<div class="resume-card-reason">{item.get("reason", "")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # ---- Internship Suggestions ----
        st.markdown(f'<div class="resume-section-title">🤝 {_t("Internship Suggestions")}</div>', unsafe_allow_html=True)
        for item in suggestions.get('internship_suggestions', []):
            st.markdown(
                f'<div class="resume-card">'
                f'<div class="resume-card-title">{item.get("internship_type", "")}</div>'
                f'<div class="resume-card-reason">{item.get("reason", "")}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    st.markdown("<hr>", unsafe_allow_html=True)

    if st.button(t("buttons.back_arrow"), use_container_width=True):
        st.session_state.page = st.session_state.get('resume_suggestions_return_page') or 'report'
        st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def _send_chatbot_message(user_text):
    """
    Shared send-and-respond flow used by both the free-text chat input and
    the suggested-question buttons: appends the student's message, shows
    the user bubble, shows an animated typing indicator while Gemini
    generates a fresh reply grounded in this student's context and the
    ongoing conversation, then replaces the indicator with the real reply
    and appends it to st.session_state.chatbot_messages.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return

    st.session_state.chatbot_messages.append({"role": "user", "content": user_text})
    with st.chat_message("user"):
        st.markdown(user_text)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown(
            '<div class="typing-indicator"><span></span><span></span><span></span></div>',
            unsafe_allow_html=True,
        )
        context_text = _build_chatbot_context()
        result = generate_chatbot_reply(user_text, context_text, st.session_state.chatbot_messages[:-1])
        if result["status"] == "success":
            placeholder.markdown(result["answer"])
            st.session_state.chatbot_messages.append({"role": "assistant", "content": result["answer"]})
            st.session_state.chatbot_error = None
        else:
            placeholder.markdown(f"⚠️ {result['message']}")
            st.session_state.chatbot_error = result["message"]


def show_career_chatbot():
    """
    AI Career Chatbot. Answers student questions about career
    recommendations, career analysis, the learning roadmap, skills,
    education, higher studies, job roles, certifications, resume, and
    interview preparation - powered entirely by Gemini AI, with NO
    predefined/scripted responses anywhere. Conversation history is
    maintained purely via st.session_state.chatbot_messages (reset with
    "Clear Chat"). Grounded in whatever real personalized context already
    exists for this student (career, skills, roadmap, etc.), gathered
    fresh on every message via _build_chatbot_context().
    """
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)

    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.6rem;">🤖 {_t("AI Career Chatbot")}</h1>', unsafe_allow_html=True)
    st.markdown(
        '<p class="sub-message" style="text-align:center;">Ask me anything about your career journey</p>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="chatbot-intro-card">💬 I can help with Career Recommendations, Career Analysis, your '
        'Learning Roadmap, Skills, Education, Higher Studies, Job Roles, Certifications, Resume, and '
        'Interview Preparation - grounded in your own answers and results so far.</div>',
        unsafe_allow_html=True,
    )

    header_col1, header_col2 = st.columns([5, 1.3])
    with header_col2:
        with st.container(key="chatbot_clear_row"):
            if st.button(t("buttons.clear_chat"), use_container_width=True):
                st.session_state.chatbot_messages = []
                st.session_state.chatbot_error = None
                st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)

    # ---- Suggested Questions ----
    # Regenerated only when the student's underlying context changes (a
    # new career selected, or new skills/roadmap/resume content
    # generated) - not on every rerun.
    context_text = _build_chatbot_context()
    context_fp = make_response_fingerprint(context_text)
    if (
        st.session_state.chatbot_suggested_questions is None
        or st.session_state.chatbot_suggested_questions_fingerprint != context_fp
    ):
        st.session_state.chatbot_suggested_questions = generate_chatbot_suggested_questions(context_text)
        st.session_state.chatbot_suggested_questions_fingerprint = context_fp

    suggested = st.session_state.chatbot_suggested_questions or []
    if suggested:
        st.markdown(f'<div class="chatbot-suggested-label">💡 {_t("Suggested Questions")}</div>', unsafe_allow_html=True)
        with st.container(key="chatbot_suggested_row"):
            cols = st.columns(2)
            clicked_question = None
            for i, question in enumerate(suggested):
                with cols[i % 2]:
                    # MODIFICATION (i18n): `question` is Gemini-generated
                    # content, already produced directly in the selected
                    # language - not passed through _t().
                    if st.button(question, key=f"chatbot_suggested_{i}", use_container_width=True):
                        clicked_question = question
            if clicked_question:
                st.session_state.chatbot_pending_message = clicked_question
                st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)

    # ---- Conversation history ----
    for message in st.session_state.chatbot_messages:
        with st.chat_message(message["role"]):
            if message["role"] == "assistant":
                st.markdown(message["content"])
            else:
                st.markdown(message["content"])

    if st.session_state.chatbot_error:
        st.caption(f"⚠️ {_t('Last message failed:')} {st.session_state.chatbot_error}")

    # A suggested-question click sets chatbot_pending_message and reruns;
    # handle it here so it goes through the exact same send flow as
    # typed messages.
    pending_message = st.session_state.chatbot_pending_message
    if pending_message:
        st.session_state.chatbot_pending_message = None
        _send_chatbot_message(pending_message)

    typed_message = st.chat_input(_t("Type your question about careers, skills, roadmap, interviews..."))
    if typed_message:
        _send_chatbot_message(typed_message)

    st.markdown("<hr>", unsafe_allow_html=True)

    if st.button(t("buttons.back_arrow"), use_container_width=True):
        st.session_state.page = st.session_state.get('chatbot_return_page') or 'report'
        st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)


def show_report():
    show_header()
    st.markdown('<div class="main-card">', unsafe_allow_html=True)
    
    st.markdown(f'<h1 class="welcome-heading" style="font-size:1.5rem;">{_t("Your Personalized Career Report")}</h1>', unsafe_allow_html=True)
    st.markdown(f'<p class="sub-message">{_t("Prepared for")} {st.session_state.student_name}</p>', unsafe_allow_html=True)

    stream = st.session_state.get('selected_stream_data')

    if not stream:
        st.warning(t("messages.warning.no_stream_selected"))
        if st.button(t("buttons.back_to_recommendations"), use_container_width=True):
            st.session_state.page = 'recommendation'
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        return

    st.markdown(f"""
    <div style="background:#FFF8F0; border-radius:16px; padding:1.2rem;">
        <strong>{_t('Selected Stream')}:</strong> {_t(stream['stream_name'])}<br>
        <strong>{_t('Match Score')}:</strong> {stream['match_percentage']}%<br><br>
        <p>{stream['explanation']}</p>
    </div>
    """, unsafe_allow_html=True)

    # Student -> Select Recommended Career -> 12-Month Learning Roadmap.
    # This is the entry point into the AI-generated learning roadmap for
    # whichever career/stream the student has selected here.
    if st.button(t("buttons.view_roadmap"), type="primary", use_container_width=True):
        st.session_state.roadmap_career_name = stream['stream_name']
        st.session_state.learning_roadmap = None
        st.session_state.roadmap_return_page = 'report'
        st.session_state.page = 'learning_roadmap'
        st.rerun()

    # Student -> Select Recommended Career -> AI Skill Gap Analysis. Entry
    # point for comparing the student's current abilities against the
    # industry skills Gemini determines this career requires.
    if st.button(t("buttons.view_skill_gap"), use_container_width=True):
        st.session_state.skillgap_career_name = stream['stream_name']
        st.session_state.skill_gap_analysis = None
        st.session_state.skillgap_return_page = 'report'
        st.session_state.page = 'skill_gap'
        st.rerun()

    # Student -> Recommended Career -> Skills -> Roadmap -> AI Resume
    # Suggestions. Pulls in whatever skills/roadmap context has already
    # been generated for this career, if any.
    if st.button(t("buttons.get_resume_suggestions"), use_container_width=True):
        st.session_state.resume_career_name = stream['stream_name']
        st.session_state.resume_suggestions = None
        st.session_state.resume_suggestions_return_page = 'report'
        st.session_state.page = 'resume_suggestions'
        st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    student_details = {
        'name': st.session_state.student_name,
        'age': st.session_state.student_age,
        'institution': st.session_state.student_institution,
        'city': st.session_state.student_city,
        'state': st.session_state.student_state,
        'grade': st.session_state.student_grade,
    }

    # CACHING POLICY: this single Gemini call produces Skills (Technical +
    # Soft), Career Opportunities, Education Path, and Learning Resources
    # (recommended certifications, free resources, platforms, books,
    # communities) together, all stored in st.session_state.ai_deep_dive -
    # one cached object instead of separate calls per section. Regenerated
    # ONLY when the questionnaire responses change or a different career/
    # stream is selected; otherwise the cached report is reused on every
    # rerun/navigation, avoiding a repeated Gemini call.
    current_fp = make_response_fingerprint(
        st.session_state.responses,
        st.session_state.user_type,
        stream['stream_name'],
    )
    if st.session_state.ai_deep_dive is None or st.session_state.ai_deep_dive_fingerprint != current_fp:
        with st.spinner(_t("Generating your full AI career report...")):
            result = generate_ai_deep_dive(
                student_details,
                st.session_state.responses,
                stream['stream_name'],
                st.session_state.user_type,
            )
        st.session_state.ai_deep_dive_status = result
        st.session_state.ai_deep_dive_fingerprint = current_fp

    report = st.session_state.ai_deep_dive

    st.markdown("<hr>", unsafe_allow_html=True)

    if not report:
        msg = (st.session_state.ai_deep_dive_status or {}).get(
            'message', _t('The full AI report could not be generated right now.')
        )
        st.markdown(f"""
        <div style="background:#FFF3E0; border-radius:16px; padding:1.2rem; margin:1rem 0;">
            <p>⚠️ {msg}</p>
        </div>
        """, unsafe_allow_html=True)
        if st.session_state.get('ai_deep_dive_error'):
            with st.expander(_t("Technical details (for debugging)")):
                st.code(st.session_state.ai_deep_dive_error)
        if st.button(t("buttons.retry_report_generation"), use_container_width=True):
            st.session_state.ai_deep_dive = None
            st.rerun()
    else:
        def section(title, icon, body):
            st.markdown(f'<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown(f"**{icon} {_t(title)}**")
            display_body = body
            if isinstance(display_body, list):
                for item in display_body:
                    st.markdown(f"- {item}")
            else:
                st.markdown(display_body)
            st.markdown('</div>', unsafe_allow_html=True)

        section("Career Overview", "📘", report.get("career_overview", ""))
        section("Future Scope", "📈", report.get("future_scope", ""))

        # ---- Required Skills: Technical Skills + Soft Skills only, shown as
        # modern skill chips. "Software Skills" is never generated or
        # displayed under any name (enforced in the prompt + validator). ----
        st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
        st.markdown(f"**{_t('🛠️ Required Skills')}**")
        st.markdown(f"*{_t('Technical Skills')}*")
        tech_chips = "".join(
            f'<span class="skill-chip-tech">{skill}</span>' for skill in report.get("technical_skills", [])
        )
        st.markdown(f'<div>{tech_chips}</div>', unsafe_allow_html=True)
        st.markdown(f"<br>*{_t('Soft Skills')}*", unsafe_allow_html=True)
        soft_chips = "".join(
            f'<span class="skill-chip-soft">{skill}</span>' for skill in report.get("soft_skills", [])
        )
        st.markdown(f'<div>{soft_chips}</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

        # ---- Career Opportunities: hiring cities, industry hubs, and hiring
        # industries, entirely AI-generated, shown as modern cards. ----
        st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
        st.markdown(f"**{_t('🌍 Career Opportunities')}**")
        st.markdown('</div>', unsafe_allow_html=True)

        def opportunity_grid(subtitle, icon, items):
            st.markdown(f"**{icon} {_t(subtitle)}**")
            items = items or []
            if not items:
                return
            display_items = items
            cards_html = "".join(
                f'<div class="opportunity-card"><div class="opp-icon">{icon}</div>'
                f'<div class="opp-label">{item}</div></div>'
                for item in display_items
            )
            st.markdown(f'<div class="card-grid card-grid-4">{cards_html}</div>', unsafe_allow_html=True)

        opportunity_grid("Top Hiring Cities in India", "🏙️", report.get("major_hiring_cities_india", []))
        st.markdown("<br>", unsafe_allow_html=True)
        opportunity_grid("Major Industry Hubs", "🏭", report.get("major_industry_hubs", []))
        st.markdown("<br>", unsafe_allow_html=True)
        opportunity_grid("Top Hiring Industries", "💼", report.get("top_hiring_industries", []))
        st.markdown("<br>", unsafe_allow_html=True)

        # ---- Education Path: structured, AI-generated, shaped differently
        # for school vs college students. ----
        st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
        st.markdown(f"**{_t('🎓 Education Path')}**")
        st.markdown('</div>', unsafe_allow_html=True)

        education_path = report.get("education_path", {}) or {}

        def edu_field(label, icon, value):
            st.markdown(f'<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown(f"**{icon} {_t(label)}**")
            display_value = value
            if isinstance(display_value, list):
                for item in display_value:
                    st.markdown(f"- {item}")
            else:
                st.markdown(display_value or "")
            st.markdown('</div>', unsafe_allow_html=True)

        if st.session_state.user_type == 'school':
            edu_field("Recommended Stream", "🧭", education_path.get("recommended_stream", ""))
            edu_field("Undergraduate Degree", "🎓", education_path.get("undergraduate_degree", ""))
            edu_field("Higher Studies", "📘", education_path.get("higher_studies", ""))
            edu_field("Certifications", "📜", education_path.get("certifications", []))
        else:
            edu_field("Higher Education Options", "🎓", education_path.get("higher_education_options", ""))
            edu_field("Professional Certifications", "📜", education_path.get("professional_certifications", []))
            edu_field("Specializations", "🧭", education_path.get("specializations", []))
            edu_field("Career Advancement", "📈", education_path.get("career_advancement", ""))

        # ---- Learning Resources: personalized, AI-generated for the
        # selected career - no hardcoded certification/resource lists. ----
        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown('<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
        st.markdown(f"**{_t('📚 Learning Resources')}**")
        st.markdown('</div>', unsafe_allow_html=True)

        learning_resources = report.get("learning_resources", {}) or {}

        def resource_grid(subtitle, icon, items):
            st.markdown(f"**{icon} {_t(subtitle)}**")
            items = items or []
            if not items:
                return
            display_items = items
            cards_html = "".join(
                f'<div class="opportunity-card"><div class="opp-icon">{icon}</div>'
                f'<div class="opp-label">{item}</div></div>'
                for item in display_items
            )
            st.markdown(f'<div class="card-grid card-grid-3">{cards_html}</div>', unsafe_allow_html=True)

        resource_grid("Recommended Certifications", "📜", learning_resources.get("recommended_certifications", []))
        st.markdown("<br>", unsafe_allow_html=True)
        resource_grid("Free Learning Resources", "🆓", learning_resources.get("free_learning_resources", []))
        st.markdown("<br>", unsafe_allow_html=True)
        resource_grid("Online Platforms", "💻", learning_resources.get("online_platforms", []))
        st.markdown("<br>", unsafe_allow_html=True)
        resource_grid("Books", "📖", learning_resources.get("books", []))
        st.markdown("<br>", unsafe_allow_html=True)
        resource_grid("Communities", "🤝", learning_resources.get("communities", []))
        st.markdown("<br>", unsafe_allow_html=True)

        roles = report.get("related_career_roles", [])
        if roles:
            st.markdown(f'<div class="question-card" style="text-align:left;">', unsafe_allow_html=True)
            st.markdown(f"**🧭 {_t('Related Career Roles')}**")
            st.markdown(_t("Click a role to see its full AI-generated breakdown."))
            st.markdown('</div>', unsafe_allow_html=True)
            for row_start in range(0, len(roles), 3):
                row_roles = roles[row_start:row_start + 3]
                row_cols = st.columns(3)
                for j, role in enumerate(row_roles):
                    r_idx = row_start + j
                    with row_cols[j]:
                        if st.button(_t(role), key=f"role_{r_idx}", use_container_width=True):
                            st.session_state.selected_career_role = role
                            st.session_state.ai_role_detail = None
                            st.session_state.role_detail_return_page = 'report'
                            st.session_state.page = 'role_detail'
                            st.rerun()

    st.markdown("<hr>", unsafe_allow_html=True)
    
    # ONLY PDF Download button (removed TXT and Print buttons)
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        if st.button(t("buttons.download_report_pdf"), type="primary", use_container_width=True):
            if st.session_state.selected_stream:
                with st.spinner(_t("Generating PDF report...")):
                    try:
                        pdf_path = generate_pdf_report()
                        with open(pdf_path, "rb") as pdf_file:
                            pdf_data = pdf_file.read()
                        st.download_button(
                            label=_t("💾 Save PDF Report"),
                            data=pdf_data,
                            file_name=f"{st.session_state.student_name}_career_report.pdf",
                            mime="application/pdf",
                            key="download_pdf"
                        )
                        os.unlink(pdf_path)
                        st.success(t("messages.success.pdf_report_generated"))
                    except Exception as e:
                        st.error(f"{t('messages.error.pdf_generation')} {str(e)}")
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button(t("buttons.back_to_ai_analysis"), use_container_width=True):
            st.session_state.page = 'ai_analysis'
            st.rerun()
    with col2:
        if st.button(t("buttons.start_new_assessment"), use_container_width=True):
            # Reset all session state
            for key in list(st.session_state.keys()):
                if key not in ['page']:
                    del st.session_state[key]
            st.session_state.page = 'welcome'
            st.rerun()
    
    st.markdown('</div>', unsafe_allow_html=True)

# Main
def main():
    if st.session_state.page == 'welcome':
        show_welcome()
    elif st.session_state.page == 'load_assessment':
        show_load_assessment()
    elif st.session_state.page == 'assessment':
        show_assessment()
    elif st.session_state.page == 'riasec_choice':
        show_riasec_choice()
    elif st.session_state.page == 'riasec_assessment':
        show_riasec_assessment()
    elif st.session_state.page == 'riasec_result':
        show_riasec_result()
    elif st.session_state.page == 'recommendation':
        show_recommendation()
    elif st.session_state.page == 'ai_analysis':
        show_ai_analysis()
    elif st.session_state.page == 'report':
        show_report()
    elif st.session_state.page == 'role_detail':
        show_role_detail()
    elif st.session_state.page == 'learning_roadmap':
        show_learning_roadmap()
    elif st.session_state.page == 'skill_gap':
        show_skill_gap_analysis()
    elif st.session_state.page == 'resume_suggestions':
        show_resume_suggestions()
    elif st.session_state.page == 'career_chatbot':
        show_career_chatbot()
    elif st.session_state.page == 'help':
        show_help()
    elif st.session_state.page == 'vocational_careers':
        show_vocational_careers()

if __name__ == "__main__":
    main()
