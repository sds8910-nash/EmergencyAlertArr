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

from eaa_common import *
logger = logging.getLogger(__name__)

__all__ = ['_ATTN_F1', '_ATTN_F2', '_SAME_BAUD', '_SAME_EVENT', '_SAME_MARK', '_SAME_ORG', '_SAME_SPACE', '_SAME_SR', '_eas_generate_tone_wavs', '_eas_same_header_string', '_eas_wav_available', '_gen_afsk', '_gen_attn', '_gen_silence', '_same_bits', '_same_burst_segments', '_same_event_code_for', '_same_issue_code', '_same_org_for', '_same_purge_code', '_same_station_id', '_tone_write_wav']

_SAME_SR    = 48000

_SAME_MARK  = 2083.3

_SAME_SPACE = 1562.5

_SAME_BAUD  = 520.833

_ATTN_F1, _ATTN_F2 = 853.0, 960.0

_SAME_ORG = {
    "nws": "WXR", "national weather service": "WXR", "weather": "WXR",
    "eas participant": "EAS", "eas": "EAS", "broadcast": "EAS",
    "civil": "CIV", "civil authorities": "CIV", "emergency management": "CIV",
    "primary entry point": "PEP", "pep": "PEP",
}

_SAME_EVENT = {
    "tornado warning": "TOR", "tornado watch": "TOA",
    "severe thunderstorm warning": "SVR", "severe thunderstorm watch": "SVA",
    "flash flood warning": "FFW", "flash flood watch": "FFA",
    "flood warning": "FLW", "flood watch": "FLA", "flood advisory": "FLS",
    "winter storm warning": "WSW", "winter storm watch": "WSA",
    "blizzard warning": "BZW", "ice storm warning": "WSW",
    "high wind warning": "HWW", "high wind watch": "HWA",
    "hurricane warning": "HUW", "hurricane watch": "HUA",
    "tropical storm warning": "TRW", "tropical storm watch": "TRA",
    "storm surge warning": "SSW", "storm surge watch": "SSA",
    "extreme wind warning": "EWW", "special weather statement": "SPS",
    "special marine warning": "SMW", "dust storm warning": "DSW",
    "fire warning": "FRW", "civil emergency message": "CEM",
    "child abduction emergency": "CAE", "evacuation immediate": "EVI",
    "shelter in place warning": "SPW", "911 telephone outage emergency": "TOE",
    "required weekly test": "RWT", "required monthly test": "RMT",
    "administrative message": "ADR", "practice/demo warning": "DMO",
    "national periodic test": "NPT",
}

def _same_bits(message):
    """SAME 16-byte 0xAB preamble + ASCII payload -> list of bits, LSB first."""
    data = bytes([0xAB] * 16) + message.encode("ascii", "replace")
    bits = []
    for byte in data:
        for k in range(8):        # least-significant bit first, per the standard
            bits.append((byte >> k) & 1)
    return bits

def _same_event_code_for(alert):
    """Best 3-letter SAME event code: the NWS-supplied code if present, else a
    mapping from the event name, else a safe generic catch-all."""
    ec = (alert.get("event_code") or "").strip().upper()
    if len(ec) == 3:
        return ec
    name = (alert.get("event") or "").strip().lower()
    if name in _SAME_EVENT:
        return _SAME_EVENT[name]
    if "tornado" in name:                       return "TOR"
    if "thunder" in name:                       return "SVR"
    if "flash flood" in name:                   return "FFW"
    if "flood" in name:                         return "FLW"
    if "winter" in name or "snow" in name or "ice" in name or "blizzard" in name:
        return "WSW"
    if "hurricane" in name:                     return "HUW"
    if "tropical" in name:                      return "TRW"
    if "wind" in name:                          return "HWW"
    if "fire" in name:                          return "FRW"
    if "test" in name:                          return "RWT"
    return "CEM"

def _same_org_for(alert):
    """3-letter SAME originator code."""
    org = (alert.get("originator") or "").strip().upper()
    if len(org) == 3:
        return org
    sender = (alert.get("sender") or "").strip().lower()
    for key, code in _SAME_ORG.items():
        if key in sender:
            return code
    return "WXR"

