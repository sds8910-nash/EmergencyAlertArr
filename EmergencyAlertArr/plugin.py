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
import sys as _sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
from eaa_common import *
from eaa_tones import *
from eaa_sources import *
logger = logging.getLogger(__name__)


def _easyplus_settings(settings):
    """(raw font setting, size scale) for EASyPlus, read from plugin settings."""
    font = settings.get("eas_easyplus_font") or ""
    try:
        scale = max(0.5, min(3.0, float(settings.get("eas_easyplus_font_scale") or 1.0)))
    except Exception:
        scale = 1.0
    return font, scale

_TTS_BIN = shutil.which("espeak-ng") or shutil.which("espeak")

if not _TTS_BIN:
    logger.warning(
        "emergencyalertarr: no TTS engine found (espeak-ng/espeak) -- EAS alerts will "
        "use silence instead of a spoken readout. Install espeak-ng in the "
        "Dispatcharr container to enable this."
    )

_TTS_DIRECTIONS = {
    "NNE": "north-northeast", "ENE": "east-northeast", "ESE": "east-southeast",
    "SSE": "south-southeast", "SSW": "south-southwest", "WSW": "west-southwest",
    "WNW": "west-northwest", "NNW": "north-northwest",
    "NE": "northeast", "SE": "southeast", "SW": "southwest", "NW": "northwest",
    "N": "north", "S": "south", "E": "east", "W": "west",
}

_TTS_ABBREV = {
    "NWS": "the National Weather Service",
    "EAS": "the Emergency Alert System",
    "RMT": "Required Monthly Test",
    "RWT": "Required Weekly Test",
    "CDT": "Central Daylight Time", "CST": "Central Standard Time",
    "EDT": "Eastern Daylight Time", "EST": "Eastern Standard Time",
    "MDT": "Mountain Daylight Time", "MST": "Mountain Standard Time",
    "PDT": "Pacific Daylight Time", "PST": "Pacific Standard Time",
    "CDLT": "Central Daylight Time",
}

_TTS_UNITS = [
    (r"\bmph\b", "miles per hour"),
    (r"\bkts?\b", "knots"),
    (r"\bin\.\b", "inches"),
    (r"\bft\b", "feet"),
    (r"\bmi\b", "miles"),
]

_TTS_MONTHS = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}

def _tts_normalize(text):
    """Rewrite alert text into something espeak-ng pronounces cleanly. This is
    applied ONLY to the spoken (TTS) path -- the on-screen overlay text is left
    exactly as-is, so the screen still shows "35 mph" and "NE" while the voice
    says "thirty-five miles per hour" and "northeast".

    Handles: visual separators, time strings, dates, cardinal directions,
    common weather units, and a handful of all-caps acronyms that espeak would
    otherwise spell out letter by letter.
    """
    if not text:
        return text
    s = text

    # 1) Strip the visual scroll separators -- read as a short pause (period).
    s = re.sub(r"\s*[•·|]+\s*", ". ", s)

    # 2) Field labels like "WHAT:" / "WHERE:" -> natural pauses.
    s = re.sub(r"\b(WHAT|WHERE|WHEN|WHY|IMPACTS|DETAILS|HAZARD|SOURCE)\s*:",
               r"\1. ", s, flags=re.I)

    # 3) Times: "8:14 PM" -> "8 14 PM" reads fine; but "1015 PM" (no colon)
    #    must not become "one thousand fifteen". Insert a colon for 3-4 digit
    #    clock times immediately followed by AM/PM.
    def _fix_clock(m):
        digits, ap = m.group(1), m.group(2)
        if len(digits) == 3:
            hh, mm = digits[0], digits[1:]
        else:
            hh, mm = digits[:2], digits[2:]
        return f"{int(hh)}:{mm} {ap}"
    s = re.sub(r"\b(\d{3,4})\s*([AP]M)\b", _fix_clock, s)

    # 4) Dates: "06/29" -> "June 29th". Two-number slash dates only.
    def _fix_date(m):
        mo, dd = m.group(1), m.group(2)
        month = _TTS_MONTHS.get(mo.zfill(2))
        if not month:
            return m.group(0)
        return f"{month} {int(dd)}"
    s = re.sub(r"\b(\d{1,2})/(\d{1,2})\b", _fix_date, s)

    # 5) Units (mph, kts, etc.) -- do before direction expansion.
    for pat, repl in _TTS_UNITS:
        s = re.sub(pat, repl, s, flags=re.I)

    # 6) Cardinal directions: only when they're standalone tokens (so we don't
    #    touch the "E" inside a word). Longest codes first so "NNE" wins over "N".
    for code in sorted(_TTS_DIRECTIONS, key=len, reverse=True):
        s = re.sub(rf"\b{code}\b", _TTS_DIRECTIONS[code], s)

    # 7) Known acronyms -> spoken form (also longest first). Guard against a
    #    preceding "the " so "the NWS" doesn't become "the the National...".
    #    Also leave the fixed term "EAS Participant" intact (it's an originator
    #    label, not an expansion target).
    s = re.sub(r"\bEAS Participant\b", "\x00EASP\x00", s)  # shield from EAS rule
    for code in sorted(_TTS_ABBREV, key=len, reverse=True):
        repl = _TTS_ABBREV[code]
        if repl.startswith("the "):
            # "the NWS" -> "the National Weather Service" (don't double "the")
            s = re.sub(rf"\bthe\s+{code}\b", repl, s, flags=re.I)
        s = re.sub(rf"\b{code}\b", repl, s)
    s = s.replace("\x00EASP\x00", "EAS Participant")  # restore

    # 8) Any remaining ALL-CAPS word of 2+ letters that espeak might spell out
    #    letter-by-letter (e.g. an uppercased event name) -> Title Case so it's
    #    read as a normal word. Leave short 1-letter tokens, numbers, and the
    #    AM/PM clock markers alone.
    def _decap(m):
        w = m.group(0)
        if w in ("AM", "PM", "EAS"):
            return w
        return w[:1] + w[1:].lower()
    s = re.sub(r"\b[A-Z]{2,}\b", _decap, s)

    # 9) Collapse whitespace and stray repeated punctuation.
    s = re.sub(r"\.\s*\.", ".", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s

def _eas_tts_synthesize(text, out_path):
    """Synthesize text to a WAV file with espeak-ng/espeak. Fully offline.
    Returns True on success, False if no TTS engine is installed, the text
    is empty, or synthesis fails for any reason (caller falls back to silence)."""
    if not _TTS_BIN or not text:
        return False
    spoken = _tts_normalize(text)
    try:
        _ensure_dirs()
        result = subprocess.run(
            [_TTS_BIN, "-v", "en-us", "-s", "165", "-w", out_path, spoken],
            timeout=20, capture_output=True,
        )
        return result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 200
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] EAS: TTS synthesis failed: {e}")
        return False

def _build_eas_easyplus_filter(channel_id, total_duration=60, font=None, scale=1.0):
    """Classic EASyPlus-style EAS takeover: full-screen black background with
    five stacked, centered white text lines. The area/effective-until line
    crawls right-to-left in a single pass timed to finish exactly as
    total_duration ends; the rest are static and centered.

    font   -- overlay font file (defaults to the bundled EASyText.ttf).
    scale  -- size multiplier applied to every line (1.0 = default size)."""
    font = font or _font_for("easyplus")
    if not font:
        raise RuntimeError(
            "No overlay font available. Ship fonts/EASyText.ttf and fonts/luximb.ttf "
            "in the plugin zip, or install a TTF font package (e.g. fonts-dejavu-core) "
            "in the Dispatcharr container."
        )
    d = RUNTIME_DIR
    try:
        scale = float(scale)
    except Exception:
        scale = 1.0
    scale = max(0.5, min(3.0, scale))

    def _sz(base):
        return max(8, int(round(base * scale)))

    # Larger defaults than before so the takeover reads like a real broadcast
    # EAS character generator (the header roughly fills the screen width).
    hdr_sz    = _sz(56)   # was 42
    body_sz   = _sz(40)   # was 30
    event_sz  = _sz(48)   # was 34

    total_duration = max(5.0, float(total_duration))
    scroll_x = f"w-t*((w+text_w+40)/{total_duration:.3f})"
    return (
        # Full-screen black takeover — completely replaces the underlying video
        "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill,"
        f"drawtext=fontfile={font}"
        f":textfile={d}/eas_{channel_id}_ep_header.txt:reload=1"
        f":fontsize={hdr_sz}:fontcolor=white"
        f":x=(w-text_w)/2:y=h*0.18,"
        f"drawtext=fontfile={font}"
        f":textfile={d}/eas_{channel_id}_ep_area.txt:reload=1"
        f":fontsize={body_sz}:fontcolor=white"
        f":x={scroll_x}:y=h*0.33,"
        f"drawtext=fontfile={font}"
        f":textfile={d}/eas_{channel_id}_ep_source.txt:reload=1"
        f":fontsize={body_sz}:fontcolor=white"
        f":x=(w-text_w)/2:y=h*0.47,"
        f"drawtext=fontfile={font}"
        f":textfile={d}/eas_{channel_id}_ep_issued.txt:reload=1"
        f":fontsize={body_sz}:fontcolor=white"
        f":x=(w-text_w)/2:y=h*0.60,"
        f"drawtext=fontfile={font}"
        f":textfile={d}/eas_{channel_id}_ep_event.txt:reload=1"
        f":fontsize={event_sz}:fontcolor=white"
        f":x=(w-text_w)/2:y=h*0.73"
    )

def _eas_dasdec_block_lines(unique_alerts, texts, width=40):
    """Build the DASDEC/ENDEC message block as a list of centered text lines, in
    the classic cable-headend wording:

        <originator> has issued <a/an> <EVENT> for the following counties or
        areas:
        <Location>;
        at <time>
        on <DATE>
        Effective until <time>.
        Message from <station>.
        <wrapped description text...>

    Long lines are wrapped to `width` monospace characters so the paginator can
    lay them out cleanly. This is the static, paginated headend look -- NOT a
    scrolling crawl."""
    import textwrap
    from datetime import datetime
    a = (unique_alerts or [{}])[0]
    event = re.sub(r"\s+", " ", (a.get("event") or "Emergency Alert")).strip()
    originator = (a.get("originator") or "").upper()
    sender = (a.get("sender") or "").strip()
    slow = sender.lower()

    if originator == "PEP" or "primary entry" in slow:
        orig_phrase = "The Primary Entry Point EAS System"
    elif originator == "WXR" or "weather" in slow or "nws" in slow:
        orig_phrase = "The National Weather Service"
    elif originator == "CIV" or "civil" in slow or "law enforcement" in slow:
        orig_phrase = "A civil authority"
    elif originator == "EAS":
        orig_phrase = "An EAS Participant"
    else:
        orig_phrase = sender or "The Emergency Alert System"

    article = "an" if event[:1].upper() in "AEIOU" else "a"

    def _fmt_time(iso):
        try:
            dt = datetime.fromisoformat((iso or "").strip().replace("Z", "+00:00"))
            return dt.strftime("%I:%M %p").lstrip("0")
        except Exception:
            return ""

    def _fmt_date(iso):
        try:
            dt = datetime.fromisoformat((iso or "").strip().replace("Z", "+00:00"))
            return dt.strftime("%b %d, %Y").replace(" 0", " ").upper()
        except Exception:
            return ""

    start_t = _fmt_time(a.get("effective"))
    end_t   = _fmt_time(a.get("expires"))
    date_s  = _fmt_date(a.get("effective"))
    station = (sender or "EAS").strip()

    lines = textwrap.wrap(
        f"{orig_phrase} has issued {article} {event} for the following "
        f"counties or areas:", width=width
    )
    areas = [x.strip() for x in (a.get("area") or "").split(";") if x.strip()]
    for loc in (areas or ["This area"]):
        for seg in textwrap.wrap(f"{loc};", width=width):
            lines.append(seg)
    if start_t:
        lines.append(f"at {start_t}")
    if date_s:
        lines.append(f"on {date_s}")
    if end_t:
        lines.append(f"Effective until {end_t}.")
    lines.append(f"Message from {station}.")

    desc = (texts.get("body") or "").strip()
    if desc and "no further details" not in desc.lower():
        lines.append("")
        for para in desc.split("\n"):
            para = para.strip()
            if not para:
                lines.append("")
                continue
            lines.extend(textwrap.wrap(para, width=width))
    return [ln for ln in lines if ln is not None]

def _eas_dasdec_pages(channel_id, block_lines, total_duration, lines_per_page=11):
    """Write each message line to its own file, split the block into pages, write
    a "1/3" page-counter file per page, and return (page_line_map, page_secs)."""
    for i, line in enumerate(block_lines):
        _atomic_write(f"eas_{channel_id}_dd_l{i}.txt", line if line.strip() else " ")
    page_line_map = []
    total = max(1, len(block_lines))
    for start in range(0, total, lines_per_page):
        page_line_map.append(list(range(start, min(start + lines_per_page, total))))
    if not page_line_map:
        page_line_map = [[]]
    n = len(page_line_map)
    page_secs = max(5.0, float(total_duration) / n)
    for pi in range(n):
        _atomic_write(f"eas_{channel_id}_dd_pg{pi}.txt", f"{pi + 1}/{n}")
    return page_line_map, page_secs

def _build_eas_dasdec_filter(channel_id, page_line_map, page_secs, font=None):
    """DASDEC / cable-headend EAS takeover: dark navy background, thin bright red
    inset border, monospace bold white text, centered, auto-paginated with a
    "1/3" page counter near the bottom. Rendered as a plain -vf drawtext chain
    (per-page lines time-gated with enable='between(t,...)') -- the same reliable
    path EASyPlus uses. No pre-render, no filter_complex, no movie= input."""
    font = font or _font_for("dasdec")
    if not font:
        raise RuntimeError(
            "No overlay font available. Ship fonts/luximb.ttf in the plugin zip, "
            "or install a TTF font package (e.g. fonts-dejavu-core) in the container."
        )
    d = RUNTIME_DIR
    navy = "0x0A1E5C"
    red  = "0xC00000"
    gold = "0xFFD200"
    margin = 22
    title_size = 34
    body_size  = 30
    line_h     = 42     # vertical spacing between centered body lines

    parts = [
        f"drawbox=x=0:y=0:w=iw:h=ih:color={navy}:t=fill",
        f"drawbox=x={margin}:y={margin}:w=iw-{margin*2}:h=ih-{margin*2}:color={red}:t=6",
        # Title header at the very top (gold), centered.
        f"drawtext=fontfile={font}:textfile={d}/eas_{channel_id}_dd_title.txt:reload=0"
        f":fontsize={title_size}:fontcolor={gold}:x=(w-text_w)/2:y={margin + 16}",
    ]

    # Body block is centered in the region BELOW the title and ABOVE the counter.
    title_bottom = margin + 16 + title_size + 18
    counter_top  = f"(h-{margin + 54})"
    page_count = max(1, len(page_line_map))
    for pi, line_idxs in enumerate(page_line_map):
        start = pi * page_secs
        end = (pi + 1) * page_secs if pi < page_count - 1 else 999999
        win = f":enable='between(t\\,{start:.2f}\\,{end})'" if page_count > 1 else ""
        block_h = max(1, len(line_idxs)) * line_h
        first_y = f"(({title_bottom}+{counter_top})/2 - {block_h}/2)"
        for row, li in enumerate(line_idxs):
            y = f"{first_y}+{row * line_h}"
            parts.append(
                f"drawtext=fontfile={font}:textfile={d}/eas_{channel_id}_dd_l{li}.txt:reload=0"
                f":fontsize={body_size}:fontcolor=white:x=(w-text_w)/2:y={y}{win}"
            )
        if page_count > 1:
            parts.append(
                f"drawtext=fontfile={font}:textfile={d}/eas_{channel_id}_dd_pg{pi}.txt:reload=0"
                f":fontsize=26:fontcolor=white:x=(w-text_w)/2:y=h-{margin+40}{win}"
            )
    return ",".join(parts)

def _inject_drawtext(params, drawtext_filter):
    is_audio_only = "-vn" in params or (
        ("-c:a" in params or "-acodec" in params)
        and "-c:v" not in params
        and "-vcodec" not in params
    )

    if is_audio_only:
        # Remove -vn and existing -map directives (replaced below)
        params = re.sub(r"\s*-vn\b", "", params)
        params = re.sub(r"\s*-map\s+\S+", "", params)
        # Add lavfi black background as second input.
        # If -i is present in params (self-contained profiles), insert after it.
        # Otherwise Dispatcharr supplies input 0 externally — prepend lavfi so it
        # becomes input 1 after Dispatcharr's stream URL.
        lavfi = '-f lavfi -i "color=c=black:s=1280x720:r=15"'
        new_params = re.sub(r"(-i\s+\S+)", rf"\1 {lavfi}", params, count=1)
        if new_params == params:
            params = f"{lavfi} {params}"
        else:
            params = new_params
        _fc_graph = f'[1:v]{drawtext_filter}[vout]'
        fc = (
            f'-filter_complex "{_fc_graph}"'
            f' -map "[vout]" -map 0:a:0'
            f' -c:v libx264 -preset ultrafast -tune stillimage -crf 28'
        )
        if "-f mpegts" in params:
            params = params.replace("-f mpegts", f"{fc} -f mpegts")
        elif "pipe:1" in params:
            params = params.replace("pipe:1", f"{fc} pipe:1")
        else:
            params = f"{params} {fc}"
        return params

    # Replace any stream-copy video flag — FFmpeg rejects filters with stream copy.
    # zerolatency removes encoder lookahead/B-frames, so -c:a copy stays in sync
    _VID_ENCODE = "-c:v libx264 -preset ultrafast -tune zerolatency -c:a copy"
    if "-c:v copy" in params:
        params = params.replace("-c:v copy", _VID_ENCODE)
    elif "-vcodec copy" in params:
        params = params.replace("-vcodec copy", _VID_ENCODE)
    # "-c copy" copies ALL streams
    params = re.sub(r'(?<![:\w])-c\s+copy\b', _VID_ENCODE, params)

    # Deduplicate -c:a copy that arises when base profile already has it and _VID_ENCODE adds another.
    params = re.sub(r'(\s+-c:a\s+copy){2,}', ' -c:a copy', params)

    # Strip -force_key_frames. In stream-copy profiles this is ignored, but once libx264
    # is active the expression expr:gte(t,n_forced*0) evaluates true on every single frame,
    # forcing all-I-frame output — output bitrate explodes and the encoder can't keep up.
    params = re.sub(r'\s*-force_key_frames\s+"[^"]*"', '', params)
    params = re.sub(r'\s*-force_key_frames\s+\S+', '', params)

    vf_clause = f'-vf "{drawtext_filter}"'

    if "-vf " in params:
        # Prepend drawtext to existing -vf, handling both quoted and unquoted forms
        params = re.sub(r'-vf\s+"([^"]*)"', rf'-vf "{drawtext_filter},\1"', params, count=1)
        if "-vf " in params and f'"{drawtext_filter},' not in params:
            params = re.sub(r'-vf\s+(\S+)', rf'-vf "{drawtext_filter},\1"', params, count=1)
    elif "-f mpegts" in params:
        params = params.replace("-f mpegts", f"{vf_clause} -f mpegts")
    elif "pipe:1" in params:
        params = params.replace("pipe:1", f"{vf_clause} pipe:1")
    else:
        params = params + f" {vf_clause}"

    # Suppress the default 1-second muxer interleave buffer. Without this, FFmpeg
    # buffers up to 1 second of packets to interleave transcoded video against
    # pass-through audio, producing visible startup lag on stream-copy base profiles.
    if "-max_interleave_delta" not in params:
        if "-f mpegts" in params:
            params = params.replace("-f mpegts", "-max_interleave_delta 1 -f mpegts")
        elif "pipe:1" in params:
            params = params.replace("pipe:1", "-max_interleave_delta 1 pipe:1")

    return params

