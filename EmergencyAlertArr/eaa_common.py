import json
import logging
import os
import re
import shutil
import subprocess
import textwrap
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

__all__ = ['CHANNEL_CACHE_FILE', 'FONTS_DIR', 'MAPPINGS_FILE', 'PROFILE_PREFIX', 'RUNTIME_DIR', 'WAV_DIR', '_DATA_DIR', '_EAS_SEV', '_EAS_WAV_ATT', '_EAS_WAV_EOM', '_EAS_WAV_HEADER', '_FONT_DASDEC', '_FONT_EASYPLUS', '_FONT_SYS_FALLBACK', '_PLUGINS_DIR', '_PLUGIN_DIR', '_PLUGIN_KEY', '_atomic_write', '_eas_have_numpy', '_ensure_dirs', '_find_font', '_font_for', '_resolve_font', '_truncate', '_wav_duration_secs']

_PLUGIN_DIR        = os.path.dirname(os.path.abspath(__file__))

_PLUGIN_KEY        = os.path.basename(_PLUGIN_DIR)

_PLUGINS_DIR       = os.path.dirname(_PLUGIN_DIR)

_DATA_DIR          = os.path.join(_PLUGINS_DIR, "emergencyalertarr_data")

RUNTIME_DIR        = os.path.join(_DATA_DIR, "runtime")

MAPPINGS_FILE      = os.path.join(_DATA_DIR, "mappings.json")

CHANNEL_CACHE_FILE = os.path.join(_DATA_DIR, "channel_cache.json")

FONTS_DIR = os.path.join(_PLUGIN_DIR, "fonts")

WAV_DIR   = os.path.join(_PLUGIN_DIR, "wav")

_EAS_WAV_HEADER = os.path.join(WAV_DIR, "eas_header.wav")

_EAS_WAV_ATT    = os.path.join(WAV_DIR, "eas_att.wav")

_EAS_WAV_EOM    = os.path.join(WAV_DIR, "eas_eom.wav")

PROFILE_PREFIX = "EmergencyAlertarr — "

_FONT_EASYPLUS = os.path.join(FONTS_DIR, "EASyText.ttf")

_FONT_DASDEC   = os.path.join(FONTS_DIR, "luximb.ttf")

def _find_font(*name_fragments):
    """Search common font directories for a TTF/OTF whose filename contains all
    given fragments (case-insensitive). Returns the first match, or None. Used
    only as a fallback when a bundled font is missing."""
    for base in ("/usr/share/fonts", "/usr/local/share/fonts", "/usr/share/fonts/truetype"):
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.lower().endswith((".ttf", ".otf")):
                    continue
                if all(frag.lower() in fn.lower() for frag in name_fragments):
                    return os.path.join(root, fn)
    return None

_FONT_SYS_FALLBACK = (
    _find_font("mono", "bold")
    or _find_font("dejavusansmono")
    or _find_font("liberationmono", "bold")
    or _find_font("dejavusans", "bold")
    or _find_font("bold")
)

def _font_for(style):
    """Bundled overlay font for the given style, with a system fallback."""
    bundled = _FONT_DASDEC if style == "dasdec" else _FONT_EASYPLUS
    if os.path.isfile(bundled):
        return bundled
    if _FONT_SYS_FALLBACK:
        logger.warning(
            f"[EmergencyAlertarr] bundled font missing ({os.path.basename(bundled)}); "
            f"using system fallback {_FONT_SYS_FALLBACK}."
        )
    return _FONT_SYS_FALLBACK

def _resolve_font(custom, style):
    """Font for an overlay: the hardcoded bundled font for the style by default,
    or an optional user override (absolute path, a filename in fonts/, or a
    system-font name). A bad override logs and falls back to the bundled font."""
    custom = (custom or "").strip().strip('"').strip("'")
    if not custom:
        return _font_for(style)
    if os.path.isfile(custom):
        return custom
    base = os.path.basename(custom)
    for d in (FONTS_DIR, _DATA_DIR, RUNTIME_DIR):
        cand = os.path.join(d, base)
        if os.path.isfile(cand):
            return cand
        for ext in (".ttf", ".otf", ".TTF", ".OTF"):
            if os.path.isfile(cand + ext):
                return cand + ext
    hit = _find_font(os.path.splitext(base)[0])
    if hit:
        return hit
    logger.warning(f"[EmergencyAlertarr] overlay font '{custom}' not found; using bundled default.")
    return _font_for(style)

def _eas_have_numpy():
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False

def _ensure_dirs():
    os.makedirs(RUNTIME_DIR, exist_ok=True)

def _atomic_write(filename, content):
    path = os.path.join(RUNTIME_DIR, filename)
    tmp = path + f".tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"emergencyalertarr: write failed for {filename}: {e}")

def _truncate(text, max_len):
    if not text or len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."

_EAS_SEV        = {"Unknown": 0, "Minor": 1, "Moderate": 2, "Severe": 3, "Extreme": 4}

def _wav_duration_secs(path):
    """Read duration from a WAV file header using stdlib only. Returns 0.0 on error."""
    try:
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0
