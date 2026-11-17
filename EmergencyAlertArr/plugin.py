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
from eaa_styles import (STYLE_CATALOG, STYLE_KEYS, build_style_overlay, eas_spoken_text,
                        eas_test_alert, eas_event_code, style_is_live)
from eaa_styles import eas_originator_code as _eas_org_code
from eaa_filters import decide, can_preempt, classify as _eas_classify
from eaa_templates import TEMPLATE_CHOICES
from eaa_audio import render_alert_audio, AUDIO_CHANNEL_MODES, LOUDNESS_TARGETS
from eaa_tts import (TTS_ENGINES, TTS_VOICES, TTS_FX, tts_synthesize, tts_status,
                     tts_available, tts_cache_key)
logger = logging.getLogger(__name__)


def _easyplus_settings(settings):
    """(raw font setting, size scale) for EASyPlus, read from plugin settings."""
    font = settings.get("eas_easyplus_font") or ""
    try:
        scale = max(0.5, min(3.0, float(settings.get("eas_easyplus_font_scale") or 1.0)))
    except Exception:
        scale = 1.0
    return font, scale

# Kept for older call sites; real availability is checked per engine (eaa_tts).
_TTS_BIN = shutil.which("espeak-ng") or shutil.which("espeak") or "flite"

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

def _eas_tts_synthesize(text, out_path, tts_opts=None):
    """Synthesize the readout with the configured engine/voice/sound preset
    (see eaa_tts). Returns True on success; callers fall back to silence."""
    if not text:
        return False
    _ensure_dirs()
    return tts_synthesize(_tts_normalize(text), out_path, tts_opts or {})

def _eas_media_secs(path):
    """Duration of any media file via ffprobe (0.0 if unknown)."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=20)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0

def _eas_cap_audio_file(alert):
    """WAV of the alert's own audio message (IPAWS CAP <resource> "EAS
    Broadcast Content", embedded or by URL), converted and cached; None if the
    alert has none or it can't be fetched. Capped at two minutes, like an
    ENDEC's message store."""
    import hashlib
    src = alert.get("audio_file") or ""
    url = alert.get("audio_url") or ""
    if not (src and os.path.isfile(src)) and not url:
        return None
    _ensure_dirs()
    key = hashlib.md5((src or url).encode("utf-8")).hexdigest()[:16]
    out = os.path.join(RUNTIME_DIR, f"capaudio_{key}.wav")
    if os.path.isfile(out) and os.path.getsize(out) > 1000:
        return out
    tmp = None
    try:
        if not (src and os.path.isfile(src)):
            if not url.lower().startswith(("http://", "https://")):
                return None
            req = urllib.request.Request(url, headers={"User-Agent": NWS_UA})
            deadline = time.time() + 25
            buf = bytearray()
            with urllib.request.urlopen(req, timeout=10) as r:
                while len(buf) <= 12 * 1024 * 1024:
                    if time.time() > deadline:
                        raise TimeoutError("audio download too slow")
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    buf += chunk
            data = bytes(buf)
            if len(data) > 12 * 1024 * 1024 or len(data) < 500:
                return None
            tmp = os.path.join(RUNTIME_DIR, f"capaudio_{key}.src")
            with open(tmp, "wb") as f:
                f.write(data)
            src = tmp
        ff = shutil.which("ffmpeg") or "ffmpeg"
        r = subprocess.run([ff, "-v", "error", "-y", "-i", src, "-t", "120", "-vn", "-ac", "1",
                            "-ar", "48000", "-c:a", "pcm_s16le", out],
                           capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 1000:
            return out
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] EAS: alert audio unavailable ({e}); using the voice instead")
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass
    return None

def _ffq(path):
    """Quote a file path for use inside an FFmpeg filter argument (fontfile=,
    textfile=). Single quotes make ':' ',' and spaces literal to the filter
    parser; an embedded single quote is closed/escaped/reopened."""
    return "'" + str(path).replace("'", "'\\''") + "'"

def _eas_local_dt(dt):
    """NWS/IPAWS timestamps carry the issuing office's local offset and are
    shown as-is. UTC timestamps (tests, injected alerts, some CAP feeds) are
    converted to the server's local time -- they used to be printed in UTC,
    so a test at 8 PM CDT showed "1:00 AM"."""
    try:
        if dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0:
            return dt.astimezone()
    except Exception:
        pass
    return dt

def _eas_audio_graph(first_idx, mid_path=None, mid_secs=15, endec_test=False, tail_secs=7.5,
                     wav_header=None, wav_att=None, wav_eom=None, chain=None):
    """FFmpeg inputs + filter graph for the EAS audio sequence, labelled [aseq].

    Returns (inputs, graph, description). `inputs` is the list of WAV paths to
    add as -i inputs starting at input index `first_idx`.

      * Normal:     header -> attention -> mid -> EOM
                    mid = TTS readout WAV, or mid_secs of silence
      * ENDEC test: header -> EOM -> tail_secs of silence

    The sequence is followed by endless silence (apad) so the audio track never
    ends before the timed restore swaps the channel back. If the tone WAVs are
    missing the alert still plays -- with a silent track -- rather than
    failing to start."""
    wav_header = wav_header or _EAS_WAV_HEADER
    wav_att    = wav_att or _EAS_WAV_ATT
    wav_eom    = wav_eom or _EAS_WAV_EOM
    fmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    sil = "anullsrc=channel_layout=stereo:sample_rate=48000"

    if not _eas_wav_available(verbose=True, header=wav_header, att=wav_att, eom=wav_eom):
        logger.warning(
            "[EmergencyAlertarr] EAS: one or more tone WAVs missing/empty (see WAV lines "
            "above) -- drop valid eas_header.wav / eas_att.wav / eas_eom.wav "
            "into the plugin's wav/ folder, or enable 'Generate tones dynamically'. "
            "Alert will play with a silent audio track."
        )
        return [], f"{sil},{fmt}[aseq]", "silent (tone WAVs missing)"

    # The ENDEC's own output path (sample rate, filtering, compression) applied
    # to its tones, like audio coming out of that unit. The readout keeps its
    # own "Voice sound" setting.
    tfmt = f"aresample=48000,{chain},{fmt}" if chain else fmt
    n = first_idx
    if endec_test:
        inputs = [wav_header, wav_eom]
        graph = (
            f"[{n}:a]{tfmt}[h];"
            f"[{n + 1}:a]{tfmt}[e];"
            f"{sil},atrim=duration={tail_secs},{fmt}[tail];"
            f"[h][e][tail]concat=n=3:v=0:a=1,apad[aseq]"
        )
        return inputs, graph, f"ENDEC test (header+EOM+{tail_secs}s tail)"

    if mid_path:
        inputs = [wav_header, wav_att, mid_path, wav_eom]
        mid = f"[{n + 2}:a]{fmt}[mid];"
        e_idx = n + 3
        desc = "TTS readout"
    else:
        inputs = [wav_header, wav_att, wav_eom]
        mid = f"{sil},atrim=duration={mid_secs},{fmt}[mid];"
        e_idx = n + 2
        desc = f"{mid_secs}s silence"
    graph = (
        f"[{n}:a]{tfmt}[h];"
        f"[{n + 1}:a]{tfmt}[a];"
        f"{mid}"
        f"[{e_idx}:a]{tfmt}[e];"
        f"[h][a][mid][e]concat=n=4:v=0:a=1,apad[aseq]"
    )
    return inputs, graph, desc

# Alert canvas per "Transcode Quality" setting: (width, height). The overlay is
# a full takeover, so the alert is rendered on an FFmpeg-generated canvas
# instead of decoding the live channel underneath it.
_EAS_CANVAS = {
    "full":    (1920, 1080),
    "1080p30": (1920, 1080),
    "720p":    (1280, 720),
    "720p30":  (1280, 720),
}

_EAS_FPS = 30

# Seconds of alert the encoder may burst out immediately on start. Dispatcharr
# holds a (re)connecting viewer until ~1 MB is buffered and treats an FFmpeg
# speed below 1.0x as "buffering" (and fails over after 15 s); a short burst
# satisfies both at once, so the alert screen appears almost immediately.
_EAS_BURST_SECS = 4

_ffmpeg_opts_cache = {}

def _ffmpeg_has_option(name, binary="ffmpeg"):
    """True if the installed FFmpeg knows `-name` (checked once, cached)."""
    key = (binary, name)
    if key not in _ffmpeg_opts_cache:
        ok = True   # assume modern FFmpeg (Dispatcharr ships 8.x) if the probe fails
        try:
            exe = shutil.which(binary) or binary
            out = subprocess.run([exe, "-hide_banner", "-h", "full"],
                                 capture_output=True, text=True, timeout=10).stdout
            if out:
                ok = f"-{name} " in out
        except Exception:
            pass
        _ffmpeg_opts_cache[key] = ok
    return _ffmpeg_opts_cache[key]

def _eas_video_kbps(height):
    """Constant video bitrate for the alert screen. Static text compresses to
    almost nothing (tens of kbps), which left Dispatcharr waiting 30-60 s to
    fill its ~1 MB start buffer before the viewer saw anything -- the long
    DASDEC switch-over. Padding to a fixed CBR fills it in well under a second."""
    return 4000 if height >= 1080 else 2500

def _build_eas_ffmpeg_params(overlay, out_w, out_h, audio_inputs, audio_graph_fn, max_secs=None,
                             sd_look=False, preroll=None, end_glitch=None, end_style="clean",
                             end_freeze=1.0, tag=None):
    """Complete, self-contained FFmpeg parameter string for an alert profile.

    Full-screen styles are drawn on a generated canvas (lavfi colour source):
    FFmpeg never has to connect to the provider, probe, wait for a keyframe or
    decode the channel before the first alert frame. Ticker styles that run
    over the program open the live stream instead ({streamUrl}/{userAgent}
    are filled in by Dispatcharr), so they start a little slower.

    sd_look:    render the page through a 480-line, chroma-smeared SD path
                (older DASDEC / EASyPLUS / Gorman-Redlich units were SD boxes)
    preroll:    {"path", "secs"} -- a pre-rendered clip played first (force-
                tune mode: the details channel, then the blocky retune)
    end_glitch: seconds into the output at which the picture breaks up as the
                set-top retunes back (force-tune mode)

    audio_graph_fn(first_input_index) -> filter graph producing [aseq]."""
    fps = _EAS_FPS
    kbps = _eas_video_kbps(out_h)
    if _ffmpeg_has_option("readrate_initial_burst"):
        pace = f"-re -readrate_initial_burst {_EAS_BURST_SECS}"
    else:
        pace = "-readrate 1.1"
    if overlay.get("live"):
        inputs = ["-user_agent {userAgent} -i {streamUrl}"]
        video = (f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                 f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},"
                 f"format=yuv420p,{overlay['filter']},{overlay['finish']}")
    else:
        cw, ch, bg = overlay["canvas"]
        inputs = [f'{pace} -f lavfi -i "color=c={bg}:s={cw}x{ch}:r={fps}"']
        sd = ("," + _eas_sd_chain(cw, ch)) if sd_look else ""
        video = f"[0:v]format=yuv420p,{overlay['filter']}{sd},{overlay['finish']},format=yuv420p"
    pre_secs = float((preroll or {}).get("secs") or 0.0)
    if end_glitch and end_style == "blocky":
        t0 = max(1.0, float(end_glitch) - pre_secs)
        video += (f"[vm];[vm]split=2[vk][vg];"
                  f"[vg]scale=iw/24:ih/24:flags=area,scale={out_w}:{out_h}:flags=neighbor,"
                  f"eq=brightness=-0.08:saturation=0.7[vb];"
                  f"[vk][vb]overlay=enable='gte(t,{t0:.2f})'")
    elif end_glitch:
        # Retune back to the channel: the alert picture freezes, then black
        # until the stream switches back.
        t0 = max(1.0, float(end_glitch) - pre_secs)
        f0 = int(round(t0 * fps))
        f1 = int(round((t0 + max(0.0, float(end_freeze))) * fps))
        frz = (f"[vm];[vm]split=2[va][vr];[va][vr]freezeframes=first={f0}:last={f1}:replace={f0}"
               if f1 > f0 else "")
        video += (f"{frz},drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:"
                  f"enable='gte(t,{(t0 + max(0.0, float(end_freeze))):.2f})'")
    for p in audio_inputs:
        inputs.append(f'-i "{p}"')
    agraph = audio_graph_fn(1)
    post = overlay.get("post")
    if preroll:
        pidx = 1 + len(audio_inputs)
        inputs.append(f'-i "{preroll["path"]}"')
        agraph = agraph[::-1].replace("]qesa[", "]0qesa[", 1)[::-1]    # last [aseq] -> [aseq0]
        tail = (f"[vc];[vc]{post},format=yuv420p[vout]" if post else "[vout]")
        fc = (f"{video}[v1];"
              f"[{pidx}:v]fps={fps},scale={out_w}:{out_h},format=yuv420p,setsar=1[pv];"
              f"[pv][v1]concat=n=2:v=1:a=0{tail};"
              f"{agraph};"
              f"[{pidx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[pa];"
              f"[pa][aseq0]concat=n=2:v=0:a=1[aseq]")
    else:
        fc = (f"{video},{post},format=yuv420p[vout];{agraph}" if post else f"{video}[vout];{agraph}")
    return (
        "-hide_banner -nostdin "
        + " ".join(inputs)
        + f' -filter_complex "{fc}"'
        + ' -map "[vout]" -map "[aseq]"'
        + " -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p"
        + f" -g {fps} -keyint_min {fps} -sc_threshold 0"
        + f" -b:v {kbps}k -minrate {kbps}k -maxrate {kbps}k -bufsize {kbps // 2}k"
        + " -x264-params nal-hrd=cbr"
        + " -c:a aac -b:a 192k -ar 48000 -ac 2"
        # Last-resort cap: an alert process can never outlive its sequence by
        # more than a few minutes, even if the restore's stream restart failed.
        + (f" -t {float(max_secs):.2f}" if max_secs else "")
        + (f" -metadata service_name=EAA-{tag}" if tag else "")
        + " -sn -dn -flush_packets 1 -f mpegts pipe:1"
    )

def _eas_sd_chain(cw, ch):
    """480-line SD path: area downscale, NTSC-ish chroma smear and delay, a
    touch of noise, then back up to the canvas size (soft, like a set-top
    upscaling a 480i channel)."""
    return ("scale=-2:480:flags=area,format=yuv420p,"
            "boxblur=luma_radius=0:luma_power=0:chroma_radius=3:chroma_power=1,"
            "chromashift=cbh=2:crh=1,noise=c0s=3:c0f=t,"
            f"scale={cw}:{ch}:flags=bicubic")

# Styles drawn over the live program (no retune lead-in for those).
_EAS_LIVE_STYLE_KEYS = {"easyplus_ticker", "crawl_overlay", "gorman_redlich_overlay", "sara", "sara_classic"}

# Older, SD-era screen styles that get the 480-line look when it's on.
_EAS_LEGACY_STYLES = {
    "dasdec_legacy", "easyplus", "easyplus_middle", "easyplus_bottom", "easyplus_gray",
    "easyplus_grayblack", "gorman_redlich", "vds_subalert", "easycap",
}

# Which ENDEC's sound goes with each screen style ("auto").
_STYLE_ENDEC = {
    "easyplus": "trilithic", "easycap": "easycap", "gorman": "gorman",
    "vds": "sage_1822",
}

def _eas_endec_for_style(style):
    st = (style or "").lower()
    for prefix, endec in _STYLE_ENDEC.items():
        if st.startswith(prefix):
            return endec
    return "dasdec"           # DASDEC, SARA/DASDEC headends, set-top and ticker styles

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

def _eas_tts_cached(spoken, tts_opts=None):
    """Synthesize (or reuse) the readout WAV for this exact text + voice. When
    one alert fires on several channels at once the readout is generated once
    instead of once per channel."""
    import hashlib
    key = hashlib.md5((tts_cache_key(tts_opts) + "\n" + spoken).encode("utf-8")).hexdigest()[:16]
    path = os.path.join(RUNTIME_DIR, f"eas_tts_{key}.wav")
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 200:
            os.utime(path, None)
            return path
    except Exception:
        pass
    tmp = path + f".tmp{os.getpid()}.wav"
    if _eas_tts_synthesize(spoken, tmp, tts_opts):
        try:
            os.replace(tmp, path)
            # Keep the runtime dir tidy: drop readouts not used for a day.
            cutoff = time.time() - 86400
            for fn in os.listdir(RUNTIME_DIR):
                if fn.startswith("eas_tts_") and fn.endswith(".wav"):
                    fp = os.path.join(RUNTIME_DIR, fn)
                    try:
                        if os.path.getmtime(fp) < cutoff:
                            os.remove(fp)
                    except Exception:
                        pass
            return path
        except Exception:
            return tmp
    return None