_DANGEROUS_FLAGS = {
    "nobuffer":  "+nobuffer in -fflags causes audio gaps on burst-delivered streams (e.g. SiriusXM via best-streams.tv). FFmpeg passes burst gaps directly to the client with no internal buffering.",
    "low_delay": "-flags low_delay disables decoder delay compensation, causing the same burst-gap disconnects.",
}

def _strip_dangerous_flags(channel_name, params):
    """Strip known problematic FFmpeg flags from cloned profile parameters.
    Logs a clear notification for every flag removed.
    The original base profile is never modified — only the EmergencyAlertarr clone is cleaned.
    """
    removed = []

    # Strip +nobuffer from -fflags value (e.g. -fflags +discardcorrupt+nobuffer)
    if "nobuffer" in params:
        def _remove_nobuffer(m):
            value = re.sub(r'\+?nobuffer', '', m.group(2))
            value = re.sub(r'\++', '+', value).strip('+')
            if not value:
                return ''
            return m.group(1) + value
        new_params = re.sub(r'(-fflags\s+)(\S+)', _remove_nobuffer, params)
        if new_params != params:
            removed.append("+nobuffer")
            params = new_params

    # Strip -flags low_delay
    if "low_delay" in params:
        new_params = re.sub(r'\s*-flags\s+low_delay\b', '', params)
        if new_params != params:
            removed.append("-flags low_delay")
            params = new_params

    for flag in removed:
        key = flag.lstrip('+-').split()[0]
        reason = _DANGEROUS_FLAGS.get(key, "known to cause stream issues")
        logger.warning(
            f"[EmergencyAlertarr] Auto-removed {flag} from cloned profile for \"{channel_name}\" "
            f"— {reason} "
            f"Your original base profile is unchanged."
        )

    return params, removed

def _inject_eas_wav_sequence(params, mid_path=None, mid_secs=15, endec_test=False, tail_secs=7.5,
                             wav_header=None, wav_att=None, wav_eom=None, lead_in_secs=0.0):
    """Build a clean EAS audio sequence profile.

    wav_header / wav_att / wav_eom override the default pre-recorded tone files
    (used when tones are generated dynamically); each defaults to its constant.

    Approach
    --------
    _inject_drawtext has already produced either:
      (A) video stream:  params contains -vf "drawtext=..."
      (B) audio-only:    params contains -filter_complex "..." with a lavfi black
                         background as input [1:v] and -map 0:a:0

    In both cases we need to:
      1. Append the WAV files as additional FFmpeg inputs.
      2. Build a filter_complex (or extend the existing one) that:
           - keeps the video path intact                    [vout]
           - concatenates the audio segments                [aseq]
           - pads [aseq] with anullsrc so it never ends early (apad)
      3. Map [vout] and [aseq] to the output.
      4. Drop the original stream audio entirely -- EAS replaces it, not mixes.

    Two audio layouts:
      * Normal (endec_test=False):  header -> attention -> mid -> EOM
          mid_path: optional TTS readout WAV played between attention and EOM;
          if None, a silent gap of mid_secs is used instead.
      * ENDEC test (endec_test=True):  header -> EOM -> tail silence
          Mimics a real EAS ENDEC running a test: just the header tones, then
          the end-of-message tones, then tail_secs of silence during which the
          alert screen stays up and keeps scrolling before clearing. No
          attention tones and no readout.

    WAV inputs are appended at the END of the input list so we can calculate
    their indices reliably regardless of what _inject_drawtext added.
    """
    wav_header = wav_header or _EAS_WAV_HEADER
    wav_att    = wav_att or _EAS_WAV_ATT
    wav_eom    = wav_eom or _EAS_WAV_EOM

    if not _eas_wav_available(verbose=True, header=wav_header, att=wav_att, eom=wav_eom):
        logger.warning(
            "[EmergencyAlertarr] EAS: one or more tone WAVs missing/empty (see WAV lines "
            "above) -- drop valid eas_header.wav / eas_att.wav / eas_eom.wav "
            "into the plugin dir, or enable 'Generate tones dynamically'. "
            "Audio sequence skipped."
        )
        return params

    # ------------------------------------------------------------------ #
    # 1. Count existing -i inputs so we know the WAV input indices        #
    # ------------------------------------------------------------------ #
    # Match both  -i "quoted path"  and  -i unquoted
    existing_inputs = re.findall(r'-i\s+(?:"[^"]*"|\S+)', params)
    n = len(existing_inputs)

    # Optional lead-in: a stretch of silence prepended to the whole sequence so
    # the tones don't begin until the video overlay has actually appeared on
    # screen. The overlay is drawn on the live source, which can take several
    # seconds to connect, while the tone WAVs are local and would otherwise play
    # immediately -- leaving the header tones running over a blank picture. The
    # lead-in absorbs that startup gap so the overlay and the header tones come
    # up together.
    lead_in_secs = max(0.0, float(lead_in_secs or 0))
    if lead_in_secs > 0:
        _lead_clause = (
            f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={lead_in_secs},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[lead];"
        )
        _lead_lbl = "[lead]"
        _lead_n = 1
    else:
        _lead_clause = ""
        _lead_lbl = ""
        _lead_n = 0

    if endec_test:
        # header -> EOM -> tail silence (no attention tones, no readout)
        wi_h = n       # eas_header.wav
        wi_e = n + 1   # eas_eom.wav
        wav_inputs = (
            f' -i "{wav_header}"'
            f' -i "{wav_eom}"'
        )
        # Use anullsrc (purpose-built silent source) + the SAME aformat applied
        # to the tone segments, so all three concat inputs are byte-identical in
        # rate/format/layout. aevalsrc here produced a layout mismatch that made
        # concat emit silence for the whole sequence.
        audio_graph = (
            f"[{wi_h}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[h];"
            f"[{wi_e}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[e];"
            f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration=1.0,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[gap];"
            f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={tail_secs},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[tail];"
            f"{_lead_clause}"
            f"{_lead_lbl}[h][gap][e][tail]concat=n={4 + _lead_n}:v=0:a=1,apad[aseq]"
        )
        wi_log = f"{wi_h},{wi_e}"
        mid_desc = f"ENDEC test (header+EOM+{tail_secs}s tail)"
    else:
        wi_h = n       # eas_header.wav  (input n)
        wi_a = n + 1   # eas_att.wav     (input n+1)
        if mid_path:
            wi_m = n + 2   # TTS readout wav (input n+2)
            wi_e = n + 3   # eas_eom.wav     (input n+3)
            wav_inputs = (
                f' -i "{wav_header}"'
                f' -i "{wav_att}"'
                f' -i "{mid_path}"'
                f' -i "{wav_eom}"'
            )
            mid_clause = f"[{wi_m}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[mid];"
        else:
            wi_e = n + 2   # eas_eom.wav     (input n+2)
            wav_inputs = (
                f' -i "{wav_header}"'
                f' -i "{wav_att}"'
                f' -i "{wav_eom}"'
            )
            mid_clause = (
                f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={mid_secs},"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo[mid];"
            )
        audio_graph = (
            f"[{wi_h}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[h];"
            f"[{wi_a}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a];"
            f"{mid_clause}"
            f"[{wi_e}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[e];"
            f"{_lead_clause}"
            f"{_lead_lbl}[h][a][mid][e]concat=n={4 + _lead_n}:v=0:a=1,apad[aseq]"
        )
        wi_log = f"{wi_h},{wi_a},{wi_e}"
        mid_desc = "TTS readout" if mid_path else f"{mid_secs}s silence"

    # ------------------------------------------------------------------ #
    # 3. Wire into the existing filter graph                               #
    # ------------------------------------------------------------------ #
    fc_match = re.search(r'-filter_complex\s+"((?:[^"\\]|\\.)*)"', params)
    vf_match  = re.search(r'-vf\s+"([^"]+)"', params)

    if fc_match:
        # Case B: audio-only -- already has filter_complex with [vout]
        # Extend it: append audio_graph, update maps
        existing_fc = fc_match.group(1)
        new_fc = existing_fc + ";" + audio_graph
        params = (
            params[:fc_match.start()]
            + f'-filter_complex "{new_fc}"'
            + params[fc_match.end():]
        )
        # Replace -map 0:a:0 (original stream audio) with [aseq]
        params = re.sub(r'\s*-map\s+0:a:0\b', ' -map "[aseq]"', params)
        # Remove any other bare -map 0 that would pull in original audio
        params = re.sub(r'\s*-map\s+0(?!:)', '', params)

    elif vf_match:
        # Case A: video stream -- has -vf, no filter_complex yet
        vf_filter = vf_match.group(1)
        video_graph = f"[0:v]{vf_filter}[vout]"
        full_fc = video_graph + ";" + audio_graph
        fc_clause = f'-filter_complex "{full_fc}" -map "[vout]" -map "[aseq]"'
        params = params[:vf_match.start()] + fc_clause + params[vf_match.end():]
        # Remove bare -map 0 (filter_complex maps replace it)
        params = re.sub(r'\s*-map\s+0(?!:)', '', params)

    else:
        logger.warning("[EmergencyAlertarr] EAS: no -vf or -filter_complex found -- audio skipped")
        return params

    # ------------------------------------------------------------------ #
    # 4. Insert WAV inputs right after the last existing -i clause        #
    # ------------------------------------------------------------------ #
    # FFmpeg treats any option placed before an -i as belonging to that
    # input, not as a global/output flag. Inserting the WAV inputs near the
    # output marker (the old approach) left output flags like "-c:a aac
    # -b:a 192k" sitting *before* these new inputs in the command line,
    # which FFmpeg then tried to apply to eas_header.wav as an input option
    # -- producing "Error opening input files: Invalid argument" and a dead
    # stream. Inserting right after the last existing input keeps all -i
    # clauses grouped together, ahead of every output flag, as FFmpeg requires.
    input_matches = list(re.finditer(r'-i\s+(?:"[^"]*"|\S+)', params))
    if input_matches:
        insert_at = input_matches[-1].end()
        params = params[:insert_at] + wav_inputs + params[insert_at:]
    else:
        params = wav_inputs + " " + params

    # ------------------------------------------------------------------ #
    # 5. Force audio re-encode (can't stream-copy when building concat)   #
    # ------------------------------------------------------------------ #
    params = re.sub(r'\s*-c:a\s+copy\b', ' -c:a aac -b:a 192k', params)
    params = re.sub(r'\s*-acodec\s+copy\b', ' -c:a aac -b:a 192k', params)
    params = re.sub(r'(\s+-c:a\s+aac\s+-b:a\s+192k){2,}', ' -c:a aac -b:a 192k', params)

    # ------------------------------------------------------------------ #
    # 6. Drop subtitle & data streams for the duration of the alert       #
    # ------------------------------------------------------------------ #
    # The overlay is a full-screen takeover, so the underlying program's
    # subtitle/teletext/data streams must not pass through -- otherwise a
    # client can render the original show's captions on top of the black
    # EAS screen, and stray data streams can upset the mpegts muxer. We only
    # -map [vout] and [aseq], so nothing else is mapped, but -sn -dn makes the
    # intent explicit and covers any base profile that sneaks a stream in.
    if "-sn" not in params:
        if "-f mpegts" in params:
            params = params.replace("-f mpegts", "-sn -dn -f mpegts", 1)
        elif "pipe:1" in params:
            params = params.replace("pipe:1", "-sn -dn pipe:1", 1)
        else:
            params = params + " -sn -dn"

    logger.info(
        f"[EmergencyAlertarr] EAS: WAV sequence injected "
        f"(inputs {wi_log} / mid={mid_desc})"
    )
    return params

_EAS_TRANSCODE_PREFIXES = {
    # Filter prefix prepended to the EAS overlay filter chain.
    # Applied at clone time; removed automatically when the alert clears and the
    # original profile is restored.  "full" = no prefix (transcode at source quality).
    "full":    "",
    "1080p30": "fps=fps=30,",
    "720p":    "scale=1280:720:flags=fast_bilinear,",
    "720p30":  "scale=1280:720:flags=fast_bilinear,fps=fps=30,",
}

def _eas_seq_fixed_overhead(header=None, att=None, eom=None):
    """Combined duration of the header tone + attention tone + EOM tone WAVs,
    i.e. everything in the audio sequence other than the middle (TTS/silence)
    segment. Falls back to a sane estimate if the WAVs aren't present yet.
    header/att/eom override the default files (used for dynamically generated
    tones)."""
    header = header or _EAS_WAV_HEADER
    att    = att or _EAS_WAV_ATT
    eom    = eom or _EAS_WAV_EOM
    if not _eas_wav_available(header=header, att=att, eom=eom):
        return 18.0
    return (
        _wav_duration_secs(header)
        + _wav_duration_secs(att)
        + _wav_duration_secs(eom)
    )

def _clone_and_inject_eas(channel_id, original_profile, channel_name="", silence_secs=15,
                          transcode_mode="full", style="easyplus", unique_alerts=None,
                          use_tts=True, endec_test=False, tail_secs=7.5,
                          generate_tones=False, att_secs=8.0,
                          easyplus_font=None, easyplus_scale=1.0, lead_in_secs=0.0):
    """Clone the channel's profile into an EAS-overlay profile.

    Returns (profile, removed_flags, total_duration). total_duration is the
    authoritative length of the whole alert sequence -- the audio concat, the
    EASyPlus scroll, and the DASDEC pagination are all timed to it so they all
    finish together (the scroll no longer gets cut off halfway, and the screen
    stays up exactly until the audio ends). Callers use the returned value to
    schedule the timed restore.

    endec_test: if True, build the real-ENDEC-style test sequence instead of
    the normal one: header tones -> EOM tones -> tail_secs of silence with the
    alert screen still up and scrolling, then clear. No attention tones, no TTS.
    """
    from core.models import StreamProfile
    raw_params = original_profile.parameters or ""
    cleaned_params, removed_flags = _strip_dangerous_flags(
        channel_name or f"channel {channel_id}", raw_params
    )

    # Hard guarantee: never render more than one alert on screen or in the
    # readout. Callers already pass a single record, but this makes the
    # "one alert at a time" rule impossible to violate (no ALERT/ALERT/ALERT,
    # no every-county pile-up) even if something upstream regresses.
    unique_alerts = list(unique_alerts or [])[:1]

    # --- Decide the middle audio segment (TTS readout vs plain silence) ------
    mid_path = None
    mid_secs = silence_secs
    if not endec_test and use_tts and _TTS_BIN and unique_alerts:
        texts = _eas_alert_texts(unique_alerts)
        spoken = texts.get("spoken", "") or ""
        if len(spoken) > _EAS_MAX_SPOKEN_CHARS:
            # Trim to the last sentence break within the limit so a very long
            # alert can't produce a runaway readout (and a runaway duration).
            cut = spoken[:_EAS_MAX_SPOKEN_CHARS]
            dot = cut.rfind(". ")
            spoken = (cut[:dot + 1] if dot > 200 else cut).strip()
        tts_path = os.path.join(RUNTIME_DIR, f"eas_{channel_id}_tts.wav")
        if _eas_tts_synthesize(spoken, tts_path):
            mid_path = tts_path
            # Pad the spoken segment with a little breathing room before EOM.
            mid_secs = _wav_duration_secs(tts_path) + 2.0
        else:
            logger.info(f"[EmergencyAlertarr] EAS: TTS unavailable for ch {channel_id}, using silence")

    # --- Optional: synthesize the tone WAVs dynamically ----------------------
    # When enabled, the header tones encode the real SAME data for this alert
    # (so more counties => a longer header burst) and the attention tone honors
    # the configured length. Falls back to the pre-recorded WAVs if generation
    # fails. gen_h/gen_a/gen_e stay None when disabled, so every downstream call
    # transparently uses the default eas_*.wav files.
    gen_h = gen_a = gen_e = None
    if generate_tones:
        src_alert = (unique_alerts or [{}])[0] if unique_alerts else {}
        header_str = _eas_same_header_string(src_alert)
        gen = _eas_generate_tone_wavs(
            channel_id, header_str, att_secs, want_attn=not endec_test
        )
        if gen:
            gen_h, gen_a, gen_e = gen
        else:
            logger.warning(
                f"[EmergencyAlertarr] EAS: dynamic tone generation failed for ch {channel_id} "
                "-- falling back to pre-recorded WAVs."
            )

    # --- Authoritative total sequence duration -------------------------------
    if endec_test:
        # ENDEC test layout: header + EOM (no attention tone) + tail silence.
        _hdr = gen_h or _EAS_WAV_HEADER
        _eom = gen_e or _EAS_WAV_EOM
        header_eom = (
            (_wav_duration_secs(_hdr) + _wav_duration_secs(_eom))
            if _eas_wav_available(header=gen_h, att=gen_a, eom=gen_e) else 6.0
        )
        # header + 1s gap + EOM + tail silence (+1s buffer so the screen doesn't
        # vanish on the last sample). The 1s gap separates the header from the EOM
        # so they don't run together and sound like one blurred burst.
        total_duration = header_eom + 1.0 + tail_secs + 1.0
    else:
        # header + attention + (TTS or silence) + EOM, with a small tail so the
        # screen doesn't vanish the instant the last tone sample plays.
        total_duration = _eas_seq_fixed_overhead(gen_h, gen_a, gen_e) + mid_secs + 2.0

    # Absolute ceiling: a single alert takeover can never exceed this, so a
    # pathological alert (or any future regression) can't strand a channel for
    # the better part of an hour.
    if total_duration > _EAS_MAX_ALERT_SECS:
        logger.warning(
            f"[EmergencyAlertarr] EAS: computed duration {total_duration:.0f}s exceeds cap "
            f"-- clamping to {_EAS_MAX_ALERT_SECS}s (ch {channel_id})"
        )
        total_duration = float(_EAS_MAX_ALERT_SECS)
    # --- Build the video overlay filter for the chosen style -----------------
    # Both styles render as a plain -vf drawtext chain (then _inject_drawtext +
    # _inject_eas_wav_sequence) -- the same reliable path. EASyPlus is a black
    # takeover with a scrolling crawl; DASDEC is the static, paginated navy/red
    # cable-headend character-generator screen.
    transcode_prefix = _EAS_TRANSCODE_PREFIXES.get(transcode_mode, "")
    if style == "dasdec":
        # DASDEC is a full takeover (no source video shows through) rendered with
        # many drawtext lines, so default it to a light 720p/15fps encode when the
        # user hasn't picked a transcode quality -- keeps it fast without any
        # visible quality loss. An explicit choice is respected.
        if transcode_mode == "full":
            transcode_prefix = "scale=1280:720:flags=fast_bilinear,fps=fps=15,"
        dd_texts = _eas_alert_texts(unique_alerts or [])
        block_lines = _eas_dasdec_block_lines(unique_alerts or [], dd_texts)
        _atomic_write(f"eas_{channel_id}_dd_title.txt", "Emergency Alert Details")
        page_line_map, page_secs = _eas_dasdec_pages(
            channel_id, block_lines, total_duration
        )
        eas_filter = transcode_prefix + _build_eas_dasdec_filter(
            channel_id, page_line_map, page_secs,
            font=_resolve_font(easyplus_font, "dasdec"),
        )
    else:
        eas_filter = transcode_prefix + _build_eas_easyplus_filter(
            channel_id, total_duration=total_duration,
            font=_resolve_font(easyplus_font, "easyplus"), scale=easyplus_scale,
        )

    params = _inject_drawtext(cleaned_params, eas_filter)
    params = _inject_eas_wav_sequence(
        params, mid_path=mid_path, mid_secs=mid_secs,
        endec_test=endec_test, tail_secs=tail_secs,
        wav_header=gen_h, wav_att=gen_a, wav_eom=gen_e,
        lead_in_secs=lead_in_secs,
    )

    profile = StreamProfile(
        name=f"{PROFILE_PREFIX}EAS [{original_profile.name}] [ch{channel_id}]",
        command=original_profile.command,
        parameters=params,
        locked=False,
        is_active=True,
    )
    profile.save()
    logger.info(
        f"emergencyalertarr: EAS profile cloned {original_profile.id} → {profile.id} "
        f"for channel {channel_id} (style={style}, dur={total_duration:.1f}s, "
        f"tts={'yes' if mid_path else 'no'})"
        + (f" (removed: {', '.join(removed_flags)})" if removed_flags else "")
    )
    # The restore countdown must cover the lead-in silence too (the overlay is
    # on screen during it), so report the full wall-clock length. The visible
    # tone/scroll/page timing above deliberately excludes the lead-in.
    return profile, removed_flags, total_duration + max(0.0, float(lead_in_secs or 0))

