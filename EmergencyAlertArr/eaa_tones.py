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

__all__ = ['_ATTN_F1', '_ATTN_F2', '_SAME_BAUD', '_SAME_EVENT', '_SAME_MARK', '_SAME_ORG', '_SAME_SPACE', '_SAME_SR', '_eas_generate_tone_wavs', '_eas_same_header_string', '_eas_wav_available', '_gen_afsk', '_gen_attn', '_gen_silence', '_same_bits', '_same_burst_segments', '_same_event_code_for', '_same_issue_code', '_same_org_for', '_same_purge_code', '_same_station_id', '_tone_write_wav', 'ENDEC_PROFILES', 'endec_profile', 'endec_audio_chain']

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

def _same_event_code_for(alert):
    """Best 3-letter SAME event code: the NWS-supplied code if present, else a
    mapping from the event name, else a safe generic catch-all."""
    ec = (alert.get("event_code") or "").strip().upper()
    if len(ec) == 3:
        return ec
    name = (alert.get("event") or "").strip().lower()
    if name in _SAME_EVENT:
        return _SAME_EVENT[name]
    try:   # full 47 CFR 11.31(e) table (incl. 2023 names)
        from eaa_styles import eas_event_code
        ec = eas_event_code(alert)
        if ec:
            return ec
    except Exception:
        pass
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
    import math
    mins = 60
    if a and b:
        mins = int(math.ceil((b - a).total_seconds() / 60.0))
    if mins <= 0:
        mins = 60
    # 47 CFR 11.31(c): 15-minute increments up to one hour, then 30-minute
    # increments. Round UP so the header never expires before the alert does.
    if mins <= 60:
        mins = max(15, int(math.ceil(mins / 15.0) * 15))
    else:
        mins = int(math.ceil(mins / 30.0) * 30)
    mins = min(mins, 99 * 60 + 30)     # largest legal value that fits +TTTT
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
    """8-char LLLLLLLL originating-station field (must be exactly 8 chars).
    Uses the alert's real station ID when known ("KXXX/NWS", "IPAWSCAP", the
    configured ID for plugin-originated tests), else one derived from the
    sender."""
    raw = (alert.get("relay_station_id") or alert.get("station_id")
           or alert.get("sender") or "").strip().upper()
    sid = "".join(ch for ch in raw if ch.isalnum() or ch in "/-")[:8]
    if not sid:
        sid = "EMERGENCYALERTARR"
    return (sid + "        ")[:8]       # 8 characters, space-padded

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
    # JJJHHMM / +TTTT come from the ORIGINATION time: a relaying ENDEC keeps
    # the originator's issue time and valid period and only swaps in its own
    # station ID.
    origin = alert.get("sent") or alert.get("effective")
    ttt = alert.get("same_purge") or _same_purge_code(origin, alert.get("expires"))   # "+TTTT"
    jjj = alert.get("same_jjj") or _same_issue_code(origin)
    llll = _same_station_id(alert)
    return f"ZCZC-{org}-{eee}-{loc}{ttt}-{jjj}-{llll}-"