def _clone_and_inject_eas(channel_id, original_profile=None, channel_name="", silence_secs=15,
                          transcode_mode="full", style="easyplus", unique_alerts=None,
                          use_tts=True, endec_test=False, tail_secs=7.5,
                          generate_tones=False, att_secs=8.0,
                          easyplus_font=None, easyplus_scale=1.0, style_opts=None,
                          tts_opts=None, endec=None, audio_opts=None, video_opts=None,
                          preroll=None, eom_only=False, national_hold=False):
    """Create the EAS-overlay stream profile for one channel.

    Returns (profile, removed_flags, total_duration). total_duration is the
    authoritative length of the whole alert sequence -- the audio concat, the
    EASyPlus scroll, and the DASDEC pagination are all timed to it so they all
    finish together. Callers use it to schedule the timed restore.

    original_profile is only used for naming (it may be None for channels that
    use the system default profile): the alert profile is a self-contained
    FFmpeg command, independent of the channel's normal profile, so odd base
    profiles (streamlink/VLC, hwaccel flags, existing -vf chains) can no longer
    break the alert.

    endec_test: header tones -> EOM tones -> tail_secs of silence with the
    alert screen still up, then clear. No attention tones, no TTS.
    """
    from core.models import StreamProfile
    _ensure_dirs()

    # Hard guarantee: never render more than one alert on screen or in the
    # readout, even if something upstream regresses.
    unique_alerts = list(unique_alerts or [])[:1]

    # --- Middle audio segment (TTS readout vs plain silence) -----------------
    mid_path = None
    mid_secs = silence_secs
    aopts = dict(audio_opts or {})
    vopts = dict(video_opts or {})
    cap_audio = None
    if eom_only:
        endec_test = True
    if not endec_test and unique_alerts and aopts.get("cap_audio", True):
        cap_audio = _eas_cap_audio_file(unique_alerts[0])
    if cap_audio:
        # The alert carries its own recorded message (IPAWS "EAS Broadcast
        # Content"): a DASDEC plays that instead of synthesizing speech.
        mid_path = cap_audio
        mid_secs = min(_wav_duration_secs(cap_audio) or _eas_media_secs(cap_audio), 120.0) + 2.0
        logger.info(f"[EmergencyAlertarr] EAS: using the alert's own audio message for ch {channel_id}")
    elif not endec_test and use_tts and tts_available(tts_opts) and unique_alerts:
        spoken = eas_spoken_text(unique_alerts[0], style_opts or {})
        if len(spoken) > _EAS_MAX_SPOKEN_CHARS:
            cut = spoken[:_EAS_MAX_SPOKEN_CHARS]
            dot = cut.rfind(". ")
            spoken = (cut[:dot + 1] if dot > 200 else cut).strip()
        tts_path = _eas_tts_cached(spoken, tts_opts) if spoken else None
        if tts_path:
            mid_path = tts_path
            mid_secs = _wav_duration_secs(tts_path) + 2.0
        else:
            logger.info(f"[EmergencyAlertarr] EAS: TTS unavailable for ch {channel_id}, using silence")

    # --- Optional: synthesize the tone WAVs dynamically ----------------------
    gen_h = gen_a = gen_e = None
    if generate_tones:
        src_alert = unique_alerts[0] if unique_alerts else {}
        header_str = _eas_same_header_string(src_alert)
        gen = _eas_generate_tone_wavs(
            channel_id, header_str, att_secs, want_attn=not endec_test,
            endec=endec or _eas_endec_for_style(style),
        )
        if gen:
            gen_h, gen_a, gen_e = gen
        else:
            logger.warning(
                f"[EmergencyAlertarr] EAS: dynamic tone generation failed for ch {channel_id} "
                "-- falling back to pre-recorded WAVs."
            )

    # --- Render the whole audio track offline (loudness, encoder quirks) -----
    endec_name = endec or _eas_endec_for_style(style)
    seq = None
    if aopts.get("render", True):
        h = gen_h or _EAS_WAV_HEADER
        a = gen_a or _EAS_WAV_ATT
        e = gen_e or _EAS_WAV_EOM
        if _eas_wav_available(header=h, att=a, eom=e):
            if eom_only:
                parts = [{"kind": "silence", "secs": 0.5}, {"kind": "tone", "path": e},
                         {"kind": "silence", "secs": 2.0}]
            elif endec_test:
                parts = [{"kind": "tone", "path": h}, {"kind": "tone", "path": e},
                         {"kind": "silence", "secs": tail_secs}]
            elif national_hold:
                # EAN: header, attention signal, message -- the EOM is sent
                # only when the national alert is over.
                mid = ({"kind": "voice", "path": mid_path} if mid_path
                       else {"kind": "silence", "secs": mid_secs})
                parts = [{"kind": "tone", "path": h}, {"kind": "tone", "path": a}, mid]
            else:
                mid = ({"kind": "voice", "path": mid_path} if mid_path
                       else {"kind": "silence", "secs": mid_secs})
                parts = [{"kind": "tone", "path": h}, {"kind": "tone", "path": a}, mid,
                         {"kind": "tone", "path": e}]
            _ensure_dirs()
            import hashlib
            sig = json.dumps([[p.get("kind"), p.get("secs"),
                               (os.path.basename(p["path"]), os.path.getsize(p["path"]),
                                int(os.path.getmtime(p["path"]) // 60)) if p.get("path") else None]
                              for p in parts] + [_eas_same_header_string(unique_alerts[0] if unique_alerts else {}),
                                                 endec_name, sorted((k, str(v)) for k, v in aopts.items())])
            skey = hashlib.md5(sig.encode("utf-8")).hexdigest()[:16]
            spath = os.path.join(RUNTIME_DIR, f"seq_{skey}.wav")
            hit = _eas_seq_cache.get(skey)
            if hit and os.path.isfile(spath):
                seq = hit
            else:
                _eas_mutex_touch()
                seq = render_alert_audio(
                    spath, parts,
                    chain=endec_audio_chain(endec_name),
                    loudness=aopts.get("loudness", "-24"),
                    tone_offset_lu=aopts.get("tone_offset", 8.0),
                    channels=aopts.get("channels", "both"),
                    hum=aopts.get("hum"), switch_click=aopts.get("click", False),
                    boost_db=aopts.get("boost_db", 0.0),
                )
                if seq:
                    if len(_eas_seq_cache) > 20:
                        _eas_seq_cache.clear()
                    _eas_seq_cache[skey] = seq
                    _eas_cleanup_seq_files()

    # --- Authoritative total sequence duration -------------------------------
    if seq:
        total_duration = seq["duration"] + 1.0
    elif endec_test:
        _hdr = gen_h or _EAS_WAV_HEADER
        _eom = gen_e or _EAS_WAV_EOM
        header_eom = (
            (_wav_duration_secs(_hdr) + _wav_duration_secs(_eom))
            if _eas_wav_available(header=gen_h, att=gen_a, eom=gen_e) else 6.0
        )
        total_duration = header_eom + tail_secs + 1.0
    else:
        total_duration = _eas_seq_fixed_overhead(gen_h, gen_a, gen_e) + mid_secs + 2.0

    if total_duration > _EAS_MAX_ALERT_SECS:
        logger.warning(
            f"[EmergencyAlertarr] EAS: computed duration {total_duration:.0f}s exceeds cap "
            f"-- clamping to {_EAS_MAX_ALERT_SECS}s (ch {channel_id})"
        )
        total_duration = float(_EAS_MAX_ALERT_SECS)

    # --- Video: the selected EAS screen style --------------------------------
    out_w, out_h = _EAS_CANVAS.get(transcode_mode, (1920, 1080))
    _eas_clear(channel_id)
    sopts = dict(style_opts or {})
    sopts.setdefault("scale", easyplus_scale)
    if easyplus_font:
        sopts.setdefault("font_override", easyplus_font)
    overlay = build_style_overlay(channel_id, unique_alerts[0] if unique_alerts else {},
                                  style, out_w, out_h, sopts)

    holder = {}
    live_prog = (overlay.get("live") and (aopts.get("program") or "switch") in ("switch", "duck")
                 and aopts.get("has_audio", True) is not False)
    audio_end = (seq or {}).get("audio_end") or total_duration

    def _agraph(first):
        if seq:
            ins = [seq["path"]]
            desc = "rendered sequence" + (f", program {aopts.get('program')}" if live_prog else "")
            fmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
            if live_prog:
                # Program audio under/around the alert: "switch" = the ENDEC's
                # audio switch (program off while EAS audio plays, back after
                # the EOM); "duck" = program kept low underneath.
                g = 0.0 if aopts.get("program") == "switch" else 10 ** (-abs(float(aopts.get("duck_db", 18))) / 20)
                back = audio_end + 0.3
                graph = (f"[0:a]asetpts=PTS-STARTPTS,{fmt},"
                         f"volume='if(lt(t,{back:.2f}),{g:.4f},1)':eval=frame[prog];"
                         f"[{first}:a]{fmt},apad[eas];"
                         f"[prog][eas]amix=inputs=2:duration=first:normalize=0[aseq]")
            else:
                graph = f"[{first}:a]{fmt},apad[aseq]"
        elif eom_only:
            e_wav = gen_e or _EAS_WAV_EOM
            ins = [e_wav]
            desc = "EOM only"
            graph = (f"[{first}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"apad[aseq]")
        else:
            ins, graph, desc = _eas_audio_graph(
                first, mid_path=mid_path, mid_secs=mid_secs,
                endec_test=endec_test, tail_secs=tail_secs,
                wav_header=gen_h, wav_att=gen_a, wav_eom=gen_e,
                chain=endec_audio_chain(endec_name),
            )
        holder["inputs"], holder["desc"] = ins, desc
        return graph

    _agraph(1)   # resolve the audio inputs first (input 0 is always the video source)
    sd = vopts.get("sd_look") and not overlay.get("live") and (
        vopts.get("sd_look") == "all" or style in _EAS_LEGACY_STYLES)
    pre = preroll if (preroll and preroll.get("path") and os.path.isfile(preroll["path"])) else None
    if pre:
        total_duration += float(pre.get("secs") or 0.0)
    # The alert stream ENDS by itself once the sequence (and the freeze/black
    # on the way out) is over: Dispatcharr then reconnects the session to the
    # channel's normal profile without dropping any player (see
    # _eas_seamless_arm). Players never have to reconnect after an alert --
    # some (TiviMate) don't always come back from a server-side disconnect.
    out_tail = (float((audio_opts or {}).get("retune_freeze", 1.0)) + _EAS_RETUNE_BLACK) if pre else 0.4
    stream_end = total_duration + out_tail
    tag = f"ch{channel_id}-{int(time.time() * 1000)}"
    _eas_clone_info[str(channel_id)] = {"tag": tag, "end": stream_end}
    params = _build_eas_ffmpeg_params(overlay, out_w, out_h, holder["inputs"], _agraph,
                                      max_secs=(4 * 3600 if national_hold else stream_end),
                                      sd_look=bool(sd), preroll=pre,
                                      end_glitch=(total_duration if (pre and not national_hold) else None),
                                      end_style=(pre or {}).get("end", "clean"),
                                      end_freeze=(audio_opts or {}).get("retune_freeze", 1.0),
                                      tag=tag)
    audio_desc = holder["desc"]
    width, height = out_w, out_h

    base_cmd = (getattr(original_profile, "command", "") or "").strip()
    command = base_cmd if os.path.basename(base_cmd).lower().startswith("ffmpeg") else "ffmpeg"
    base_name = getattr(original_profile, "name", None) or "default"

    profile = StreamProfile(
        name=f"{PROFILE_PREFIX}EAS [{base_name}] [ch{channel_id}]",
        command=command,
        parameters=params,
        locked=False,
        is_active=True,
    )
    profile.save()
    logger.info(
        f"emergencyalertarr: EAS profile {profile.id} created for channel {channel_id} "
        f"(style={style}, {width}x{height}, pages={overlay.get('pages', 1)}, "
        f"dur={total_duration:.1f}s, audio={audio_desc})"
    )
    return profile, [], total_duration

def _eas_profile_slot(channel):
    """Where this channel's effective stream profile lives. Auto-synced
    channels can carry a ChannelOverride whose stream_profile wins over the
    channel's own field -- writing the alert profile to the channel field there
    did nothing, so the alert silently never appeared."""
    try:
        ov = channel.override
    except Exception:
        ov = None
    if ov is not None and getattr(ov, "stream_profile_id", None):
        return "override", ov
    return "channel", channel

def _eas_effective_profile(channel):
    """The StreamProfile the channel actually plays with (override first), or
    None when it falls back to the system default profile."""
    _slot, obj = _eas_profile_slot(channel)
    try:
        return obj.stream_profile
    except Exception:
        return None

def _assign_profile(channel, profile, slot=None):
    """Point the channel's effective stream profile at `profile` (None = system
    default). Returns the slot written ("override" or "channel")."""
    if slot == "override":
        try:
            obj = channel.override
        except Exception:
            obj, slot = channel, "channel"
    elif slot == "channel":
        obj = channel
    else:
        slot, obj = _eas_profile_slot(channel)
    obj.stream_profile = profile
    if slot == "override":
        obj.save()
    else:
        obj.save(update_fields=["stream_profile"])
    # NOTE: Channel.update_stream_profile() is NOT called here. Despite the name
    # it moves the channel's M3U *account* connection slot to another M3U
    # profile id; feeding it a StreamProfile id corrupted provider connection
    # counts (or moved the slot to an unrelated account).
    return slot

def _restore_profile(channel, original_profile_id, slot=None):
    from core.models import StreamProfile
    original = None
    if original_profile_id:
        original = StreamProfile.objects.filter(id=original_profile_id).first()
    # original None => restore to the system default profile.
    _assign_profile(channel, original, slot=slot)

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
            dt = _eas_local_dt(datetime.fromisoformat(iso_str.strip().replace("Z", "+00:00")))
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

def _eas_test_same_codes(settings=None):
    """Location codes for plugin-originated tests and custom alerts, like the
    list of counties a real station puts in its RMT header (more counties ->
    a longer header): the "Locations in test headers" setting, else your
    IPAWS county codes, else the counties of your NWS county zones, else the
    whole US (000000)."""
    from eaa_fips import zones_to_same
    try:
        st = settings if settings is not None else _get_settings()
        for key in ("eas_test_locations", "eas_ipaws_same_codes"):
            raw = (st.get(key) or "").replace(" ", "")
            codes = [c for c in raw.split(",") if c.isdigit() and len(c) == 6]
            if codes:
                return codes[:31]
        z = zones_to_same([x for x in (st.get("eas_zones") or "").split(",")])
        return z[:31] or ["000000"]
    except Exception:
        return ["000000"]

def _eas_build_test_alert(mode, now_utc, area=None, originator=None, station_id=None):
    """RMT/RWT as a real encoder originates it (see eaa_styles.eas_test_alert):
    EAS Participant originator, the standard RMT script, valid for one hour
    (RMT) or fifteen minutes (RWT). With no area text set, the county names
    of the header's location codes are shown."""
    from eaa_fips import describe_codes
    codes = _eas_test_same_codes()
    if not area and codes != ["000000"]:
        area = describe_codes(codes) or None
    return eas_test_alert(mode, now_utc, area=area, station_id=station_id, same_codes=codes)

def _eas_write_alert(channel_id, unique_alerts, style="easyplus"):
    """Compatibility shim: overlay text files are now written by the style
    builder when the alert profile is created. Returns the shared texts dict."""
    if not unique_alerts:
        return {}
    return _eas_alert_texts(list(unique_alerts)[:1])

def _eas_clear(channel_id):
    """Remove this channel's overlay text files (all styles)."""
    _ensure_dirs()
    import glob
    for path in glob.glob(os.path.join(RUNTIME_DIR, f"eas_{channel_id}_*.txt")):
        try:
            os.remove(path)
        except Exception:
            pass

def _restart_channel_stream(channel_uuid, label="", proactive=False, channel_id=None):
    """Stop an active channel so clients reconnect with the updated stream profile.

    stop_channel() sets a Redis 'channel_stopping' key with a 60-second TTL.
    While that key exists, is_channel_teardown_active() returns True and Dispatcharr
    rejects ALL reconnect attempts before FFmpeg even starts — causing the 10-second
    stall loop. We delete the key after ~2s (enough for FFmpeg teardown to finish)
    so clients can reconnect immediately with the new profile.

    proactive=True marks an alert being switched ON: once the stop succeeds a
    short-lived Redis marker is left so the restore timer knows the old session
    really ended (fast clients can reconnect between two restore ticks, and the
    timer used to miss the down/up cycle and leave the alert screen up for an
    extra ~90 s).
    """
    import time as _time
    tag = f" [{label}]" if label else ""
    try:
        from apps.proxy.live_proxy.services.channel_service import ChannelService
        from apps.proxy.live_proxy.redis_keys import RedisKeys
        from apps.channels.models import RedisClient
        rc = RedisClient.get_client()
        meta_key = RedisKeys.channel_metadata(channel_uuid)
        # Only wait while the channel is still starting up; any running state
        # (including "buffering", which a live provider stream drops into
        # whenever it dips below 1.0x) can be stopped right away. Waiting for
        # exactly "active" used to add up to 8 s of dead air per switch.
        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            raw = rc.hget(meta_key, "state")
            state_val = (raw.decode() if isinstance(raw, bytes) else raw) if raw else ""
            if state_val not in ("initializing", "connecting"):
                break
            _time.sleep(0.2)
        result = ChannelService.stop_channel(channel_uuid)
        if result.get("status") != "success":
            return
        if proactive and channel_id is not None:
            try:
                rc.setex(f"{_EAS_STOPPED_PREFIX}{channel_id}", 300, str(_time.time()))
            except Exception:
                pass
        # Newer Dispatcharr deletes its own stop marker (with the rest of the
        # channel's Redis keys) within milliseconds of stop_channel(). Players
        # attached through OTHER uWSGI workers only check for that marker once
        # a second, so they could miss it, sit on a frozen picture for ~15 s
        # until a data timeout, and reconnect long after the alert started
        # (or never see it). Hold the marker for a moment so every worker
        # drops its players right away, and so the old session's stream thread
        # has finished before the new one (the alert) starts; players retry on
        # the 503 + Retry-After: 1 meanwhile.
        try:
            rc.setex(RedisKeys.channel_stopping(channel_uuid), 15, "true")
        except Exception:
            pass
        _time.sleep(2.5)
        rc.delete(RedisKeys.channel_stopping(channel_uuid))
        state_raw = rc.hget(meta_key, "state")
        state_val = (state_raw.decode() if isinstance(state_raw, bytes) else state_raw) if state_raw else ""
        if state_val == "stopping":
            rc.hdel(meta_key, "state")
        logger.info(f"emergencyalertarr:{tag} channel {channel_uuid} reconnect gate cleared")
    except ImportError:
        logger.warning(f"emergencyalertarr:{tag} live_proxy unavailable — profile will apply on next client connect")
    except Exception as e:
        logger.warning(f"emergencyalertarr:{tag} restart {channel_uuid} failed: {e}")

def _restart_channel_stream_async(channel, label="", proactive=False):
    """Non-blocking wrapper — runs _restart_channel_stream in a daemon thread so the 2s sleep never blocks the caller."""
    import threading as _threading
    _threading.Thread(
        target=_restart_channel_stream,
        args=(str(channel.uuid), label, proactive, str(channel.id)),
        daemon=True,
    ).start()

def _eas_restart_channel(channel_uuid):
    _restart_channel_stream(channel_uuid, label="EAS")

_EMERGENCYALERTARR_EAS_BUILD = "2026-10-14 v0.9.10 (alert follows its own session; no early switch-back)"

_EAS_MAX_SPOKEN_CHARS = 1400

_EAS_MAX_ALERT_SECS   = 240

_eas_active     = {}

_eas_lock       = threading.Lock()

_eas_seen_mem   = {"init": False, "ids": {}}

_eas_last_end_mem = {"ts": 0.0}

_eas_clone_mem  = set()

_eas_tick_count = {"n": 0}

_eas_sched_mem  = {"last": None}

_eas_json_mem   = {}

_TICKER_FIXED_LEN = 600

_EAS_REDIS_KEY    = f"emergencyalertarr:{_PLUGIN_KEY}:eas_result"

_EAS_REDIS_LOCK   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_poll_lock"

_EAS_STATE_KEY    = f"emergencyalertarr:{_PLUGIN_KEY}:eas_state"

_EAS_OWNER_KEY    = f"emergencyalertarr:{_PLUGIN_KEY}:eas_owner"

_EAS_ALERTS_KEY   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_alerts"

_EAS_ROTATION_KEY = f"emergencyalertarr:{_PLUGIN_KEY}:eas_rotation"

_EAS_GLOBAL_KEY   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_global_announced"

_EAS_SEEN_KEY     = f"emergencyalertarr:{_PLUGIN_KEY}:eas_seen"

_EAS_SEEN_TTL     = 24 * 3600

# How long an announced alert is remembered after it leaves the feed.
_EAS_SEEN_KEEP    = 12 * 3600

# One lock for every read-modify-write of mappings.json (see _EasMutex).
_EAS_MUTEX_KEY    = f"emergencyalertarr:{_PLUGIN_KEY}:eas_mutex"

_EAS_MUTEX_TTL    = 120

# Set by _restart_channel_stream when an alert switch-on stop succeeded.
_EAS_STOPPED_PREFIX = f"emergencyalertarr:{_PLUGIN_KEY}:eas_stopped:"

_EAS_SCHED_KEY    = f"emergencyalertarr:{_PLUGIN_KEY}:eas_sched_last"

_EAS_SCHED_LOCK   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_sched_lock"

_EAS_HISTORY_KEY  = f"emergencyalertarr:{_PLUGIN_KEY}:eas_history"

# Fingerprint of the alert-area settings the seen set was seeded for.
_EAS_CFG_FP_KEY   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_cfg_fp"

# When the last alert finished (epoch secs) -- spacing between alerts.
_EAS_LAST_END_KEY = f"emergencyalertarr:{_PLUGIN_KEY}:eas_last_end"

# Alert profile ids this plugin created recently (stale-stream watchdog).
_EAS_CLONES_KEY   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_clone_ids"

_EAS_WD_PREFIX    = f"emergencyalertarr:{_PLUGIN_KEY}:eas_wd:"

# Alerts waiting out a timed-relay hold ({key: {"first": ts, "hold": min}}).
_EAS_PENDING_KEY  = f"emergencyalertarr:{_PLUGIN_KEY}:eas_pending"

# Recently received alerts by SAME identity (cross-source duplicate check).
_EAS_RECENT_KEY   = f"emergencyalertarr:{_PLUGIN_KEY}:eas_recent"

# The national alert (EAN) currently holding the channels, if any.
_EAS_NATIONAL_KEY = f"emergencyalertarr:{_PLUGIN_KEY}:eas_national"

# Alerts decoded from a monitored channel's audio (list, newest last).
_EAS_MONITOR_KEY  = f"emergencyalertarr:{_PLUGIN_KEY}:eas_monitor_alerts"

_EAS_MONITOR_STATUS_KEY = f"emergencyalertarr:{_PLUGIN_KEY}:eas_monitor_status"

# The alert that just went out, for viewers who tune in during the next few seconds.
_EAS_JOIN_KEY     = f"emergencyalertarr:{_PLUGIN_KEY}:eas_join_window"

_EAS_JOIN_SECS    = 5.0
# The alert "airs" system-wide for its length whether or not anyone is
# watching: an alert nobody saw is NOT saved up for the next channel someone
# tunes to, and the next queued alert waits for this one's slot to end.
_EAS_AIR_KEY      = f"emergencyalertarr:{_PLUGIN_KEY}:eas_air"
_EAS_UNWATCHED_AIR_SECS = 60.0      # slot length when it went out to nobody
# A channel whose viewer left mid-alert is put back right away (after the
# switch-on reconnect has had time to happen), so flipping back later shows
# the normal channel instead of the alert.
_EAS_LEFT_MIN_AGE = 12.0
_EAS_LEFT_GRACE   = 15.0

_EAS_HISTORY_FILE = os.path.join(_DATA_DIR, "eas_history.json")

_EAS_HISTORY_MAX  = 50

_EAS_CACHE_TTL    = 50

_EAS_RECONNECT_GRACE = 8.0

_EAS_POST_RESUME_GRACE = 3.0

_EAS_MAX_RECONNECT_WAIT = 90.0

# Hard limit, from the moment an alert fires, after which its channel is
# restored no matter what state the restore bookkeeping is in.
_EAS_HARD_DEADLINE_EXTRA = 150.0

# The alert FFmpeg process itself exits this long after its sequence (-t),
# so a channel whose restart was missed cannot sit on the alert forever.
_EAS_PROC_CAP_EXTRA = 240.0

# Alert-flow guards (settings can override the first two).
_EAS_DEFAULT_GAP_SECS = 60          # quiet time between two alerts
_EAS_ALL_MODE_GAP_SECS = 300        # ... when watching the whole country ("ALL")
_EAS_DEFAULT_MAX_AGE_MIN = 20       # alerts older than this are never announced
_EAS_WATCHDOG_EVERY = 15            # restore ticks between watchdog passes

_EAS_TICK_SECS = 2

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
    """Stable string key for a set of alerts (kept for diagnostics)."""
    ids = sorted(a.get("id", "") for a in alerts)
    return "|".join(ids) if ids else ""

# ---------------------------------------------------------------------------
# Cross-worker mutex
# ---------------------------------------------------------------------------
# Dispatcharr runs several worker processes and every one of them runs this
# plugin's poller (sweep + fast restore tick), plus UI actions run in whichever
# worker served the request. All of them read-modify-write mappings.json. They
# used to do that under different locks (or none), so one worker could save a
# stale copy over another's fresh write: an alert that just fired lost its
# restore timer (channel stuck on the alert profile), or a restore was undone.
# Every mapping mutation now happens under this one lock, and the mappings are
# re-read after it is acquired.

_eas_local_mutex = threading.Lock()

_EAS_MUTEX_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

class _EasMutex:
    """`with _EasMutex(rc, wait=s) as held:` -- held is True when the lock was
    acquired within `wait` seconds. Not re-entrant: never nest it."""

    def __init__(self, rc=None, wait=0.0, ttl=None):
        self.rc = rc if rc is not None else _get_redis_client()
        self.wait = max(0.0, float(wait or 0))
        self.ttl = int(ttl or _EAS_MUTEX_TTL)
        self.token = f"{os.getpid()}:{threading.get_ident()}:{time.time()}"
        self.held = False
        self._local = False

    def __enter__(self):
        deadline = time.time() + self.wait
        if self.rc is None:
            self.held = _eas_local_mutex.acquire(timeout=self.wait if self.wait > 0 else 0.01)
            self._local = self.held
            return self.held
        while True:
            try:
                if self.rc.set(_EAS_MUTEX_KEY, self.token, nx=True, ex=self.ttl):
                    self.held = True
                    _eas_mutex_tls.held = (self.rc, self.token, self.ttl)
                    return True
            except Exception as e:
                logger.warning(f"[EmergencyAlertarr] EAS: mutex unavailable ({e}); continuing unlocked")
                self.held = True
                self.rc = None
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.25)

    def __exit__(self, *exc):
        if not self.held:
            return False
        _eas_mutex_tls.held = None
        if self._local:
            try:
                _eas_local_mutex.release()
            except Exception:
                pass
        elif self.rc is not None:
            try:
                self.rc.eval(_EAS_MUTEX_RELEASE_LUA, 1, _EAS_MUTEX_KEY, self.token)
            except Exception:
                try:
                    self.rc.delete(_EAS_MUTEX_KEY)
                except Exception:
                    pass
        self.held = False
        return False

_eas_mutex_tls = threading.local()

def _eas_mutex_touch():
    """Extend the mutex TTL while this thread holds it -- called between slow
    steps (preroll capture, audio render, one channel after another) so a long
    activation can never outlive the lock and let a second worker fire the
    same alert."""
    h = getattr(_eas_mutex_tls, "held", None)
    if not h:
        return
    rc, token, ttl = h
    try:
        v = rc.get(_EAS_MUTEX_KEY)
        v = v.decode() if isinstance(v, bytes) else v
        if v == token:
            rc.expire(_EAS_MUTEX_KEY, int(ttl))
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Already-announced ("seen") set
# ---------------------------------------------------------------------------
# {alert key -> last time it was present in the feed}. Keys are the merged
# content ID plus the raw NWS/IPAWS identifiers. Entries are kept for
# _EAS_SEEN_KEEP after the alert leaves the feed, instead of being dropped the
# moment one poll doesn't contain them -- a single failed/partial fetch used to
# wipe the set and make every still-active alert play again.

def _eas_seen_read(rc):
    """dict of seen keys, or None if the set has never been initialised."""
    raw = None
    if rc:
        try:
            raw = rc.get(_EAS_SEEN_KEY)
        except Exception:
            raw = None
        if raw is None:
            return None
    else:
        if not _eas_seen_mem["init"]:
            return None
        return dict(_eas_seen_mem["ids"])
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    now = time.time()
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            try:
                out[k] = float(v)
            except Exception:
                out[k] = now
        return out
    if isinstance(data, list):           # legacy format: plain list of IDs
        return {k: now for k in data if k}
    return {}

def _eas_seen_write(rc, seen):
    """Persist the seen set (Redis, or in-process fallback)."""
    if not isinstance(seen, dict):
        now = time.time()
        seen = {k: now for k in (seen or []) if k}
    clean = {k: v for k, v in seen.items() if k}
    if rc:
        try:
            rc.setex(_EAS_SEEN_KEY, _EAS_SEEN_TTL, json.dumps(clean))
            return
        except Exception:
            pass
    _eas_seen_mem["ids"] = dict(clean)
    _eas_seen_mem["init"] = True

def _eas_streaming_ids(rc=None):
    """Armed channel IDs someone is actually watching right now.

    Uses Dispatcharr's live-proxy client registry (live:channel:<uuid>:clients
    plus each client's heartbeat-refreshed record). The old check used the
    channel_stream:<n> keys, but those also exist for stream IDs (stream
    previews, M3U slot bookkeeping) and can be left behind after a crash -- so
    with every channel armed, an alert fired on hundreds of channels nobody
    was watching. Our own frame grabs / channel monitor don't count."""
    rc = rc or _get_redis_client()
    ids = set()
    if not rc:
        return ids
    try:
        umap = _get_uuid_to_id_map(_get_mappings())
    except Exception:
        umap = {}
    if not umap:
        return ids
    try:
        keys = list(rc.scan_iter("live:channel:*:clients", count=1000))
    except Exception:
        try:
            keys = rc.keys("live:channel:*:clients")
        except Exception:
            keys = []
    for raw in keys:
        k = raw.decode() if isinstance(raw, bytes) else raw
        parts = k.split(":")
        if len(parts) != 4:
            continue
        cid = umap.get(parts[2].lower())
        if cid is None:
            continue
        try:
            members = rc.smembers(k) or []
        except Exception:
            continue
        for m in members:
            m = m.decode() if isinstance(m, bytes) else m
            try:
                ua = rc.hget(f"live:channel:{parts[2]}:clients:{m}", "user_agent")
            except Exception:
                ua = None
            if ua is None:
                continue                      # stale entry (record expired)
            ua = ua.decode() if isinstance(ua, bytes) else str(ua)
            if ua.startswith("EmergencyAlertarr/"):
                continue                      # our own frame grab / monitor
            ids.add(str(cid))
            break
    return ids

def _eas_pop_stopped(rc, cid):
    """True (and clears it) if the alert switch-on left a 'stream stopped' marker."""
    if not rc:
        return False
    try:
        return bool(rc.delete(f"{_EAS_STOPPED_PREFIX}{cid}"))
    except Exception:
        return False

def _eas_state_set(rc, cid, value):
    """Update one channel's entry in the shared EAS state hash (best-effort)."""
    if not rc:
        return
    try:
        raw = rc.get(_EAS_STATE_KEY)
        st = json.loads(raw) if raw else {}
        if value is None:
            st.pop(str(cid), None)
        else:
            st[str(cid)] = value
        rc.setex(_EAS_STATE_KEY, 3600, json.dumps(st))
    except Exception:
        pass

def _eas_do_restore(cid, mapping, mappings, rc=None):
    """Restore one channel's normal profile and clear all EAS state.

    Every step is independent: a failure in one (DB hiccup, a deleted override
    row, a stream-restart error) is logged but never leaves the channel's EAS
    bookkeeping behind. Previously an exception here kept the channel marked
    busy, so the restore was retried and failed forever and every channel sat
    on the alert screen."""
    eas_pid = mapping.get("eas_profile_id") or mapping.get("ticker_profile_id")
    channel = None
    try:
        from apps.channels.models import Channel as _Ch
        channel = _Ch.objects.filter(id=int(cid)).first()
    except Exception as e:
        logger.error(f"[EmergencyAlertarr] EAS: restore ch {cid}: channel lookup failed: {e}")
    if channel:
        try:
            _restore_profile(channel, mapping.get("original_profile_id"), slot=mapping.get("eas_slot"))
        except Exception as e:
            logger.error(f"[EmergencyAlertarr] EAS: restore ch {cid}: profile restore failed: {e}")
            try:
                _assign_profile(channel, None, slot=mapping.get("eas_slot"))   # system default
            except Exception:
                pass
        try:
            _restart_channel_stream_async(channel, label="EAS restore")
        except Exception as e:
            logger.error(f"[EmergencyAlertarr] EAS: restore ch {cid}: stream restart failed: {e}")
    if eas_pid:
        _eas_clone_remember(rc, eas_pid)       # watchdog: catch a stream still on it
        _delete_cloned_profile(eas_pid)
    _eas_delete_passthrough(mapping.get("eas_passthrough_id"))
    _eas_armed_clear(rc, cid)
    for k in ("eas_profile_id", "ticker_profile_id", "eas_restore_at", "eas_await_stream",
              "eas_seen_down", "eas_seq_secs", "eas_slot", "eas_hard_deadline", "eas_fired_at",
              "eas_national", "eas_priority", "eas_returned", "eas_tag", "eas_stream_end",
              "eas_return_profile_id", "eas_passthrough_id", "eas_gone_since"):
        mapping.pop(k, None)
    mappings[cid] = mapping
    for fn in (lambda: _eas_clear(cid), lambda: _eas_pop_stopped(rc, cid),
               lambda: _eas_state_set(rc, cid, None)):
        try:
            fn()
        except Exception:
            pass
    with _eas_lock:
        _eas_active.pop(cid, None)
    _eas_mark_alert_end(rc)
    logger.info(f"[EmergencyAlertarr] EAS: sequence complete, restored ch {cid}")

def _eas_mark_alert_end(rc, ts=None):
    """Remember when the last alert finished (spacing between alerts)."""
    ts = float(ts or time.time())
    _eas_last_end_mem["ts"] = ts
    if rc:
        try:
            rc.setex(_EAS_LAST_END_KEY, 6 * 3600, str(ts))
        except Exception:
            pass

def _eas_last_end(rc):
    if rc:
        try:
            v = rc.get(_EAS_LAST_END_KEY)
            if v:
                return float(v.decode() if isinstance(v, bytes) else v)
        except Exception:
            pass
    return float(_eas_last_end_mem.get("ts") or 0.0)

def _eas_clone_remember(rc, pid):
    """Keep a short record of alert profile ids so the watchdog can tell when a
    live session is still running one after its channel was restored."""
    if not pid:
        return
    _eas_clone_mem.add(str(pid))
    if rc:
        try:
            rc.sadd(_EAS_CLONES_KEY, str(pid))
            rc.expire(_EAS_CLONES_KEY, 24 * 3600)
        except Exception:
            pass

def _eas_clone_ids(rc):
    ids = set(_eas_clone_mem)
    if rc:
        try:
            for v in rc.smembers(_EAS_CLONES_KEY) or []:
                ids.add(v.decode() if isinstance(v, bytes) else str(v))
        except Exception:
            pass
    return ids

def restore_due_national(mapping, now_ts):
    try:
        return now_ts >= float(mapping.get("eas_restore_at") or 0) or \
            now_ts >= float(mapping.get("eas_hard_deadline") or 0)
    except Exception:
        return False

def _eas_process_restores(mappings, streaming_ids, now_ts, rc=None):
    """Anchor each pending alert's restore countdown to actual stream resumption
    (the channel goes DOWN when the alert switches on, then comes back UP on the
    alert profile), and restore any channel whose sequence has finished.
    Returns True if anything changed.

    "Down" is detected either by the channel dropping out of the streaming set
    or by the marker _restart_channel_stream leaves once its stop succeeded --
    a fast client can reconnect between two ticks, and without the marker the
    down/up cycle was missed and the alert screen sat there until the ~90 s
    safety timeout."""
    changed = False
    for cid, mapping in list(mappings.items()):
        if not isinstance(mapping, dict):
            continue
        if mapping.get("eas_passthrough_id") and not mapping.get("eas_profile_id"):
            if _eas_passthrough_cleanup(cid, mapping, mappings, streaming_ids):
                changed = True
            continue
        if not (mapping.get("eas_await_stream") or mapping.get("eas_restore_at")):
            continue
        hard = mapping.get("eas_hard_deadline")
        if hard is None:
            # Armed by an older version: give it one from now.
            seq = float(mapping.get("eas_seq_secs") or 60.0)
            hard = now_ts + seq + _EAS_HARD_DEADLINE_EXTRA
            mapping["eas_hard_deadline"] = hard
            mappings[cid] = mapping
            changed = True
        if mapping.get("eas_national") and restore_due_national(mapping, now_ts):
            _eas_json_del(rc, _EAS_NATIONAL_KEY)
        if now_ts >= float(hard):
            logger.warning(f"[EmergencyAlertarr] EAS: hard deadline reached on ch {cid} -- forcing restore")
            _eas_do_restore(cid, mapping, mappings, rc)
            changed = True
            continue
        # Only once the alert has actually been playing (the arm watcher saw it
        # start): a player that is slow to reconnect after the switch must not
        # lose the alert before it ever got it.
        if (mapping.get("eas_profile_id") and not mapping.get("eas_national")
                and (mapping.get("eas_returned") or _eas_armed_get(rc, cid))
                and now_ts - float(mapping.get("eas_fired_at") or now_ts) >= _EAS_LEFT_MIN_AGE):
            if cid in streaming_ids:
                if mapping.pop("eas_gone_since", None) is not None:
                    mappings[cid] = mapping
                    changed = True
            else:
                gone = mapping.get("eas_gone_since")
                if gone is None:
                    mapping["eas_gone_since"] = now_ts
                    mappings[cid] = mapping
                    changed = True
                elif now_ts - float(gone) >= _EAS_LEFT_GRACE:
                    logger.info(f"[EmergencyAlertarr] EAS: nobody is watching ch {cid} any more -- "
                                f"alert taken off it")
                    _eas_do_restore(cid, mapping, mappings, rc)
                    changed = True
                    continue
        if mapping.get("eas_await_stream"):
            restore_at = mapping.get("eas_restore_at")
            if restore_at and now_ts >= float(restore_at):
                # Safety timeout: the viewer never came back on the alert profile.
                try:
                    _eas_do_restore(cid, mapping, mappings, rc)
                    changed = True
                except Exception as e:
                    logger.error(f"[EmergencyAlertarr] EAS: safety restore failed ch {cid}: {e}", exc_info=True)
                continue
            armed = _eas_armed_get(rc, cid) if not mapping.get("eas_national") else None
            if (armed and armed.get("tag") == mapping.get("eas_tag") and mapping.get("eas_stream_end")
                    and armed.get("neutralized", True)):
                started = float(armed.get("ts") or now_ts)
                mapping.pop("eas_await_stream", None)
                mapping.pop("eas_seen_down", None)
                mapping["eas_returned"] = True
                mapping["eas_restore_at"] = started + float(mapping["eas_stream_end"]) + 12
                mappings[cid] = mapping
                changed = True
                logger.info(f"[EmergencyAlertarr] EAS: overlay live on ch {cid} -- back to the channel "
                            f"in {max(0.0, started + float(mapping['eas_stream_end']) - now_ts):.0f}s "
                            f"(seamless)")
                continue
            if not mapping.get("eas_seen_down"):
                if cid not in streaming_ids or _eas_pop_stopped(rc, cid):
                    mapping["eas_seen_down"] = True
                    mappings[cid] = mapping
                    changed = True
                continue   # countdown starts on a later tick, once it is back up
            if cid in streaming_ids and mapping.get("eas_national"):
                mapping.pop("eas_await_stream", None)
                mapping.pop("eas_seen_down", None)
                mappings[cid] = mapping
                changed = True
                logger.info(f"[EmergencyAlertarr] EAS: national alert live on ch {cid} -- holding")
                continue
            if cid in streaming_ids:
                seq = float(mapping.get("eas_seq_secs") or 30.0)
                mapping.pop("eas_await_stream", None)
                mapping.pop("eas_seen_down", None)
                armed = False
                fired = float(mapping.get("eas_fired_at") or 0)
                if mapping.get("eas_stream_end") and mapping.get("eas_profile_id"):
                    # The arm watcher follows the alert session and switches it
                    # back near the end. Neutralizing here, the moment the
                    # viewer showed up again, raced Dispatcharr starting the
                    # alert session: a player that reconnected slowly (TiviMate)
                    # got the normal stream back instead of the alert.
                    a2 = _eas_armed_get(rc, cid)
                    if not (a2 and a2.get("tag") == mapping.get("eas_tag")):
                        if now_ts - fired < 120:
                            mapping["eas_await_stream"] = True
                            mapping["eas_seen_down"] = True
                            mappings[cid] = mapping
                            continue
                        # Never confirmed: fall back to a timed stop/restart.
                    else:
                        mapping["eas_await_stream"] = True
                        mapping["eas_seen_down"] = True
                        mappings[cid] = mapping
                        continue
                if armed:
                    # The alert stream ends itself at eas_stream_end; tidy up a
                    # little after that.
                    mapping["eas_restore_at"] = max(fired, now_ts - 10) + float(mapping["eas_stream_end"]) + 12
                    logger.info(f"[EmergencyAlertarr] EAS: overlay live on ch {cid} -- back to the "
                                f"channel in {float(mapping['eas_stream_end']):.0f}s (seamless)")
                else:
                    mapping["eas_restore_at"] = now_ts + seq + _EAS_POST_RESUME_GRACE
                    logger.info(
                        f"[EmergencyAlertarr] EAS: overlay live on ch {cid} -- "
                        f"restoring in {seq + _EAS_POST_RESUME_GRACE:.0f}s"
                    )
                mappings[cid] = mapping
                changed = True
            continue
        restore_at = mapping.get("eas_restore_at")
        if restore_at and now_ts >= float(restore_at):
            try:
                if mapping.get("eas_returned"):
                    _eas_finish_seamless(cid, mapping, mappings, rc)
                else:
                    _eas_do_restore(cid, mapping, mappings, rc)
                changed = True
            except Exception as e:
                logger.error(f"[EmergencyAlertarr] EAS: timed restore failed ch {cid}: {e}", exc_info=True)
    return changed

def _eas_restore_tick():
    """Fast, network-free restore check run every couple of seconds between full
    alert polls, so overlay timing is accurate even when the poll interval is
    long."""
    try:
        _eas_tick_count["n"] += 1
        watchdog_due = _eas_tick_count["n"] % _EAS_WATCHDOG_EVERY == 0
        mappings = _get_mappings()
        busy = mappings and any(
            isinstance(m, dict) and (m.get("eas_await_stream") or m.get("eas_restore_at")
                                     or m.get("eas_passthrough_id"))
            for m in mappings.values()
        )
        rc0 = _get_redis_client()
        if _eas_json_get(rc0, _EAS_JOIN_KEY, None):
            with _EasMutex(rc0) as held:
                if held:
                    mp = _get_mappings()
                    if _eas_join_window_locked(rc0, _get_settings(), mp, _eas_streaming_ids(rc0)):
                        _save_mappings(mp)
        if not busy:
            if watchdog_due and mappings:
                rc = _get_redis_client()
                with _EasMutex(rc) as held:
                    if held:
                        _eas_watchdog_locked(rc)
            return
        rc = _get_redis_client()
        with _EasMutex(rc) as held:
            if not held:
                return   # another worker is mid-sweep/tick; it will handle it
            mappings = _get_mappings()   # fresh copy now that we hold the lock
            streaming = _eas_streaming_ids(rc)
            ch = _eas_process_restores(mappings, streaming, time.time(), rc)
            if _eas_tick_count["n"] % 5 == 0 and any(
                    isinstance(m, dict) and m.get("eas_national") for m in mappings.values()):
                if _eas_national_join_locked(rc, _get_settings(), mappings, streaming):
                    ch = True
            if ch:
                _save_mappings(mappings)
            if watchdog_due:
                _eas_watchdog_locked(rc)
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
    mapping["eas_fired_at"] = float(now)
    mapping["eas_hard_deadline"] = (float(now) + _EAS_MAX_RECONNECT_WAIT + float(seq_duration)
                                    + _EAS_HARD_DEADLINE_EXTRA)

def _eas_fire_cfg(settings):
    """Overlay/audio settings shared by real alerts, tests and injections."""
    def _num(key, default, lo, hi, cast=float):
        try:
            return max(lo, min(hi, cast(settings.get(key) or default)))
        except Exception:
            return default
    ep_font, ep_scale = _easyplus_settings(settings)
    return {
        "silence_secs":   _num("eas_silence_secs", 15, 10, 20, int),
        "transcode_mode": settings.get("eas_transcode_mode") or "full",
        "style":          (settings.get("eas_overlay_style") or "dasdec").lower(),
        "use_tts":        bool(settings.get("eas_tts_enabled", True)),
        "generate_tones": bool(settings.get("eas_generate_tones", True)),
        "att_secs":       _num("eas_att_secs", 8.0, 4.0, 30.0),
        "tail_secs":      _num("eas_endec_tail_secs", 7.5, 1.0, 30.0),
        "ep_font":        ep_font,
        "ep_scale":       ep_scale,
        "style_opts":     _eas_style_opts(settings),
        "tts_opts":       _eas_tts_opts(settings),
        "endec":          None,          # always the encoder that matches the screen style
        # FCC 47 CFR 11.61(a)(2): a Required Weekly Test is header + EOM only.
        "rwt_short":      bool(settings.get("eas_endec_test_mode", True)),
        "audio_opts": {
            "render":      True,
            "loudness":    str(settings.get("eas_loudness") or "-24"),
            "tone_offset": _num("eas_tone_offset_lu", 8.0, 0.0, 20.0),
            "channels":    (settings.get("eas_audio_channels") or "both").lower(),
            "hum":         {"60": 60, "50": 50}.get(str(settings.get("eas_audio_hum") or "off")),
            "click":       bool(settings.get("eas_audio_click", True)),
            "program":     (settings.get("eas_program_audio") or "switch").lower(),
            "duck_db":     _num("eas_duck_db", 18.0, 6.0, 40.0),
            "cap_audio":   bool(settings.get("eas_use_cap_audio", True)),
            "retune_freeze": _EAS_RETUNE_FREEZE,
            # SARA set-top boxes play the alert a little hotter than the channel.
            "boost_db":    4.0 if (settings.get("eas_overlay_style") or "").lower().startswith("sara") else 0.0,
        },
        "video_opts": {
            "sd_look": {"legacy": "legacy", "all": "all"}.get(
                (settings.get("eas_sd_look") or "legacy").lower()),
        },
        "relay_own_id":   bool(settings.get("eas_relay_own_id", True)),
        "cable_mode":     _eas_transition_for(settings)[0],
        "details_channel": _eas_chan(settings, "eas_details_channel"),
        "details_secs":   2.0,
        "retune_freeze":  _EAS_RETUNE_FREEZE,
        "retune_black":   _EAS_RETUNE_BLACK,
        "retune_blocky":  _eas_transition_for(settings)[1],
        "dispatcharr_url": (settings.get("eas_dispatcharr_url") or "").strip(),
        "transcode_mode_out": settings.get("eas_transcode_mode") or "full",
    }

# Channel switch timing (fixed, like a set-top box retuning).
_EAS_RETUNE_FREEZE = 1.0
_EAS_RETUNE_BLACK = 1.5

def _eas_chan(settings, key):
    """Channel id from a channel-picker setting ('' when none). Dispatcharr
    rejects select options with an empty value, so 'none' is the sentinel."""
    v = str(settings.get(key) or "").strip()
    return "" if v.lower() in ("", "none", "off", "0") else v

def _eas_transition_for(settings):
    """(cable mode, blocky). Every full-screen style switches like a set-top
    box retuning: the channel's picture freezes for a second, goes black, then
    the alert comes up (and the same on the way back). With a details channel
    picked, the box tunes there first. Styles drawn over the program (SARA,
    tickers) just appear."""
    style = (settings.get("eas_overlay_style") or "dasdec").lower()
    if style in _EAS_LIVE_STYLE_KEYS:
        return "sa", False
    if _eas_chan(settings, "eas_details_channel"):
        return "motorola", False
    return "retune", False

def _eas_style_opts(settings):
    """Screen-style options from the plugin settings (see eaa_styles)."""
    def _num(key, default, lo, hi, cast=float):
        try:
            return max(lo, min(hi, cast(settings.get(key) or default)))
        except Exception:
            return default
    heading = settings.get("eas_dasdec_heading")
    heading = None if heading in (None, "") else str(heading).strip()
    if heading is not None and heading.lower() == "none":
        heading = ""
    return {
        "heading":       heading,
        "heading_mode":  "custom" if heading is not None else "auto",
        "spanish":       bool(settings.get("eas_spanish", False)),
        "aspect":        (settings.get("eas_aspect") or "pillarbox").lower(),
        "page_secs":     _num("eas_page_secs", 7.0, 3.0, 30.0),
        "crawl_speed":   _num("eas_crawl_speed", 5, 1, 10, int),
        "crawl_px":      30.0 * _num("eas_crawl_speed", 5, 1, 10, int) + 30.0,
        "tune_to":       (settings.get("eas_tune_to") or "").strip(),
        "station_id":    _eas_station_id_setting(settings),
        "fcc2023":       bool(settings.get("eas_fcc2023_wording", True)),
        "show_cap_text": bool(settings.get("eas_show_cap_text", True)),
        "bg":            (settings.get("eas_bg_color") or "").strip().lower(),
        "legacy_names":  bool(settings.get("eas_legacy_originator", False)),
        "operator_name": (settings.get("eas_operator_name") or "").strip(),
        "sara_secs":     _num("eas_sara_secs", 3.0, 1.0, 15.0),
    }

def _eas_tts_opts(settings):
    """Readout voice settings (see eaa_tts)."""
    try:
        speed = max(0.6, min(1.6, float(settings.get("eas_tts_speed") or 1.0)))
    except Exception:
        speed = 1.0
    return {
        "engine":      (settings.get("eas_tts_engine") or "auto").lower(),
        "voice":       (settings.get("eas_tts_voice") or "kal16").lower(),
        "fx":          (settings.get("eas_tts_fx") or "endec").lower(),
        "speed":       speed,
        "custom_cmd":  (settings.get("eas_tts_custom_cmd") or "").strip(),
        "piper_model": (settings.get("eas_tts_piper_model") or "").strip(),
    }

def _eas_station_id_setting(settings):
    """8-character LLLLLLLL station ID for alerts this plugin originates."""
    raw = (settings.get("eas_station_id") or "EAA/IPTV").strip().upper()
    raw = "".join(ch for ch in raw if ch.isalnum() or ch in "/-")[:8]
    return raw or "EAA/IPTV"

_eas_preroll_cache = {}

_eas_clone_info = {}

_eas_armed_mem = {}

# Temporary "remux" profile used to hand a session back to a channel whose
# normal profile is Dispatcharr's built-in Proxy (which can't take over a
# running FFmpeg session). It is swapped back for the real profile as soon
# as nobody is watching the channel any more.
_EAS_PASSTHRU_PREFIX = "EmergencyAlertarr passthrough "

def _eas_session_on_alert(rc, cid, mapping):
    """True when Dispatcharr's live session for this channel was started with
    our alert profile (metadata stream_profile == the clone's id)."""
    pid = mapping.get("eas_profile_id")
    if not (rc and pid):
        return False
    try:
        from apps.channels.models import Channel as _Ch
        ch = _Ch.objects.filter(id=int(cid)).first()
        if not ch:
            return False
        return _eas_channel_meta_profile(rc, str(ch.uuid)) == str(pid)
    except Exception:
        return False

def _eas_proc_pid(tag):
    """PID of the FFmpeg carrying this alert tag in this container, 0 if none,
    None if /proc can't be read."""
    if not tag:
        return None
    needle = f"EAA-{tag}".encode()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    if needle in f.read():
                        return int(pid)
            except Exception:
                continue
        return 0
    except Exception:
        return None

def _eas_meta_session(rc, channel_uuid):
    """(stream_profile_id, init_time) of the channel's live session, or
    (None, None)."""
    if not rc:
        return None, None
    try:
        from apps.proxy.live_proxy.redis_keys import RedisKeys
        key = RedisKeys.channel_metadata(channel_uuid)
    except Exception:
        key = f"live:channel:{channel_uuid}:metadata"
    try:
        vals = rc.hmget(key, ["stream_profile", "init_time"])
        out = []
        for v in vals:
            out.append((v.decode() if isinstance(v, bytes) else str(v)).strip() if v is not None else None)
        return out[0] or None, out[1] or None
    except Exception:
        return None, None

def _eas_proc_running(tag):
    """True if an FFmpeg with this alert tag is still running in this
    container (None if that can't be checked)."""
    if not tag:
        return None
    needle = f"EAA-{tag}".encode()
    try:
        found = False
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    if needle in f.read():
                        found = True
                        break
            except Exception:
                continue
        return found
    except Exception:
        return None

def _eas_return_profile(channel, mapping):
    """The profile the session should continue on after the alert: the
    channel's own profile when it is an FFmpeg/Streamlink/VLC command, else a
    temporary FFmpeg remux of the same stream (for Proxy / Redirect / system
    default). Returns (profile_or_None, passthrough_created)."""
    from core.models import StreamProfile
    orig = None
    if mapping.get("original_profile_id"):
        orig = StreamProfile.objects.filter(id=mapping["original_profile_id"]).first()
    if orig is None:
        try:
            from core.models import CoreSettings
            dp = CoreSettings.get_default_stream_profile_id()
            orig = StreamProfile.objects.filter(id=dp).first() if dp else None
        except Exception:
            orig = None
    usable = (orig is not None and not orig.is_proxy() and not getattr(orig, "is_redirect", lambda: False)()
              and (orig.command or "").strip() and (orig.parameters or "").strip())
    if usable:
        # The channel's own profile (or, for "system default", None = default).
        return (orig if mapping.get("original_profile_id") else None), False
    pt = StreamProfile(
        name=f"{_EAS_PASSTHRU_PREFIX}[ch{mapping.get('_cid', '')}]",
        command="ffmpeg",
        parameters="-hide_banner -nostdin -user_agent {userAgent} -i {streamUrl} -map 0:v? -map 0:a? "
                   "-c copy -f mpegts pipe:1",
        locked=False, is_active=True,
    )
    pt.save()
    return pt, True

_EAS_ARMED_PREFIX = f"emergencyalertarr:{_PLUGIN_KEY}:eas_armed:"

def _eas_return_command(original_profile_id):
    """(command, parameters) the session should run after the alert: the
    channel's own FFmpeg/Streamlink/VLC profile (or the system default when
    that is one), else a copy-only FFmpeg remux of the same stream (Proxy /
    Redirect can't take over a running FFmpeg session)."""
    from core.models import StreamProfile
    orig = None
    if original_profile_id:
        orig = StreamProfile.objects.filter(id=original_profile_id).first()
    if orig is None:
        try:
            from core.models import CoreSettings
            dp = CoreSettings.get_default_stream_profile_id()
            orig = StreamProfile.objects.filter(id=dp).first() if dp else None
        except Exception:
            orig = None
    if (orig is not None and not orig.is_proxy() and not getattr(orig, "is_redirect", lambda: False)()
            and (orig.command or "").strip() and (orig.parameters or "").strip()):
        return orig.command, orig.parameters
    return ("ffmpeg", "-hide_banner -nostdin -user_agent {userAgent} -i {streamUrl} -map 0:v? -map 0:a? "
                      "-c copy -f mpegts pipe:1")

def _eas_neutralize_clone(clone_id, original_profile_id):
    """Turn the alert profile into the channel's normal stream command once
    the alert FFmpeg is running. Whatever makes Dispatcharr reconnect the
    session next -- the alert ending itself (-t), a health reconnect, a
    failover retry -- then continues on the normal stream instead of playing
    the alert a second time."""
    from core.models import StreamProfile
    prof = StreamProfile.objects.filter(id=clone_id).first()
    if prof is None:
        return False
    cmd, params = _eas_return_command(original_profile_id)
    prof.command = cmd
    prof.parameters = params
    prof.save()
    return True

def _eas_neutralize_at(started, stream_end):
    """When to turn the alert profile back into the normal command: late
    enough that a session hiccup in the first seconds of the alert simply
    restarts the ALERT (instead of dropping the viewer back on the normal
    channel -- the "stream pauses and comes back normal" failure), early
    enough to be done before the alert FFmpeg ends itself (-t, which comes a
    few seconds early because of the initial read burst)."""
    se = float(stream_end or 0)
    return float(started) + max(se * 0.5, se - 12.0)

def _eas_start_arm_watcher(channel, mapping):
    """Follow the alert session on this channel (independently of the restore
    tick, which can be held up by the mutex):

      * the moment the alert FFmpeg is seen running, mark the alert as
        confirmed (Redis eas_armed:<cid>, neutralized=False);
      * if the session restarts before the alert is over (Dispatcharr
        teardown race, provider hiccup, player reconnect), start counting
        from the new session -- the alert plays again from the top;
      * shortly before the alert ends, neutralize the alert profile so the
        session continues on the normal stream instead of replaying it, and
        mark neutralized=True (the tick then schedules the tidy-up)."""
    clone_id = mapping.get("eas_profile_id")
    tag = mapping.get("eas_tag")
    orig_id = mapping.get("original_profile_id")
    stream_end = float(mapping.get("eas_stream_end") or 0)
    cid = str(channel.id)
    uuid = str(channel.uuid)
    if not (clone_id and tag and stream_end):
        return

    def _mark(rc, started, neutralized):
        data = {"tag": tag, "ts": started, "neutralized": bool(neutralized)}
        if rc is not None:
            try:
                rc.setex(f"{_EAS_ARMED_PREFIX}{cid}", 900, json.dumps(data))
                return
            except Exception:
                pass
        _eas_armed_mem[cid] = data

    def _run():
        rc = _get_redis_client()
        t0 = time.time()
        started = None          # when the current alert session began
        sig = None              # identity of that session (pid / init_time)
        last_proc = 0.0
        pid = None
        meta_since = None
        try:
            while True:
                now = time.time()
                if started is None and now - t0 > 120:
                    logger.warning(f"[EmergencyAlertarr] EAS: couldn't confirm the alert started on ch {cid}")
                    return
                if now - t0 > 900:
                    return
                if now - last_proc >= 2.0:
                    # Is the alert profile still ours? (restored / stopped -> done)
                    try:
                        from core.models import StreamProfile
                        if not StreamProfile.objects.filter(id=clone_id).exists():
                            return
                    except Exception:
                        pass
                    pid = _eas_proc_pid(tag)
                    last_proc = now
                meta_prof, init_time = _eas_meta_session(rc, uuid)
                on_meta = meta_prof == str(clone_id)
                new_sig = None
                if pid:
                    new_sig = f"pid:{pid}"
                elif on_meta:
                    if pid is None:
                        # No /proc view (FFmpeg in another container): trust the
                        # session metadata once it has said so for a moment.
                        meta_since = meta_since or now
                        if now - meta_since >= 3.0:
                            new_sig = f"init:{init_time}"
                    else:
                        meta_since = None
                else:
                    meta_since = None
                if new_sig and new_sig != sig:
                    if started is not None:
                        logger.info(f"[EmergencyAlertarr] EAS: alert session restarted on ch {cid} -- "
                                    f"playing it from the top")
                    sig = new_sig
                    started = now - (3.0 if new_sig.startswith("init:") else 1.0)
                    _mark(rc, started, False)
                    logger.info(f"[EmergencyAlertarr] EAS: alert playing on ch {cid}")
                if started is not None and now >= _eas_neutralize_at(started, stream_end):
                    if _eas_neutralize_clone(clone_id, orig_id):
                        _mark(rc, started, True)
                        logger.info(f"[EmergencyAlertarr] EAS: alert ending on ch {cid} -- "
                                    f"return to the channel armed")
                    return
                time.sleep(0.5)
        except Exception as e:
            logger.warning(f"[EmergencyAlertarr] EAS: arm watcher ch {cid}: {e}")
        finally:
            try:
                from django.db import connection
                connection.close()
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()

def _eas_armed_get(rc, cid):
    if rc is not None:
        try:
            v = rc.get(f"{_EAS_ARMED_PREFIX}{cid}")
            return json.loads(v.decode() if isinstance(v, bytes) else v) if v else None
        except Exception:
            pass
    return _eas_armed_mem.get(cid)

def _eas_armed_clear(rc, cid):
    _eas_armed_mem.pop(cid, None)
    if rc is not None:
        try:
            rc.delete(f"{_EAS_ARMED_PREFIX}{cid}")
        except Exception:
            pass

def _eas_finish_seamless(cid, mapping, mappings, rc=None):
    """After the alert stream ended by itself: tidy up WITHOUT stopping the
    channel (the session already continues on the return profile). Falls back
    to the stop/restart restore if the alert FFmpeg is somehow still running."""
    running = _eas_proc_running(mapping.get("eas_tag"))
    _eas_armed_clear(rc, cid)
    if running:
        logger.warning(f"[EmergencyAlertarr] EAS: alert stream on ch {cid} didn't end -- restarting it")
        return _eas_do_restore(cid, mapping, mappings, rc)
    eas_pid = mapping.get("eas_profile_id")
    # The session now runs the (neutralized) alert profile's normal command.
    # Put the channel's real profile back in the DB -- or, for Proxy, a remux
    # profile until nobody is watching (see _eas_passthrough_cleanup).
    try:
        from apps.channels.models import Channel as _Ch
        ch0 = _Ch.objects.filter(id=int(cid)).first()
        if ch0 is not None:
            mapping["_cid"] = cid
            prof, created = _eas_return_profile(ch0, mapping)
            mapping.pop("_cid", None)
            _assign_profile(ch0, prof, slot=mapping.get("eas_slot"))
            if created:
                mapping["eas_passthrough_id"] = prof.id
            if prof is not None:
                mapping["eas_return_profile_id"] = prof.id
            else:
                try:
                    from core.models import CoreSettings
                    mapping["eas_return_profile_id"] = CoreSettings.get_default_stream_profile_id()
                except Exception:
                    mapping["eas_return_profile_id"] = None
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] EAS: return profile ch {cid}: {e}")
    try:
        from apps.channels.models import Channel as _Ch
        ch = _Ch.objects.filter(id=int(cid)).first()
        if ch is not None and rc is not None and mapping.get("eas_return_profile_id"):
            try:
                from apps.proxy.live_proxy.redis_keys import RedisKeys
                key = RedisKeys.channel_metadata(str(ch.uuid))
            except Exception:
                key = f"live:channel:{ch.uuid}:metadata"
            try:
                if rc.exists(key):
                    rc.hset(key, "stream_profile", str(mapping["eas_return_profile_id"]))
            except Exception:
                pass
    except Exception:
        pass
    if eas_pid:
        _eas_clone_remember(rc, eas_pid)
        _delete_cloned_profile(eas_pid)
    for k in ("eas_profile_id", "ticker_profile_id", "eas_restore_at", "eas_await_stream",
              "eas_seen_down", "eas_seq_secs", "eas_hard_deadline", "eas_fired_at",
              "eas_national", "eas_priority", "eas_returned", "eas_tag", "eas_stream_end",
              "eas_return_profile_id"):
        mapping.pop(k, None)
    mappings[cid] = mapping
    for fn in (lambda: _eas_clear(cid), lambda: _eas_pop_stopped(rc, cid),
               lambda: _eas_state_set(rc, cid, None)):
        try:
            fn()
        except Exception:
            pass
    with _eas_lock:
        _eas_active.pop(cid, None)
    _eas_mark_alert_end(rc)
    logger.info(f"[EmergencyAlertarr] EAS: sequence complete on ch {cid} -- back on the normal stream "
                f"(no reconnect needed)")

def _eas_passthrough_cleanup(cid, mapping, mappings, streaming_ids):
    """Put the channel's real profile back once nobody is watching (the
    temporary remux profile only existed to carry the running session)."""
    if not mapping.get("eas_passthrough_id") or mapping.get("eas_profile_id"):
        return False
    if cid in streaming_ids:
        return False
    try:
        from apps.channels.models import Channel as _Ch
        ch = _Ch.objects.filter(id=int(cid)).first()
        if ch is not None:
            _restore_profile(ch, mapping.get("original_profile_id"), slot=mapping.get("eas_slot"))
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] EAS: passthrough cleanup ch {cid}: {e}")
        return False
    _eas_delete_passthrough(mapping.pop("eas_passthrough_id", None))
    mapping.pop("eas_slot", None)
    mappings[cid] = mapping
    logger.info(f"[EmergencyAlertarr] EAS: ch {cid} idle -- normal stream profile put back")
    return True

def _eas_delete_passthrough(pid):
    if not pid:
        return
    try:
        from core.models import StreamProfile
        StreamProfile.objects.filter(id=pid, name__startswith=_EAS_PASSTHRU_PREFIX).delete()
    except Exception:
        pass

_eas_seq_cache = {}

def _eas_channel_has_audio(channel):
    """False when Dispatcharr's stream stats say the channel's stream has video
    but no audio (an over-the-program alert must not reference [0:a] then);
    True/None otherwise."""
    try:
        seen_video = False
        for st in channel.streams.all()[:5]:
            stats = getattr(st, "stream_stats", None) or {}
            if isinstance(stats, str):
                stats = json.loads(stats or "{}")
            if stats.get("audio_codec"):
                return True
            if stats.get("video_codec"):
                seen_video = True
        return False if seen_video else None
    except Exception:
        return None

def _eas_cleanup_seq_files(keep_secs=3 * 3600):
    now = time.time()
    try:
        for f in os.listdir(RUNTIME_DIR):
            if f.startswith(("seq_", "capaudio_", "capsrc_")) and now - os.path.getmtime(
                    os.path.join(RUNTIME_DIR, f)) > keep_secs:
                os.remove(os.path.join(RUNTIME_DIR, f))
    except Exception:
        pass

def _eas_details_channel(cfg):
    """The force-tune details channel (Channel object) or None."""
    if cfg.get("cable_mode") != "motorola" or not cfg.get("details_channel"):
        return None
    try:
        from apps.channels.models import Channel
        return Channel.objects.filter(id=int(cfg["details_channel"])).first()
    except Exception:
        return None

def _eas_alert_still(cfg, alert, out_w, out_h):
    """First frame of the alert screen (PNG, cached per alert + style) -- what
    the blocky retune breaks up into."""
    from eaa_forcetune import render_still, preroll_key
    style = cfg["style"]
    sd = cfg["video_opts"].get("sd_look") and (
        cfg["video_opts"]["sd_look"] == "all" or style in _EAS_LEGACY_STYLES)
    key = preroll_key("still", alert.get("id") or _eas_same_header_string(alert), style,
                      out_w, out_h, bool(sd))
    path = os.path.join(RUNTIME_DIR, f"preroll_still_{key}.png")
    if os.path.isfile(path):
        return path
    sopts = dict(cfg["style_opts"])
    sopts.setdefault("scale", cfg["ep_scale"])
    if cfg["ep_font"]:
        sopts.setdefault("font_override", cfg["ep_font"])
    ov = build_style_overlay("ft", alert, style, out_w, out_h, sopts)
    if ov.get("live"):
        ok = render_still(None, f"scale={out_w}:{out_h},setsar=1", path)
    else:
        cw, ch, _bg = ov["canvas"]
        sdc = ("," + _eas_sd_chain(cw, ch)) if sd else ""
        ok = render_still(ov["canvas"], f"format=yuv420p,{ov['filter']}{sdc},{ov['finish']}", path)
    _eas_clear("ft")
    return path if ok else None

def _eas_retune_frame_path(cid):
    return os.path.join(RUNTIME_DIR, f"retune_frame_{cid}.png")

def _eas_capture_retune_frames(cfg, channels):
    """Grab the current picture of every channel about to switch, in parallel
    (each is the frame that 'freezes' when the box retunes)."""
    if cfg.get("cable_mode") != "retune" or not channels:
        return
    from eaa_forcetune import capture_channel_frame
    _ensure_dirs()
    threads = []
    for ch in channels:
        path = _eas_retune_frame_path(ch.id)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
        t = threading.Thread(target=capture_channel_frame,
                             args=(str(ch.uuid), path, cfg.get("dispatcharr_url") or ""), daemon=True)
        t.start()
        threads.append(t)
    deadline = time.time() + 9
    for t in threads:
        t.join(timeout=max(0.1, deadline - time.time()))
    _eas_mutex_touch()

def _eas_prepare_retune(cfg, alert, channel):
    """Set-top retune lead-in for one channel: its own picture frozen for a
    second, black, then the alert. Clean (no blocks) unless asked for."""
    if channel is None or cfg.get("cable_mode") != "retune":
        return None
    try:
        from eaa_forcetune import build_retune, cleanup_prerolls
        _ensure_dirs()
        cleanup_prerolls(RUNTIME_DIR)
        out_w, out_h = _EAS_CANVAS.get(cfg.get("transcode_mode_out") or "full", (1920, 1080))
        frame = _eas_retune_frame_path(channel.id)
        if not (os.path.isfile(frame) and time.time() - os.path.getmtime(frame) < 120):
            frame = None
        out = os.path.join(RUNTIME_DIR, f"preroll_retune_{channel.id}_{int(time.time())}.mp4")
        if cfg.get("retune_blocky"):
            still = _eas_alert_still(cfg, alert, out_w, out_h)
            res = None
            if still:
                from eaa_forcetune import build_preroll
                res = build_preroll(frame, still, out, out_w, out_h,
                                    details_secs=max(0.5, cfg["retune_freeze"]), blocky=True)
        else:
            res = build_retune(frame, out, out_w, out_h, cfg["retune_freeze"], cfg["retune_black"])
        if res:
            res["end"] = "blocky" if cfg.get("retune_blocky") else "clean"
            logger.info(f"[EmergencyAlertarr] retune: lead-in for ch {channel.id} "
                        f"({'freeze + ' if frame else ''}black, {res['secs']:.1f}s)")
        return res
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] retune: lead-in unavailable ({e})")
        return None