def _assign_profile(channel, profile):
    channel.stream_profile = profile
    channel.save(update_fields=["stream_profile"])
    try:
        channel.update_stream_profile(profile.id)
    except Exception:
        pass

def _restore_profile(channel, original_profile_id):
    from core.models import StreamProfile
    if not original_profile_id:
        # No known original profile (e.g. legacy/incomplete mapping data) --
        # nothing safe to restore to, so just clear the profile rather than crash.
        channel.stream_profile = None
        channel.save(update_fields=["stream_profile"])
        return
    try:
        original = StreamProfile.objects.get(id=original_profile_id)
        _assign_profile(channel, original)
    except StreamProfile.DoesNotExist:
        channel.stream_profile = None
        channel.save(update_fields=["stream_profile"])

def _delete_cloned_profile(profile_id):
    if not profile_id:
        return
    from core.models import StreamProfile
    try:
        StreamProfile.objects.filter(id=profile_id, name__startswith=PROFILE_PREFIX).delete()
    except Exception as e:
        logger.warning(f"emergencyalertarr: could not delete profile {profile_id}: {e}")

def _get_emergencyalertarr_profiles():
    from core.models import StreamProfile
    return list(StreamProfile.objects.filter(name__startswith=PROFILE_PREFIX))

def _eas_alert_texts(unique_alerts):
    """Build every text representation of the current alert set from one shared
    source, so EASyPlus, DASDEC, and the TTS readout never drift out of sync
    with each other. Returns a dict of ready-to-use strings."""
    if not unique_alerts:
        return {}

    def _area(a):
        return (a.get("area") or "").replace("; ", "  ·  ").strip()

    def _is_nws(a):
        # NWS alerts (originator WXR) use the "* WHAT... * WHERE... * WHEN..."
        # bullet convention; tests, IPAWS civil/AMBER alerts, and custom alerts
        # do not, so we don't synthesize WHAT:/WHERE:/WHEN: labels for them.
        return (a.get("originator") or "").upper() == "WXR"

    def _plain_desc(raw):
        # Collapse a (possibly bullet-formatted) description into clean prose,
        # dropping any "* LABEL..." markers rather than turning them into labels.
        out = []
        for chunk in re.split(r"\*\s+", raw):
            chunk = re.sub(r"\s+", " ", chunk).strip()
            if not chunk:
                continue
            chunk = re.sub(r"^[A-Z][A-Z0-9 /&'-]*\.{2,}\s*", "", chunk).strip()
            if chunk:
                out.append(chunk)
        return " ".join(out)

    def _headline(a):
        return re.sub(r"\s+", " ", (a.get("headline") or "")).strip()

    def _instruction(a):
        return re.sub(r"\s+", " ", (a.get("instruction") or "")).strip()

    def _description(a):
        """NWS bullet description -> "WHAT: foo  |  WHERE: bar". Non-NWS alerts
        (tests, IPAWS civil, custom) are shown as plain prose, no labels."""
        raw_desc = (a.get("description") or "").strip()
        if not raw_desc:
            return ""
        if not _is_nws(a):
            return _plain_desc(raw_desc)
        bullets = re.split(r"\*\s+", raw_desc)
        parts = []
        for chunk in bullets:
            chunk = re.sub(r"\s+", " ", chunk).strip().rstrip(".")
            if not chunk:
                continue
            chunk = re.sub(r"\.{2,}", ": ", chunk, count=1)
            parts.append(chunk)
        return "  |  ".join(parts)

    def _description_paragraphs(a):
        """Same as _description but kept as separate paragraphs for the DASDEC
        paginated body. Non-NWS alerts stay as plain prose without labels."""
        raw_desc = (a.get("description") or "").strip()
        if not raw_desc:
            return ""
        if not _is_nws(a):
            return _plain_desc(raw_desc)
        bullets = re.split(r"\*\s+", raw_desc)
        parts = []
        for chunk in bullets:
            chunk = re.sub(r"[ \t]+", " ", chunk).strip()
            if not chunk:
                continue
            chunk = re.sub(r"\.{2,}", ": ", chunk, count=1)
            parts.append(chunk)
        return "\n\n".join(parts)

    def _fmt_time(iso_str, fallback="Until Further Notice"):
        if not iso_str:
            return fallback
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(iso_str)
            time_str = dt.strftime("%I:%M %p").lstrip("0")
            return f"{dt.strftime('%m/%d')}  {time_str}"
        except Exception:
            return fallback

    if len(unique_alerts) == 1:
        a = unique_alerts[0]
        event_text = a["event"].upper()
        area_text = _area(a) or "ALL AREAS"
        headline_text = _headline(a)
        description_text = _description(a)
        description_paras = _description_paragraphs(a)
        instruction_text = _instruction(a)
        sender_text = a.get("sender") or "EAS Participant"
        effective_text = _fmt_time(a.get("effective"), fallback="Now")
    else:
        event_text = " / ".join(a["event"].upper() for a in unique_alerts)
        area_text = "; ".join(_area(a) for a in unique_alerts if _area(a)) or "ALL AREAS"
        headline_text = "   •••   ".join(_headline(a) for a in unique_alerts if _headline(a))
        description_text = "   •••   ".join(_description(a) for a in unique_alerts if _description(a))
        description_paras = "\n\n•••\n\n".join(_description_paragraphs(a) for a in unique_alerts if _description_paragraphs(a))
        instruction_text = "  ".join(_instruction(a) for a in unique_alerts if _instruction(a))
        sender_text = unique_alerts[0].get("sender") or "EAS Participant"
        effective_text = _fmt_time(unique_alerts[0].get("effective"), fallback="Now")

    expires_text = _fmt_time(unique_alerts[0].get("expires"))

    scroll_text = f"{area_text}.  Effective Until {expires_text}"
    if headline_text:
        scroll_text += f"     •••     {headline_text}"
    if description_text:
        scroll_text += f"     •••     {description_text}"

    # Plain-language sentence form for the TTS readout -- avoid the "|" / "•••"
    # scroll separators, which read aloud as awkward noise.
    def _sentence(s):
        s = (s or "").strip()
        if not s:
            return ""
        return s if s[-1] in ".!?" else s + "."

    spoken_parts = [_sentence(event_text), _sentence(area_text)]
    if headline_text:
        spoken_parts.append(_sentence(headline_text))
    if description_paras:
        spoken_parts.append(_sentence(description_paras.replace("\n\n", " ")))
    if instruction_text:
        spoken_parts.append(_sentence(instruction_text))
    spoken_text = " ".join(p for p in spoken_parts if p).strip()

    body_text = description_paras or headline_text or "No further details available."
    if instruction_text:
        body_text += "\n\n" + instruction_text

    return {
        "event": event_text, "area": area_text, "headline": headline_text,
        "description": description_text, "expires": expires_text,
        "effective": effective_text, "sender": sender_text,
        "scroll": scroll_text, "spoken": spoken_text, "body": body_text,
    }

def _eas_build_test_alert(mode, now_utc, area=None, originator=None):
    """Build a realistic Required Weekly/Monthly Test alert dict, worded the way
    a real EAS originator/encoder would -- e.g. "A Required Monthly Test has
    been issued for the following counties..." -- rather than a placeholder
    "Test Zone" message.

    mode: "weekly" -> RWT, anything else -> RMT
    area: the location text to announce (defaults to the configured area or a
          generic "this viewing area" when none is set)
    originator: the sender name to display (defaults to "EAS Participant",
          which is the standard originator code for station-originated tests)
    """
    is_weekly = (mode or "").lower() == "weekly"
    test_label = "Required Weekly Test" if is_weekly else "Required Monthly Test"
    sender = originator or "EAS Participant"
    location = (area or "").strip() or "this viewing area"

    # Worded like a genuine RWT/RMT script. RWTs are typically text/tone only
    # and brief; RMTs include the spoken "this is a test" announcement.
    if is_weekly:
        headline = (
            f"A {test_label} has been issued by {sender} for {location}. "
            f"This is a test. No action is required."
        )
        instruction = (
            "This is a Required Weekly Test of the Emergency Alert System. "
            "This concludes this test."
        )
    else:
        headline = (
            f"A {test_label} has been issued by {sender} for {location}. "
            f"This is only a test."
        )
        instruction = (
            "This is a Required Monthly Test of the Emergency Alert System, "
            "conducted by your local broadcasters and cable systems in "
            "cooperation with the Federal Communications Commission. "
            "If this had been an actual emergency, official messages would "
            "have followed the alert tone. This concludes this test."
        )

    return {
        "event":       test_label,
        "area":        location,
        "severity":    "Minor",
        "effective":   now_utc.isoformat(),
        "expires":     now_utc.isoformat(),   # caller overrides once duration is known
        "headline":    headline,
        "description": (
            f"A {test_label} of the Emergency Alert System is in effect for "
            f"{location} for the duration of this test message. "
            f"No action is required."
        ),
        "instruction": instruction,
        "sender":      sender,
        # SAME fields for dynamic tone generation: RWT/RMT event code, EAS
        # originator, and a few dummy county codes so the test button still
        # produces a realistic multi-county header burst rather than a single
        # entire-US code. (Real tests carry a short code list.)
        "event_code":  "RWT" if is_weekly else "RMT",
        "originator":  "EAS",
        "same_codes":  ["040013", "040027", "040109"],
    }

def _eas_write_alert(channel_id, unique_alerts, style="easyplus"):
    """Write the EAS overlay text files for the given style. Returns the shared
    texts dict (also needed for TTS) so callers don't have to recompute it."""
    _ensure_dirs()
    if not unique_alerts:
        return {}
    # One alert on screen, always -- matches _clone_and_inject_eas.
    unique_alerts = list(unique_alerts)[:1]
    texts = _eas_alert_texts(unique_alerts)

    if style == "dasdec":
        # DASDEC: static paginated character-generator block (dd_l*/dd_pg* files).
        block_lines = _eas_dasdec_block_lines(unique_alerts, texts)
        _atomic_write(f"eas_{channel_id}_dd_title.txt", "Emergency Alert Details")
        # Nominal duration -- the clone-time call writes the authoritative timing.
        _eas_dasdec_pages(channel_id, block_lines, 60.0)
    else:
        # EASyPlus: black takeover with a scrolling crawl (ep_* files).
        _atomic_write(f"eas_{channel_id}_ep_header.txt", "EMERGENCY ALERT SYSTEM")
        _atomic_write(f"eas_{channel_id}_ep_area.txt",   texts["scroll"])
        _atomic_write(f"eas_{channel_id}_ep_source.txt", texts.get("sender") or "EAS Participant")
        _atomic_write(f"eas_{channel_id}_ep_issued.txt", "Issued a")
        _atomic_write(f"eas_{channel_id}_ep_event.txt",  texts["event"])

    return texts

def _eas_clear(channel_id):
    _ensure_dirs()
    for suffix in ("ep_header", "ep_area", "ep_source", "ep_issued", "ep_event"):
        _atomic_write(f"eas_{channel_id}_{suffix}.txt", "")
    # Remove DASDEC per-line, page-counter, and title files.
    import glob
    for path in glob.glob(os.path.join(RUNTIME_DIR, f"eas_{channel_id}_dd_*.txt")):
        try:
            os.remove(path)
        except Exception:
            pass

def _restart_channel_stream(channel_uuid, label="", proactive=False):
    """Stop an active channel so clients reconnect with the updated stream profile.

    stop_channel() sets a Redis 'channel_stopping' key with a 60-second TTL.
    While that key exists, is_channel_teardown_active() returns True and Dispatcharr
    rejects ALL reconnect attempts before FFmpeg even starts — causing the 10-second
    stall loop. We delete the key after ~2s (enough for FFmpeg teardown to finish)
    so clients can reconnect immediately with the new profile.

    Note: the `proactive` parameter is accepted for call-site compatibility but is
    intentionally a no-op. Earlier attempts to proactively re-initialize the channel
    here (via ChannelService.initialize_channel) raced Dispatcharr's own teardown and
    wedged the channel into an endless "teardown active" 503 loop. The reliable
    approach — matching the original working EmergencyAlertarr — is to simply clear the gate and
    let the client reconnect on its own; the reconnect-grace buffer on the restore
    timer keeps the alert window open long enough to cover that reconnect.
    """
    import time as _time
    tag = f" [{label}]" if label else ""
    try:
        from apps.proxy.live_proxy.services.channel_service import ChannelService
        from apps.proxy.live_proxy.redis_keys import RedisKeys
        from apps.channels.models import RedisClient
        rc = RedisClient.get_client()
        meta_key = RedisKeys.channel_metadata(channel_uuid)
        deadline = _time.time() + 8.0
        while _time.time() < deadline:
            raw = rc.hget(meta_key, "state")
            state_val = (raw.decode() if isinstance(raw, bytes) else raw) if raw else ""
            if state_val == "active":
                break
            _time.sleep(0.25)
        result = ChannelService.stop_channel(channel_uuid)
        if result.get("status") != "success":
            return
        rc.delete(RedisKeys.channel_stopping(channel_uuid))
        meta_key = RedisKeys.channel_metadata(channel_uuid)
        state_raw = rc.hget(meta_key, "state")
        state_val = (state_raw.decode() if isinstance(state_raw, bytes) else state_raw) if state_raw else ""
        if state_val == "stopping":
            rc.hdel(meta_key, "state")
        logger.info(f"emergencyalertarr:{tag} channel {channel_uuid} reconnect gate cleared")
    except ImportError:
        logger.warning(f"emergencyalertarr:{tag} live_proxy unavailable — profile will apply on next client connect")
    except Exception as e:
        logger.debug(f"emergencyalertarr:{tag} restart {channel_uuid}: {e}")

def _restart_channel_stream_async(channel, label="", proactive=False):
    """Non-blocking wrapper — runs _restart_channel_stream in a daemon thread so the 2s sleep never blocks the caller."""
    import threading as _threading
    _threading.Thread(
        target=_restart_channel_stream,
        args=(str(channel.uuid), label, proactive),
        daemon=True,
    ).start()

def _eas_restart_channel(channel_uuid):
    _restart_channel_stream(channel_uuid, label="EAS")

_EMERGENCYALERTARR_EAS_BUILD = "2026-07-02 EAS-only private edition"

_EAS_MAX_SPOKEN_CHARS = 1400

_EAS_MAX_ALERT_SECS   = 240

_eas_active     = {}

_eas_lock       = threading.Lock()

_eas_seen_mem   = {"init": False, "ids": set()}