def _same_purge_code(effective_iso, expires_iso):
    """+TTTT valid-time field, rounded to SAME-legal increments (15-min up to
    1h, 30-min up to 6h, hourly beyond), clamped to a sane range."""
    from datetime import datetime
    def _p(s):
        try:
            return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        except Exception:
            return None
    a, b = _p(effective_iso), _p(expires_iso)
    mins = 60
    if a and b:
        mins = int(round((b - a).total_seconds() / 60.0))
    if mins <= 0:
        mins = 60
    if mins <= 60:
        mins = max(15, int(round(mins / 15.0) * 15))
    elif mins <= 360:
        mins = int(round(mins / 30.0) * 30)
    else:
        mins = int(round(mins / 60.0) * 60)
    mins = min(mins, 99 * 60 + 45)     # SAME field maxes out at +9945
    return f"+{mins // 60:02d}{mins % 60:02d}"

def _same_issue_code(effective_iso):
    """JJJHHMM issue-time field: zero-padded UTC day-of-year + hour + minute."""
    from datetime import datetime, timezone
    dt = None
    try:
        dt = datetime.fromisoformat((effective_iso or "").replace("Z", "+00:00"))
    except Exception:
        dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return f"{dt.timetuple().tm_yday:03d}{dt.hour:02d}{dt.minute:02d}"

def _same_station_id(alert):
    """8-char LLLLLLLL originating-station field (must be exactly 8 chars)."""
    raw = (alert.get("sender") or "").strip().upper()
    sid = "".join(ch for ch in raw if ch.isalnum() or ch in "/-")[:8]
    if not sid:
        sid = "EMERGENCYALERTARR"
    return (sid + "--------")[:8]

def _eas_same_header_string(alert):
    """Assemble a valid SAME/ZCZC header string from an alert dict. More county
    codes -> a longer string -> a longer header tone, just like a real ENDEC."""
    org = _same_org_for(alert)
    eee = _same_event_code_for(alert)
    codes = []
    for c in (alert.get("same_codes") or []):
        c = "".join(ch for ch in str(c) if ch.isdigit())
        if not c:
            continue
        c = c.zfill(6)[:6]            # PSSCCC, 6 digits
        if c not in codes:
            codes.append(c)
    if not codes:
        codes = ["000000"]            # entire US, when no county codes are available
    codes = codes[:31]                # SAME permits at most 31 location codes
    loc = "-".join(codes)
    ttt = _same_purge_code(alert.get("effective"), alert.get("expires"))   # "+TTTT"
    jjj = _same_issue_code(alert.get("effective"))
    llll = _same_station_id(alert)
    return f"ZCZC-{org}-{eee}-{loc}{ttt}-{jjj}-{llll}-"

def _gen_afsk(bits):
    """Continuous-phase AFSK for the given bit list -> float samples in [-1,1].
    Sample positions are derived continuously (not per-bit rounding) so the
    520.833-baud timing stays accurate across the whole burst."""
    if _eas_have_numpy():
        import numpy as np
        n = len(bits)
        total = int(round(n / _SAME_BAUD * _SAME_SR))
        idx = np.arange(total)
        bi = np.clip(np.floor(idx / _SAME_SR * _SAME_BAUD).astype(int), 0, n - 1)
        barr = np.asarray(bits, dtype=np.float64)
        freqs = np.where(barr[bi] > 0.5, _SAME_MARK, _SAME_SPACE)
        phase = np.cumsum(2 * np.pi * freqs / _SAME_SR)
        return 0.7 * np.sin(phase)
    # pure-stdlib fallback
    import math
    n = len(bits)
    total = int(round(n / _SAME_BAUD * _SAME_SR))
    out = [0.0] * total
    phase = 0.0
    twopi = 2 * math.pi
    for i in range(total):
        bi = int(i / _SAME_SR * _SAME_BAUD)
        if bi >= n:
            bi = n - 1
        f = _SAME_MARK if bits[bi] else _SAME_SPACE
        phase += twopi * f / _SAME_SR
        out[i] = 0.7 * math.sin(phase)
    return out

def _gen_attn(sec):
    """Two-tone (853/960 Hz) attention signal, `sec` seconds long."""
    if _eas_have_numpy():
        import numpy as np
        t = np.arange(int(sec * _SAME_SR)) / _SAME_SR
        return 0.45 * (np.sin(2 * np.pi * _ATTN_F1 * t) + np.sin(2 * np.pi * _ATTN_F2 * t))
    import math
    tot = int(sec * _SAME_SR)
    twopi = 2 * math.pi
    return [0.45 * (math.sin(twopi * _ATTN_F1 * i / _SAME_SR)
                    + math.sin(twopi * _ATTN_F2 * i / _SAME_SR)) for i in range(tot)]

def _gen_silence(sec):
    n = int(sec * _SAME_SR)
    if _eas_have_numpy():
        import numpy as np
        return np.zeros(n)
    return [0.0] * n