# ---------------------------------------------------------------------------
# ENDEC sound profiles
# ---------------------------------------------------------------------------
# Every ENDEC sends the same SAME signal on paper (47 CFR 11.31: 16 x 0xAB
# preamble, AFSK mark 2083 1/3 Hz / space 1562.5 Hz at 520 5/6 bit/s, three
# header bursts and three NNNN EOM bursts about 1 s apart, 853 + 960 Hz
# attention signal). What makes each unit *sound* different is the framing
# around the bursts, the gaps, its internal sample rate and its audio path.
# The per-unit details below follow the community ENDEC emulation work
# (EASGen / EASGen-Remastered "emulation modes", EAS-Tools' DASDEC macro,
# eas-station's Sage 3644 notes) -- recordings of the real hardware.
#
#   lead_first / lead_rest : bytes sent before the 16-byte preamble on the
#                            first / later header bursts (Sage 3644 sends a
#                            0x00 on the first burst; the Sage 1822 starts
#                            with 0xFF 0x00, then 0xFF)
#   trail                  : bytes sent after the header (Sage: 0xFF ..;
#                            NWS: two 0x00)
#   eom_lead_first / eom_lead_rest / eom_trail
#                          : the same for the three NNNN bursts (the 1822
#                            frames its EOM differently from its header)
#   gap                    : silence between bursts (Trilithic: 0.85 s)
#   sr                     : the unit's internal sample rate (TFT 911 is an
#                            8 kHz telephone-band unit, NWR 11.025 kHz, DASDEC
#                            48 kHz, most others 24 kHz)
#   attn                   : "dual" 853+960 Hz, or "wat" = NOAA Weather Radio's
#                            single 1050 Hz Warning Alarm Tone
#   ramp_ms                : raised-cosine edge on every tone (Sage 3644
#                            shapes its tone edges; the others switch hard)
#   chain                  : the unit's output audio path (ffmpeg -af), applied
#                            to its tones (the readout keeps its own sound)
ENDEC_PROFILES = {
    "dasdec": {
        "label": "DASDEC / One-Net (Digital Alert Systems)",
        "sr": 48000, "lead_first": b"", "lead_rest": b"", "trail": b"",
        "eom_lead_first": b"", "eom_lead_rest": b"", "eom_trail": b"",
        "gap": 1.0, "attn": "dual", "attn_db": -10.0, "afsk_db": -3.0, "ramp_ms": 0.0,
        # EAS-Tools' "DASDEC audio character" macro: a 4 kHz, 48 dB/oct low-pass.
        "chain": "lowpass=f=4000:p=2,lowpass=f=4000:p=2,lowpass=f=4000:p=2,lowpass=f=4000:p=2",
    },
    "sage_digital": {
        "label": "Sage Digital ENDEC 3644",
        "sr": 24000, "lead_first": b"\x00", "lead_rest": b"", "trail": b"\xff\xff\xff",
        "eom_lead_first": b"\x00", "eom_lead_rest": b"", "eom_trail": b"\xff\xff\xff",
        "gap": 1.0, "attn": "dual", "attn_db": -10.0, "afsk_db": -3.0, "ramp_ms": 5.0,
        "chain": "highpass=f=40,lowpass=f=9000",
    },
    "sage_1822": {
        "label": "Sage EAS ENDEC 1822 (original Sage)",
        "sr": 24000, "lead_first": b"\xff\x00", "lead_rest": b"\xff", "trail": b"\xff",
        "eom_lead_first": b"\x00", "eom_lead_rest": b"", "eom_trail": b"\xff\xff\xff",
        "gap": 1.0, "attn": "dual", "attn_db": -10.0, "afsk_db": -3.0, "ramp_ms": 0.0,
        # Analog-era unit: band-limited, a little compressed.
        "chain": "highpass=f=80,lowpass=f=6000,acompressor=threshold=-18dB:ratio=2:attack=5:release=60",
    },
    "sage_eas": {
        "label": "Sage EAS ENDEC (classic Sage sound)",
        "sr": 24000, "lead_first": b"", "lead_rest": b"", "trail": b"\xff",
        "eom_lead_first": b"", "eom_lead_rest": b"", "eom_trail": b"\xff",
        "gap": 1.0, "attn": "dual", "attn_db": -10.0, "afsk_db": -3.0, "ramp_ms": 0.0,
        "chain": "highpass=f=80,lowpass=f=7000",
    },
    "trilithic": {
        "label": "Trilithic EASyPLUS / EASyCAST / EASyIPTV",
        "sr": 24000, "lead_first": b"", "lead_rest": b"", "trail": b"",
        "eom_lead_first": b"", "eom_lead_rest": b"", "eom_trail": b"",
        "gap": 0.85, "attn": "dual", "attn_db": -10.0, "afsk_db": -3.0, "ramp_ms": 0.0,
        "chain": "highpass=f=50,lowpass=f=8000",
    },
    "easycap": {
        "label": "Trilithic / VIAVI EASyCAP",
        "sr": 24000, "lead_first": b"", "lead_rest": b"", "trail": b"",
        "eom_lead_first": b"", "eom_lead_rest": b"", "eom_trail": b"",
        "gap": 1.0, "attn": "dual", "attn_db": -10.0, "afsk_db": -3.0, "ramp_ms": 0.0,
        "chain": "highpass=f=40,lowpass=f=10000",
    },
    "tft": {
        "label": "TFT EAS 911 (8 kHz)",
        "sr": 8000, "lead_first": b"", "lead_rest": b"", "trail": b"",
        "eom_lead_first": b"", "eom_lead_rest": b"", "eom_trail": b"",
        "gap": 1.0, "attn": "dual", "attn_db": -10.0, "afsk_db": -3.0, "ramp_ms": 0.0,
        "chain": "aresample=8000,highpass=f=200,lowpass=f=3600",
    },
    "gorman": {
        "label": "Gorman-Redlich EAS-1",
        "sr": 24000, "lead_first": b"", "lead_rest": b"", "trail": b"",
        "eom_lead_first": b"", "eom_lead_rest": b"", "eom_trail": b"",
        "gap": 1.0, "attn": "dual", "attn_db": -10.0, "afsk_db": -3.0, "ramp_ms": 0.0,
        "chain": "highpass=f=100,lowpass=f=7000",
    },
    "nws": {
        "label": "NOAA Weather Radio (NWS, 1050 Hz alarm)",
        "sr": 11025, "lead_first": b"", "lead_rest": b"", "trail": b"\x00\x00",
        "eom_lead_first": b"", "eom_lead_rest": b"", "eom_trail": b"\x00\x00",
        "gap": 1.0, "attn": "wat", "attn_db": -4.0, "afsk_db": -3.0, "ramp_ms": 0.0,
        # Heard through a weather radio receiver: narrowband FM audio.
        "chain": "aresample=11025,highpass=f=300,lowpass=f=3200,"
                 "acompressor=threshold=-20dB:ratio=4:attack=3:release=80:makeup=2",
    },
}