_TICKER_FIXED_LEN = 600

_EAS_REDIS_KEY    = f"emergencyalertarr:{_PLUGIN_KEY}:eas_result"

_EAS_REDIS_LOCK   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_poll_lock"

_EAS_STATE_KEY    = f"emergencyalertarr:{_PLUGIN_KEY}:eas_state"

_EAS_OWNER_KEY    = f"emergencyalertarr:{_PLUGIN_KEY}:eas_owner"

_EAS_ALERTS_KEY   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_alerts"

_EAS_ROTATION_KEY = f"emergencyalertarr:{_PLUGIN_KEY}:eas_rotation"

_EAS_GLOBAL_KEY   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_global_announced"

_EAS_SEEN_KEY     = f"emergencyalertarr:{_PLUGIN_KEY}:eas_seen"

_EAS_SEEN_TTL     = 6 * 3600

_EAS_SCHED_KEY    = f"emergencyalertarr:{_PLUGIN_KEY}:eas_sched_last"

_EAS_SCHED_LOCK   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_sched_lock"

_EAS_HISTORY_KEY  = f"emergencyalertarr:{_PLUGIN_KEY}:eas_history"

_EAS_HISTORY_FILE = os.path.join(_DATA_DIR, "eas_history.json")

_EAS_HISTORY_MAX  = 50

_EAS_CACHE_TTL    = 50

_EAS_RECONNECT_GRACE = 8.0

_EAS_POST_RESUME_GRACE = 3.0

_EAS_MAX_RECONNECT_WAIT = 90.0

_EAS_TICK_SECS = 3

_EAS_LOGGED_CHANSERVICE_API = False

def _eas_history_read():
    """Return the recent-alert history as a list (most recent first). Prefers
    Redis, falls back to the on-disk JSON file so history survives restarts
    even without Redis."""
    rc = _get_redis_client()
    if rc:
        try:
            raw = rc.get(_EAS_HISTORY_KEY)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    try:
        if os.path.exists(_EAS_HISTORY_FILE):
            with open(_EAS_HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _eas_history_add(event, area, channels, kind="alert", severity=""):
    """Append one entry to the alert history (kept to _EAS_HISTORY_MAX, most
    recent first). kind is 'alert', 'test', or 'manual'. Best-effort: never
    raises, since logging history must never break an actual alert firing."""
    try:
        from datetime import datetime, timezone
        entry = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "event":    event or "Alert",
            "area":     (area or "")[:120],
            "channels": channels if isinstance(channels, list) else [channels],
            "kind":     kind,
            "severity": severity or "",
        }
        hist = _eas_history_read()
        # Light dedup: collapse an identical event+kind fired within 5s (e.g.
        # the same alert hitting several channels in one sweep) into one entry
        # with the channel list merged, rather than spamming the log.
        if hist:
            from datetime import datetime as _dt
            try:
                prev = hist[0]
                same = (prev.get("event") == entry["event"] and prev.get("kind") == entry["kind"])
                dt_prev = _dt.fromisoformat(prev["ts"])
                dt_now = _dt.fromisoformat(entry["ts"])
                if same and abs((dt_now - dt_prev).total_seconds()) <= 5:
                    merged = list(dict.fromkeys((prev.get("channels") or []) + entry["channels"]))
                    prev["channels"] = merged
                    prev["ts"] = entry["ts"]
                    hist[0] = prev
                    _eas_history_write(hist)
                    return
            except Exception:
                pass
        hist.insert(0, entry)
        hist = hist[:_EAS_HISTORY_MAX]
        _eas_history_write(hist)
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] EAS: failed to record history: {e}")

def _eas_history_write(hist):
    """Persist history to Redis and the disk fallback. Best-effort."""
    rc = _get_redis_client()
    if rc:
        try:
            rc.set(_EAS_HISTORY_KEY, json.dumps(hist))
        except Exception:
            pass
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = _EAS_HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f)
        os.replace(tmp, _EAS_HISTORY_FILE)
    except Exception:
        pass

def _eas_history_clear():
    rc = _get_redis_client()
    if rc:
        try:
            rc.delete(_EAS_HISTORY_KEY)
        except Exception:
            pass
    try:
        if os.path.exists(_EAS_HISTORY_FILE):
            os.remove(_EAS_HISTORY_FILE)
    except Exception:
        pass

def _eas_sequence_duration(silence_secs):
    """Total seconds the EAS WAV sequence plays: header + att + silence + eom.
    Falls back to silence_secs + 30 if WAVs are not present yet."""
    if not _eas_wav_available():
        return silence_secs + 30
    return (
        _wav_duration_secs(_EAS_WAV_HEADER)
        + _wav_duration_secs(_EAS_WAV_ATT)
        + silence_secs
        + _wav_duration_secs(_EAS_WAV_EOM)
    )

def _eas_alert_fingerprint(alerts):
    """Stable string key for a set of alerts. Changes when alert IDs change --
    a new or different alert gets a new fingerprint and triggers a fresh
    broadcast even if NWS is still returning active alerts."""
    ids = sorted(a.get("id", "") for a in alerts)
    return "|".join(ids) if ids else ""

def _eas_seen_write(rc, ids):
    """Persist the set of already-announced alert IDs (Redis, or in-process
    fallback). Refreshes the TTL so the set stays alive as long as sweeps run."""
    clean = sorted(x for x in ids if x)
    if rc:
        try:
            rc.setex(_EAS_SEEN_KEY, _EAS_SEEN_TTL, json.dumps(clean))
            return
        except Exception:
            pass
    _eas_seen_mem["ids"] = set(clean)
    _eas_seen_mem["init"] = True

def _eas_streaming_ids(rc=None):
    """Set of channel IDs currently being streamed (Redis channel_stream:* keys)."""
    rc = rc or _get_redis_client()
    ids = set()
    if rc:
        try:
            for k in rc.keys("channel_stream:*"):
                ids.add((k if isinstance(k, str) else k.decode()).split(":")[-1])
        except Exception:
            pass
    return ids

def _eas_do_restore(cid, mapping, mappings):
    """Restore one channel's passthrough profile and clear all EAS state."""
    from apps.channels.models import Channel as _Ch
    channel = _Ch.objects.filter(id=int(cid)).first()
    eas_pid = mapping.get("eas_profile_id")
    if channel:
        _restore_profile(channel, mapping.get("original_profile_id"))
        _restart_channel_stream_async(channel, label="EAS")
    if eas_pid:
        _delete_cloned_profile(eas_pid)
    for k in ("eas_profile_id", "eas_restore_at", "eas_await_stream",
              "eas_seen_down", "eas_seq_secs"):
        mapping.pop(k, None)
    mappings[cid] = mapping
    _eas_clear(cid)
    with _eas_lock:
        _eas_active.pop(cid, None)
    logger.info(f"[EmergencyAlertarr] EAS: sequence complete, restored ch {cid}")

def _eas_process_restores(mappings, streaming_ids, now_ts):
    """Anchor each pending alert's restore countdown to actual stream resumption
    (wait for the channel to go DOWN after firing, then come back UP), and
    restore any channel whose sequence has finished. Returns True if changed.

    This fixes the timing bug where the restore counted from fire time: the
    channel-stop -> client-reconnect gap is highly variable (20s+), so counting
    from fire could restore before the overlay was ever seen. We instead start
    the countdown the moment the viewer's stream is actually back on screen."""
    changed = False
    pending = [c for c, m in mappings.items()
               if m.get("eas_await_stream") or m.get("eas_restore_at")]
    for cid in pending:
        mapping = mappings.get(cid, {})
        restore_at = mapping.get("eas_restore_at")
        if mapping.get("eas_await_stream"):
            if cid not in streaming_ids:
                # Channel has gone down as expected after the profile swap.
                if not mapping.get("eas_seen_down"):
                    mapping["eas_seen_down"] = True
                    mappings[cid] = mapping
                    changed = True
            elif mapping.get("eas_seen_down"):
                # Stream is back UP with the overlay -> start the real countdown.
                seq = float(mapping.get("eas_seq_secs") or 30.0)
                mapping["eas_restore_at"] = now_ts + seq + _EAS_POST_RESUME_GRACE
                mapping.pop("eas_await_stream", None)
                mapping.pop("eas_seen_down", None)
                mappings[cid] = mapping
                changed = True
                logger.info(
                    f"[EmergencyAlertarr] EAS: overlay live on ch {cid} -- "
                    f"restoring in {seq + _EAS_POST_RESUME_GRACE:.0f}s"
                )
                continue
            # SAFETY NET: a channel that fired but never got a viewer never goes
            # down/up, so it would otherwise stay armed forever. Once the safety
            # deadline (set at fire time) passes, restore it anyway so a test or
            # alert can never get stuck on an unwatched channel.
            if restore_at and now_ts >= float(restore_at):
                try:
                    _eas_do_restore(cid, mapping, mappings)
                    changed = True
                    logger.info(f"[EmergencyAlertarr] EAS: safety-restored ch {cid} (viewer never returned)")
                except Exception as e:
                    logger.error(f"[EmergencyAlertarr] EAS: safety restore failed ch {cid}: {e}", exc_info=True)
            continue
        if restore_at and now_ts >= float(restore_at):
            try:
                _eas_do_restore(cid, mapping, mappings)
                changed = True
            except Exception as e:
                logger.error(f"[EmergencyAlertarr] EAS: timed restore failed ch {cid}: {e}", exc_info=True)
    return changed

def _eas_restore_tick():
    """Fast, network-free restore check run every few seconds between full alert
    polls, so overlay timing is accurate (anchored to stream resumption and
    restored promptly) even when the poll interval is long."""
    try:
        mappings = _get_mappings()
        if not mappings or not any(
            m.get("eas_await_stream") or m.get("eas_restore_at")
            for m in mappings.values()
        ):
            return
        rc = _get_redis_client()
        # Light lock so only one worker runs the tick per window (avoids N workers
        # racing on restores every few seconds).
        if rc and not rc.set("emergencyalertarr:eas_tick_lock", "1", nx=True, ex=_EAS_TICK_SECS):
            return
        if _eas_process_restores(mappings, _eas_streaming_ids(rc), time.time()):
            _save_mappings(mappings)
    except Exception as e:
        logger.error(f"[EmergencyAlertarr] EAS restore tick error: {e}", exc_info=True)

def _eas_arm_restore(mapping, seq_duration, now):
    """Arm a just-fired alert so its restore counts from when the overlay is
    actually live on screen: mark it awaiting stream resumption, then count
    seq_duration once the stream is back. A generous safety timeout restores
    anyway if the viewer never reconnects."""
    mapping["eas_seq_secs"] = float(seq_duration)
    mapping["eas_await_stream"] = True
    mapping["eas_seen_down"] = False
    mapping["eas_restore_at"] = float(now) + _EAS_MAX_RECONNECT_WAIT + float(seq_duration)

def _eas_sweep():
    settings = _get_settings()
    zones_raw = (settings.get("eas_zones") or "").strip()
    zones = [z.strip() for z in zones_raw.split(",") if z.strip()]
    # Determine which sources are actually configured. IPAWS can run with no NWS
    # zones at all, so "no zones" alone doesn't mean nothing is active.
    _source = (settings.get("eas_source") or "nws").lower()
    _ipaws_on = _source in ("ipaws", "both") and bool((settings.get("eas_ipaws_same_codes") or "").strip())
    _nws_on = _source in ("nws", "both") and bool(zones)
    # Nothing configured — still need to clear any channels left in active EAS state
    # (handles the source being removed while an alert was active)
    if not _nws_on and not _ipaws_on:
        rc = _get_redis_client()
        current_state = {}
        if rc:
            try:
                raw = rc.get(_EAS_STATE_KEY)
                if raw:
                    current_state = json.loads(raw)
            except Exception:
                pass
        if not any(v for v in current_state.values()):
            return  # nothing active, nothing to clear
        mappings = _get_mappings()
        from apps.channels.models import Channel
        from core.models import StreamProfile
        for cid, event in list(current_state.items()):
            if not event:
                continue
            try:
                mapping = mappings.get(cid, {}) or {}
                channel = Channel.objects.filter(id=int(cid)).first()
                if channel:
                    _restore_profile(channel, mapping.get("original_profile_id"))
                    eas_pid = mapping.get("eas_profile_id") or mapping.get("ticker_profile_id")
                    if eas_pid:
                        _delete_cloned_profile(eas_pid)
                    mapping.pop("eas_profile_id", None)
                    mapping.pop("ticker_profile_id", None)
                    mappings[cid] = mapping
                    _eas_clear(cid)
                    _restart_channel_stream_async(channel, label="EAS")
                    logger.info(f"[EmergencyAlertarr] EAS: cleared ch {cid} — no zones configured")
            except Exception as e:
                logger.error(f"[EmergencyAlertarr] EAS: clear failed ch {cid}: {e}")
        _save_mappings(mappings)
        if rc:
            try:
                rc.delete(_EAS_STATE_KEY)
                rc.delete(_EAS_GLOBAL_KEY)
            except Exception:
                pass
        return
    severity_threshold = settings.get("eas_severity_filter") or "Moderate"
    try:
        silence_secs = max(10, min(20, int(settings.get("eas_silence_secs") or 15)))
    except Exception:
        silence_secs = 15
    mappings = _get_mappings()
    eas_cids = [cid for cid, m in mappings.items() if m and (m.get("type") == "eas" or m.get("eas_armed"))]
    if not eas_cids:
        return

    # One worker polls NWS; all others read from Redis cache.
    alerts = None
    rc = _get_redis_client()
    try:
        if rc:
            cached = rc.get(_EAS_REDIS_KEY)
            if cached:
                alerts = json.loads(cached)
            else:
                lock_acquired = rc.set(_EAS_REDIS_LOCK, "1", nx=True, ex=30)
                if lock_acquired:
                    try:
                        alerts = _fetch_all_alerts(settings, zones, severity_threshold)
                        rc.setex(_EAS_REDIS_KEY, _EAS_CACHE_TTL, json.dumps(alerts))
                    except Exception as e:
                        logger.warning(f"[EmergencyAlertarr] EAS: alert fetch failed: {e}")
                        return
                else:
                    return  # another worker is fetching right now
        else:
            alerts = _fetch_all_alerts(settings, zones, severity_threshold)
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] EAS: alert fetch failed: {e}")
        return

    # Sort all active alert RECORDS worst-first. Each NWS record is a single
    # alert (its own event + its own areaDesc); we show one record at a time and
    # never merge multiple records into a combined "ALERT / ALERT / ALERT + every
    # county" screen. Extra records queue and play one per channel restore cycle
    # via the seen-set logic below.
    all_alerts = sorted(alerts or [], key=lambda a: _EAS_SEV.get(a["severity"], 0), reverse=True)

    # Read persisted alert state (shared across all workers via Redis).
    current_state = {}
    if rc:
        try:
            raw = rc.get(_EAS_STATE_KEY)
            if raw:
                current_state = json.loads(raw)
        except Exception:
            pass

    # Read the last alert fingerprint announced system-wide (across ALL channels,
    # not just this one). When every channel is EAS-armed, per-channel memory alone
    # means each channel gets its own "first time seeing this alert" announcement --
    # this global gate stops a switch to a not-yet-watched channel from re-announcing
    # an alert that's already been shown elsewhere.
    global_announced = None
    if rc:
        try:
            global_announced = rc.get(_EAS_GLOBAL_KEY)
            if isinstance(global_announced, bytes):
                global_announced = global_announced.decode()
        except Exception:
            pass

    # Channels currently being streamed (Redis channel_stream:{id} key exists).
    # EAS only activates on channels with an active viewer; clears always run so
    # profiles are restored even if the viewer stopped watching mid-alert.
    streaming_ids = set()
    if rc:
        try:
            for k in rc.keys("channel_stream:*"):
                streaming_ids.add((k if isinstance(k, str) else k.decode()).split(":")[-1])
        except Exception:
            pass

    now_ts = time.time()

    # --- Timed restore + stream-resume anchoring ---------------------------
    # Anchors each pending alert's restore countdown to actual stream resumption
    # and restores finished channels. Runs every sweep (and also on the fast tick
    # between sweeps) so restores survive worker reloads without daemon threads.
    restore_changed = _eas_process_restores(mappings, streaming_ids, now_ts)
    if restore_changed:
        _save_mappings(mappings)
        # Reload so the transition check below sees the cleared eas_restore_at
        mappings = _get_mappings()

    # --- One-at-a-time queue with a system-wide "seen" set -------------------
    # We keep a set of alert IDs already announced. Each free, streaming channel
    # takes the single worst not-yet-announced alert and shows it alone; the rest
    # stay queued and play one per restore cycle. On the FIRST poll after a
    # (re)start the whole current backlog is marked seen, so only alerts issued
    # afterward ever fire (no flood of pre-existing alerts on startup).
    active_id_set = {a.get("id", "") for a in all_alerts if a.get("id")}

    # Acquire the owner lock BEFORE reading/deciding so the read-decide-write is
    # atomic across Dispatcharr's multiple worker processes.
    if rc and not rc.set(_EAS_OWNER_KEY, "1", nx=True, ex=120):
        return

    # Re-read shared alert state fresh now that we hold the lock.
    if rc:
        try:
            raw = rc.get(_EAS_STATE_KEY)
            current_state = json.loads(raw) if raw else {}
        except Exception:
            pass

    # Read (or, on first poll, seed) the seen-set.
    first_run = False
    seen_ids = set()
    if rc:
        try:
            raw_seen = rc.get(_EAS_SEEN_KEY)
            if raw_seen is None:
                first_run = True
            else:
                seen_ids = set(json.loads(raw_seen) or [])
        except Exception:
            seen_ids = set()
    else:
        first_run = not _eas_seen_mem["init"]
        seen_ids = set(_eas_seen_mem["ids"])

    if first_run:
        # Invalidate the existing backlog: everything active right now counts as
        # already announced, so nothing fires until a genuinely new alert arrives.
        _eas_seen_write(rc, active_id_set)
        if active_id_set:
            logger.info(
                f"[EmergencyAlertarr] EAS: first poll -- {len(active_id_set)} pre-existing "
                "alert(s) invalidated; only newly issued alerts will be shown"
            )
        if rc:
            try:
                rc.delete(_EAS_OWNER_KEY)
            except Exception:
                pass
        return

    # Prune IDs that are no longer active (keeps the set bounded; NWS IDs are
    # unique per issuance so a pruned ID never legitimately reappears), and
    # refresh the TTL every sweep so the set stays alive while polling continues.
    seen_ids &= active_id_set
    _eas_seen_write(rc, seen_ids)

    # The queue: active records not yet announced, worst-first.
    queue = [a for a in all_alerts if a.get("id") and a.get("id") not in seen_ids]

    # Assign the next queued alert to each free, streaming channel. Distinct free
    # channels take distinct alerts; anything left over waits for the next cycle.
    transitions = {}   # cid -> ("fire", alert_record) | ("clear", None)
    pending = list(queue)
    for cid in eas_cids:
        mapping = mappings.get(cid, {})
        if mapping.get("eas_restore_at"):
            continue   # busy playing an alert; picks the next one when it restores
        if mapping.get("eas_profile_id"):
            # Leftover EAS profile with no restore scheduled: clean up only when
            # NWS has nothing active, and never disturb a test overlay.
            if not active_id_set and current_state.get(cid) != "__test__":
                transitions[cid] = ("clear", None)
            continue
        if cid not in streaming_ids:
            continue   # nobody watching -- don't activate
        if pending:
            transitions[cid] = ("fire", pending.pop(0))

    if not transitions:
        if rc:
            try:
                rc.delete(_EAS_OWNER_KEY)
            except Exception:
                pass
        return

    from apps.channels.models import Channel
    from core.models import StreamProfile

    new_state = dict(current_state)
    changed = False

    for cid, (action, alert) in transitions.items():
        mapping = mappings.get(cid, {})
        try:
            channel = Channel.objects.filter(id=int(cid)).first()
            if not channel:
                continue

            if action == "fire":
                # One alert record -> one clean single-alert overlay. Clone a
                # fresh EAS profile and schedule the timed restore.
                if mapping.get("ticker_profile_id"):
                    _restore_profile(channel, mapping.get("original_profile_id"))
                    _delete_cloned_profile(mapping["ticker_profile_id"])
                    mapping.pop("ticker_profile_id", None)

                if mapping.get("eas_profile_id"):
                    _delete_cloned_profile(mapping["eas_profile_id"])
                    mapping.pop("eas_profile_id", None)

                orig = StreamProfile.objects.filter(id=mapping.get("original_profile_id")).first()
                if not orig:
                    logger.warning(f"[EmergencyAlertarr] EAS: original profile missing for ch {cid}")
                    continue

                single = [alert]   # exactly one alert on screen -- never combined

                transcode_mode = settings.get("eas_transcode_mode") or "full"
                overlay_style = (settings.get("eas_overlay_style") or "easyplus").lower()
                use_tts = bool(settings.get("eas_tts_enabled", True))
                generate_tones = bool(settings.get("eas_generate_tones", False))
                try:
                    att_secs = max(4.0, min(30.0, float(settings.get("eas_att_secs") or 8)))
                except Exception:
                    att_secs = 8.0
                _ep_font, _ep_scale = _easyplus_settings(settings)
                _lead_in = float(settings.get("eas_lead_in_secs") or 0)
                eas_profile, _, seq_duration = _clone_and_inject_eas(
                    channel.id, orig, channel.name, silence_secs, transcode_mode,
                    style=overlay_style, unique_alerts=single, use_tts=use_tts,
                    generate_tones=generate_tones, att_secs=att_secs,
                    easyplus_font=_ep_font, easyplus_scale=_ep_scale,
                    lead_in_secs=_lead_in,
                )
                _assign_profile(channel, eas_profile)
                _eas_write_alert(cid, single, style=overlay_style)

                mapping["eas_profile_id"] = eas_profile.id
                _eas_arm_restore(mapping, seq_duration, now_ts)
                mappings[cid] = mapping
                seen_ids.add(alert.get("id", ""))
                new_state[cid] = alert.get("id", "") or "EAS"
                still_queued = len([a for a in queue if a.get("id") not in seen_ids])
                logger.info(
                    f"[EmergencyAlertarr] EAS ALERT: {alert.get('event')} -- "
                    f"{(alert.get('area') or '')[:60]} (ch {cid}, "
                    f"restore in {seq_duration:.0f}s"
                    + (f", {still_queued} more queued)" if still_queued else ")")
                )
                _eas_history_add(
                    alert.get("event") or "Alert", alert.get("area") or "",
                    channel.name, kind="alert", severity=alert.get("severity") or "",
                )
                _restart_channel_stream_async(channel, label="EAS", proactive=True)

                with _eas_lock:
                    _eas_active[cid] = alert.get("event") or "EAS"

            else:  # action == "clear"
                eas_pid = mapping.get("eas_profile_id") or mapping.get("ticker_profile_id")
                _restore_profile(channel, mapping.get("original_profile_id"))
                if eas_pid:
                    _delete_cloned_profile(eas_pid)
                mapping.pop("eas_profile_id", None)
                mapping.pop("ticker_profile_id", None)
                mapping.pop("eas_restore_at", None)
                mappings[cid] = mapping
                _eas_clear(cid)
                new_state[cid] = None
                logger.info(f"[EmergencyAlertarr] EAS: alert cleared -- ch {cid}")
                _restart_channel_stream_async(channel, label="EAS")

                with _eas_lock:
                    _eas_active.pop(cid, None)

            changed = True

        except Exception as e:
            logger.error(f"[EmergencyAlertarr] EAS: transition failed ch {cid}: {e}", exc_info=True)

    # Persist the seen-set including the IDs just announced.
    _eas_seen_write(rc, seen_ids & active_id_set if active_id_set else seen_ids)

    if changed:
        _save_mappings(mappings)
        if rc:
            try:
                rc.setex(_EAS_STATE_KEY, 3600, json.dumps(new_state))
            except Exception:
                pass

    if rc:
        try:
            rc.delete(_EAS_OWNER_KEY)
        except Exception:
            pass