def _eas_prepare_preroll(cfg, alert, channel=None):
    """Lead-in before the alert: "retune" mode = the viewer's channel freezes,
    goes black, then the alert; "motorola" mode = a few seconds of the details
    channel first (built once, shared by every channel). None in instant
    (Scientific-Atlanta style) mode, and for styles drawn over the program."""
    if cfg.get("style") in _EAS_LIVE_STYLE_KEYS:
        return None
    if cfg.get("cable_mode") == "retune":
        return _eas_prepare_retune(cfg, alert, channel)
    det = _eas_details_channel(cfg)
    if det is None:
        return None
    try:
        from eaa_forcetune import (capture_details_clip, render_still, build_preroll,
                                   cleanup_prerolls, preroll_key)
        _ensure_dirs()
        style = cfg["style"]
        out_w, out_h = _EAS_CANVAS.get(cfg.get("transcode_mode_out") or "full", (1920, 1080))
        sd = cfg["video_opts"].get("sd_look") and (
            cfg["video_opts"]["sd_look"] == "all" or style in _EAS_LEGACY_STYLES)
        key = preroll_key(alert.get("id") or _eas_same_header_string(alert), style, det.id,
                          out_w, out_h, bool(sd), cfg["details_secs"], cfg["retune_blocky"],
                          cfg["retune_freeze"], cfg["retune_black"])
        hit = _eas_preroll_cache.get(key)
        if hit and os.path.isfile(hit["path"]):
            return hit
        cleanup_prerolls(RUNTIME_DIR)
        clip = os.path.join(RUNTIME_DIR, f"details_clip_{det.id}.mp4")
        fresh = os.path.isfile(clip) and time.time() - os.path.getmtime(clip) < 600
        if not fresh:
            _eas_mutex_touch()
            capture_details_clip(str(det.uuid), clip, secs=cfg["details_secs"] + 2.0,
                                 base_setting=cfg.get("dispatcharr_url") or "")
            _eas_mutex_touch()
        if not os.path.isfile(clip):
            clip = None
        still = _eas_alert_still(cfg, alert, out_w, out_h) if cfg["retune_blocky"] else None
        if cfg["retune_blocky"] and not still:
            return None
        _eas_mutex_touch()
        res = build_preroll(clip, still, os.path.join(RUNTIME_DIR, f"preroll_{key}.mp4"),
                            out_w, out_h, details_secs=cfg["details_secs"],
                            blocky=cfg["retune_blocky"], freeze_secs=cfg["retune_freeze"],
                            black_secs=cfg["retune_black"])
        _eas_mutex_touch()
        if res:
            res["end"] = "blocky" if cfg["retune_blocky"] else "clean"
            _eas_preroll_cache.clear()
            _eas_preroll_cache[key] = res
            logger.info(f"[EmergencyAlertarr] force-tune: lead-in ready via '{det.name}' ({res['secs']:.1f}s)")
        return res
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] force-tune: lead-in unavailable ({e}) -- alert plays without it")
        return None