def endec_profile(name):
    return ENDEC_PROFILES.get((name or "").lower(), ENDEC_PROFILES["dasdec"])

def endec_audio_chain(name):
    """ffmpeg filter chain for the unit's output audio (tones + voice)."""
    return endec_profile(name)["chain"]

def _db(v):
    return 10 ** (v / 20.0)

def _same_bits_raw(data):
    bits = []
    for byte in data:
        for k in range(8):        # least-significant bit first, per the standard
            bits.append((byte >> k) & 1)
    return bits

def _same_bits(message, lead=b"", trail=b""):
    """[lead] + 16-byte 0xAB preamble + ASCII payload + [trail] -> bits, LSB first."""
    data = bytes(lead) + bytes([0xAB] * 16) + message.encode("ascii", "replace") + bytes(trail)
    return _same_bits_raw(data)

def _ramp(sig, sr, ms):
    """Raised-cosine fade in/out (in place) on a numpy segment."""
    if not ms or not _eas_have_numpy():
        return sig
    import numpy as np
    n = int(sr * ms / 1000.0)
    if n < 2 or len(sig) < 2 * n:
        return sig
    w = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, n))
    sig[:n] *= w
    sig[-n:] *= w[::-1]
    return sig

def _gen_afsk(bits, sr=_SAME_SR, amp=0.7, ramp_ms=0.0):
    """Continuous-phase AFSK for the given bit list -> float samples in [-1,1].
    Sample positions are derived continuously (not per-bit rounding) so the
    520 5/6-baud timing stays accurate across the whole burst."""
    if _eas_have_numpy():
        import numpy as np
        n = len(bits)
        total = int(round(n / _SAME_BAUD * sr))
        idx = np.arange(total)
        bi = np.clip(np.floor(idx / sr * _SAME_BAUD).astype(int), 0, n - 1)
        barr = np.asarray(bits, dtype=np.float64)
        freqs = np.where(barr[bi] > 0.5, _SAME_MARK, _SAME_SPACE)
        phase = np.cumsum(2 * np.pi * freqs / sr)
        return _ramp(amp * np.sin(phase), sr, ramp_ms)
    import math
    n = len(bits)
    total = int(round(n / _SAME_BAUD * sr))
    out = [0.0] * total
    phase = 0.0
    twopi = 2 * math.pi
    for i in range(total):
        bi = int(i / sr * _SAME_BAUD)
        if bi >= n:
            bi = n - 1
        f = _SAME_MARK if bits[bi] else _SAME_SPACE
        phase += twopi * f / sr
        out[i] = amp * math.sin(phase)
    return out

def _gen_attn(sec, sr=_SAME_SR, kind="dual", level=0.316, ramp_ms=0.0):
    """Attention signal: EAS two-tone (853 + 960 Hz, each at `level`) or the
    NOAA Weather Radio 1050 Hz Warning Alarm Tone."""
    if _eas_have_numpy():
        import numpy as np
        t = np.arange(int(sec * sr)) / sr
        if kind == "wat":
            sig = level * np.sin(2 * np.pi * 1050.0 * t)
        else:
            sig = level * (np.sin(2 * np.pi * _ATTN_F1 * t) + np.sin(2 * np.pi * _ATTN_F2 * t))
        return _ramp(sig, sr, ramp_ms or 3.0)
    import math
    tot = int(sec * sr)
    twopi = 2 * math.pi
    if kind == "wat":
        return [level * math.sin(twopi * 1050.0 * i / sr) for i in range(tot)]
    return [level * (math.sin(twopi * _ATTN_F1 * i / sr) + math.sin(twopi * _ATTN_F2 * i / sr))
            for i in range(tot)]