def _eas_sweep_loop(stop_event):
    logger.info("[EmergencyAlertarr] EAS module initialized.")
    logger.info(f"[EmergencyAlertarr] EAS build: {_EMERGENCYALERTARR_EAS_BUILD}")
    while not stop_event.is_set():
        interval = 60
        try:
            try:
                interval = max(15, int((_get_settings().get("eas_poll_interval") or 60)))
            except Exception:
                pass
            _eas_sweep()
        except Exception as e:
            logger.error(f"[EmergencyAlertarr] EAS loop error: {e}", exc_info=True)
        finally:
            try:
                from django.db import connection
                connection.close()
            except Exception:
                pass
        # Sleep until the next full poll, but run a fast, network-free restore
        # check every few seconds so the overlay window is timed accurately
        # (anchored to stream resumption) instead of only once per poll interval.
        slept = 0
        while slept < interval and not stop_event.is_set():
            stop_event.wait(timeout=_EAS_TICK_SECS)
            slept += _EAS_TICK_SECS
            try:
                _eas_restore_tick()
            except Exception:
                pass

def _get_mappings():
    try:
        if os.path.exists(MAPPINGS_FILE):
            with open(MAPPINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"emergencyalertarr: failed to read mappings: {e}")
    return {}

def _eas_scheduled_test_due(settings, now_local, last_key):
    """Decide whether a scheduled Required Weekly/Monthly Test should fire right
    now. Returns a window-key string (unique per scheduled occurrence) if due
    and not already fired, else None.

    Settings used:
      eas_test_schedule: "off" | "weekly" | "monthly"
      eas_test_day:      weekly -> 0..6 (Mon..Sun); monthly -> 1..28 day-of-month
      eas_test_hour:     0..23 (local hour the test fires)
    """
    mode = (settings.get("eas_test_schedule") or "off").lower()
    if mode not in ("weekly", "monthly"):
        return None
    try:
        hour = int(settings.get("eas_test_hour", 12))
    except Exception:
        hour = 12
    if now_local.hour != hour:
        return None
    if mode == "weekly":
        try:
            day = int(settings.get("eas_test_day", 2))  # default Wednesday
        except Exception:
            day = 2
        if now_local.weekday() != day:
            return None
        # One window per ISO week+hour
        window_key = f"weekly-{now_local.isocalendar()[0]}-{now_local.isocalendar()[1]}-{hour}"
    else:  # monthly
        try:
            dom = int(settings.get("eas_test_day", 1))
        except Exception:
            dom = 1
        if now_local.day != dom:
            return None
        window_key = f"monthly-{now_local.year}-{now_local.month}-{hour}"
    if window_key == last_key:
        return None
    return window_key

def _eas_fire_scheduled_test():
    """Fire a Required Weekly/Monthly Test across all EAS-armed channels that
    currently have a viewer, exactly like a real station's automated RWT/RMT.
    Reuses the same clone/overlay/restore machinery as the sweep."""
    from apps.channels.models import Channel
    from core.models import StreamProfile
    from datetime import datetime, timedelta, timezone as _tz

    settings = _get_settings()
    mappings = _get_mappings()
    rc = _get_redis_client()
    silence_secs   = max(10, min(20, int(settings.get("eas_silence_secs") or 15)))
    transcode_mode = settings.get("eas_transcode_mode") or "full"
    overlay_style  = (settings.get("eas_overlay_style") or "easyplus").lower()
    use_tts        = bool(settings.get("eas_tts_enabled", True))
    mode = (settings.get("eas_test_schedule") or "off").lower()
    test_area = (settings.get("eas_test_area") or "").strip() or None
    endec_test = bool(settings.get("eas_endec_test_mode", True))
    generate_tones = bool(settings.get("eas_generate_tones", False))
    try:
        att_secs = max(4.0, min(30.0, float(settings.get("eas_att_secs") or 8)))
    except Exception:
        att_secs = 8.0
    try:
        tail_secs = max(1.0, min(30.0, float(settings.get("eas_endec_tail_secs") or 7.5)))
    except Exception:
        tail_secs = 7.5

    # Which channels currently have a viewer (same gate the sweep uses).
    streaming_ids = set()
    if rc:
        try:
            for k in rc.keys("channel_stream:*"):
                streaming_ids.add((k if isinstance(k, str) else k.decode()).split(":")[-1])
        except Exception:
            pass

    fired = 0
    now_utc = datetime.now(_tz.utc)
    for cid, mapping in list(mappings.items()):
        if not (mapping.get("type") == "eas" or mapping.get("eas_armed")):
            continue
        if mapping.get("eas_profile_id") or mapping.get("eas_restore_at"):
            continue  # already mid-alert
        if cid not in streaming_ids:
            continue  # nobody watching this one
        if not mapping.get("original_profile_id"):
            continue
        channel = Channel.objects.filter(id=int(cid)).first()
        if not channel:
            continue
        orig = StreamProfile.objects.filter(id=mapping.get("original_profile_id")).first()
        if not orig:
            continue
        fake_alerts = [_eas_build_test_alert(mode, now_utc, area=test_area)]
        try:
            _ep_font, _ep_scale = _easyplus_settings(settings)
            _lead_in = float(settings.get("eas_lead_in_secs") or 0)
            eas_profile, _, seq_duration = _clone_and_inject_eas(
                channel.id, orig, channel.name, silence_secs, transcode_mode,
                style=overlay_style, unique_alerts=fake_alerts, use_tts=use_tts,
                endec_test=endec_test, tail_secs=tail_secs,
                generate_tones=generate_tones, att_secs=att_secs,
                easyplus_font=_ep_font, easyplus_scale=_ep_scale,
                lead_in_secs=_lead_in,
            )
            _assign_profile(channel, eas_profile)
            fake_alerts[0]["expires"] = (now_utc + timedelta(seconds=seq_duration)).isoformat()
            _eas_write_alert(cid, fake_alerts, style=overlay_style)
            mapping["eas_profile_id"] = eas_profile.id
            _eas_arm_restore(mapping, seq_duration, time.time())
            mappings[cid] = mapping
            if rc:
                try:
                    raw_st = rc.get(_EAS_STATE_KEY)
                    st = json.loads(raw_st) if raw_st else {}
                    st[cid] = "__test__"
                    rc.setex(_EAS_STATE_KEY, 3600, json.dumps(st))
                except Exception:
                    pass
            _restart_channel_stream_async(channel, label="EAS", proactive=True)
            fired += 1
            _eas_history_add(
                fake_alerts[0].get("event"), fake_alerts[0].get("area"),
                channel.name, kind="test",
            )
        except Exception as e:
            logger.error(f"[EmergencyAlertarr] EAS scheduled test failed for ch {cid}: {e}", exc_info=True)

    if fired:
        _save_mappings(mappings)
    label = "Required Weekly Test" if mode == "weekly" else "Required Monthly Test"
    logger.info(f"[EmergencyAlertarr] EAS {label}: fired on {fired} channel(s)")
    return fired

def _eas_scheduler_loop(stop_event):
    """Background loop that checks once a minute whether a scheduled RWT/RMT is
    due, and fires it (once, via a Redis lock so only one worker does)."""
    while not stop_event.is_set():
        try:
            settings = _get_settings()
            if (settings.get("eas_test_schedule") or "off").lower() in ("weekly", "monthly"):
                from datetime import datetime
                # Local time per the OS/container timezone.
                now_local = datetime.now()
                rc = _get_redis_client()
                last_key = None
                if rc:
                    try:
                        last_key = rc.get(_EAS_SCHED_KEY)
                        if isinstance(last_key, bytes):
                            last_key = last_key.decode()
                    except Exception:
                        pass
                window_key = _eas_scheduled_test_due(settings, now_local, last_key)
                if window_key:
                    # Only one worker fires the scheduled test.
                    if not rc or rc.set(_EAS_SCHED_LOCK, "1", nx=True, ex=300):
                        if rc:
                            try:
                                rc.set(_EAS_SCHED_KEY, window_key)
                            except Exception:
                                pass
                        logger.info(f"[EmergencyAlertarr] EAS scheduled test window reached: {window_key}")
                        _eas_fire_scheduled_test()
        except Exception as e:
            logger.error(f"[EmergencyAlertarr] EAS scheduler error: {e}", exc_info=True)
        finally:
            try:
                from django.db import connection
                connection.close()
            except Exception:
                pass
        stop_event.wait(timeout=60)

def _save_mappings(mappings):
    global _uuid_map_cache
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = MAPPINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mappings, f, indent=2)
        os.replace(tmp, MAPPINGS_FILE)
        _uuid_map_cache = {"map": {}, "fetched_at": 0}  # force refresh on next scan
    except Exception as e:
        logger.error(f"emergencyalertarr: failed to save mappings: {e}")

def _get_settings():
    from apps.plugins.models import PluginConfig
    config = PluginConfig.objects.filter(key=_PLUGIN_KEY).first()
    if not config or not config.settings:
        return {}
    settings = dict(config.settings)
    settings.pop("channel_mappings", None)
    settings.pop("channel_cache", None)
    return settings

_scheduler_thread = None

_stop_event = threading.Event()

_redis_client_cache = None

_redis_client_lock = threading.Lock()

STALE_THRESHOLD   = 120

STALE_BATCH_SIZE  = 10

_uuid_map_cache = {"map": {}, "fetched_at": 0}

UUID_MAP_TTL = 300

SWEEP_LOCK_KEY   = "emergencyalertarr:sweep_lock"

FAST_LOCK_KEY    = "emergencyalertarr:fast_lock"

_SWEEP_LOCK_TTL  = 45

_FAST_LOCK_TTL   = 10

_IDLE_RESTORE_DELAY = 30

def _get_redis_client():
    global _redis_client_cache
    with _redis_client_lock:
        if _redis_client_cache is not None:
            try:
                _redis_client_cache.ping()
                return _redis_client_cache
            except Exception:
                _redis_client_cache = None
        try:
            from django_redis import get_redis_connection
            rc = get_redis_connection("default")
            rc.ping()
            _redis_client_cache = rc
            return rc
        except Exception:
            pass
        try:
            from django.conf import settings as _settings
            import redis as _redis
            url = (getattr(_settings, "REDIS_URL", None)
                   or getattr(_settings, "CACHES", {}).get("default", {}).get("LOCATION")
                   or "redis://redis:6379/0")
            rc = _redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
            rc.ping()
            _redis_client_cache = rc
            return rc
        except Exception:
            pass
        return None

def _redis_lock_acquire_or_refresh(rc, key, ttl):
    """Acquire or renew a Redis distributed lock for this process.
    Returns True if this worker holds the lock, False if another worker holds it.
    On first call uses NX-set; on subsequent calls by the same pid, refreshes TTL."""
    my_pid = str(os.getpid())
    if rc.set(key, my_pid, nx=True, ex=ttl):
        return True
    current = rc.get(key)
    if current and current.decode() == my_pid:
        rc.expire(key, ttl)
        return True
    return False

def _get_uuid_to_id_map(mappings):
    """Returns {uuid_str: int_channel_id} for all currently mapped channels.
    Cached for UUID_MAP_TTL seconds to avoid hitting the DB on every 2s tick."""
    global _uuid_map_cache
    now = time.time()
    if now - _uuid_map_cache["fetched_at"] < UUID_MAP_TTL and _uuid_map_cache["map"]:
        return _uuid_map_cache["map"]
    try:
        from apps.channels.models import Channel
        mapped_ids = set()
        for cid in mappings.keys():
            try:
                mapped_ids.add(int(cid))
            except (ValueError, TypeError):
                pass
        result = {}
        for ch in Channel.objects.filter(id__in=mapped_ids):
            uuid_val = getattr(ch, "uuid", None)
            if uuid_val:
                result[str(uuid_val).lower()] = ch.id
        _uuid_map_cache = {"map": result, "fetched_at": now}
        logger.debug(f"emergencyalertarr: uuid map refreshed — {len(result)} entries")
        return result
    except Exception as e:
        logger.debug(f"emergencyalertarr: uuid map error: {e}")
        return _uuid_map_cache.get("map", {})
    finally:
        # Close the thread-local DB connection so it doesn't sit open indefinitely.
        # Background threads are never part of Django's request/response cycle, so
        # connections are never automatically cleaned up without this.
        try:
            from django.db import connection
            connection.close()
        except Exception:
            pass