def _eas_activate(channel, mapping, alerts, settings, endec_test=False, now_ts=None,
                  eom_only=False):
    """Switch one channel onto a freshly built alert profile and arm its timed
    restore. Updates `mapping` in place and returns the sequence duration. The
    caller saves mappings and kicks the stream restart.

    The profile the channel is really playing right now (override-aware, or
    the system default) is snapshotted as the restore target, so the restore
    always puts back exactly what was there."""
    from core.models import StreamProfile
    cfg = _eas_fire_cfg(settings)
    if cfg["rwt_short"] and alerts and eas_event_code(alerts[0]) == "RWT":
        endec_test = True     # RWT = header + EOM only (FCC 11.61)
    for a in alerts or []:
        if not a.get("station_id") and _eas_org_code(a) in ("EAS", "CIV"):
            a["station_id"] = cfg["style_opts"]["station_id"]
        if cfg["relay_own_id"] and a.get("source") in ("NWS", "IPAWS", "monitor"):
            # Relaying: the outgoing header carries OUR station ID; the
            # originator's issue time and valid period are kept.
            a["relay_station_id"] = cfg["style_opts"]["station_id"]
    national = bool(alerts) and _eas_classify(alerts[0]) == "national" and not eom_only
    slot, holder = _eas_profile_slot(channel)
    cur = getattr(holder, "stream_profile", None)
    if cur is not None and not (cur.name or "").startswith((PROFILE_PREFIX, _EAS_PASSTHRU_PREFIX)):
        mapping["original_profile_id"] = cur.id
    elif cur is None and slot == "channel":
        mapping["original_profile_id"] = None          # system default profile
    # (cur is a leftover EAS clone -> keep the stored original)

    for k in ("eas_profile_id", "ticker_profile_id"):
        if mapping.get(k):
            _delete_cloned_profile(mapping[k])
            mapping.pop(k, None)

    orig = None
    if mapping.get("original_profile_id"):
        orig = StreamProfile.objects.filter(id=mapping.get("original_profile_id")).first()

    cfg["audio_opts"] = dict(cfg["audio_opts"], has_audio=_eas_channel_has_audio(channel))
    eas_profile, _, seq_duration = _clone_and_inject_eas(
        channel.id, orig, channel.name, cfg["silence_secs"], cfg["transcode_mode"],
        style=cfg["style"], unique_alerts=alerts, use_tts=cfg["use_tts"],
        endec_test=endec_test, tail_secs=cfg["tail_secs"],
        style_opts=cfg["style_opts"], tts_opts=cfg["tts_opts"],
        endec=cfg["endec"],
        audio_opts=cfg["audio_opts"], video_opts=cfg["video_opts"],
        preroll=(None if eom_only else _eas_prepare_preroll(cfg, alerts[0] if alerts else {}, channel)),
        eom_only=eom_only, national_hold=national,
        generate_tones=cfg["generate_tones"], att_secs=cfg["att_secs"],
        easyplus_font=cfg["ep_font"], easyplus_scale=cfg["ep_scale"],
    )
    try:
        slot = _assign_profile(channel, eas_profile, slot=slot)
    except Exception:
        _delete_cloned_profile(eas_profile.id)
        raise
    mapping["eas_profile_id"] = eas_profile.id
    mapping["eas_slot"] = slot
    info = _eas_clone_info.pop(str(channel.id), {}) or {}
    mapping["eas_tag"] = info.get("tag")
    mapping["eas_stream_end"] = info.get("end")
    for k in ("eas_returned", "eas_return_profile_id"):
        mapping.pop(k, None)
    if mapping.get("eas_passthrough_id"):
        _eas_delete_passthrough(mapping.pop("eas_passthrough_id"))
    _eas_clone_remember(_get_redis_client(), eas_profile.id)
    now_ts = now_ts or time.time()
    _eas_arm_restore(mapping, seq_duration, now_ts)
    mapping["eas_priority"] = int((alerts[0] if alerts else {}).get("_priority") or
                                  decide(alerts[0] if alerts else {}, settings)["priority"])
    if not national and mapping.get("eas_tag"):
        _eas_armed_clear(_get_redis_client(), str(channel.id))
        _eas_start_arm_watcher(channel, mapping)
    if national:
        # No timeout and no automatic reset for a national alert: it holds the
        # channel until it's over (then the sweep sends the EOM).
        hold = max(seq_duration, _eas_national_hold_secs(alerts[0], now_ts))
        mapping["eas_national"] = True
        # The sweep ends it (EOM) when it leaves the feed or expires; these
        # are only the fallbacks if the sweep can't run.
        mapping["eas_restore_at"] = now_ts + hold + 300
        mapping["eas_hard_deadline"] = now_ts + hold + 420
    else:
        mapping.pop("eas_national", None)
    return seq_duration