def _gen_silence(sec, sr=_SAME_SR):
    n = int(sec * sr)
    if _eas_have_numpy():
        import numpy as np
        return np.zeros(n)
    return [0.0] * n

def _same_burst_segments(message, reps=3, gap=1.0, prof=None, pre=0.0, post=0.0):
    """`reps` SAME bursts of `message` framed per ENDEC profile, `gap` seconds
    apart, with optional silence before the first / after the last burst.
    NNNN (EOM) bursts use the profile's EOM framing bytes."""
    p = prof or ENDEC_PROFILES["dasdec"]
    sr = p["sr"]
    amp = _db(p["afsk_db"])
    eom = message.strip() == "NNNN"
    pre_k = "eom_" if eom else ""
    segs = []
    if pre:
        segs.append(_gen_silence(pre, sr))
    for i in range(reps):
        lead = p.get(f"{pre_k}lead_first", b"") if i == 0 else p.get(f"{pre_k}lead_rest", b"")
        trail = p.get(f"{pre_k}trail", b"")
        segs.append(_gen_afsk(_same_bits(message, lead, trail), sr, amp, p["ramp_ms"]))
        if i != reps - 1:
            segs.append(_gen_silence(gap, sr))
    if post:
        segs.append(_gen_silence(post, sr))
    return segs

def _tone_write_wav(path, segments, sr=_SAME_SR):
    """Concatenate sample segments (numpy arrays or lists), clip to [-1,1], and
    write a 16-bit mono PCM WAV at `sr`."""
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
                v = max(-1.0, min(1.0, v))
                buf += struct.pack("<h", int(v * 32767.0))
        pcm = bytes(buf)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm)

def _eas_generate_tone_wavs(channel_id, header_str, att_secs, want_attn=True, endec="dasdec"):
    """Synthesize the header / attention / EOM WAVs for one alert, the way the
    chosen ENDEC sends them. Returns (header_path, att_path, eom_path) or None.

    Timing (all profiles):
      header : 3 bursts, `gap` apart, then 1 s of silence
      attn   : attention signal, then 1 s of silence   (full alerts)
      eom    : 1 s of silence, then 3 NNNN bursts       (full alerts)
               3 NNNN bursts straight away              (RWT: header + EOM only)
    so a Required Weekly Test has exactly one second between the last header
    burst and the first EOM burst."""
    p = endec_profile(endec)
    sr = p["sr"]
    try:
        _ensure_dirs()
        h_path = os.path.join(RUNTIME_DIR, f"eas_{channel_id}_gen_header.wav")
        a_path = os.path.join(RUNTIME_DIR, f"eas_{channel_id}_gen_att.wav")
        e_path = os.path.join(RUNTIME_DIR, f"eas_{channel_id}_gen_eom.wav")
        try:
            att_secs = max(4.0, min(30.0, float(att_secs or 8.0)))
        except Exception:
            att_secs = 8.0
        if p["attn"] == "wat":
            att_secs = max(att_secs, 9.0)       # NWR sounds its 1050 Hz alarm ~9 s
        _tone_write_wav(h_path, _same_burst_segments(header_str, 3, p["gap"], p, post=1.0), sr)
        if want_attn:
            _tone_write_wav(a_path, [_gen_attn(att_secs, sr, p["attn"], _db(p["attn_db"]), p["ramp_ms"]),
                                     _gen_silence(1.0, sr)], sr)
        else:
            _tone_write_wav(a_path, [_gen_silence(0.02, sr)], sr)
        _tone_write_wav(e_path, _same_burst_segments("NNNN", 3, p["gap"], p,
                                                     pre=(p["gap"] if want_attn else 0.0)), sr)
        logger.info(
            f"[EmergencyAlertarr] EAS: generated tones ch {channel_id} ({p['label']}) — "
            f"header={_wav_duration_secs(h_path):.1f}s (\"{header_str[:48]}\"), "
            f"attn={'off' if not want_attn else ('%.0fs %s' % (att_secs, p['attn']))}, "
            f"eom={_wav_duration_secs(e_path):.1f}s"
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
            logger.debug(f"[EmergencyAlertarr] EAS WAV OK: {name} ({os.path.getsize(path)} bytes, {_wav_duration_secs(path):.1f}s)")
    return ok