def _same_burst_segments(message, reps=3, gap=1.0):
    """A SAME burst repeated `reps` times with `gap`s of silence between,
    returned as a list of sample segments ready for _tone_write_wav()."""
    one = _gen_afsk(_same_bits(message))
    segs = []
    for i in range(reps):
        segs.append(one)
        if i != reps - 1:
            segs.append(_gen_silence(gap))
    return segs

def _tone_write_wav(path, segments):
    """Concatenate sample segments (numpy arrays or lists), clip to [-1,1], and
    write a 16-bit mono PCM WAV at _SAME_SR."""
    import wave
    if _eas_have_numpy():
        import numpy as np
        if segments:
            sig = np.concatenate([np.asarray(s, dtype=np.float64) for s in segments])
        else:
            sig = np.zeros(0)
        sig = np.clip(sig, -1.0, 1.0)
        pcm = (sig * 32767.0).astype("<i2").tobytes()
    else:
        import struct
        buf = bytearray()
        for seg in segments:
            for v in seg:
                if v > 1.0:
                    v = 1.0
                elif v < -1.0:
                    v = -1.0
                buf += struct.pack("<h", int(v * 32767.0))
        pcm = bytes(buf)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAME_SR)
        w.writeframes(pcm)

def _eas_generate_tone_wavs(channel_id, header_str, att_secs, want_attn=True):
    """Synthesize the header / attention / EOM tone WAVs for one alert into
    RUNTIME_DIR. Returns (header_path, att_path, eom_path) on success, or None on
    failure (callers then fall back to the pre-recorded WAVs)."""
    try:
        _ensure_dirs()
        h_path = os.path.join(RUNTIME_DIR, f"eas_{channel_id}_gen_header.wav")
        a_path = os.path.join(RUNTIME_DIR, f"eas_{channel_id}_gen_att.wav")
        e_path = os.path.join(RUNTIME_DIR, f"eas_{channel_id}_gen_eom.wav")
        try:
            att_secs = max(4.0, min(30.0, float(att_secs or 8.0)))
        except Exception:
            att_secs = 8.0
        # Header: 3 SAME bursts (preamble + ZCZC string), 1s apart.
        _tone_write_wav(h_path, _same_burst_segments(header_str, reps=3, gap=1.0))
        # Attention: two-tone, user-configurable length (skipped work if unused,
        # but still written so the concat input always exists).
        _tone_write_wav(a_path, [_gen_attn(att_secs if want_attn else 0.05)])
        # EOM: 3 "NNNN" bursts, 1s apart.
        _tone_write_wav(e_path, _same_burst_segments("NNNN", reps=3, gap=1.0))
        logger.info(
            f"[EmergencyAlertarr] EAS: generated tones ch {channel_id} — "
            f"header={_wav_duration_secs(h_path):.1f}s "
            f"(\"{header_str[:48]}\"), attn={att_secs:.0f}s, "
            f"eom={_wav_duration_secs(e_path):.1f}s, "
            f"backend={'numpy' if _eas_have_numpy() else 'stdlib'}"
        )
        return (h_path, a_path, e_path)
    except Exception as e:
        logger.error(f"[EmergencyAlertarr] EAS: dynamic tone generation failed: {e}", exc_info=True)
        return None

def _eas_wav_available(verbose=False, header=None, att=None, eom=None):
    """Return True only when all three EAS tone WAVs are present and non-empty.
    When verbose=True, logs exactly which files are missing or zero-length --
    use this to diagnose 'only one tone played' issues, which are almost always
    a missing or empty header/EOM WAV (the attention tone playing alone means
    the other two files didn't load).

    header/att/eom may override the default file paths -- used when the tones are
    generated dynamically into RUNTIME_DIR rather than dropped into the plugin
    folder as eas_header.wav / eas_att.wav / eas_eom.wav."""
    files = {
        "header": header or _EAS_WAV_HEADER,
        "att":    att or _EAS_WAV_ATT,
        "eom":    eom or _EAS_WAV_EOM,
    }
    ok = True
    for name, path in files.items():
        if not os.path.isfile(path):
            ok = False
            if verbose:
                logger.warning(f"[EmergencyAlertarr] EAS WAV MISSING: {name} not found at {path}")
        elif os.path.getsize(path) < 200:
            ok = False
            if verbose:
                logger.warning(f"[EmergencyAlertarr] EAS WAV EMPTY: {name} is {os.path.getsize(path)} bytes (looks empty/corrupt) at {path}")
        elif verbose:
            logger.info(f"[EmergencyAlertarr] EAS WAV OK: {name} ({os.path.getsize(path)} bytes, {_wav_duration_secs(path):.1f}s)")
    return ok