def _eas_kick(channel, rc=None):
    """Restart the channel's stream so the viewer reconnects onto the alert."""
    _eas_pop_stopped(rc, str(channel.id))   # clear any stale marker first
    _restart_channel_stream_async(channel, label="EAS", proactive=True)

def _eas_get_alerts(rc, settings, zones, severity_threshold, interval):
    """Current alert list, shared across workers through a short Redis cache so
    only one worker hits the feeds per poll. Returns (alerts, complete), or
    (None, False) when another worker is fetching right now."""
    try:
        ttl = int(max(10, min(_EAS_CACHE_TTL, int(interval) - 5)))
    except Exception:
        ttl = _EAS_CACHE_TTL
    if not rc:
        return _fetch_all_alerts_ex(settings, zones, severity_threshold)
    try:
        cached = rc.get(_EAS_REDIS_KEY)
    except Exception:
        cached = None
    if cached:
        try:
            data = json.loads(cached)
            if isinstance(data, dict):
                return list(data.get("alerts") or []), bool(data.get("complete", True))
            return list(data or []), True
        except Exception:
            pass
    try:
        if not rc.set(_EAS_REDIS_LOCK, "1", nx=True, ex=30):
            return None, False
    except Exception:
        return _fetch_all_alerts_ex(settings, zones, severity_threshold)
    try:
        alerts, complete = _fetch_all_alerts_ex(settings, zones, severity_threshold)
        try:
            # A partial result is cached only briefly so a recovered feed is
            # picked up quickly.
            rc.setex(_EAS_REDIS_KEY, ttl if complete else min(ttl, 15),
                     json.dumps({"alerts": alerts, "complete": complete, "ts": time.time()}))
        except Exception:
            pass
        return alerts, complete
    finally:
        try:
            rc.delete(_EAS_REDIS_LOCK)
        except Exception:
            pass

def _eas_sort_key(a):
    # Worst first; among equals, the earliest issued first.
    return (-_EAS_SEV.get(a.get("severity"), 0), _norm_ts(a.get("effective")))

def _eas_national_test_settings(settings):
    """National test mode overrides area, source and severity: every active
    alert in the US (NWS + IPAWS), any severity."""
    if not settings.get("eas_national_test"):
        return settings
    s2 = dict(settings)
    s2.update({"eas_source": "both", "eas_zones": "ALL", "eas_ipaws_same_codes": "ALL",
               "eas_severity_filter": "Minor"})
    return s2

def _eas_sweep():
    settings = _eas_national_test_settings(_get_settings())
    zones_raw = (settings.get("eas_zones") or "").strip()
    zones = [z.strip() for z in zones_raw.split(",") if z.strip()]
    _source = (settings.get("eas_source") or "nws").lower()
    _ipaws_on = _source in ("ipaws", "both") and bool((settings.get("eas_ipaws_same_codes") or "").strip())
    _nws_on = _source in ("nws", "both") and bool(zones)

    mappings = _get_mappings()
    eas_cids = [cid for cid, m in mappings.items()
                if isinstance(m, dict) and (m.get("type") == "eas" or m.get("eas_armed"))]
    if not eas_cids:
        return
    rc = _get_redis_client()

    _mon_on = bool(_eas_chan(settings, "eas_monitor_channel"))
    if not _nws_on and not _ipaws_on and not _mon_on:
        # No alert source configured: nothing to poll. Restores still run on
        # the tick; just make sure no channel is left on an orphaned overlay.
        if any(mappings[c].get("eas_profile_id") and not mappings[c].get("eas_restore_at")
               for c in eas_cids):
            with _EasMutex(rc) as held:
                if held:
                    _eas_cleanup_orphans_locked(rc)
        return

    severity_threshold = settings.get("eas_severity_filter") or "Moderate"
    try:
        interval = max(15, int(settings.get("eas_poll_interval") or 60))
    except Exception:
        interval = 60
    try:
        if _nws_on or _ipaws_on:
            alerts, complete = _eas_get_alerts(rc, settings, zones, severity_threshold, interval)
        else:
            alerts, complete = [], True          # channel monitor only
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] EAS: alert fetch failed: {e}")
        return
    if alerts is None:
        return   # another worker is fetching right now
    try:
        alerts = list(alerts) + _eas_monitor_alerts(rc, settings)
    except Exception as e:
        logger.debug(f"[EmergencyAlertarr] monitor alerts unavailable: {e}")

    with _EasMutex(rc) as held:
        if not held:
            return   # another worker is mid-sweep; it has the same data
        _eas_sweep_locked(rc, settings, alerts, complete)

def _eas_cleanup_orphans_locked(rc):
    """Restore channels left on an EAS profile with no restore scheduled
    (crashed worker, lost write). Caller holds the mutex."""
    mappings = _get_mappings()
    changed = False
    for cid, m in list(mappings.items()):
        if not isinstance(m, dict):
            continue
        if m.get("eas_profile_id") and not m.get("eas_restore_at") and not m.get("eas_await_stream"):
            try:
                _eas_do_restore(cid, m, mappings, rc)
                changed = True
                logger.info(f"[EmergencyAlertarr] EAS: cleaned up orphaned alert overlay on ch {cid}")
            except Exception as e:
                logger.error(f"[EmergencyAlertarr] EAS: orphan cleanup failed ch {cid}: {e}", exc_info=True)
    if changed:
        _save_mappings(mappings)
    return changed

def _eas_sweep_locked(rc, settings, alerts, complete):
    """Decide and apply alert transitions. Caller holds the mutex."""
    from apps.channels.models import Channel

    _eas_cleanup_orphans_locked(rc)
    mappings = _get_mappings()   # fresh, under the lock
    eas_cids = [cid for cid, m in mappings.items()
                if isinstance(m, dict) and (m.get("type") == "eas" or m.get("eas_armed"))]
    streaming_ids = _eas_streaming_ids(rc)
    now_ts = time.time()
    changed = _eas_process_restores(mappings, streaming_ids, now_ts, rc)

    all_alerts = sorted([a for a in (alerts or []) if a.get("id")], key=_eas_sort_key)

    seen = _eas_seen_read(rc)
    fp = _eas_cfg_fingerprint(settings)
    fp_changed = seen is not None and _eas_cfg_fp_get(rc) not in (None, fp)
    if seen is None or fp_changed:
        # First poll after a (re)start -- or the area / source / severity
        # settings changed: everything already active counts as announced, so
        # only alerts issued from now on ever fire. (Without the re-seed on a
        # settings change, switching area -- or to ALL -- queued that area's
        # whole backlog, and the channels played alert after alert for hours.)
        # Never seed from a partial fetch: the missing alerts would later look
        # "new".
        if complete:
            merged = dict(seen or {})
            merged.update({k: now_ts for a in all_alerts for k in _alert_keys(a)})
            _eas_seen_write(rc, merged)
            _eas_cfg_fp_set(rc, fp)
            if all_alerts:
                why = "alert area/source settings changed" if fp_changed else "first poll"
                logger.info(
                    f"[EmergencyAlertarr] EAS: {why} -- {len(all_alerts)} already-active "
                    "alert(s) marked as announced; only newly issued alerts will be shown"
                )
        if changed:
            _save_mappings(mappings)
        return
    if _eas_cfg_fp_get(rc) is None and complete:
        _eas_cfg_fp_set(rc, fp)

    # Refresh last-seen times for everything still in the feed, then forget
    # entries that have been gone for a long time.
    for a in all_alerts:
        keys = _alert_keys(a)
        if any(k in seen for k in keys):
            for k in keys:
                seen[k] = now_ts
    seen = {k: t for k, t in seen.items() if now_ts - t < _EAS_SEEN_KEEP}

    gap, max_age = _eas_flow_limits(settings)
    pending = _eas_json_get(rc, _EAS_PENDING_KEY, {})
    recent = _eas_json_get(rc, _EAS_RECENT_KEY, [])
    recent = [r for r in recent if now_ts - float(r.get("seen", 0)) < 3 * 3600]
    feed_keys = set()
    queue = []
    for a in all_alerts:
        keys = _alert_keys(a)
        feed_keys.update(keys)
        k0 = keys[0]
        if any(k in seen for k in keys):
            pending.pop(k0, None)
            continue
        if any(r in seen for r in (a.get("refs") or [])):
            # An update/extension/continuation of an alert that already played
            # (NWS re-issues with a new expiry => new content ID). A real
            # ENDEC doesn't re-tone these; absorb it silently.
            for k in keys:
                seen[k] = now_ts
            logger.info(
                f"[EmergencyAlertarr] EAS: {a.get('event')} update for an alert already "
                "announced -- not re-announced"
            )
            continue
        dup = _eas_recent_match(recent, a)
        if dup:
            # The same message from a second source (IPAWS + NWS, or a
            # monitored station relaying an alert we already had): a real
            # ENDEC never relays a duplicate (47 CFR 11.33(a)(10)).
            for k in keys:
                seen[k] = now_ts
            pending.pop(k0, None)
            logger.info(f"[EmergencyAlertarr] EAS: {a.get('event')} already received "
                        f"(via {dup.get('src', 'another source')}) -- duplicate ignored")
            continue
        if (a.get("event_code") or "").upper() == "EAT":
            # Emergency Action Termination: ends an active national alert.
            if _eas_json_get(rc, _EAS_NATIONAL_KEY, None):
                _eas_json_set(rc, _EAS_NATIONAL_KEY + ":eat", 1, 3600)
        dec = decide(a, settings)
        a["_priority"], a["_category"] = dec["priority"], dec["category"]
        if k0 not in pending:
            age = (now_ts - float(a.get("received") or now_ts)) if a.get("source") == "monitor" \
                else _eas_alert_age_secs(a, now_ts)
            if age is not None and age > max_age and dec["category"] != "national":
                # Too old to be news (issued while nothing was polling). Real
                # ENDECs drop stale messages too; mark it so it can't pile up.
                for k in keys:
                    seen[k] = now_ts
                logger.info(
                    f"[EmergencyAlertarr] EAS: {a.get('event')} issued {age / 60:.0f} min ago -- "
                    "too old, not announced"
                )
                continue
            recent.append(_eas_recent_entry(a, now_ts))
            if dec["action"] == "ignore":
                for k in keys:
                    seen[k] = now_ts
                logger.info(f"[EmergencyAlertarr] EAS: {a.get('event')} received -- ignored by filter")
                continue
            if dec["action"] == "log":
                for k in keys:
                    seen[k] = now_ts
                logger.info(f"[EmergencyAlertarr] EAS: {a.get('event')} received -- logged, not aired (filter)")
                _eas_history_add(a.get("event") or "Alert", a.get("area") or "", [],
                                 kind="logged", severity=a.get("severity") or "")
                continue
            pending[k0] = {"first": now_ts, "hold": dec["hold_min"]}
            if dec["hold_min"]:
                logger.info(f"[EmergencyAlertarr] EAS: {a.get('event')} received -- held "
                            f"{dec['hold_min']:.0f} min before relay (filter)")
        p = pending.get(k0) or {"first": now_ts, "hold": 0}
        if now_ts < float(p.get("first", now_ts)) + float(p.get("hold", 0)) * 60:
            continue
        queue.append(a)
    if complete:
        pending = {k: v for k, v in pending.items() if k in feed_keys}
    queue.sort(key=lambda x: (-x.get("_priority", 40), _eas_sort_key(x)))

    # One alert at a time, system-wide, like a real ENDEC: while any channel
    # is still playing an alert the next one waits -- unless the new one may
    # interrupt it (a national alert takes over from anything; a warning may
    # cut into a test). The alert goes out on EVERY armed channel with a
    # viewer.
    excluded = _eas_excluded_cids(settings)
    busy = [c for c in eas_cids
            if mappings[c].get("eas_restore_at") or mappings[c].get("eas_await_stream")]
    running_prio = max((int(mappings[c].get("eas_priority") or 40) for c in busy), default=None)
    air_until, air_prio = _eas_air_get(rc, now_ts)
    if air_until:
        # The last alert's slot is still running, even if its viewers left.
        busy = busy or ["(on the air)"]
        running_prio = max(running_prio if running_prio is not None else 0, air_prio)
    top = queue[0] if queue else None
    preempt = bool(top and busy and can_preempt(top.get("_priority", 40), running_prio))
    last_end = _eas_last_end(rc)
    if last_end > now_ts + 5:
        last_end = 0.0            # clock went backwards: don't block alerts for hours
    wait = gap - (now_ts - last_end)
    urgent = bool(top and top.get("_priority", 40) >= 61)     # EAN / NPT: immediately
    if top and not busy and wait > 0 and not urgent:
        logger.debug(f"[EmergencyAlertarr] EAS: next alert in {wait:.0f}s (spacing between alerts)")
    if top and (not busy or preempt) and (wait <= 0 or urgent or preempt):
        targets = [c for c in eas_cids
                   if c in streaming_ids and c not in excluded
                   and (preempt or not mappings[c].get("eas_profile_id"))]
        logger.info(f"[EmergencyAlertarr] EAS: {top.get('event')} due -- {len(targets)} of "
                    f"{len(eas_cids)} armed channel(s) being watched"
                    + (f": {', '.join(targets[:10])}" if targets else ""))
        if targets:
            alert = top
            if preempt:
                logger.warning(f"[EmergencyAlertarr] EAS: {alert.get('event')} (priority "
                               f"{alert.get('_priority')}) interrupts the alert on the air")
            fired_names = _eas_fire_on(rc, settings, mappings, targets, alert, now_ts)
            if fired_names:
                changed = True
                for k in _alert_keys(alert):
                    seen[k] = now_ts
                pending.pop(_alert_keys(alert)[0], None)
                still = len(queue) - 1
                if still:
                    logger.info(f"[EmergencyAlertarr] EAS: {still} more alert(s) queued")
                _eas_history_add(
                    alert.get("event") or "Alert", alert.get("area") or "",
                    fired_names, kind="alert", severity=alert.get("severity") or "",
                )
                slot = max([float(mappings[c].get("eas_stream_end") or mappings[c].get("eas_seq_secs") or 0)
                            for c in targets if isinstance(mappings.get(c), dict)] or [0.0])
                _eas_air_set(rc, now_ts + max(slot, 20.0) + 5.0, alert.get("_priority", 40))
        else:
            # Nobody is watching an armed channel right now: the alert still
            # "goes out" (to no one) and is done -- it is NOT kept for whoever
            # tunes in next. Someone flipping over within the join window
            # still gets it.
            alert = top
            for k in _alert_keys(alert):
                seen[k] = now_ts
            pending.pop(_alert_keys(alert)[0], None)
            _eas_open_windows(rc, alert, now_ts)
            _eas_air_set(rc, now_ts + _EAS_UNWATCHED_AIR_SECS, alert.get("_priority", 40))
            _eas_history_add(alert.get("event") or "Alert", alert.get("area") or "", [],
                             kind="unwatched", severity=alert.get("severity") or "")
            logger.info(f"[EmergencyAlertarr] EAS: {alert.get('event')} went out with no one watching "
                        f"an armed channel -- not shown")

    # A national alert holds the channels until it is over (no timeout, no
    # automatic reset -- 47 CFR 11.33(a)(9)); then the EOM goes out. Checked
    # BEFORE the late-joiner pass, so an ended alert can never be re-fired.
    if _eas_national_end_check(rc, settings, mappings, feed_keys, complete, now_ts):
        changed = True
    elif _eas_national_join_locked(rc, settings, mappings, streaming_ids):
        changed = True

    _eas_json_set(rc, _EAS_PENDING_KEY, pending, 6 * 3600)
    _eas_json_set(rc, _EAS_RECENT_KEY, recent[-300:], 6 * 3600)
    _eas_seen_write(rc, seen)
    if changed:
        _save_mappings(mappings)
    _eas_watchdog_locked(rc)

def _eas_json_get(rc, key, default):
    """Shared state lives in Redis. The in-process copy is only a fallback for
    when Redis is unreachable -- never for a key that was deleted (another
    worker's "Stop All Alerts" / end of a national alert)."""
    if rc:
        try:
            v = rc.get(key)
            if v is None:
                return default
            return json.loads(v.decode() if isinstance(v, bytes) else v)
        except Exception:
            pass
    mem = _eas_json_mem.get(key)
    return default if mem is None else mem

def _eas_json_set(rc, key, value, ttl=3600):
    _eas_json_mem[key] = value
    if rc:
        try:
            rc.setex(key, int(ttl), json.dumps(value))
        except Exception:
            pass

def _eas_json_del(rc, key):
    _eas_json_mem.pop(key, None)
    if rc:
        try:
            rc.delete(key)
        except Exception:
            pass

def _eas_origin_ts(a):
    from datetime import datetime, timezone
    for f in ("sent", "effective"):
        raw = (a.get(f) or "").strip()
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except Exception:
                continue
    return None

def _eas_recent_entry(a, now_ts):
    codes = sorted({"".join(ch for ch in str(c) if ch.isdigit()).zfill(6)[1:6]
                    for c in (a.get("same_codes") or []) if str(c).strip()})
    origin = a.get("sent") or a.get("effective")
    try:
        purge = a.get("same_purge") or _same_purge_code(origin, a.get("expires"))
    except Exception:
        purge = ""
    return {"key": _alert_keys(a)[0], "ec": _same_event_code_for(a), "codes": codes,
            "ts": _eas_origin_ts(a) or now_ts, "seen": now_ts, "purge": purge,
            "src": a.get("source") or "feed", "event": a.get("event") or ""}

def _eas_recent_match(recent, a):
    """A previously received alert that is the same message as `a` from a
    different source: same event code, overlapping locations, origination
    time within a few minutes. Only cross-checks a monitored-channel alert
    against the feeds (feed-vs-feed duplicates are merged upstream)."""
    keys = set(_alert_keys(a))
    me = _eas_recent_entry(a, 0)
    mine_mon = (a.get("source") == "monitor")
    for r in recent:
        if r.get("key") in keys:
            continue
        if not (mine_mon or r.get("src") == "monitor"):
            continue
        if r.get("ec") != me["ec"]:
            continue
        rc_, mc = set(r.get("codes") or []), set(me["codes"])
        same_set = rc_ == mc
        same_purge = bool(r.get("purge")) and r.get("purge") == me["purge"]
        # The same message relayed keeps the originator's time (minute
        # precision) and either the exact location list or its valid period.
        if not (same_set or (same_purge and (rc_ & mc))):
            continue
        if abs(float(r.get("ts") or 0) - float(me["ts"] or 0)) <= 150:
            return r
    return None

def _eas_excluded_cids(settings):
    """Channels that never get alerts: the force-tune details channel and the
    channel being monitored for tones."""
    out = set()
    for key in ("eas_details_channel", "eas_monitor_channel"):
        v = _eas_chan(settings, key)
        if v:
            out.add(v)
    return out

def _eas_national_hold_secs(alert, now_ts):
    """How long a national alert may hold the channels at most: until it
    expires, capped at two hours (a live address can't run forever here).
    0 once it has expired."""
    from datetime import datetime, timezone
    raw = (alert.get("expires") or "").strip()
    if not raw:
        return 2 * 3600
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, min(dt.timestamp() - now_ts, 2 * 3600))
    except Exception:
        return 2 * 3600

def _eas_fire_on(rc, settings, mappings, targets, alert, now_ts, eom_only=False):
    """Put `alert` on the given channels (mappings updated in place, streams
    kicked). Returns the names of the channels it fired on."""
    from apps.channels.models import Channel
    fired_names = []
    to_kick = []
    if not eom_only:
        try:
            cfg0 = _eas_fire_cfg(settings)
            if cfg0.get("cable_mode") == "retune" and cfg0.get("style") not in _EAS_LIVE_STYLE_KEYS:
                chans = [c for c in (Channel.objects.filter(id=int(x)).first() for x in targets) if c]
                _eas_capture_retune_frames(cfg0, chans)
        except Exception as e:
            logger.debug(f"[EmergencyAlertarr] retune frames unavailable: {e}")
    for cid in targets:
        mapping = mappings.get(cid, {})
        try:
            _eas_mutex_touch()
            channel = Channel.objects.filter(id=int(cid)).first()
            if not channel:
                continue
            seq_duration = _eas_activate(channel, mapping, [dict(alert)], settings, now_ts=now_ts,
                                         eom_only=eom_only)
            mappings[cid] = mapping
            _eas_state_set(rc, cid, alert.get("id") or "EAS")
            to_kick.append(channel)
            with _eas_lock:
                _eas_active[cid] = alert.get("event") or "EAS"
            fired_names.append(channel.name)
            logger.info(
                f"[EmergencyAlertarr] EAS {'EOM' if eom_only else 'ALERT'}: {alert.get('event')} -- "
                f"{(alert.get('area') or '')[:60]} (ch {cid}, sequence {seq_duration:.0f}s)"
            )
        except Exception as e:
            logger.error(f"[EmergencyAlertarr] EAS: activation failed ch {cid}: {e}", exc_info=True)
    # Switch every channel only once ALL alert profiles are built: building
    # one (text layout, tones, audio render) is CPU work inside Dispatcharr's
    # own worker, and doing it while another channel was being torn down
    # stalled that teardown -- the old session then broke the new one and the
    # player hung (timeout / 504) instead of showing the alert.
    for channel in to_kick:
        try:
            _eas_kick(channel, rc)
        except Exception as e:
            logger.error(f"[EmergencyAlertarr] EAS: switch failed ch {channel.id}: {e}")
    if fired_names and not eom_only:
        # Someone flipping channels right as the alert goes out still gets it.
        _eas_open_windows(rc, alert, now_ts)
    return fired_names

def _eas_air_get(rc, now_ts=None):
    """(until, priority) of the alert slot on the air, or (0, None)."""
    a = _eas_json_get(rc, _EAS_AIR_KEY, None)
    now_ts = now_ts or time.time()
    try:
        until = float((a or {}).get("until") or 0)
    except Exception:
        until = 0.0
    if until > now_ts + 4 * 3600:
        until = 0.0                 # clock went backwards
    if until <= now_ts:
        return 0.0, None
    return until, int((a or {}).get("prio") or 40)