def _redis_scan_active():
    """Scan Redis for ts_proxy:channel:{UUID}:activity keys. Returns set of
    integer channel IDs with active streams, or None if Redis is unavailable."""
    rc = _get_redis_client()
    if rc is None:
        return None
    mappings = _get_mappings()
    if not mappings:
        return set()
    uuid_to_id = _get_uuid_to_id_map(mappings)
    if not uuid_to_id:
        return set()
    active = set()
    try:
        # v0.25+ uses live:channel:*:activity; v0.24 used ts_proxy:channel:*:activity
        for pattern in ("live:channel:*:activity", "ts_proxy:channel:*:activity"):
            for raw_key in rc.scan_iter(pattern, count=200):
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                parts = key.split(":")
                if len(parts) < 4:
                    continue
                uuid = parts[2].lower()
                cid = uuid_to_id.get(uuid)
                if cid is not None:
                    active.add(cid)
    except Exception as e:
        logger.debug(f"emergencyalertarr: Redis scan error: {e}")
        return None
    return active

def _poll_loop(stop_event):
    # EAS sweep -- NWS alert polling, interval configurable (default 60s)
    eas_t = threading.Thread(target=_eas_sweep_loop, args=(stop_event,), daemon=True)
    eas_t.start()
    # RWT/RMT scheduler -- fires a Required Weekly/Monthly Test on schedule
    sched_t = threading.Thread(target=_eas_scheduler_loop, args=(stop_event,), daemon=True)
    sched_t.start()
    eas_t.join()
    sched_t.join()