def _eas_air_set(rc, until, prio):
    _eas_json_set(rc, _EAS_AIR_KEY, {"until": float(until), "prio": int(prio or 40)},
                  int(max(60, until - time.time() + 60)))

def _eas_open_windows(rc, alert, now_ts):
    """After an alert goes out (on channels or to nobody): open the 5 s join
    window, or -- for a national alert -- the hold that late joiners get."""
    if _eas_classify(alert) != "national":
        _eas_json_set(rc, _EAS_JOIN_KEY, {"alert": alert, "until": time.time() + _EAS_JOIN_SECS}, 120)
        return
    old = _eas_json_get(rc, _EAS_NATIONAL_KEY, None)
    since = now_ts
    if old and set(old.get("keys") or []) & set(_alert_keys(alert)):
        since = float(old.get("since") or now_ts)      # a late joiner: same alert
    _eas_json_set(rc, _EAS_NATIONAL_KEY, {"alert": alert, "keys": _alert_keys(alert),
                                         "since": since,
                                         "until": since + _eas_national_hold_secs(alert, since)},
                  4 * 3600)

def _eas_manual_fire(channels, alert, settings):
    """Test / injected alert from the plugin page. Like real alerts (and like
    the old version's Test button) it only goes on armed channels someone is
    WATCHING -- with every channel armed, the old loop built an alert profile
    for every single channel inside the web request, which took minutes,
    ended in a 504, and the watched channel often never got its turn.
    Caller holds the EAS mutex. Returns (fired_names, notes)."""
    rc = _get_redis_client()
    notes = []
    mappings = _get_mappings()
    watched = _eas_streaming_ids(rc)
    excluded = _eas_excluded_cids(settings)
    not_armed = unwatched = 0
    busy = []
    targets = []
    for ch in channels:
        cid = str(ch.id)
        m = mappings.get(cid)
        if not isinstance(m, dict) or not (m.get("type") == "eas" or m.get("eas_armed")):
            not_armed += 1
            continue
        if cid in excluded:
            continue
        if m.get("eas_profile_id") or m.get("eas_restore_at") or m.get("eas_await_stream"):
            busy.append(ch.name)
            continue
        if cid not in watched:
            unwatched += 1
            continue
        targets.append(cid)
    now_ts = time.time()
    fired = []
    if targets:
        fired = _eas_fire_on(rc, settings, mappings, targets, dict(alert), now_ts)
        if fired:
            _save_mappings(mappings)
            slot = max([float(mappings[c].get("eas_stream_end") or mappings[c].get("eas_seq_secs") or 0)
                        for c in targets if isinstance(mappings.get(c), dict)] or [0.0])
            prio = max([int(mappings[c].get("eas_priority") or 40)
                        for c in targets if isinstance(mappings.get(c), dict)] or [40])
            _eas_air_set(rc, now_ts + max(slot, 20.0) + 5.0, prio)
    elif _eas_classify(alert) != "national":
        # Nobody watching right now: anyone tuning in within the next few
        # seconds still gets it (same 5 s window as real alerts).
        _eas_open_windows(rc, alert, now_ts)
    if busy:
        notes.append(f"Already playing an alert: {', '.join(busy[:10])}"
                     + (f" +{len(busy) - 10} more" if len(busy) > 10 else ""))
    if unwatched:
        notes.append(f"Not sent to {unwatched} armed channel(s) nobody is watching"
                     + ("" if fired else " -- tune to an armed channel, then run it again"
                        " (anyone tuning in within 5 seconds still gets it)"))
    if not_armed:
        notes.append(f"{not_armed} selected channel(s) aren't armed for EAS")
    return fired, notes

def _eas_join_window_locked(rc, settings, mappings, streaming_ids):
    """Within _EAS_JOIN_SECS of an alert going out, armed channels that start
    being watched (someone flipped over right as it switched on) get the same
    alert. Returns True if anything fired."""
    win = _eas_json_get(rc, _EAS_JOIN_KEY, None)
    if not win or not win.get("alert"):
        return False
    if time.time() > float(win.get("until") or 0):
        _eas_json_del(rc, _EAS_JOIN_KEY)
        return False
    excluded = _eas_excluded_cids(settings)
    targets = [cid for cid, m in mappings.items()
               if isinstance(m, dict) and (m.get("type") == "eas" or m.get("eas_armed"))
               and cid in streaming_ids and cid not in excluded and not m.get("eas_profile_id")]
    if not targets:
        return False
    keep = dict(win)
    fired = _eas_fire_on(rc, settings, mappings, targets, win["alert"], time.time())
    _eas_json_set(rc, _EAS_JOIN_KEY, keep, 120)     # fire_on re-opened it; keep the original end
    if fired:
        logger.info(f"[EmergencyAlertarr] EAS: viewer tuned in as the alert went out -- also on "
                    f"{', '.join(fired)}")
    return bool(fired)

def _eas_national_join_locked(rc, settings, mappings, streaming_ids):
    """While a national alert is on, anyone who tunes to an armed channel gets
    it too (it isn't a one-shot like a normal alert). Returns True if changed."""
    nat = _eas_json_get(rc, _EAS_NATIONAL_KEY, None)
    if not nat or not nat.get("alert"):
        return False
    if time.time() >= float(nat.get("until") or 0):
        return False                     # over; the sweep's end check sends the EOM
    excluded = _eas_excluded_cids(settings)
    targets = [cid for cid, m in mappings.items()
               if isinstance(m, dict) and (m.get("type") == "eas" or m.get("eas_armed"))
               and cid in streaming_ids and cid not in excluded and not m.get("eas_profile_id")]
    if not targets:
        return False
    return bool(_eas_fire_on(rc, settings, mappings, targets, nat["alert"], time.time()))

def _eas_national_end_check(rc, settings, mappings, feed_keys, complete, now_ts):
    """End a national alert once it has left the feed (or expired): send the
    EOM on every channel holding it, then they restore normally."""
    nat = _eas_json_get(rc, _EAS_NATIONAL_KEY, None)
    holding = [cid for cid, m in mappings.items() if isinstance(m, dict) and m.get("eas_national")]
    if not nat:
        return False
    alert = nat.get("alert") or {}
    gone = complete and not (set(nat.get("keys") or []) & set(feed_keys))
    expired = now_ts >= float(nat.get("until") or 0)
    terminated = bool(_eas_json_get(rc, _EAS_NATIONAL_KEY + ":eat", None))
    if not (gone or expired or terminated):
        return False
    _eas_json_del(rc, _EAS_NATIONAL_KEY + ":eat")
    _eas_json_del(rc, _EAS_NATIONAL_KEY)
    logger.info(f"[EmergencyAlertarr] EAS: national alert over -- sending EOM on {len(holding)} channel(s)")
    if holding:
        _eas_fire_on(rc, settings, mappings, holding, alert, now_ts, eom_only=True)
    return True

def _eas_cfg_fingerprint(settings):
    """The settings that decide WHICH alerts are in the feed."""
    parts = [
        (settings.get("eas_source") or "nws").lower(),
        ",".join(sorted(z.strip().upper() for z in (settings.get("eas_zones") or "").split(",") if z.strip())),
        ",".join(sorted(c.strip() for c in (settings.get("eas_ipaws_same_codes") or "").split(",") if c.strip())),
        (settings.get("eas_severity_filter") or "Moderate").lower(),
    ]
    return "|".join(parts)

def _eas_cfg_fp_get(rc):
    if rc:
        try:
            v = rc.get(_EAS_CFG_FP_KEY)
            if v is not None:
                return v.decode() if isinstance(v, bytes) else str(v)
            return None
        except Exception:
            pass
    return _eas_seen_mem.get("fp")

def _eas_cfg_fp_set(rc, fp):
    _eas_seen_mem["fp"] = fp
    if rc:
        try:
            rc.setex(_EAS_CFG_FP_KEY, _EAS_SEEN_TTL, fp)
        except Exception:
            pass

def _eas_all_mode(settings):
    zones = [z.strip().upper() for z in (settings.get("eas_zones") or "").split(",") if z.strip()]
    codes = (settings.get("eas_ipaws_same_codes") or "").strip().upper()
    src = (settings.get("eas_source") or "nws").lower()
    return ((src in ("nws", "both") and zones == ["ALL"])
            or (src in ("ipaws", "both") and codes == "ALL"))

def _eas_flow_limits(settings):
    """(seconds of quiet between two alerts, max alert age in seconds)."""
    try:
        gap = int(settings.get("eas_alert_gap_secs") if settings.get("eas_alert_gap_secs") not in (None, "")
                  else _EAS_DEFAULT_GAP_SECS)
    except Exception:
        gap = _EAS_DEFAULT_GAP_SECS
    gap = max(0, min(3600, gap))
    if _eas_all_mode(settings):
        gap = max(gap, _EAS_ALL_MODE_GAP_SECS)     # nationwide: never back-to-back
    try:
        age_min = int(settings.get("eas_max_alert_age_min") or _EAS_DEFAULT_MAX_AGE_MIN)
    except Exception:
        age_min = _EAS_DEFAULT_MAX_AGE_MIN
    age_min = max(5, min(720, age_min))
    return float(gap), float(age_min * 60)

def _eas_alert_age_secs(a, now_ts):
    """Seconds since the alert was issued (CAP 'sent', else 'effective');
    None if unknown or in the future."""
    from datetime import datetime, timezone
    for f in ("sent", "effective"):
        raw = (a.get(f) or "").strip()
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = now_ts - dt.timestamp()
            return age if age > 0 else None
        except Exception:
            continue
    return None

def _eas_channel_meta_profile(rc, channel_uuid):
    """Stream profile id the channel's live session was started with, or None."""
    if not rc:
        return None
    try:
        from apps.proxy.live_proxy.redis_keys import RedisKeys
        key = RedisKeys.channel_metadata(channel_uuid)
    except Exception:
        key = f"live:channel:{channel_uuid}:metadata"
    try:
        v = rc.hget(key, "stream_profile")
        if v is None:
            return None
        return (v.decode() if isinstance(v, bytes) else str(v)).strip() or None
    except Exception:
        return None

def _eas_watchdog_locked(rc):
    """Safety net for 'alert stuck on screen'. Caller holds the mutex.

    1. A channel whose profile in the database is still one of our alert
       profiles while no alert is running on it is put back (mappings lost,
       crash between steps, manual edits).
    2. A channel whose LIVE session is still running an alert profile while no
       alert is active (the restore's stream restart was missed or failed) is
       stopped so the viewer reconnects on the normal profile. Rate-limited per
       channel.

    Cost is independent of how many channels are armed: (1) asks the database
    only for channels that point at one of our alert profiles (normally none),
    and (2) only looks at channels someone is watching. The old version
    loaded every armed channel (plus its override and profile) every pass --
    thousands of queries with all channels armed, holding the plugin lock
    long enough to delay alerts and slow Dispatcharr down."""
    try:
        from apps.channels.models import Channel
        from core.models import StreamProfile
    except Exception:
        return
    mappings = _get_mappings()
    changed = False
    clone_ids = _eas_clone_ids(rc)

    def _armed_idle(cid):
        m = mappings.get(cid)
        return (isinstance(m, dict) and (m.get("type") == "eas" or m.get("eas_armed"))
                and not (m.get("eas_profile_id") or m.get("eas_restore_at") or m.get("eas_await_stream")))

    # 1. Channels still pointing at an alert profile in the database.
    try:
        db_clones = [p.id for p in StreamProfile.objects.filter(name__startswith=PROFILE_PREFIX)]
    except Exception:
        db_clones = []
    clone_ids |= {str(i) for i in db_clones}
    suspects = {}
    if db_clones:
        for kw in ({"stream_profile_id__in": db_clones}, {"override__stream_profile_id__in": db_clones}):
            try:
                for ch in Channel.objects.filter(**kw):
                    suspects[str(ch.id)] = ch
            except Exception:
                pass
    for cid, channel in suspects.items():
        if not _armed_idle(cid):
            continue            # an alert is legitimately running here (or not ours)
        try:
            eff = _eas_effective_profile(channel)
        except Exception:
            eff = None
        if eff is None or not (eff.name or "").startswith(PROFILE_PREFIX):
            continue
        logger.warning(f"[EmergencyAlertarr] EAS watchdog: ch {cid} left on alert profile "
                       f"{eff.id} with no alert running -- restoring")
        orig = mappings[cid].get("original_profile_id")
        if orig and str(orig) == str(eff.id):
            orig = None
        try:
            _restore_profile(channel, orig, slot=_eas_profile_slot(channel)[0])
        except Exception as e:
            logger.error(f"[EmergencyAlertarr] EAS watchdog: restore ch {cid} failed: {e}")
        _eas_clone_remember(rc, eff.id)
        clone_ids.add(str(eff.id))
        _delete_cloned_profile(eff.id)
        changed = True

    # 2. Live sessions (watched channels only) still running an alert profile.
    if clone_ids and time.time() - _eas_last_end(rc) >= 20:
        try:
            watched = _eas_streaming_ids(rc)
        except Exception:
            watched = set()
        for cid in watched:
            if not _armed_idle(cid):
                continue
            try:
                channel = Channel.objects.filter(id=int(cid)).first()
            except Exception:
                channel = None
            if not channel:
                continue
            live_pid = _eas_channel_meta_profile(rc, str(channel.uuid))
            if not (live_pid and live_pid in clone_ids):
                continue
            ok = True
            if rc:
                try:
                    ok = bool(rc.set(f"{_EAS_WD_PREFIX}{cid}", "1", nx=True, ex=30))
                except Exception:
                    ok = True
            if ok:
                logger.warning(f"[EmergencyAlertarr] EAS watchdog: ch {cid} is still streaming alert "
                               f"profile {live_pid} after its alert ended -- restarting the stream")
                _restart_channel_stream_async(channel, label="EAS watchdog")
    if changed:
        _save_mappings(mappings)

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
        # check every couple of seconds so the overlay window is timed
        # accurately instead of only once per poll interval.
        slept = 0
        while slept < interval and not stop_event.is_set():
            stop_event.wait(timeout=_EAS_TICK_SECS)
            slept += _EAS_TICK_SECS
            try:
                _eas_restore_tick()
            except Exception:
                pass
            try:
                if _eas_monitor_new_pop(_get_redis_client()):
                    break      # a monitored channel just aired an alert: sweep now
            except Exception:
                pass
            finally:
                try:
                    from django.db import connection
                    connection.close()
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
    if mode == "realistic":
        return _eas_realistic_test_due(settings, now_local, last_key)
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

def _eas_realistic_plan(settings, year, month):
    """This month's RMT start time + the set of ISO weeks it covers.

    47 CFR 11.61(a)(1): RMTs run between 8:30 a.m. and local sunset in odd
    months and between sunset and 8:30 a.m. in even months. The day is the
    configured day-of-month; the time is picked pseudo-randomly inside the
    window (seeded by the month, so every worker agrees)."""
    import random
    from datetime import datetime
    try:
        dom = max(1, min(28, int(settings.get("eas_test_day", 1) or 1)))
    except Exception:
        dom = 1
    rnd = random.Random(f"rmt-{_PLUGIN_KEY}-{year}-{month}")
    if month % 2 == 1:
        hour = rnd.randint(9, 16)               # daytime window, before sunset
    else:
        hour = rnd.choice([20, 21, 22, 23])     # night window, after sunset
    minute = rnd.randint(0, 59)
    return datetime(year, month, dom, hour, minute)

def _eas_realistic_test_due(settings, now_local, last_key):
    """'Realistic' schedule: one RMT a month inside the FCC window, and an RWT
    at a random day/time every other week ("at random days and times"; none
    in the week of the RMT, per 11.61(a)(2)). Returns (key, kind) or None."""
    import random
    from datetime import datetime, timedelta
    naive = now_local.replace(tzinfo=None)
    rmt = _eas_realistic_plan(settings, naive.year, naive.month)
    if rmt <= naive < rmt + timedelta(minutes=45):
        key = f"rmt-{naive.year}-{naive.month}"
        return None if key == last_key else (key, "monthly")
    iso = naive.isocalendar()
    if rmt.isocalendar()[:2] == iso[:2]:
        return None                              # RMT week: no RWT
    rnd = random.Random(f"rwt-{_PLUGIN_KEY}-{iso[0]}-{iso[1]}")
    day = rnd.randint(0, 6)
    hour = rnd.randint(8, 21)
    minute = rnd.randint(0, 59)
    monday = (naive - timedelta(days=naive.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    when = monday + timedelta(days=day, hours=hour, minutes=minute)
    if when <= naive < when + timedelta(minutes=45):
        key = f"rwt-{iso[0]}-{iso[1]}"
        return None if key == last_key else (key, "weekly")
    return None

def _eas_fire_scheduled_test(kind=None):
    """Fire a Required Weekly/Monthly Test across all EAS-armed channels that
    currently have a viewer, exactly like a real station's automated RWT/RMT.
    Reuses the same activation/restore machinery as the sweep."""
    from apps.channels.models import Channel
    from datetime import datetime, timedelta, timezone as _tz

    settings = _get_settings()
    rc = _get_redis_client()
    mode = kind or (settings.get("eas_test_schedule") or "off").lower()
    test_area = (settings.get("eas_test_area") or "").strip() or None

    fired = []
    with _EasMutex(rc, wait=30) as held:
        if not held:
            logger.warning("[EmergencyAlertarr] EAS scheduled test skipped: plugin busy")
            return 0
        mappings = _get_mappings()
        streaming_ids = _eas_streaming_ids(rc)
        now_utc = datetime.now(_tz.utc)
        for cid, mapping in list(mappings.items()):
            if not isinstance(mapping, dict):
                continue
            if not (mapping.get("type") == "eas" or mapping.get("eas_armed")):
                continue
            if mapping.get("eas_profile_id") or mapping.get("eas_restore_at"):
                continue  # already mid-alert
            if cid not in streaming_ids:
                continue  # nobody watching this one
            if cid in _eas_excluded_cids(settings):
                continue  # details / monitored channel
            channel = Channel.objects.filter(id=int(cid)).first()
            if not channel:
                continue
            fake_alerts = [_eas_build_test_alert(mode, now_utc, area=test_area)]
            try:
                seq_duration = _eas_activate(channel, mapping, fake_alerts, settings)
                mappings[cid] = mapping
                _eas_state_set(rc, cid, "__test__")
                _eas_kick(channel, rc)
                fired.append(channel.name)
            except Exception as e:
                logger.error(f"[EmergencyAlertarr] EAS scheduled test failed for ch {cid}: {e}", exc_info=True)
        if fired:
            _save_mappings(mappings)

    label = "Required Weekly Test" if mode == "weekly" else "Required Monthly Test"
    if fired:
        _eas_history_add(label, test_area or "this viewing area", fired, kind="test")
    logger.info(f"[EmergencyAlertarr] EAS {label}: fired on {len(fired)} channel(s)")
    return len(fired)

def _eas_scheduler_loop(stop_event):
    """Background loop that checks once a minute whether a scheduled RWT/RMT is
    due, and fires it (once, via a Redis lock so only one worker does)."""
    while not stop_event.is_set():
        try:
            settings = _get_settings()
            if (settings.get("eas_test_schedule") or "off").lower() in ("weekly", "monthly", "realistic"):
                from datetime import datetime
                # Local time per the OS/container timezone.
                now_local = datetime.now()
                rc = _get_redis_client()
                last_key = _eas_sched_mem.get("last")
                if rc:
                    try:
                        last_key = rc.get(_EAS_SCHED_KEY)
                        if isinstance(last_key, bytes):
                            last_key = last_key.decode()
                    except Exception:
                        pass
                due = _eas_scheduled_test_due(settings, now_local, last_key)
                kind = None
                if isinstance(due, tuple):
                    due, kind = due
                window_key = due
                if window_key:
                    # Only one worker fires the scheduled test.
                    if not rc or rc.set(_EAS_SCHED_LOCK, "1", nx=True, ex=300):
                        _eas_sched_mem["last"] = window_key
                        if rc:
                            try:
                                rc.set(_EAS_SCHED_KEY, window_key)
                            except Exception:
                                pass
                        logger.info(f"[EmergencyAlertarr] EAS scheduled test window reached: {window_key}")
                        _eas_fire_scheduled_test(kind)
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
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = f"{MAPPINGS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mappings, f, indent=2)
        os.replace(tmp, MAPPINGS_FILE)
        # (the uuid map refreshes itself when the set of mapped channels changes)
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
    Cached; refreshed every UUID_MAP_TTL seconds or when the set of mapped
    channels changes (not on every mappings save -- with every channel armed
    that reloaded thousands of channels on each alert step)."""
    global _uuid_map_cache
    now = time.time()
    keyset = frozenset(str(k) for k in mappings.keys())
    if (now - _uuid_map_cache["fetched_at"] < UUID_MAP_TTL and _uuid_map_cache["map"]
            and _uuid_map_cache.get("ids") == keyset):
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
        _uuid_map_cache = {"map": result, "fetched_at": now, "ids": keyset}
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

_EAS_MONITOR_LOCK = f"emergencyalertarr:{_PLUGIN_KEY}:eas_monitor_lock"

_EAS_MONITOR_NEW  = f"emergencyalertarr:{_PLUGIN_KEY}:eas_monitor_new"

_EAS_MONITOR_SR = 22050

def _eas_monitor_area_ok(alert, settings):
    """A decoded header is relayed only if it is for our area (an ENDEC's
    location filter). National codes always pass. With no county codes
    configured, everything the monitored station airs is accepted."""
    if settings.get("eas_national_test"):
        return True
    from eaa_fips import zones_to_same
    from eaa_fips import state_postal_to_fips
    codes = [c for c in (settings.get("eas_ipaws_same_codes") or "").replace(" ", "").split(",")
             if c.isdigit() and len(c) == 6]
    zones = [z.strip().upper() for z in (settings.get("eas_zones") or "").split(",") if z.strip()]
    codes += zones_to_same(zones)
    if not codes:
        # Forecast zones only (TXZ119): accept the zones' whole states.
        for z in zones:
            ss = state_postal_to_fips(z[:2]) if len(z) == 6 else None
            if ss and "0" + ss + "000" not in codes:
                codes.append("0" + ss + "000")
    if not codes:
        return True
    ac = alert.get("same_codes") or []
    if any(str(c).endswith("00000") for c in ac) or _is_national_alert(alert):
        return True
    return _same_codes_match(ac, codes)

def _eas_monitor_alerts(rc, settings):
    """Alerts decoded from the monitored channel that are still valid."""
    if not _eas_chan(settings, "eas_monitor_channel"):
        return []
    items = _eas_json_get(rc, _EAS_MONITOR_KEY, [])
    out = []
    now = time.time()
    from datetime import datetime
    for a in items:
        try:
            exp = datetime.fromisoformat((a.get("expires") or "").replace("Z", "+00:00")).timestamp()
        except Exception:
            exp = float(a.get("received") or 0) + 3600
        if exp > now:
            out.append(a)
    return out

def _eas_monitor_push(rc, alert):
    items = _eas_json_get(rc, _EAS_MONITOR_KEY, [])
    items = [a for a in items if a.get("id") != alert.get("id")][-40:]
    items.append(alert)
    _eas_json_set(rc, _EAS_MONITOR_KEY, items, 12 * 3600)
    if rc:
        try:
            rc.setex(_EAS_MONITOR_NEW, 600, "1")
        except Exception:
            pass

def _eas_monitor_new_pop(rc):
    if not rc:
        return False
    try:
        return bool(rc.delete(_EAS_MONITOR_NEW))
    except Exception:
        return False

def _eas_monitor_loop(stop_event):
    """Listen to one Dispatcharr channel's audio for EAS tones, like an
    ENDEC's monitoring assignment (LP-1/LP-2). Decoded messages (header +
    the recorded audio message) are handed to the sweep, which applies the
    same filters, duplicate check and priority as feed alerts. The channel is
    pulled through Dispatcharr's own proxy, so it uses one upstream
    connection, shared with anyone watching it."""
    import select
    import uuid as _uuid
    token = _uuid.uuid4().hex
    proc = None
    cur = None
    dec = None
    status_t = 0.0
    fail = 0
    stats = {"since": 0.0, "headers": 0, "last": ""}

    def _stop():
        nonlocal proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        proc = None

    while not stop_event.is_set():
        try:
            settings = _get_settings()
            chid = _eas_chan(settings, "eas_monitor_channel")
            try:
                from django.db import connection
                connection.close()
            except Exception:
                pass
            if not chid:
                _stop()
                cur = None
                stop_event.wait(timeout=15)
                continue
            rc = _get_redis_client()
            owner = True
            if rc:
                try:
                    if rc.set(_EAS_MONITOR_LOCK, token, nx=True, ex=40):
                        owner = True
                    else:
                        v = rc.get(_EAS_MONITOR_LOCK)
                        v = v.decode() if isinstance(v, bytes) else v
                        owner = (v == token)
                        if owner:
                            rc.expire(_EAS_MONITOR_LOCK, 40)
                except Exception:
                    owner = True
            if not owner:
                _stop()
                stop_event.wait(timeout=20)
                continue
            if proc is None or chid != cur:
                _stop()
                from apps.channels.models import Channel
                from eaa_forcetune import dispatcharr_base_urls
                from eaa_same import SameDecoder
                ch = Channel.objects.filter(id=int(chid)).first()
                if not ch:
                    stop_event.wait(timeout=30)
                    continue
                base = dispatcharr_base_urls(settings.get("eas_dispatcharr_url") or "")[fail % 3]
                url = f"{base}/proxy/ts/stream/{ch.uuid}"
                ff = shutil.which("ffmpeg") or "ffmpeg"
                proc = subprocess.Popen(
                    [ff, "-hide_banner", "-v", "error", "-nostdin", "-user_agent",
                     "EmergencyAlertarr/monitor", "-rw_timeout", "15000000", "-i", url,
                     "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(_EAS_MONITOR_SR), "-f", "s16le", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                cur = chid
                _ensure_dirs()
                dec = SameDecoder(_EAS_MONITOR_SR, workdir=os.path.join(RUNTIME_DIR, "monitor"))
                stats.update({"since": time.time(), "channel": ch.name, "url": base})
                logger.info(f"[EmergencyAlertarr] monitor: listening to '{ch.name}' for EAS tones")
            # read ~0.5 s of audio
            r, _, _ = select.select([proc.stdout], [], [], 5.0)
            data = proc.stdout.read(_EAS_MONITOR_SR) if r else b""
            if not data:
                if proc.poll() is not None:
                    fail += 1
                    logger.warning(f"[EmergencyAlertarr] monitor: stream ended -- reconnecting "
                                   f"in {min(60, 5 * fail)}s")
                    _stop()
                    stop_event.wait(timeout=min(60, 5 * fail))
                continue
            fail = 0
            import numpy as np
            x = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float64) / 32768.0
            for ev in dec.feed(x):
                from eaa_same import header_to_alert
                from eaa_fips import describe_codes
                from eaa_styles import SAME_EVENT_NAMES
                ean = "-EAN-" in (ev.get("text") or "")
                if ev["type"] == "header":
                    stats["headers"] += 1
                    stats["last"] = ev["text"]
                    logger.info(f"[EmergencyAlertarr] monitor: EAS header received: {ev['text']}")
                    if ean:
                        # A national alert is relayed live, at the header -- it
                        # ends when the station sends its EOM.
                        a = header_to_alert(ev["text"], ev["time"], event_names=SAME_EVENT_NAMES,
                                            describe=describe_codes,
                                            channel_name=stats.get("channel", ""))
                        if a:
                            _eas_monitor_push(rc, a)
                elif ev["type"] == "message":
                    a = header_to_alert(ev["text"], ev["time"], event_names=SAME_EVENT_NAMES,
                                        describe=describe_codes, audio_file=ev.get("audio"),
                                        channel_name=stats.get("channel", ""))
                    if not a:
                        continue
                    if ean:
                        # EOM for the national alert: drop it from the monitor
                        # list so the sweep sees it as over and sends the EOM.
                        items = [x for x in _eas_json_get(rc, _EAS_MONITOR_KEY, []) if x.get("id") != a["id"]]
                        _eas_json_set(rc, _EAS_MONITOR_KEY, items, 12 * 3600)
                        if rc:
                            try:
                                rc.setex(_EAS_MONITOR_NEW, 600, "1")
                            except Exception:
                                pass
                        logger.info("[EmergencyAlertarr] monitor: national alert ended (EOM received)")
                        continue
                    if not _eas_monitor_area_ok(a, settings):
                        logger.info(f"[EmergencyAlertarr] monitor: {a['event']} is not for this area -- ignored")
                        continue
                    _eas_monitor_push(rc, a)
                    logger.info(f"[EmergencyAlertarr] monitor: {a['event']} received "
                                f"({'with' if ev.get('audio') else 'no'} audio, "
                                f"{'EOM' if ev.get('eom') else 'no EOM / reset'})")
            if time.time() - status_t > 30:
                status_t = time.time()
                _eas_json_set(rc, _EAS_MONITOR_STATUS_KEY, dict(stats, ts=status_t), 300)
        except Exception as e:
            logger.error(f"[EmergencyAlertarr] monitor error: {e}", exc_info=True)
            _stop()
            stop_event.wait(timeout=30)
    _stop()

def _poll_loop(stop_event):
    # EAS sweep -- NWS alert polling, interval configurable (default 60s)
    eas_t = threading.Thread(target=_eas_sweep_loop, args=(stop_event,), daemon=True)
    eas_t.start()
    # Channel monitor -- listens to one channel's audio for EAS tones
    mon_t = threading.Thread(target=_eas_monitor_loop, args=(stop_event,), daemon=True)
    mon_t.start()
    # RWT/RMT scheduler -- fires a Required Weekly/Monthly Test on schedule
    sched_t = threading.Thread(target=_eas_scheduler_loop, args=(stop_event,), daemon=True)
    sched_t.start()
    eas_t.join()
    sched_t.join()

_EAS_LOCKED_ACTIONS = {
    "enable_eas", "disable_eas", "test_eas", "inject_alert", "migrate_eas",
    "clean_orphans", "reset_all_eas", "stop_alerts",
}

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
        # Channel picker (number · name, every channel), for the details and
        # monitored channel.
        try:
            from apps.channels.models import Channel
            def _num_label(c):
                n = getattr(c, "channel_number", None)
                try:
                    n = (str(int(n)) if float(n).is_integer() else str(n)) if n is not None else ""
                except Exception:
                    n = str(n or "")
                name = c.name or f"Channel {c.id}"
                return f"{n} · {name}" if n else name
            def _sort_key(c):
                n = getattr(c, "channel_number", None)
                try:
                    return (0, float(n), c.name or "")
                except Exception:
                    return (1, 0.0, c.name or "")
            channel_picker = [{"value": str(c.id), "label": _num_label(c)}
                              for c in sorted(Channel.objects.all(), key=_sort_key)]
        except Exception:
            channel_picker = [{"value": c["value"], "label": c["label"]} for c in channels]

        try:
            mappings = _get_mappings()
        except Exception:
            mappings = {}

        # ---- Status line -------------------------------------------------
        armed = [(cid, m) for cid, m in mappings.items()
                 if isinstance(m, dict) and (m.get("type") == "eas" or m.get("eas_armed"))]
        playing = [m.get("channel_name", f"Channel {cid}") + f" — {_eas_active.get(cid)}"
                   for cid, m in armed if _eas_active.get(cid)]
        try:
            tts_ok = tts_available(_eas_tts_opts(_get_settings()))
        except Exception:
            tts_ok = False
        if armed:
            status = f"{len(armed)} channel(s) armed for EAS."
            if playing:
                status += " Alert on screen: " + "; ".join(playing) + "."
            else:
                status += " No alert playing right now."
        else:
            status = "No channels armed yet — pick channels in section 1 and press \"Enable EAS\"."
        status += " Spoken readout: " + ("ready." if tts_ok else "no TTS engine found (alerts use silence).")

        def section(n, title, text=None):
            f = {"id": f"_sec_{n}", "type": "info", "label": f"{n} · {title}"}
            if text:
                f["help_text"] = text
            return f

        def note(i, text):
            return {"id": f"_note_{i}", "type": "info", "label": "", "help_text": text}

        return [
            # ── Overview ─────────────────────────────────────────────────
            {"id": "_eas_title", "type": "info", "label": "EmergencyAlertarr — EAS for your channels",
             "help_text": "Watches NWS / FEMA IPAWS alerts for your area. When one is issued, whoever is "
                          "watching an armed channel gets a real-looking EAS screen with SAME tones and a "
                          "spoken readout, then the channel goes back to normal. Quick start: 1) choose "
                          "channels, 2) enter your area, 3) press \"Enable EAS\", 4) press \"Test EAS Alert\"."},
            {"id": "_eas_status", "type": "info", "label": "Status", "help_text": status},

            # ── 1 · Channels ─────────────────────────────────────────────
            section(1, "CHANNELS",
                    "Which channels get alerts. Armed channels play normally and only switch over while "
                    "an alert is playing. After changing this, press \"Enable EAS\" (or \"Disable EAS\")."),
            {"id": "eas_target_type", "type": "select", "label": "Apply to", "default": "channel",
             "options": [{"value": "all",     "label": "All channels"},
                         {"value": "group",   "label": "One channel group"},
                         {"value": "groups",  "label": "Several channel groups"},
                         {"value": "channel", "label": "One channel"}]},
            {"id": "eas_channel_id", "type": "select", "label": "Channel",
             "help_text": "Used when 'Apply to' is One channel.", "options": channels},
            {"id": "eas_channel_group_id", "type": "select", "label": "Channel group",
             "help_text": "Used when 'Apply to' is One channel group.", "options": groups},
            {"id": "eas_channel_group_names", "type": "string", "label": "Channel groups",
             "help_text": "Used when 'Apply to' is Several channel groups. Comma-separated names.",
             "placeholder": "News, Locals, Networks"},
            {"id": "eas_exclude_groups", "type": "string", "label": "Skip these groups (optional)",
             "help_text": "Comma-separated group names that never get alerts.", "placeholder": "Music, Radio"},

            # ── 2 · Your area ────────────────────────────────────────────
            section(2, "YOUR AREA",
                    "Where alerts come from and which ones count as yours. National alerts (presidential "
                    "message, nationwide test) always come through."),
            {"id": "eas_national_test", "type": "boolean", "label": "National test mode (receive every US alert)",
             "default": False,
             "help_text": "For testing: overrides everything below -- NWS + IPAWS, the whole country, every "
                          "severity. Alerts already active when you switch it on are skipped; new ones play, "
                          "at least 5 minutes apart. Turn it off to go back to your area."},
            {"id": "eas_source", "type": "select", "label": "Alert source", "default": "nws",
             "help_text": "NWS = weather alerts from api.weather.gov. IPAWS = FEMA's national feed "
                          "(adds AMBER, civil and law-enforcement alerts). Both = merged, duplicates removed.",
             "options": [{"value": "nws",   "label": "NWS (weather)"},
                         {"value": "ipaws", "label": "IPAWS (FEMA — weather + civil + AMBER)"},
                         {"value": "both",  "label": "Both"}]},
            {"id": "eas_zones", "type": "string", "label": "NWS county / zone codes",
             "help_text": "For NWS or Both. Your county code is in the URL on weather.gov (e.g. TXC113 "
                          "= Dallas County, TX). Comma-separated. ALL = every US alert (for testing).",
             "placeholder": "TXC113, TXC121"},
            {"id": "eas_ipaws_same_codes", "type": "string", "label": "IPAWS county codes (6-digit SAME)",
             "help_text": "For IPAWS or Both. 0 + state + county FIPS, e.g. 048113 = Dallas County TX. "
                          "048000 = all of Texas. ALL = every alert nationwide (for testing).",
             "placeholder": "048113, 048121"},
            {"id": "eas_severity_filter", "type": "select", "label": "Which alerts", "default": "Moderate",
             "help_text": "Life-safety alerts (tornado, flash flood, AMBER, civil emergency, evacuation…) "
                          "always play for your area, whatever you pick here.",
             "options": [{"value": "Moderate", "label": "Watches and warnings"},
                         {"value": "Severe",   "label": "Warnings only"},
                         {"value": "Extreme",  "label": "Only the most extreme"}]},

            # ── 3 · Alert screen ─────────────────────────────────────────
            section(3, "ALERT SCREEN",
                    "What viewers see. Each style recreates a real EAS encoder or cable box. Styles marked "
                    "'over the live program' draw on top of the channel, so they take a few seconds longer "
                    "to appear and use more CPU."),
            {"id": "eas_overlay_style", "type": "select", "label": "Screen style", "default": "dasdec",
             "options": [{"value": k, "label": lbl} for k, lbl in STYLE_CATALOG]},
            {"id": "eas_aspect", "type": "select", "label": "Older 4:3 screens on a widescreen TV",
             "default": "pillarbox",
             "options": [{"value": "pillarbox", "label": "Black bars on the sides (like the real equipment)"},
                         {"value": "stretch",   "label": "Stretch to fill the screen (like many cable boxes)"}]},
            {"id": "eas_sd_look", "type": "select", "label": "Old SD cable look", "default": "legacy",
             "help_text": "Renders the screen through a 480-line path with smeared colour, like the SD "
                          "encoders and cable channels of the time.",
             "options": [{"value": "legacy", "label": "Older styles only (DASDEC legacy, EASyPLUS, EASyCAP, Gorman-Redlich, VDS)"},
                         {"value": "all",    "label": "All full-screen styles"},
                         {"value": "off",    "label": "Off — clean HD"}]},
            {"id": "eas_details_channel", "type": "select", "label": "EAS details channel (optional)",
             "default": "none",
             "help_text": "Like a cable system that force-tunes every box to one channel during an alert: "
                          "viewers see a couple of seconds of this channel, then it freezes, goes black and "
                          "the alert comes up. Leave on 'none' and the channel being watched freezes, goes "
                          "black, then the alert. This channel never gets alerts itself.",
             "options": [{"value": "none", "label": "— none —"}] + channel_picker},
            {"id": "eas_show_cap_text", "type": "boolean", "label": "Show the full alert text", "default": True,
             "help_text": "On: after the 'has issued…' line, show the details (what, where, instructions). "
                          "Off: only the short ENDEC translation."},
            {"id": "eas_operator_name", "type": "string", "label": "Your 'cable company' name (optional)",
             "help_text": "Used when tests or custom alerts come from you: '<name> has issued A REQUIRED "
                          "WEEKLY TEST…'. Blank = 'A broadcast station or cable system'.",
             "placeholder": "e.g. Your Cable Co."},
            note("timing", "Timing — pages and bars loop until the alert ends."),
            {"id": "eas_page_secs", "type": "number", "label": "Seconds per page", "default": 7,
             "min": 3, "max": 30,
             "help_text": "DASDEC, EASyCAP and Gorman-Redlich pages."},
            {"id": "eas_sara_secs", "type": "number", "label": "Seconds per SARA bar chunk", "default": 3,
             "min": 1, "max": 15},
            {"id": "eas_crawl_speed", "type": "number", "label": "Crawl speed (1 slow – 10 fast)",
             "default": 5, "min": 1, "max": 10,
             "help_text": "Scrolling text: EASyPLUS, VDS, tickers."},
            note("dasdec", "DASDEC options"),
            {"id": "eas_dasdec_heading", "type": "string", "label": "Page heading",
             "help_text": "Blank = the style's own heading. Type none for no heading.",
             "placeholder": "Emergency Alert System"},
            {"id": "eas_bg_color", "type": "select", "label": "Background colour", "default": "default",
             "options": [{"value": "default",    "label": "Style default"},
                         {"value": "purpleblue", "label": "Purple-blue (DASDEC default)"},
                         {"value": "blue",       "label": "Blue"},
                         {"value": "black",      "label": "Black"},
                         {"value": "red",        "label": "Red"},
                         {"value": "maroon",     "label": "Dark red"},
                         {"value": "orange",     "label": "Orange"}]},
            {"id": "eas_spanish", "type": "boolean", "label": "Add Spanish pages", "default": False,
             "help_text": "Like DASDEC's English + Spanish translation option. VDS shows the event in Spanish too."},
            note("easy", "EASyPLUS / VDS options"),
            {"id": "eas_tune_to", "type": "string", "label": "'Tune to channel' line (optional)",
             "placeholder": "Tune to Channel 15 for Further Information"},
            {"id": "eas_legacy_originator", "type": "boolean", "label": "Early EASyPLUS wording",
             "default": False,
             "help_text": "Shows 'Broadcast Station or Cable System' instead of 'EAS Participant'."},

            # ── 4 · Sound ────────────────────────────────────────────────
            section(4, "SOUND",
                    "During an alert the channel's audio is replaced by: SAME header tones → attention tone → "
                    "spoken readout → end-of-message tones."),
            {"id": "eas_generate_tones", "type": "boolean", "label": "Real SAME tones for each alert",
             "default": True,
             "help_text": "On: the header tones carry this alert's real data (event, counties, time) like a "
                          "real encoder. Off: play the WAV files in the plugin's wav/ folder."},
            {"id": "eas_att_secs", "type": "number", "label": "Attention tone length (seconds)", "default": 8,
             "min": 8, "max": 25, "help_text": "The FCC allows 8–25 seconds; 8 is typical."},
            {"id": "eas_tts_enabled", "type": "boolean", "label": "Read the alert aloud", "default": True,
             "help_text": "Built in — nothing to install."},
            {"id": "eas_tts_voice", "type": "select", "label": "Voice", "default": "kal16",
             "options": [{"value": k, "label": l} for k, l in TTS_VOICES]},
            {"id": "eas_tts_fx", "type": "select", "label": "Voice sound", "default": "endec",
             "options": [{"value": k, "label": l} for k, l in TTS_FX]},
            {"id": "eas_tts_speed", "type": "number", "label": "Speaking speed", "default": 1.0,
             "min": 0.6, "max": 1.6, "help_text": "1.0 = normal."},
            {"id": "eas_use_cap_audio", "type": "boolean", "label": "Use the alert's own recorded message",
             "default": True,
             "help_text": "IPAWS alerts (and alerts heard on a monitored channel) often carry the "
                          "originator's own audio. Like a DASDEC, play that instead of the synthesized voice."},
            note("levels", "Levels and the encoder's audio output"),
            {"id": "eas_loudness", "type": "select", "label": "Loudness", "default": "-24",
             "help_text": "The spoken message is levelled to this, so the alert sits where the channel sits.",
             "options": [{"value": k, "label": l} for k, l in LOUDNESS_TARGETS]},
            {"id": "eas_tone_offset_lu", "type": "number", "label": "Tones louder than the voice by (LU)",
             "default": 8, "min": 0, "max": 20,
             "help_text": "Real EAS tones are noticeably louder than programme audio. 0 = same level."},
            {"id": "eas_audio_channels", "type": "select", "label": "Encoder audio output", "default": "both",
             "help_text": "Some headends only fed the EAS audio to one side.",
             "options": [{"value": k, "label": l} for k, l in AUDIO_CHANNEL_MODES]},
            {"id": "eas_audio_hum", "type": "select", "label": "Mains hum", "default": "off",
             "help_text": "A faint ground-loop hum under the alert, like an analog headend.",
             "options": [{"value": "off", "label": "Off"}, {"value": "60", "label": "60 Hz (North America)"},
                         {"value": "50", "label": "50 Hz"}]},
            {"id": "eas_audio_click", "type": "boolean", "label": "Audio switch click", "default": True,
             "help_text": "The relay click and short dip as the encoder switches the audio over."},
            {"id": "eas_program_audio", "type": "select", "label": "Programme audio (over-the-program styles)",
             "default": "switch",
             "help_text": "For styles drawn over the live channel.",
             "options": [{"value": "switch", "label": "Audio switch — programme off during the EAS audio, back after the EOM (DASDEC)"},
                         {"value": "duck",   "label": "Duck — programme kept low under the alert"},
                         {"value": "mute",   "label": "Mute until the alert screen goes away"}]},
            {"id": "eas_duck_db", "type": "number", "label": "Duck programme by (dB)", "default": 18,
             "min": 6, "max": 40},

            # ── 5 · Tests ────────────────────────────────────────────────
            section(5, "TESTS",
                    "\"Test EAS Alert\" plays a test on the chosen channels right away. Weekly tests are "
                    "tones only (header + end), monthly tests add the attention tone and the test script — "
                    "the same as real stations (47 CFR 11.61)."),
            {"id": "eas_test_type", "type": "select", "label": "Test button plays", "default": "monthly",
             "options": [{"value": "monthly", "label": "Required Monthly Test (RMT)"},
                         {"value": "weekly",  "label": "Required Weekly Test (RWT)"}]},
            {"id": "eas_test_area", "type": "string", "label": "Area named in tests (optional)",
             "help_text": "Blank = the county names of the locations below.",
             "placeholder": "e.g. Dallas County, Texas"},
            {"id": "eas_test_locations", "type": "string", "label": "Locations in test / custom headers",
             "help_text": "6-digit SAME codes (up to 31), like the counties a station covers. More counties = a "
                          "longer header. Blank = your IPAWS county codes, else your NWS county zones.",
             "placeholder": "048113, 048121, 048439"},
            {"id": "eas_test_schedule", "type": "select", "label": "Automatic tests", "default": "off",
             "help_text": "Only plays on channels someone is watching.",
             "options": [{"value": "off",       "label": "Off"},
                         {"value": "realistic", "label": "Like a real station — monthly test + random weekly tests"},
                         {"value": "weekly",    "label": "Weekly test at a fixed day and hour"},
                         {"value": "monthly",   "label": "Monthly test at a fixed day and hour"}]},
            {"id": "eas_test_day", "type": "number", "label": "Test day", "default": 1, "min": 0, "max": 28,
             "help_text": "Weekly: 0 = Monday … 6 = Sunday. Monthly / realistic: day of the month (1–28)."},
            {"id": "eas_test_hour", "type": "number", "label": "Test hour (0–23)", "default": 12,
             "min": 0, "max": 23, "help_text": "Fixed schedules only. 'Like a real station' picks its own times."},

            # ── 6 · Custom alert ─────────────────────────────────────────
            section(6, "CUSTOM ALERT",
                    "Fill in and press \"Inject Custom Alert\" to send your own alert to the chosen "
                    "channels — same screen, tones and voice as a real one."),
            {"id": "eas_inject_template", "type": "select", "label": "Template", "default": "custom",
             "help_text": "Fills in the event code, message, instructions, sender and valid time. "
                          "Anything you type below overrides it.",
             "options": [{"value": k, "label": l} for k, l in TEMPLATE_CHOICES]},
            {"id": "eas_inject_event", "type": "string", "label": "Event",
             "placeholder": "Civil Emergency Message"},
            {"id": "eas_inject_area", "type": "string", "label": "Area", "placeholder": "e.g. Dallas County, Texas"},
            {"id": "eas_inject_message", "type": "text", "label": "Message",
             "placeholder": "A water main break has closed Main St. Avoid the area."},
            {"id": "eas_inject_instruction", "type": "text", "label": "Instructions (optional)",
             "placeholder": "Use an alternate route and monitor local news."},
            {"id": "eas_inject_sender", "type": "string", "label": "From", "default": "EAS Participant",
             "help_text": "Who issued it, e.g. EAS Participant, County Emergency Management."},
            {"id": "eas_inject_severity", "type": "select", "label": "Severity", "default": "Severe",
             "options": [{"value": "Minor", "label": "Minor"}, {"value": "Moderate", "label": "Moderate"},
                         {"value": "Severe", "label": "Severe"}, {"value": "Extreme", "label": "Extreme"}]},
            {"id": "eas_inject_duration_min", "type": "number", "label": "Valid for (minutes)", "default": 30,
             "min": 1, "max": 360},

            # ── 7 · Relay rules & monitoring ─────────────────────────────
            section(7, "RELAY RULES & MONITORING",
                    "What happens to each alert that comes in, like an ENDEC's filter table. Priority: "
                    "a national alert (EAN) goes out at once, interrupts anything and holds the channels "
                    "until it's over; a nationwide test goes out at once; warnings may cut into a test "
                    "that's still playing; everything else waits its turn."),
            {"id": "eas_rwt_action", "type": "select", "label": "Received weekly tests (RWT)", "default": "relay",
             "options": [{"value": "relay", "label": "Relay — header + EOM on your channels"},
                         {"value": "log",   "label": "Log only (factory Sage setting)"},
                         {"value": "ignore", "label": "Ignore"}]},
            {"id": "eas_rmt_action", "type": "select", "label": "Received monthly tests (RMT)", "default": "hold",
             "help_text": "Stations must relay a received RMT within 60 minutes.",
             "options": [{"value": "hold",  "label": "Relay after a hold (Sage default: 5 minutes)"},
                         {"value": "relay", "label": "Relay immediately"},
                         {"value": "log",   "label": "Log only"},
                         {"value": "ignore", "label": "Ignore"}]},
            {"id": "eas_rmt_hold_min", "type": "number", "label": "RMT hold (minutes)", "default": 5,
             "min": 0, "max": 60},
            {"id": "eas_other_action", "type": "select", "label": "Everything else", "default": "relay",
             "options": [{"value": "relay", "label": "Relay immediately"},
                         {"value": "hold",  "label": "Relay after a hold"},
                         {"value": "log",   "label": "Log only"}]},
            {"id": "eas_other_hold_min", "type": "number", "label": "Hold for everything else (minutes)",
             "default": 10, "min": 0, "max": 120},
            {"id": "eas_filter_rules", "type": "text", "label": "Custom rules (optional)",
             "help_text": "One per line: event codes = relay | hold N | log | ignore [priority 0-63]. "
                          "Example: 'FLS, SPS = log' or 'SVA = hold 10'.",
             "placeholder": "FLS, SPS = log\nSVA, TOA = hold 10"},
            {"id": "eas_relay_own_id", "type": "boolean", "label": "Put my station ID in relayed headers",
             "default": True,
             "help_text": "Like a real relay: the header carries your station ID, but keeps the "
                          "originator's issue time and valid period."},
            note("monitor", "Monitor a channel for EAS tones"),
            {"id": "eas_monitor_channel", "type": "select", "label": "Monitored channel", "default": "none",
             "help_text": "Listens to this channel's audio around the clock (like an ENDEC's LP-1 "
                          "monitoring assignment) and relays what it airs: its header, its recorded "
                          "message, then the EOM. Alerts you already got from NWS/IPAWS are recognised as "
                          "duplicates and not played twice. Uses one stream connection to that channel "
                          "permanently. The channel itself never gets alerts.",
             "options": [{"value": "none", "label": "— off —"}] + channel_picker},

            # ── 8 · Advanced ─────────────────────────────────────────────
            section(8, "ADVANCED", "You normally don't need to change anything below."),
            {"id": "eas_poll_interval", "type": "number", "label": "Check for alerts every (seconds)",
             "default": 60, "min": 15},
            {"id": "eas_alert_gap_secs", "type": "number", "label": "Quiet time between two alerts (seconds)",
             "default": 60, "min": 0, "max": 3600,
             "help_text": "When several alerts are waiting, normal TV plays this long between them. "
                          "With the area set to ALL it is at least 300."},
            {"id": "eas_max_alert_age_min", "type": "number", "label": "Skip alerts older than (minutes)",
             "default": 20, "min": 5, "max": 720,
             "help_text": "Alerts issued longer ago than this are never played, so a backlog can't pile up."},
            {"id": "eas_transcode_mode", "type": "select", "label": "Alert video resolution", "default": "full",
             "options": [{"value": "full", "label": "1080p"},
                         {"value": "720p", "label": "720p — lighter on CPU"}]},
            {"id": "eas_station_id", "type": "string", "label": "Station ID (8 characters)",
             "help_text": "Your 'call sign' in the SAME header and 'Message from …' for tests and custom alerts.",
             "placeholder": "EAA/IPTV"},
            {"id": "eas_fcc2023_wording", "type": "boolean", "label": "Current FCC names", "default": True,
             "help_text": "Uses the 2023 names: 'United States Government', 'National Emergency Message', "
                          "'Nationwide Test of the Emergency Alert System'."},
            {"id": "eas_endec_test_mode", "type": "boolean", "label": "Weekly tests are tones only",
             "default": True, "help_text": "Off: weekly tests also get the attention tone and readout."},
            {"id": "eas_endec_tail_secs", "type": "number", "label": "Screen stays up after a weekly test (seconds)",
             "default": 7.5, "min": 1, "max": 30},
            {"id": "eas_silence_secs", "type": "number", "label": "Silent gap when there's no readout (seconds)",
             "default": 15, "min": 10, "max": 20},
            {"id": "eas_ipaws_feeds", "type": "string", "label": "IPAWS feeds", "default": "eas",
             "help_text": "Comma-separated: eas, wea, public."},
            {"id": "eas_easyplus_font_scale", "type": "number", "label": "Text size", "default": 1.0,
             "min": 0.5, "max": 3, "help_text": "1.0 = same size as the real equipment."},
            {"id": "eas_easyplus_font", "type": "string", "label": "Font override",
             "help_text": "A .ttf/.otf filename in the plugin's fonts/ folder (or a full path). Blank = the style's own font."},
            {"id": "eas_tts_engine", "type": "select", "label": "Speech engine", "default": "auto",
             "options": [{"value": k, "label": l} for k, l in TTS_ENGINES]},
            {"id": "eas_tts_custom_cmd", "type": "string", "label": "Custom speech command",
             "help_text": "Speech engine = Custom. Placeholders: {textfile} {text} {out} (WAV output).",
             "placeholder": "/opt/dectalk/say -fi {textfile} -fo {out}"},
            {"id": "eas_tts_piper_model", "type": "string", "label": "Piper voice model (.onnx)",
             "help_text": "Speech engine = Piper.", "placeholder": "/data/piper/en_US-ryan-medium.onnx"},
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
            "stop_alerts":          self._stop_alerts,
            "redis_diag":           self._redis_diag,
            "reload_poller":        self._reload_poller,
            "restart_dispatcharr":  self._restart_dispatcharr,
        }
        handler = dispatch.get(action)
        if not handler:
            return {"success": False, "message": f"Unknown action: {action}"}
        try:
            if action in _EAS_LOCKED_ACTIONS:
                # Same lock the background sweep/restore tick use, so a button
                # press and the poller can never overwrite each other's
                # mappings.json changes.
                with _EasMutex(wait=20) as held:
                    if not held:
                        return {"success": False, "message":
                                "EmergencyAlertarr is busy switching an alert right now -- "
                                "try again in a few seconds."}
                    return handler(params)
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
                    ". Fill it in under section 2 · YOUR AREA, save, then enable again."}

        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels selected. Choose them in section 1 · CHANNELS, save, then try again."}

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
                        current_profile = _eas_effective_profile(channel)
                        existing["original_profile_id"] = current_profile.id if current_profile else None
                        logger.warning(f"emergencyalertarr: repaired legacy mapping for {channel.name} (ch {cid}) — backfilled missing original_profile_id")
                    # Co-arm EAS alongside the existing ticker without disrupting it
                    _eas_clear(channel.id)
                    existing["eas_armed"] = True
                    mappings[cid] = existing
                    enabled.append(channel.name)
                    continue
                # Override-aware; None = the channel uses the system default
                # profile, which is fine now (the alert profile no longer needs
                # the channel's own FFmpeg parameters). Those channels used to
                # be skipped, so "All Channels" quietly armed only some of them.
                original_profile = _eas_effective_profile(channel)
                if original_profile and original_profile.name.startswith(PROFILE_PREFIX):
                    skipped.append(f"{channel.name} (already has a EmergencyAlertarr profile — disable first)")
                    continue
                _eas_clear(channel.id)
                mappings[cid] = {
                    "original_profile_id": original_profile.id if original_profile else None,
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
            parts.append("These channels keep playing normally and switch to the alert screen only while an alert for your area is playing. Press \"Test EAS Alert\" to try it.")
        if skipped: parts.append("Skipped:\n" + "\n".join(f"  - {s}" for s in skipped))
        if failed:  parts.append("Failed:\n"  + "\n".join(f"  - {f}" for f in failed))
        return {"success": not failed, "message": "\n\n".join(parts) or "Nothing to do."}

    def _disable_eas(self, params):
        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels selected. Choose them in section 1 · CHANNELS, save, then try again."}
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
                if mapping.get("eas_profile_id") or mapping.get("eas_passthrough_id"):
                    _restore_profile(channel, mapping.get("original_profile_id"), slot=mapping.get("eas_slot"))
                    if mapping.get("eas_profile_id"):
                        _delete_cloned_profile(mapping["eas_profile_id"])
                    _eas_delete_passthrough(mapping.get("eas_passthrough_id"))
                    _restart_channel_stream_async(channel, label="EAS")
                _eas_clear(channel.id)
                with _eas_lock:
                    _eas_active.pop(cid, None)
                if is_pure_eas:
                    del mappings[cid]
                else:
                    # Co-armed: remove EAS keys, leave the ticker mapping intact
                    for k in ("eas_armed", "eas_profile_id", "eas_restore_at", "eas_await_stream",
                              "eas_seen_down", "eas_seq_secs", "eas_slot", "eas_hard_deadline",
                              "eas_fired_at", "eas_national", "eas_priority", "eas_gone_since"):
                        mapping.pop(k, None)
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
        """Fire a test EAS alert (RWT/RMT) on EAS-enabled channels, then auto-restore."""
        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels selected. Choose them in section 1 · CHANNELS, save, then try again."}

        settings  = _get_settings()
        test_type = (settings.get("eas_test_type") or "monthly").lower()
        test_area = (settings.get("eas_test_area") or "").strip() or None

        from datetime import datetime, timezone as _tz
        test_alert = _eas_build_test_alert(test_type, datetime.now(_tz.utc), area=test_area)
        fired, notes = _eas_manual_fire(channels, test_alert, settings)

        if fired:
            label = "Required Weekly Test" if test_type == "weekly" else "Required Monthly Test"
            _eas_history_add(label, test_area or "this viewing area", fired, kind="test")

        parts = []
        if fired:
            parts.append(f"EAS test alert fired on: {', '.join(fired)}\nThe overlay will auto-restore once the full tone + readout sequence finishes.")
        parts += notes
        return {"success": bool(fired), "message": "\n\n".join(parts) or "Nothing to do."}


    def _inject_alert(self, params):
        """Fire a user-defined custom alert on the selected EAS channels, using
        the text entered in the Custom Alert Injection settings. Reuses the same
        activation/overlay/TTS machinery as real alerts."""
        channels = self._resolve_channels(params, prefix="eas_")
        if not channels:
            return {"success": False, "message": "No channels selected. Choose them in section 1 · CHANNELS, save, then try again."}

        settings = _get_settings()
        from eaa_templates import apply_template
        from eaa_fips import describe_codes
        tkey = (settings.get("eas_inject_template") or "custom").lower()
        templ = tkey != "custom"
        typed_sender = (settings.get("eas_inject_sender") or "").strip()
        typed_sev = settings.get("eas_inject_severity") or ""
        try:
            typed_min = int(settings.get("eas_inject_duration_min") or 0)
        except Exception:
            typed_min = 0
        # With a template, the fields' factory defaults count as "not typed".
        if templ:
            if typed_sender == "EAS Participant":
                typed_sender = ""
            if typed_sev == "Severe":
                typed_sev = ""
            if typed_min == 30:
                typed_min = 0
        t = apply_template(tkey, settings.get("eas_inject_event") or "",
                           settings.get("eas_inject_message") or "",
                           settings.get("eas_inject_instruction") or "",
                           typed_sender, typed_sev, typed_min or None)
        event = t["event"]
        if templ and (settings.get("eas_inject_event") or "").strip():
            from eaa_templates import CUSTOM_TEMPLATES
            tv = CUSTOM_TEMPLATES.get(tkey) or {}
            if event.lower() != (tv.get("event") or "").lower():
                t["code"] = ""           # typed a different event: code from its name
        if not event:
            return {"success": False, "message": "Pick a template or fill in the Event under section 6 · CUSTOM ALERT (e.g. \"Civil Emergency Message\"), save, then try again."}
        codes = _eas_test_same_codes(settings)
        area    = ((settings.get("eas_inject_area") or "").strip()
                   or (describe_codes(codes) if codes != ["000000"] else "") or "this viewing area")
        message = t["message"]
        instruction = t["instruction"]
        sender  = t["sender"] or "EAS Participant"
        severity = t["severity"] or "Severe"
        try:
            duration_min = max(1, min(360, int(t["minutes"] or 30)))
        except Exception:
            duration_min = 30

        from datetime import datetime, timedelta, timezone as _tz
        now_utc = datetime.now(_tz.utc)
        expires = (now_utc + timedelta(minutes=duration_min)).isoformat()
        desc = f"* WHAT...{event}.  * WHERE...{area}."
        if message:
            desc += f"  * DETAILS...{message}"
        custom_alert = [{
            "event":       event,
            "area":        area,
            "severity":    severity,
            "effective":   now_utc.isoformat(),
            "sent":        now_utc.isoformat(),
            "expires":     expires,
            "headline":    message or f"A {event} has been issued for {area} by {sender}.",
            "description": desc,
            "instruction": instruction,
            "sender":      sender,
            # Template code/originator when one is picked; otherwise mapped
            # from the event name / sender.
            "event_code":  t["code"],
            "originator":  t["org"],
            "same_codes":  [] if t["code"] == "NPT" else codes,
            "station_id":  _eas_station_id_setting(settings),
        }]
        if t["code"] == "NPT":
            custom_alert[0]["same_codes"] = ["000000"]

        fired, notes = _eas_manual_fire(channels, custom_alert[0], settings)
        skipped, failed = notes, []
        if fired:
            _eas_history_add(event, area, fired, kind="manual", severity=severity)
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
        kind_label = {"alert": "ALERT", "test": "TEST", "manual": "MANUAL", "logged": "LOGGED",
                      "unwatched": "NO VIEWERS"}
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

    def _stop_alerts(self, params):
        """Panic button: end every alert that is on screen right now, put every
        channel back on its normal profile, kick any stream still running an
        alert profile, and mark everything currently in the feed as already
        announced (so the queue is empty -- only alerts issued after this point
        will play)."""
        from apps.channels.models import Channel
        rc = _get_redis_client()
        for k in (_EAS_JOIN_KEY, _EAS_AIR_KEY):
            _eas_json_del(rc, k)
        mappings = _get_mappings()
        ended = []
        for cid, m in list(mappings.items()):
            if not isinstance(m, dict):
                continue
            if m.get("eas_profile_id") or m.get("eas_restore_at") or m.get("eas_await_stream"):
                _eas_do_restore(cid, m, mappings, rc)
                ended.append(cid)
        _save_mappings(mappings)

        # Any live session still on an alert profile (ours by name or by id).
        kicked = []
        clone_ids = _eas_clone_ids(rc)
        try:
            from core.models import StreamProfile
            clone_ids |= {str(p.id) for p in StreamProfile.objects.filter(name__startswith=PROFILE_PREFIX)}
        except Exception:
            pass
        for cid in _eas_streaming_ids(rc):
            try:
                ch = Channel.objects.filter(id=int(cid)).first()
            except Exception:
                ch = None
            if not ch:
                continue
            live_pid = _eas_channel_meta_profile(rc, str(ch.uuid))
            if (live_pid and live_pid in clone_ids) or cid in ended:
                if cid not in ended:
                    _restart_channel_stream_async(ch, label="EAS stop")
                kicked.append(ch.name)
        _eas_watchdog_locked(rc)

        # Empty the queue: the next poll re-seeds from the feed, so everything
        # active now counts as announced.
        if rc:
            for key in (_EAS_SEEN_KEY, _EAS_CFG_FP_KEY, _EAS_REDIS_KEY, _EAS_PENDING_KEY,
                        _EAS_NATIONAL_KEY, _EAS_MONITOR_KEY):
                try:
                    rc.delete(key)
                except Exception:
                    pass
        _eas_json_mem.clear()
        _eas_seen_mem["ids"] = {}
        _eas_seen_mem["init"] = False
        _eas_mark_alert_end(rc)
        msg = [f"Stopped alerts on {len(ended)} channel(s)."]
        if kicked:
            msg.append("Restarted streams:\n" + "\n".join(f"  - {n}" for n in kicked))
        msg.append("Alerts already in the feed are now marked as announced -- only newly "
                   "issued alerts will play.")
        return {"success": True, "message": "\n\n".join(msg)}

    def _clean_orphans(self, params):
        from core.models import StreamProfile
        _eas_watchdog_locked(_get_redis_client())   # put channels back before deleting
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
                    if mapping.get("eas_profile_id"):
                        # Mid-alert: put back exactly what the alert replaced.
                        _restore_profile(channel, orig_id, slot=mapping.get("eas_slot"))
                        _restart_channel_stream_async(channel, label="EAS reset")
                        restored += 1
                    elif orig_id:
                        _restore_profile(channel, orig_id)
                        _restart_channel_stream_async(channel, label="EAS reset")
                        restored += 1
            except Exception as e:
                errors.append(f"ch {cid}: {e}")

        # 2. Delete every EmergencyAlertarr-cloned StreamProfile (name-prefixed or tagged).
        deleted = 0
        try:
            for p in StreamProfile.objects.filter(name__startswith=_EAS_PASSTHRU_PREFIX):
                try:
                    p.delete(); deleted += 1
                except Exception:
                    pass
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
                        _EAS_SEEN_KEY, _EAS_OWNER_KEY, _EAS_REDIS_KEY,
                        _EAS_CFG_FP_KEY, _EAS_LAST_END_KEY, _EAS_PENDING_KEY,
                        _EAS_RECENT_KEY, _EAS_NATIONAL_KEY, _EAS_MONITOR_KEY):
                try:
                    rc.delete(key)
                except Exception:
                    pass
        # Reset the in-process seen-set too, so a reset always starts fresh
        # (the next poll re-seeds and invalidates whatever is currently active).
        _eas_seen_mem["ids"] = {}
        _eas_seen_mem["init"] = False

        msg = (f"EAS reset complete.\n"
               f"  - Restored {restored} channel(s) to their original profile\n"
               f"  - Deleted {deleted} EmergencyAlertarr cloned profile(s)\n"
               f"  - Cleared all EAS mappings and Redis state")
        if errors:
            msg += "\n\nSome issues (non-fatal):\n" + "\n".join(f"  - {e}" for e in errors[:10])
        msg += "\n\nYou can now re-arm EAS from a clean slate."
        return {"success": True, "message": msg}

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
        try:
            wav_lines.extend(tts_status(_eas_tts_opts(_get_settings())))
        except Exception as e:
            wav_lines.append(f"TTS status error: {e}")
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
                "Viewer detection, the restore timer and the channel monitor need Redis; "
                "alerts will not switch reliably until it is reachable."
            )}

        lines = [wav_prefix + "Redis: connected\n"]
        try:
            st = _get_settings()
            mon = _eas_json_get(rc, _EAS_MONITOR_STATUS_KEY, None)
            if _eas_chan(st, "eas_monitor_channel"):
                if mon:
                    lines.append(f"Channel monitor: listening to '{mon.get('channel')}' via {mon.get('url')} "
                                 f"since {time.strftime('%H:%M', time.localtime(mon.get('since') or 0))}; "
                                 f"{mon.get('headers', 0)} header(s) decoded"
                                 + (f", last: {mon.get('last')}" if mon.get('last') else ""))
                else:
                    lines.append("Channel monitor: set, but not running yet (or no status in the last 5 min)")
            pend = _eas_json_get(rc, _EAS_PENDING_KEY, {})
            held = [(k, v) for k, v in pend.items() if float(v.get("hold") or 0) > 0]
            if held:
                lines.append("Held alerts (timed relay): " + "; ".join(
                    f"{k[:24]} -> {max(0, (float(v['first']) + float(v['hold']) * 60 - time.time()) / 60):.1f} min left"
                    for k, v in held))
            nat = _eas_json_get(rc, _EAS_NATIONAL_KEY, None)
            if nat:
                lines.append(f"NATIONAL ALERT ACTIVE: {(nat.get('alert') or {}).get('event')}")
            det = _eas_details_channel(_eas_fire_cfg(st))
            if det is not None:
                lines.append(f"Force-tune details channel: {det.name}")
            lines.append("")
        except Exception as e:
            lines.append(f"(status error: {e})\n")

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

        # EAS engine state -- what the sweep actually sees.
        try:
            mappings = _get_mappings()
            streaming = _eas_streaming_ids(rc)
            armed = [c for c, m in mappings.items()
                     if isinstance(m, dict) and (m.get("type") == "eas" or m.get("eas_armed"))]
            lines.append(f"\nEAS engine: {len(armed)} armed channel(s), "
                         f"{len(streaming & set(armed))} with a viewer right now")
            now = time.time()
            for c in armed:
                m = mappings[c]
                st = "idle"
                if m.get("eas_await_stream"):
                    st = "alert switching on (waiting for the viewer to reconnect)"
                elif m.get("eas_restore_at"):
                    st = f"alert on screen, restores in {max(0, float(m['eas_restore_at']) - now):.0f}s"
                elif m.get("eas_profile_id"):
                    st = "ORPHANED alert profile (will be cleaned up next poll)"
                if st != "idle" or c in streaming:
                    lines.append(f"  - [{c}] {m.get('channel_name', '?')}: {st}"
                                 + ("  [viewer]" if c in streaming else ""))
            raw = rc.get(_EAS_REDIS_KEY)
            if raw:
                data = json.loads(raw)
                al = data.get("alerts") if isinstance(data, dict) else data
                ok = data.get("complete", True) if isinstance(data, dict) else True
                lines.append(f"Last poll: {len(al or [])} matching alert(s) for your area"
                             + ("" if ok else " (PARTIAL -- a source failed)"))
                for a in (al or [])[:10]:
                    lines.append(f"  - {a.get('event')} [{a.get('severity')}] {(a.get('area') or '')[:60]}")
            seen = _eas_seen_read(rc)
            lines.append("Announced-alert memory: "
                         + ("not initialised yet (first poll pending)" if seen is None
                            else f"{len(seen)} key(s)"))
            holder = rc.get(_EAS_MUTEX_KEY)
            if holder:
                lines.append(f"Mutex held by: {holder.decode() if isinstance(holder, bytes) else holder}")
        except Exception as e:
            lines.append(f"\nEAS engine status error: {e}")

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
        target_type = params.get(f"{prefix}target_type", "channel")

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