class Plugin:
    @property
    def fields(self):
        try:
            return self._build_fields()
        except Exception as e:
            logger.error(f"emergencyalertarr: _build_fields failed: {e}", exc_info=True)
            return [{"id": "_error", "type": "info", "label": f"EmergencyAlertarr error: {e}"}]

    def __init__(self):
        global _scheduler_thread, _stop_event
        if _scheduler_thread is None or not _scheduler_thread.is_alive():
            _stop_event = threading.Event()
            _scheduler_thread = threading.Thread(
                target=_poll_loop,
                args=(_stop_event,),
                daemon=True,
                name="emergencyalertarr-poller",
            )
            _scheduler_thread.start()
            logger.info("emergencyalertarr: poller thread started")

    def _build_fields(self):
        try:
            from apps.channels.models import ChannelGroup, Channel
            managed_group_ids = set(
                Channel.objects.exclude(channel_group=None).values_list("channel_group_id", flat=True)
            )
            groups   = [{"value": str(g.id), "label": g.name}
                        for g in ChannelGroup.objects.filter(id__in=managed_group_ids).order_by("name")]
            channels = [{"value": str(c.id), "label": c.name}
                        for c in Channel.objects.exclude(channel_group=None).order_by("name")]
        except Exception:
            groups = []
            channels = []

        try:
            mappings = _get_mappings()
        except Exception:
            mappings = {}

        ticker_lines = []
        for cid, m in mappings.items():
            name = m.get("channel_name", f"Channel {cid}")
            ticker_type = m.get("type", "eas")
            if ticker_type == "eas":
                event = _eas_active.get(cid)
                now_playing = f"[EAS] ALERT: {event}" if event else "[EAS] monitoring"
            else:
                now_playing = f"[{ticker_type}]"
            ticker_lines.append(f"- {name}: {now_playing}")

        active_label = (f"{len(mappings)} monitored channel(s):\n" + "\n".join(ticker_lines)) if mappings else "No channels are being monitored."

        return [
            # Overview
            {"id": "_eas_section",  "type": "info", "label": "══════════  EMERGENCY ALERT SYSTEM  ══════════"},
            {"id": "_eas_about",    "type": "info",
             "label": "Turns your channels into an EAS relay. EmergencyAlertarr watches live alerts from the National Weather Service and/or FEMA IPAWS for your area; when one fires, the channel being watched takes over with a broadcast-style alert screen, real EAS tones, and (optionally) a spoken readout, then returns to normal programming when it clears. Alerts play one at a time. Everything is generated on the fly — no WAV files or extra downloads required."},

            # 1 - Alert sources
            {"id": "_eas_src_header", "type": "info", "label": "──────  1 · ALERT SOURCES  ──────"},
            {"id": "_eas_source_note", "type": "info",
             "label": "Where alerts come from. NWS (api.weather.gov) is weather-only, filtered by NWS zone/county codes. IPAWS (FEMA) is the national feed — it adds AMBER, civil, and law-enforcement alerts, filtered by SAME/FIPS county codes. 'Both' merges them and de-duplicates an alert that arrives from each. You must fill in the code field for whichever source(s) you turn on."},
            {"id": "eas_source", "type": "select", "label": "Alert Source",
             "options": [
                 {"value": "nws",   "label": "NWS only (weather.gov)"},
                 {"value": "ipaws", "label": "IPAWS only (FEMA / national)"},
                 {"value": "both",  "label": "Both (NWS + IPAWS)"},
             ]},
            {"id": "_eas_zone_help", "type": "info",
             "label": "NWS codes: find yours at weather.gov — pick your state, then your county; the code (e.g. TXC113) shows in the URL. Separate multiple with commas. Type ALL by itself to monitor every US alert (handy for testing — there's almost always something active). Required when Alert Source is NWS or Both."},
            {"id": "eas_zones",     "type": "text",   "label": "NWS Zone / County Codes",
             "placeholder": "e.g. TXC113,TXC121 — or ALL"},
            {"id": "_eas_ipaws_note", "type": "info",
             "label": "IPAWS codes: 6-digit SAME/FIPS county codes (e.g. Tulsa County OK = 040143). A whole state uses a trailing 000 (040000 = all Oklahoma). Type ALL by itself to take every IPAWS alert nationwide (testing). National alerts — EAN, National Periodic Test (NPT), and Primary Entry Point (PEP) activations — always come through regardless of the codes you set. Required when Alert Source is IPAWS or Both."},
            {"id": "eas_ipaws_same_codes", "type": "text", "label": "IPAWS County Codes (6-digit SAME/FIPS, or ALL)",
             "placeholder": "e.g. 040143,040131 — or 040000, or ALL"},
            {"id": "eas_ipaws_feeds", "type": "text", "label": "IPAWS Feeds (advanced — comma-separated: eas, wea, public)",
             "placeholder": "eas"},
            {"id": "eas_severity_filter", "type": "select", "label": "Minimum Severity to trigger",
             "options": [
                 {"value": "Moderate", "label": "Watches and up (Moderate+)"},
                 {"value": "Severe",   "label": "Warnings and up (Severe+)"},
                 {"value": "Extreme",  "label": "Emergencies only (Extreme)"},
             ]},
            {"id": "eas_poll_interval",   "type": "number", "label": "How often to check for alerts (seconds, min 15, default 60)", "min": 15},

            # 2 - On-screen look
            {"id": "_eas_ovl_header", "type": "info", "label": "──────  2 · ON-SCREEN LOOK  ──────"},
            {"id": "_eas_overlay_style_note", "type": "info",
             "label": "How the alert screen looks. EASyPlus = classic broadcast EAS generator: full-screen black, large centered white text with a scrolling alert line. DASDEC = cable-headend look: navy background, red border, centered monospace text worded like a real ENDEC ('… has issued … for the following counties or areas …'), auto-paginated with a page counter for long alerts."},
            {"id": "eas_overlay_style", "type": "select", "label": "Alert Overlay Style",
             "options": [
                 {"value": "easyplus", "label": "EASyPlus — classic broadcast EAS character generator"},
                 {"value": "dasdec",   "label": "DASDEC — cable-headend style (navy, red border, paginated)"},
             ]},
            {"id": "_eas_epfont_note", "type": "info",
             "label": "Font (optional override): EASyPlus uses the bundled fonts/EASyText.ttf and DASDEC uses fonts/luximb.ttf. To override, drop a .ttf/.otf into the plugin fonts/ folder and put its filename here (an absolute path or system-font name also works). Leave blank to use the bundled font; a name that can't be found falls back to it."},
            {"id": "eas_easyplus_font", "type": "text", "label": "Overlay Font (filename, path, or system font name)", "placeholder": "e.g. my_eas_font.ttf"},
            {"id": "eas_easyplus_font_scale", "type": "number", "label": "Overlay Text Size (1.0 = default; 1.2–1.5 is bigger, up to 3.0)", "min": 0.5, "max": 3},

            # 3 - Performance
            {"id": "_eas_perf_header", "type": "info", "label": "──────  3 · PERFORMANCE  ──────"},
            {"id": "_eas_transcode_note", "type": "info",
             "label": "During an alert (only) the channel is re-encoded by FFmpeg to draw the overlay, so it briefly uses more CPU; it returns to pass-through the moment the alert clears. If an alert buffers or stutters, step this down. High-framerate sources (59.94fps) cost about double a 29.97fps source."},
            {"id": "eas_transcode_mode", "type": "select", "label": "Transcode Quality (during alerts)",
             "options": [
                 {"value": "full",    "label": "Full — source resolution & framerate (default; best CPU/GPU)"},
                 {"value": "1080p30", "label": "1080p30 — full res, capped at 30fps (try first if buffering)"},
                 {"value": "720p",    "label": "720p — scaled down, source framerate (big CPU saving)"},
                 {"value": "720p30",  "label": "720p30 — scaled down & capped at 30fps (max CPU saving)"},
             ]},
            {"id": "_eas_leadin_note", "type": "info",
             "label": "Overlay lead-in: seconds of silence held before the alert tones start, so the on-screen overlay has time to appear first. The overlay is drawn on the live channel, which can take a few seconds to reconnect after switching to the alert, while the tones are instant — without a lead-in the header tones play over a blank picture. Set this roughly to how long your channels take to come back after a switch (4 is a good start; lower it if your streams reconnect fast, raise it if the tones still beat the picture)."},
            {"id": "eas_lead_in_secs", "type": "number", "label": "Overlay Lead-in (seconds before tones, default 4)", "min": 0, "max": 15},

            # 4 - Alert audio
            {"id": "_eas_audio_header", "type": "info", "label": "──────  4 · ALERT AUDIO  ──────"},
            {"id": "_eas_gentone_note", "type": "info",
             "label": "Tone generation (recommended ON): EmergencyAlertarr synthesizes the EAS tones itself — a real AFSK SAME header built from the live alert (originator, event code, counties, valid time, so bigger alerts get a longer header, like a real ENDEC), the two-tone attention signal, and the EOM tones. No WAV files needed. Turn OFF only if you'd rather supply your own eas_header.wav / eas_att.wav / eas_eom.wav in the plugin folder."},
            {"id": "eas_generate_tones", "type": "boolean", "label": "Generate EAS tones automatically (no WAV files needed)"},
            {"id": "eas_att_secs", "type": "number", "label": "Attention Tone length (seconds — only when tones are generated; broadcast EAS uses 8, real range 8–25)", "min": 4, "max": 30},
            {"id": "_eas_tts_note", "type": "info",
             "label": "Spoken readout (TTS): reads the alert aloud (offline, via espeak-ng) in the gap between the attention and EOM tones instead of dead air. Needs espeak-ng in the Dispatcharr container (apt-get install espeak-ng); if it's missing, alerts fall back to the silent gap below automatically."},
            {"id": "eas_tts_enabled", "type": "boolean", "label": "Read the alert aloud (TTS)"},
            {"id": "eas_silence_secs",  "type": "number", "label": "Silent gap length (seconds, 10–20) — used only when TTS is OFF/unavailable; when TTS is ON the gap is sized to the readout", "min": 10, "max": 20},

            # 5 - Tests
            {"id": "_eas_test_header", "type": "info", "label": "──────  5 · TESTS  ──────"},
            {"id": "_eas_test_type_note", "type": "info",
             "label": "Controls the manual \"Test EAS Alert\" button and the announced area for scheduled tests. Test alerts are worded as a real RWT/RMT from \"EAS Participant\" (the standard test originator), not the Weather Service."},
            {"id": "eas_test_type", "type": "select", "label": "Manual Test Type (\"Test EAS Alert\" button)",
             "options": [
                 {"value": "monthly", "label": "RMT — Required Monthly Test"},
                 {"value": "weekly",  "label": "RWT — Required Weekly Test"},
             ]},
            {"id": "eas_test_area", "type": "text", "label": "Test Announce Area (optional — blank says \"this viewing area\")", "placeholder": "e.g. Tulsa County, Oklahoma"},
            {"id": "_eas_endec_note", "type": "info",
             "label": "ENDEC test sequence (tests only): plays like a real ENDEC self-test — header tones, EOM tones, then a silent tail with the screen still up — no attention tone or readout. Affects tests only; real alerts always use the full header → attention → readout → EOM sequence."},
            {"id": "eas_endec_test_mode", "type": "boolean", "label": "Use ENDEC-style test sequence (header → EOM → silent tail). ON by default."},
            {"id": "eas_endec_tail_secs", "type": "number", "label": "ENDEC silent-tail length (seconds the screen lingers after EOM, default 7.5)", "min": 1, "max": 30},
            {"id": "_eas_sched_note", "type": "info",
             "label": "Scheduled auto-test: fire an RWT or RMT automatically on a schedule, like a station's automated encoder. Only fires on channels that currently have a viewer."},
            {"id": "eas_test_schedule", "type": "select", "label": "Auto-Test Schedule",
             "options": [
                 {"value": "off",     "label": "Off"},
                 {"value": "weekly",  "label": "Weekly (RWT)"},
                 {"value": "monthly", "label": "Monthly (RMT)"},
             ]},
            {"id": "eas_test_day", "type": "number", "label": "Test Day — Weekly: 0=Mon…6=Sun (default 2=Wed). Monthly: 1–28 (default 1)", "min": 0, "max": 28},
            {"id": "eas_test_hour", "type": "number", "label": "Test Hour (0–23 local, default 12=noon)", "min": 0, "max": 23},

            # 6 - Custom alert injection
            {"id": "_eas_inject_header", "type": "info", "label": "──────  6 · CUSTOM ALERT INJECTION  ──────"},
            {"id": "_eas_inject_note", "type": "info",
             "label": "Fill these in, then use the \"Inject Custom Alert\" action button to broadcast your own alert on the monitored channel(s) — full overlay, tones, and readout, just like a real alert. Good for local announcements or trying out a look."},
            {"id": "eas_inject_event", "type": "text", "label": "Event Name", "placeholder": "e.g. Civil Emergency Message"},
            {"id": "eas_inject_area", "type": "text", "label": "Area", "placeholder": "e.g. Tulsa County, Oklahoma"},
            {"id": "eas_inject_message", "type": "text", "label": "Message / Headline", "placeholder": "e.g. A water main break has closed Main St. Avoid the area."},
            {"id": "eas_inject_instruction", "type": "text", "label": "Instruction (optional)", "placeholder": "e.g. Seek an alternate route and monitor local news."},
            {"id": "eas_inject_sender", "type": "text", "label": "Sender / Originator (default: EAS Participant)", "placeholder": "default: EAS Participant"},
            {"id": "eas_inject_severity", "type": "select", "label": "Severity",
             "options": [
                 {"value": "Minor",    "label": "Minor"},
                 {"value": "Moderate", "label": "Moderate"},
                 {"value": "Severe",   "label": "Severe"},
                 {"value": "Extreme",  "label": "Extreme"},
             ]},
            {"id": "eas_inject_duration_min", "type": "number", "label": "Effective Duration (minutes — shown as the Expires time, default 30)", "min": 1, "max": 360},

            # 7 - Channels to monitor
            {"id": "_eas_chan_header", "type": "info", "label": "──────  7 · CHANNELS TO MONITOR  ──────"},
            {"id": "_eas_allchannels_warn", "type": "info",
             "label": "Pick which channels get EAS monitoring, then use the \"Enable EAS\" action to arm them. Armed channels play normally and only switch to the alert overlay when something fires. Use the Enable / Disable action buttons to turn monitoring on or off."},
            {"id": "eas_target_type",   "type": "select", "label": "Apply To",
             "options": [{"value": "all", "label": "All Channels"}, {"value": "group", "label": "Channel Group"}, {"value": "groups", "label": "Multiple Groups (CSV)"}, {"value": "channel", "label": "Single Channel"}]},
            {"id": "_eas_target_note",         "type": "info",   "label": "Fill in only the field matching your Apply To choice above — leave the rest blank."},
            {"id": "eas_channel_group_id",    "type": "select", "label": "Channel Group (for 'Channel Group')",             "options": groups},
            {"id": "eas_channel_group_names", "type": "text",   "label": "Group Names (for 'Multiple Groups' — comma-separated)", "placeholder": "e.g. News, Locals, Networks"},
            {"id": "eas_channel_id",          "type": "select", "label": "Channel (for 'Single Channel')",                 "options": channels},
            {"id": "eas_exclude_groups",      "type": "text",   "label": "Exclude Groups (optional — comma-separated names to skip)", "placeholder": "e.g. Music, Radio"},

            # Status
            {"id": "_active_section", "type": "info", "label": "══════════  STATUS  ══════════"},
            {"id": "_ticker_list",    "type": "info", "label": active_label},
        ]

    def run(self, action, params, context):
        saved = _get_settings()
        base = {k: v for k, v in saved.items() if k not in ("channel_mappings", "channel_cache")}
        if params:
            base.update(params)
        params = base

        dispatch = {
            "enable_eas":           self._enable_eas,
            "disable_eas":          self._disable_eas,
            "test_eas":             self._test_eas,
            "inject_alert":         self._inject_alert,
            "view_history":         self._view_history,
            "clear_history":        self._clear_history,
            "migrate_eas":          self._migrate_eas,
            "view_active":          self._view_active,
            "refresh_channels":     self._refresh_channels,
            "clean_orphans":        self._clean_orphans,
            "reset_all_eas":        self._reset_all_eas,
            "redis_diag":           self._redis_diag,
            "fetch_alerts":         self._fetch_alerts_now,
            "reload_poller":        self._reload_poller,
            "restart_dispatcharr":  self._restart_dispatcharr,
        }
        handler = dispatch.get(action)
        if not handler:
            return {"success": False, "message": f"Unknown action: {action}"}
        try:
            return handler(params)
        except Exception as e:
            logger.error(f"emergencyalertarr: action {action} failed: {e}", exc_info=True)
            return {"success": False, "message": f"Error: {e}"}

    def stop(self, context):
        global _stop_event, _scheduler_thread
        _stop_event.set()
        if _scheduler_thread:
            _scheduler_thread.join(timeout=5)
        logger.info("emergencyalertarr: poller thread stopped")

    # ------------------------------------------------------------------ #
    # Actions                                                              #
    # ------------------------------------------------------------------ #


    def _do_disable(self, channels, type_filter=None):
        """Core disable logic shared by all three phase-specific disable actions."""
        mappings = _get_mappings()
        disabled, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            if not mapping:
                skipped.append(f"{channel.name} (no ticker active on this channel)")
                continue
            ticker_type = mapping.get("type", "eas")
            if type_filter and ticker_type != type_filter:
                skipped.append(f"{channel.name} (has a {ticker_type} ticker, not {type_filter} — use the correct disable action)")
                continue
            try:
                _restore_profile(channel, mapping.get("original_profile_id"))
                _delete_cloned_profile(mapping.get("ticker_profile_id"))
                if mapping.get("eas_armed"):
                    # EAS is co-armed — preserve it as a pure EAS channel rather than deleting the mapping
                    mappings[cid] = {
                        "original_profile_id": mapping.get("original_profile_id"),
                        "channel_name":        channel.name,
                        "type":                "eas",
                    }
                else:
                    del mappings[cid]
                disabled.append(channel.name)
            except Exception as e:
                logger.error(f"emergencyalertarr: disable failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        _save_mappings(mappings)
        parts = []
        if disabled: parts.append(f"Disabled: {len(disabled)} channel(s)")
        if skipped:  parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:   parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}


    def _enable_eas(self, params):
        # Validate the required area codes based on which alert source(s) are on.
        source = (params.get("eas_source") or "nws").lower()
        zones = (params.get("eas_zones") or "").strip()
        ipaws_codes = (params.get("eas_ipaws_same_codes") or "").strip()
        missing = []
        if source in ("nws", "both") and not zones:
            missing.append("NWS Zone / County Codes (a zone code, or ALL)")
        if source in ("ipaws", "both") and not ipaws_codes:
            missing.append("EAS IPAWS County Codes (a 6-digit SAME code, or ALL)")
        if missing:
            return {"success": False, "message":
                    "Can't enable EAS — the selected Alert Source (" + source +
                    ") needs: " + "; ".join(missing) +
                    ". Set it in EAS settings above, then enable again."}

        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels found. Set the EAS Weather Alerts > Apply To / Channel selector above."}

        mappings = _get_mappings()
        enabled, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            try:
                if cid in mappings:
                    existing = mappings[cid]
                    if existing.get("type") == "eas" or existing.get("eas_armed"):
                        skipped.append(f"{channel.name} (EAS already armed — already enabled)")
                        continue
                    if existing.get("eas_profile_id"):
                        skipped.append(f"{channel.name} (EAS alert currently active — wait for it to clear)")
                        continue
                    if not existing.get("original_profile_id"):
                        if existing.get("ticker_profile_id"):
                            # A ticker clone is currently assigned, so the channel's
                            # *current* profile is that clone, not the real original --
                            # backfilling from it here would lock the clone in
                            # permanently. Needs manual cleanup instead of a guess.
                            skipped.append(f"{channel.name} (legacy mapping is missing its original profile and has an active ticker clone — disable its current ticker first, then re-enable EAS)")
                            continue
                        # No clone currently active, so the channel's current profile
                        # genuinely is the original -- safe to repair the mapping
                        # instead of carrying broken legacy data forward.
                        current_profile = channel.stream_profile
                        if not current_profile:
                            skipped.append(f"{channel.name} (legacy mapping missing original profile, and no current stream profile to recover it from — assign one in Channels first)")
                            continue
                        existing["original_profile_id"] = current_profile.id
                        logger.warning(f"emergencyalertarr: repaired legacy mapping for {channel.name} (ch {cid}) — backfilled missing original_profile_id")
                    # Co-arm EAS alongside the existing ticker without disrupting it
                    _eas_clear(channel.id)
                    existing["eas_armed"] = True
                    mappings[cid] = existing
                    enabled.append(channel.name)
                    continue
                original_profile = channel.stream_profile
                if not original_profile:
                    skipped.append(f"{channel.name} (no stream profile assigned — assign one in Channels first)")
                    continue
                if original_profile.name.startswith(PROFILE_PREFIX):
                    skipped.append(f"{channel.name} (already has a EmergencyAlertarr profile — disable first)")
                    continue
                _eas_clear(channel.id)
                mappings[cid] = {
                    "original_profile_id": original_profile.id,
                    "channel_name":        channel.name,
                    "type":                "eas",
                }
                enabled.append(channel.name)
            except Exception as e:
                logger.error(f"emergencyalertarr: enable EAS failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} ({e})")

        _save_mappings(mappings)
        parts = []
        if enabled:
            parts.append(f"EAS registered: {len(enabled)} channel(s)\n" + "\n".join(f"  - {e}" for e in enabled))
            parts.append(f"Monitoring zones: {zones}\nChannels continue using their normal profile. When a qualifying NWS alert fires, they automatically switch to the EAS overlay and restart. No re-encoding overhead until an actual alert occurs.")
        if skipped: parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:  parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _disable_eas(self, params):
        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels found. Set the EAS Weather Alerts > Apply To / Channel selector above."}
        mappings = _get_mappings()
        disabled, skipped, failed = [], [], []
        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            is_pure_eas = mapping and mapping.get("type") == "eas"
            is_coarmed  = mapping and mapping.get("eas_armed")
            if not mapping or (not is_pure_eas and not is_coarmed):
                skipped.append(f"{channel.name} (no EAS ticker active)")
                continue
            try:
                if mapping.get("eas_profile_id"):
                    _restore_profile(channel, mapping.get("original_profile_id"))
                    _delete_cloned_profile(mapping["eas_profile_id"])
                _eas_clear(channel.id)
                with _eas_lock:
                    _eas_active.pop(cid, None)
                if is_pure_eas:
                    del mappings[cid]
                else:
                    # Co-armed: remove EAS keys, leave the ticker mapping intact
                    mapping.pop("eas_armed", None)
                    mapping.pop("eas_profile_id", None)
                    mappings[cid] = mapping
                disabled.append(channel.name)
            except Exception as e:
                logger.error(f"emergencyalertarr: disable EAS failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} ({e})")
        _save_mappings(mappings)
        parts = []
        if disabled: parts.append(f"EAS disabled: {len(disabled)} channel(s)")
        if skipped:  parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:   parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _test_eas(self, params):
        """Fire a fake EAS alert on EAS-enabled channels for a configurable duration, then auto-restore."""
        import threading as _threading
        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels found. Select a channel in the EAS section."}

        mappings  = _get_mappings()
        settings  = _get_settings()
        streaming_ids = _eas_streaming_ids()
        fired, skipped, failed = [], [], []

        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            if not mapping or (mapping.get("type") != "eas" and not mapping.get("eas_armed")):
                skipped.append(f"{channel.name} (no EAS ticker active — enable it first)")
                continue
            if cid not in streaming_ids:
                # Only fire on channels someone is actually watching, so a test
                # can't leave the overlay stuck on idle channels.
                skipped.append(f"{channel.name} (not currently being watched — tune to it first)")
                continue
            if mapping.get("eas_profile_id"):
                skipped.append(f"{channel.name} (EAS already active — clear it first)")
                continue
            try:
                from core.models import StreamProfile
                if not mapping.get("original_profile_id"):
                    failed.append(f"{channel.name} (incomplete mapping data — no original profile saved; disable then re-enable EAS for this channel to fix)")
                    continue
                orig = StreamProfile.objects.filter(id=mapping.get("original_profile_id")).first()
                if not orig:
                    failed.append(f"{channel.name} (original profile missing)")
                    continue

                silence_secs   = max(10, min(20, int(settings.get("eas_silence_secs") or 15)))
                transcode_mode = settings.get("eas_transcode_mode")  or "full"
                overlay_style  = (settings.get("eas_overlay_style") or "easyplus").lower()
                use_tts        = bool(settings.get("eas_tts_enabled", True))
                test_type      = (settings.get("eas_test_type") or "monthly").lower()
                test_area      = (settings.get("eas_test_area") or "").strip() or None
                endec_test     = bool(settings.get("eas_endec_test_mode", True))
                generate_tones = bool(settings.get("eas_generate_tones", False))
                try:
                    att_secs   = max(4.0, min(30.0, float(settings.get("eas_att_secs") or 8)))
                except Exception:
                    att_secs   = 8.0
                try:
                    tail_secs  = max(1.0, min(30.0, float(settings.get("eas_endec_tail_secs") or 7.5)))
                except Exception:
                    tail_secs  = 7.5

                from datetime import datetime, timedelta, timezone as _tz
                now_utc = datetime.now(_tz.utc)
                # Realistic RWT/RMT copy; expires refined below once duration known.
                fake_alerts = [_eas_build_test_alert(test_type, now_utc, area=test_area)]

                _ep_font, _ep_scale = _easyplus_settings(settings)
                _lead_in = float(settings.get("eas_lead_in_secs") or 0)
                eas_profile, _, seq_duration = _clone_and_inject_eas(
                    channel.id, orig, channel.name,
                    silence_secs, transcode_mode,
                    style=overlay_style, unique_alerts=fake_alerts, use_tts=use_tts,
                    endec_test=endec_test, tail_secs=tail_secs,
                    generate_tones=generate_tones, att_secs=att_secs,
                    easyplus_font=_ep_font, easyplus_scale=_ep_scale,
                    lead_in_secs=_lead_in,
                )
                _assign_profile(channel, eas_profile)

                # Now that we know the real sequence duration, set a matching expires.
                fake_alerts[0]["expires"] = (now_utc + timedelta(seconds=seq_duration)).isoformat()
                _eas_write_alert(cid, fake_alerts, style=overlay_style)
                # Use eas_restore_at so the sweep loop handles the timed restore --
                # survives worker reloads, no daemon thread needed.
                # Use "test" fingerprint so a real alert after the test fires fresh.
                # Add a reconnect-grace buffer: stopping the channel and waiting
                # for the stream to come back on the EAS profile burns several
                # seconds of dead time. Without this pad the restore could fire
                # before the viewer's stream finished reconnecting, so they'd
                # never see the overlay. The grace keeps the alert window open
                # long enough for the reconnect to land inside it.
                mapping["eas_profile_id"] = eas_profile.id
                _eas_arm_restore(mapping, seq_duration, time.time())
                mappings[cid] = mapping
                rc = _get_redis_client()
                if rc:
                    try:
                        raw_st = rc.get(_EAS_STATE_KEY)
                        st = json.loads(raw_st) if raw_st else {}
                        st[cid] = "__test__"
                        rc.setex(_EAS_STATE_KEY, 3600, json.dumps(st))
                    except Exception:
                        pass
                _restart_channel_stream_async(channel, label="EAS", proactive=True)
                fired.append((cid, channel.name, eas_profile.id, str(channel.uuid)))
                _eas_history_add(
                    fake_alerts[0].get("event"), fake_alerts[0].get("area"),
                    channel.name, kind="test",
                )

            except Exception as e:
                logger.error(f"emergencyalertarr: test_eas failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        if fired:
            _save_mappings(mappings)

        parts = []
        if fired:
            names = ", ".join(n for _, n, _, _ in fired)
            parts.append(f"EAS test alert fired on: {names}\nThe overlay will auto-restore once the full tone + readout sequence finishes.")
        if skipped:
            parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:
            parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": bool(fired), "message": "\n\n".join(parts) or "Nothing to do."}


    def _inject_alert(self, params):
        """Fire a user-defined custom alert on the selected EAS channels, using
        the text entered in the Manual Alert settings fields. Reuses the same
        clone/overlay/TTS machinery as real alerts, so it looks and sounds
        identical -- it just carries your custom event/area/message."""
        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels found. Select a channel/group in the EAS section first."}

        settings = _get_settings()
        event = (settings.get("eas_inject_event") or "").strip()
        if not event:
            return {"success": False, "message": "Enter a Custom Alert Event name in settings before injecting (e.g. \"Civil Emergency Message\")."}
        area    = (settings.get("eas_inject_area") or "").strip() or "this viewing area"
        message = (settings.get("eas_inject_message") or "").strip()
        instruction = (settings.get("eas_inject_instruction") or "").strip()
        sender  = (settings.get("eas_inject_sender") or "EAS Participant").strip()
        severity = (settings.get("eas_inject_severity") or "Severe")
        try:
            duration_min = max(1, min(360, int(settings.get("eas_inject_duration_min") or 30)))
        except Exception:
            duration_min = 30

        mappings = _get_mappings()
        silence_secs   = max(10, min(20, int(settings.get("eas_silence_secs") or 15)))
        transcode_mode = settings.get("eas_transcode_mode") or "full"
        overlay_style  = (settings.get("eas_overlay_style") or "easyplus").lower()
        use_tts        = bool(settings.get("eas_tts_enabled", True))
        generate_tones = bool(settings.get("eas_generate_tones", False))
        try:
            att_secs   = max(4.0, min(30.0, float(settings.get("eas_att_secs") or 8)))
        except Exception:
            att_secs   = 8.0

        from datetime import datetime, timedelta, timezone as _tz
        now_utc = datetime.now(_tz.utc)
        expires = (now_utc + timedelta(minutes=duration_min)).isoformat()
        # Build a description that works for both overlay styles and TTS.
        desc = f"* WHAT...{event}.  * WHERE...{area}."
        if message:
            desc += f"  * DETAILS...{message}"
        custom_alert = [{
            "event":       event,
            "area":        area,
            "severity":    severity,
            "effective":   now_utc.isoformat(),
            "expires":     expires,
            "headline":    message or f"A {event} has been issued for {area} by {sender}.",
            "description": desc,
            "instruction": instruction,
            "sender":      sender,
            # event_code left blank -> mapped from the event name; originator
            # inferred from the sender; blank code list -> entire-US header.
            "event_code":  "",
            "originator":  "",
            "same_codes":  [],
        }]

        fired, skipped, failed = [], [], []
        streaming_ids = _eas_streaming_ids()
        for channel in channels:
            cid = str(channel.id)
            mapping = mappings.get(cid)
            if not mapping or (mapping.get("type") != "eas" and not mapping.get("eas_armed")):
                skipped.append(f"{channel.name} (EAS not enabled here)")
                continue
            if cid not in streaming_ids:
                skipped.append(f"{channel.name} (not currently being watched — tune to it first)")
                continue
            if mapping.get("eas_profile_id"):
                skipped.append(f"{channel.name} (an alert is already active — wait for it to clear)")
                continue
            if not mapping.get("original_profile_id"):
                failed.append(f"{channel.name} (incomplete mapping — re-enable EAS on this channel)")
                continue
            try:
                from core.models import StreamProfile
                orig = StreamProfile.objects.filter(id=mapping.get("original_profile_id")).first()
                if not orig:
                    failed.append(f"{channel.name} (original profile missing)")
                    continue
                _ep_font, _ep_scale = _easyplus_settings(settings)
                _lead_in = float(settings.get("eas_lead_in_secs") or 0)
                eas_profile, _, seq_duration = _clone_and_inject_eas(
                    channel.id, orig, channel.name, silence_secs, transcode_mode,
                    style=overlay_style, unique_alerts=custom_alert, use_tts=use_tts,
                    generate_tones=generate_tones, att_secs=att_secs,
                    easyplus_font=_ep_font, easyplus_scale=_ep_scale,
                    lead_in_secs=_lead_in,
                )
                _assign_profile(channel, eas_profile)
                _eas_write_alert(cid, custom_alert, style=overlay_style)
                mapping["eas_profile_id"] = eas_profile.id
                _eas_arm_restore(mapping, seq_duration, time.time())
                mappings[cid] = mapping
                rc = _get_redis_client()
                if rc:
                    try:
                        raw_st = rc.get(_EAS_STATE_KEY)
                        st = json.loads(raw_st) if raw_st else {}
                        st[cid] = "__manual__"
                        rc.setex(_EAS_STATE_KEY, 3600, json.dumps(st))
                    except Exception:
                        pass
                _restart_channel_stream_async(channel, label="EAS", proactive=True)
                fired.append(channel.name)
                _eas_history_add(event, area, channel.name, kind="manual", severity=severity)
            except Exception as e:
                logger.error(f"emergencyalertarr: inject_alert failed for {channel.name}: {e}", exc_info=True)
                failed.append(f"{channel.name} (error: {e})")

        if fired:
            _save_mappings(mappings)
        parts = []
        if fired:
            parts.append(f"Custom alert \"{event}\" fired on: {', '.join(fired)}\nThe overlay will auto-restore once the sequence finishes.")
        if skipped:
            parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:
            parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": bool(fired), "message": "\n\n".join(parts) or "Nothing to do."}


    def _view_history(self, params):
        """Show the recent EAS alert history (most recent first)."""
        hist = _eas_history_read()
        if not hist:
            return {"success": True, "message": "No EAS alerts have fired yet."}

        from datetime import datetime
        kind_label = {"alert": "ALERT", "test": "TEST", "manual": "MANUAL"}
        lines = []
        for h in hist:
            try:
                dt = datetime.fromisoformat(h.get("ts", ""))
                when = dt.astimezone().strftime("%m/%d %I:%M %p").lstrip("0")
            except Exception:
                when = h.get("ts", "?")
            tag = kind_label.get(h.get("kind", "alert"), "ALERT")
            chans = h.get("channels") or []
            chan_str = ", ".join(chans[:4]) + (f" +{len(chans) - 4} more" if len(chans) > 4 else "")
            sev = f" [{h.get('severity')}]" if h.get("severity") else ""
            area = h.get("area", "")
            lines.append(f"{when}  ·  {tag}{sev}  ·  {h.get('event', 'Alert')}"
                         + (f"  —  {area}" if area else "")
                         + (f"\n        on: {chan_str}" if chan_str else ""))

        return {"success": True, "message": f"EAS Alert History (last {len(hist)}):\n\n" + "\n".join(lines)}


    def _clear_history(self, params):
        """Wipe the EAS alert history."""
        _eas_history_clear()
        return {"success": True, "message": "EAS alert history cleared."}


    def _migrate_eas(self, params):
        """Migrate old always-on EAS profiles (ticker_profile_id) to dynamic mode."""
        from apps.channels.models import Channel

        mappings = _get_mappings()
        old_eas = {cid: m for cid, m in mappings.items()
                   if m and m.get("type") == "eas" and m.get("ticker_profile_id")}
        if not old_eas:
            return {"success": True, "message": "No old static EAS profiles found - all EAS channels are already in dynamic mode."}

        migrated, failed = [], []
        for cid, mapping in old_eas.items():
            name = mapping.get("channel_name", f"Channel {cid}")
            try:
                channel = Channel.objects.filter(id=int(cid)).first()
                if channel:
                    _restore_profile(channel, mapping.get("original_profile_id"))
                    _restart_channel_stream_async(channel, label="EAS")
                _delete_cloned_profile(mapping["ticker_profile_id"])
                mapping.pop("ticker_profile_id", None)
                mapping.pop("eas_profile_id", None)
                mappings[cid] = mapping
                with _eas_lock:
                    _eas_active.pop(cid, None)
                migrated.append(name)
            except Exception as e:
                logger.error(f"emergencyalertarr: migrate EAS failed for {name}: {e}", exc_info=True)
                failed.append(f"{name} ({e})")

        _save_mappings(mappings)
        rc = _get_redis_client()
        if rc:
            try:
                rc.delete(_EAS_STATE_KEY, _EAS_OWNER_KEY)
            except Exception:
                pass

        parts = []
        if migrated:
            parts.append(f"Migrated {len(migrated)} channel(s) to dynamic EAS:\n" + "\n".join(f"  - {n}" for n in migrated))
            parts.append("Channels restored to passthrough profiles. Re-encoding only occurs when a real NWS alert fires.")
        if failed:
            parts.append("Failed:\n" + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}


    def _view_active(self, params):
        mappings = _get_mappings()
        if not mappings:
            return {"success": True, "message": "No channels currently armed for EAS."}

        eas_channels = [(cid, m) for cid, m in mappings.items()]
        lines = [f"{len(eas_channels)} channel(s) armed for EAS:", ""]
        for cid, mapping in eas_channels:
            name = mapping.get("channel_name", f"Channel {cid}")
            event = _eas_active.get(cid)
            state = f"ALERT: {event}" if event else "monitoring"
            lines.append(f"  {name}: {state}")
        return {"success": True, "message": "\n".join(lines)}

    def _refresh_channels(self, params):
        return {"success": True, "message": "Channel refresh not available in EAS-only mode."}

    def _clean_orphans(self, params):
        from core.models import StreamProfile
        mappings = _get_mappings()
        # Collect every profile id still referenced by any mapping -- both the
        # legacy ticker clones and the EAS clones. Use .get() since legacy/
        # EAS-only mappings won't have every key (a missing key here previously
        # crashed this action with KeyError: 'ticker_profile_id').
        active_ticker_ids = set()
        for m in mappings.values():
            if not isinstance(m, dict):
                continue
            for key in ("ticker_profile_id", "eas_profile_id"):
                pid = m.get(key)
                if pid:
                    active_ticker_ids.add(pid)

        # Primary sweep: profiles whose name starts with PROFILE_PREFIX
        named = list(StreamProfile.objects.filter(name__startswith=PROFILE_PREFIX))
        orphans = [p for p in named if p.id not in active_ticker_ids]

        # Secondary sweep: catch FIFO-era leftovers whose name didn't use the current
        # prefix (different dash, older naming) but whose parameters contain emergencyalertarr_data
        seen_ids = {p.id for p in named}
        for p in StreamProfile.objects.all():
            if p.id in seen_ids or p.id in active_ticker_ids:
                continue
            if "emergencyalertarr_data" in (p.parameters or ""):
                orphans.append(p)

        if not orphans:
            return {"success": True, "message": "No orphaned profiles found."}
        deleted = []
        for profile in orphans:
            try:
                profile.delete()
                deleted.append(profile.name)
            except Exception as e:
                logger.warning(f"emergencyalertarr: could not delete profile {profile.name}: {e}")
        return {"success": True, "message": f"Deleted {len(deleted)} orphaned profile(s):\n" + "\n".join(f"  - {n}" for n in deleted)}

    def _reset_all_eas(self, params):
        """Full clean slate: restore every channel's original profile, delete ALL
        EmergencyAlertarr cloned profiles, and wipe the mappings file plus EAS Redis state.

        This exists because mappings persist on disk (mappings.json) and cloned
        profiles persist in the database -- so uninstalling/reinstalling the
        plugin does NOT clear them, leaving stale 'monitoring' armings (e.g. 210
        channels still armed from earlier sessions). This resets everything so
        you can reconfigure from scratch.
        """
        from apps.channels.models import Channel
        from core.models import StreamProfile

        mappings = _get_mappings()
        restored = 0
        errors = []

        # 1. Restore each mapped channel to its original profile.
        for cid, mapping in list(mappings.items()):
            if not isinstance(mapping, dict):
                continue
            try:
                channel = Channel.objects.filter(id=int(cid)).first()
                if channel:
                    orig_id = mapping.get("original_profile_id")
                    if orig_id:
                        _restore_profile(channel, orig_id)
                        _restart_channel_stream_async(channel, label="EAS reset")
                        restored += 1
            except Exception as e:
                errors.append(f"ch {cid}: {e}")

        # 2. Delete every EmergencyAlertarr-cloned StreamProfile (name-prefixed or tagged).
        deleted = 0
        try:
            for p in StreamProfile.objects.filter(name__startswith=PROFILE_PREFIX):
                try:
                    p.delete(); deleted += 1
                except Exception:
                    pass
            for p in StreamProfile.objects.all():
                if "emergencyalertarr_data" in (p.parameters or ""):
                    try:
                        p.delete(); deleted += 1
                    except Exception:
                        pass
        except Exception as e:
            errors.append(f"profile cleanup: {e}")

        # 3. Wipe the mappings file.
        _save_mappings({})

        # 4. Clear EAS Redis state keys.
        rc = _get_redis_client()
        if rc:
            for key in (_EAS_STATE_KEY, _EAS_GLOBAL_KEY, _EAS_ALERTS_KEY,
                        _EAS_ROTATION_KEY, _EAS_SCHED_KEY, _EAS_SCHED_LOCK,
                        _EAS_SEEN_KEY, _EAS_OWNER_KEY):
                try:
                    rc.delete(key)
                except Exception:
                    pass
        # Reset the in-process seen-set too, so a reset always starts fresh
        # (the next poll re-seeds and invalidates whatever is currently active).
        _eas_seen_mem["ids"] = set()
        _eas_seen_mem["init"] = False

        msg = (f"EAS reset complete.\n"
               f"  - Restored {restored} channel(s) to their original profile\n"
               f"  - Deleted {deleted} EmergencyAlertarr cloned profile(s)\n"
               f"  - Cleared all EAS mappings and Redis state")
        if errors:
            msg += "\n\nSome issues (non-fatal):\n" + "\n".join(f"  - {e}" for e in errors[:10])
        msg += "\n\nYou can now re-arm EAS from a clean slate."
        return {"success": True, "message": msg}

    def _fetch_alerts_now(self, params):
        """Diagnostic: poll NWS/IPAWS right now (instead of waiting for the timer),
        show exactly what came back, and fire any genuinely new alerts on watched
        channels using the normal seen-set logic (already-played alerts are not
        replayed)."""
        settings = _get_settings()
        source = (settings.get("eas_source") or "nws").lower()
        zones = [z.strip() for z in (settings.get("eas_zones") or "").split(",") if z.strip()]
        severity = settings.get("eas_severity_filter") or "Moderate"

        try:
            alerts = _fetch_all_alerts(settings, zones, severity)
        except Exception as e:
            return {"success": False, "message": f"Fetch failed: {e}"}

        rc = _get_redis_client()

        # Snapshot the seen-set BEFORE firing so we can label new vs already-played.
        seen = set(_eas_seen_mem.get("ids") or set())
        if rc:
            try:
                raw = rc.get(_EAS_SEEN_KEY)
                if raw:
                    seen |= set(json.loads(raw if isinstance(raw, str) else raw.decode()))
            except Exception:
                pass
        was_first_run = not _eas_seen_mem.get("init", False)

        # Prime the cache with this exact fetch, then run the normal poll cycle so
        # the standard logic fires only new alerts on channels that are watched.
        if rc:
            try:
                rc.setex(_EAS_REDIS_KEY, _EAS_CACHE_TTL, json.dumps(alerts))
            except Exception:
                pass
        try:
            _eas_sweep()
        except Exception as e:
            logger.error(f"[EmergencyAlertarr] fetch-now sweep error: {e}", exc_info=True)

        streaming_ids = _eas_streaming_ids(rc)
        new_alerts = [a for a in alerts if a.get("id") not in seen]
        lines = [
            f"Polled {source.upper()} - {len(alerts)} active alert(s), {len(new_alerts)} new.",
            f"Watched channels right now: {len(streaming_ids)}",
            "",
        ]
        if not alerts:
            lines.append("No active alerts for your configured area.")
            lines.append("(Polling is working -- there's just nothing active. Use ALL codes/zones to test.)")
        else:
            for a in alerts:
                src = "IPAWS" if str(a.get("id", "")).startswith("ipaws:") else "NWS"
                area = (a.get("area") or "")
                area = (area[:48] + "...") if len(area) > 48 else area
                tag = "NEW" if a.get("id") not in seen else "already played"
                lines.append(f"- [{src}] {a.get('event')} [{a.get('severity')}]"
                             + (f" - {area}" if area else "") + f"  ({tag})")
            lines.append("")
            if was_first_run:
                lines.append("First poll since (re)start: existing alerts were marked as seen and "
                             "NOT fired (prevents a startup flood). New alerts from here on will fire.")
            elif new_alerts:
                lines.append(f"{len(new_alerts)} new alert(s) fired on any watched channel(s); "
                             "already-played alerts were skipped.")
            else:
                lines.append("Nothing new to fire -- all active alerts were already played.")
        return {"success": True, "message": "\n".join(lines)}

    def _redis_diag(self, params):
        # WAV tone file status first -- common cause of 'only one tone played'.
        wav_lines = ["EAS Tone WAV files:"]
        for name, path in (("eas_header.wav", _EAS_WAV_HEADER),
                           ("eas_att.wav", _EAS_WAV_ATT),
                           ("eas_eom.wav", _EAS_WAV_EOM)):
            if not os.path.isfile(path):
                wav_lines.append(f"  ✗ {name}: MISSING ({path})")
            else:
                sz = os.path.getsize(path)
                dur = _wav_duration_secs(path)
                flag = "✓" if sz >= 200 and dur > 0 else "✗ EMPTY/INVALID"
                wav_lines.append(f"  {flag} {name}: {sz} bytes, {dur:.1f}s")
        tts_state = f"TTS engine: {_TTS_BIN}" if _TTS_BIN else "TTS engine: NOT installed (espeak-ng) — alerts use silence"
        wav_lines.append(tts_state)
        # List ChannelService methods so we can find the right proactive-restart
        # call (the channel-stop -> client-reconnect gap is what makes test
        # overlays sometimes not appear).
        try:
            from apps.proxy.live_proxy.services.channel_service import ChannelService
            cs_methods = [m for m in dir(ChannelService)
                          if not m.startswith("_") and callable(getattr(ChannelService, m, None))]
            wav_lines.append("ChannelService methods: " + ", ".join(cs_methods))
        except Exception as e:
            wav_lines.append(f"ChannelService introspection failed: {e}")
        wav_prefix = "\n".join(wav_lines) + "\n\n"

        rc = _get_redis_client()
        if rc is None:
            return {"success": False, "message": wav_prefix + (
                "Redis unavailable — could not connect.\n\n"
                "Stream-start detection is disabled. The sweep loop will poll all active "
                "channels every 15 seconds (one bulk stellartunerlog.com fetch per cycle)."
            )}

        lines = [wav_prefix + "Redis: connected\n"]

        # Scan for active stream keys (both v0.24 and v0.25 patterns)
        try:
            ts_keys = (list(rc.scan_iter("live:channel:*", count=500)) +
                       list(rc.scan_iter("ts_proxy:channel:*", count=500)))
            if ts_keys:
                lines.append(f"Stream keys ({len(ts_keys)} found):")
                for raw in ts_keys[:40]:
                    k = raw.decode() if isinstance(raw, bytes) else raw
                    try:
                        ktype = rc.type(raw).decode()
                        if ktype == "set":
                            n = rc.scard(raw)
                            lines.append(f"  {k}  [set, {n} member(s)]")
                        elif ktype == "string":
                            v = (rc.get(raw) or b"").decode(errors="replace")
                            lines.append(f"  {k}  [string: {v[:80]}]")
                        else:
                            lines.append(f"  {k}  [{ktype}]")
                    except Exception as ex:
                        lines.append(f"  {k}  [error: {ex}]")
                if len(ts_keys) > 40:
                    lines.append(f"  ... and {len(ts_keys) - 40} more")
            else:
                lines.append("No stream keys found - scanning ALL keys to find active stream pattern:\n")
                try:
                    all_keys = list(rc.scan_iter("*", count=500))
                    if all_keys:
                        lines.append(f"All Redis keys ({len(all_keys)} total, showing first 60):")
                        for raw in all_keys[:60]:
                            k = raw.decode() if isinstance(raw, bytes) else raw
                            try:
                                ktype = rc.type(raw).decode()
                                if ktype == "string":
                                    v = (rc.get(raw) or b"").decode(errors="replace")
                                    lines.append(f"  {k}  [string: {v[:60]}]")
                                elif ktype == "set":
                                    n = rc.scard(raw)
                                    lines.append(f"  {k}  [set, {n} member(s)]")
                                else:
                                    lines.append(f"  {k}  [{ktype}]")
                            except Exception:
                                lines.append(f"  {k}")
                        if len(all_keys) > 60:
                            lines.append(f"  ... and {len(all_keys) - 60} more")
                    else:
                        lines.append("  Redis is empty - no keys at all.")
                except Exception as e2:
                    lines.append(f"  Full scan error: {e2}")
        except Exception as e:
            lines.append(f"ts_proxy scan error: {e}")

        # Show what _redis_scan_active() currently returns
        active = _redis_scan_active()
        if active is None:
            lines.append("\n_redis_scan_active(): returned None (error during scan)")
        else:
            mappings = _get_mappings()
            mapped_ids = set(int(k) for k in mappings.keys() if k.isdigit())
            matched = active & mapped_ids
            lines.append(f"\n_redis_scan_active(): {len(active)} active channel ID(s) total, "
                         f"{len(matched)} matching mapped channels")
            if matched:
                from apps.channels.models import Channel
                id_to_name = dict(Channel.objects.filter(id__in=matched).values_list("id", "name"))
                for cid in sorted(matched):
                    lines.append(f"  - [{cid}] {id_to_name.get(cid, '?')}")

        return {"success": True, "message": "\n".join(lines)}

    def _reload_poller(self, params):
        global _scheduler_thread, _stop_event
        _stop_event.set()
        if _scheduler_thread:
            _scheduler_thread.join(timeout=5)
        _stop_event = threading.Event()
        _scheduler_thread = threading.Thread(
            target=_poll_loop,
            args=(_stop_event,),
            daemon=True,
            name="emergencyalertarr-poller",
        )
        _scheduler_thread.start()
        logger.info("emergencyalertarr: poller thread reloaded via action")
        return {"success": True, "message": "Poller thread restarted. Live data will resume within 15 seconds."}

    def _restart_dispatcharr(self, params):
        import signal as _signal

        def _do_restart():
            time.sleep(2)
            try:
                result = subprocess.run(
                    ["pgrep", "-of", "gunicorn"],
                    capture_output=True, text=True
                )
                pid = int(result.stdout.strip())
                logger.info(f"emergencyalertarr: sending SIGHUP to gunicorn master PID {pid}")
                os.kill(pid, _signal.SIGHUP)
            except Exception as e:
                logger.warning(f"emergencyalertarr: gunicorn SIGHUP failed ({e}), falling back to PID 1")
                try:
                    os.kill(1, _signal.SIGHUP)
                except Exception as e2:
                    logger.error(f"emergencyalertarr: restart failed: {e2}")

        threading.Thread(target=_do_restart, daemon=True).start()
        return {"success": True, "message": "Restart signal sent. Dispatcharr will reload in ~2 seconds.\n\nRefresh this page in about 15 seconds."}

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _resolve_channels(self, params, prefix=""):
        from apps.channels.models import Channel, ChannelGroup
        target_type = params.get(f"{prefix}target_type", "group")

        # Build exclusion set from the exclude_groups field (applies to all target types)
        exclude_ids = set()
        raw_exclude = params.get(f"{prefix}exclude_groups", "")
        if raw_exclude:
            for name in [n.strip() for n in raw_exclude.split(",") if n.strip()]:
                try:
                    grp = ChannelGroup.objects.get(name__iexact=name)
                    exclude_ids.update(
                        Channel.objects.filter(channel_group=grp).values_list("id", flat=True)
                    )
                except ChannelGroup.DoesNotExist:
                    pass

        def _apply_exclusions(channels):
            if not exclude_ids:
                return channels
            return [ch for ch in channels if ch.id not in exclude_ids]

        if target_type == "all":
            return _apply_exclusions(list(Channel.objects.all().order_by("name")))

        if target_type == "group":
            group_id = params.get(f"{prefix}channel_group_id")
            if not group_id:
                return []
            try:
                group = ChannelGroup.objects.get(id=int(group_id))
                return _apply_exclusions(list(Channel.objects.filter(channel_group=group).order_by("name")))
            except ChannelGroup.DoesNotExist:
                return []

        if target_type == "groups":
            raw = params.get(f"{prefix}channel_group_names", "")
            names = [n.strip() for n in raw.split(",") if n.strip()]
            if not names:
                return []
            channels = []
            seen = set()
            for name in names:
                try:
                    group = ChannelGroup.objects.get(name__iexact=name)
                    for ch in Channel.objects.filter(channel_group=group).order_by("name"):
                        if ch.id not in seen:
                            seen.add(ch.id)
                            channels.append(ch)
                except ChannelGroup.DoesNotExist:
                    pass
            return _apply_exclusions(channels)

        # single channel
        channel_id = params.get(f"{prefix}channel_id")
        if not channel_id:
            return []
        try:
            ch = Channel.objects.get(id=int(channel_id))
            return [] if ch.id in exclude_ids else [ch]
        except Channel.DoesNotExist:
            return []
