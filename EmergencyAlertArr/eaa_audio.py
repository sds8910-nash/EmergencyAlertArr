"""Offline rendering of the alert's audio track.

The whole EAS audio sequence (header -> attention -> message -> EOM, or
header -> EOM for a weekly test) is rendered to ONE WAV before the alert goes
on air. That lets the plugin do things a live filter graph can't do well:

  * Loudness. Program audio on US cable/TV is mixed to -24 LKFS (ATSC A/85,
    the CALM Act). The spoken message is levelled to that target and the tones
    to a fixed amount above it, so an alert sits where the channel sits
    instead of jumping out at full scale (or vanishing on a loud channel).
    The tones keep their exact relative levels (AFSK vs. two-tone), because
    one gain is applied to all of them.
  * Encoder output quirks. Real ENDEC outputs aren't pristine: some feed the
    EAS audio to one side only, some carry a little mains hum, and the audio
    switch clicks and dips as it flips from program to EAS.

Everything here is best-effort: if FFmpeg fails the caller falls back to the
old live filter graph.
"""

import logging
import os
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

__all__ = ["render_alert_audio", "measure_lufs", "AUDIO_CHANNEL_MODES", "LOUDNESS_TARGETS"]

AUDIO_CHANNEL_MODES = [
    ("both",  "Both channels (mono on left and right)"),
    ("left",  "Left channel only (right silent)"),
    ("right", "Right channel only (left silent)"),
]

LOUDNESS_TARGETS = [
    ("-24", "-24 LKFS — US cable/TV (CALM Act)"),
    ("-23", "-23 LUFS — EBU R128"),
    ("-16", "-16 LUFS — louder, streaming level"),
    ("off", "Off — as generated (tones at full level)"),
]

_SR = 48000


def _ff():
    return shutil.which("ffmpeg") or "ffmpeg"


def measure_lufs(path, chain=None, timeout=60):
    """Integrated loudness (LUFS) of a file, optionally after `chain`.
    Returns (lufs, true_peak_dbfs) or (None, None)."""
    af = (chain + "," if chain else "") + "ebur128=peak=true:framelog=quiet"
    try:
        r = subprocess.run([_ff(), "-hide_banner", "-nostats", "-i", path, "-af", af, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=timeout)
        txt = r.stderr or ""
        m = re.findall(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", txt)
        p = re.findall(r"Peak:\s+(-?\d+(?:\.\d+)?|-inf)\s+dBFS", txt)
        lufs = float(m[-1]) if m else None
        peak = None
        if p and p[-1] != "-inf":
            peak = float(p[-1])
        if lufs is not None and lufs <= -69.0:
            lufs = None
        return lufs, peak
    except Exception as e:
        logger.debug(f"[EmergencyAlertarr] loudness measure failed for {path}: {e}")
        return None, None


def _dur(path):
    try:
        import wave
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        pass
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=20)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def render_alert_audio(out_path, parts, chain=None, loudness="-24", tone_offset_lu=8.0,
                       channels="both", hum=None, switch_click=False, max_voice_secs=120.0,
                       boost_db=0.0):
    """Render the alert audio to `out_path` (48 kHz stereo 16-bit WAV).

    parts: ordered list of dicts:
        {"kind": "tone",    "path": wav}       header / attention / EOM
        {"kind": "voice",   "path": wav}       readout or the alert's own audio
        {"kind": "silence", "secs": float}
    chain:      the ENDEC's output path, applied to tone parts only
    loudness:   "-24" / "-23" / "-16" (LUFS target for the voice) or "off"
    tone_offset_lu: tones sit this many LU above the voice target
    channels:   "both" | "left" | "right"
    hum:        None | 50 | 60  (mains hum frequency, very low level)
    switch_click: a relay click + short level dip when the audio switches over
    max_voice_secs: ENDECs store/relay about two minutes of message audio

    Returns {"path", "duration", "audio_end"} or None on failure."""
    try:
        target = None if str(loudness).lower() in ("off", "none", "") else float(loudness)
    except Exception:
        target = -24.0
    tone_gain = voice_gain = 0.0
    if target is not None:
        ref = next((p for p in parts if p.get("kind") == "tone" and p.get("path")), None)
        if ref:
            l, _pk = measure_lufs(ref["path"], ("aresample=48000," + chain) if chain else None)
            if l is not None:
                tone_gain = (target + float(tone_offset_lu)) - l
        voices = [p for p in parts if p.get("kind") == "voice" and p.get("path")]
        if voices:
            l, pk = measure_lufs(voices[0]["path"])
            if l is not None:
                voice_gain = target - l
                if pk is not None:
                    voice_gain = min(voice_gain, -1.0 - pk + 6.0)   # limiter handles the rest
    # boost_db: a box that plays the alert hotter than the channel (SARA).
    tone_gain = max(-40.0, min(12.0, tone_gain + float(boost_db or 0.0)))
    voice_gain = max(-30.0, min(24.0, voice_gain + float(boost_db or 0.0)))

    args = [_ff(), "-hide_banner", "-v", "error", "-y"]
    labels = []
    fc = []
    idx = 0
    fmt = f"aresample={_SR},aformat=sample_fmts=fltp:channel_layouts=mono"
    for n, p in enumerate(parts):
        kind = p.get("kind")
        if kind == "silence" or not p.get("path"):
            secs = max(0.0, float(p.get("secs") or 0.0))
            if secs <= 0:
                continue
            fc.append(f"anullsrc=channel_layout=mono:sample_rate={_SR},atrim=duration={secs:.4f},{fmt}[p{n}]")
        else:
            args += ["-i", p["path"]]
            if kind == "tone":
                ch = (f"aresample={_SR},{chain}," if chain else "")
                fc.append(f"[{idx}:a]{ch}{fmt},volume={tone_gain:.2f}dB[p{n}]")
            else:
                fc.append(f"[{idx}:a]{fmt},atrim=duration={max_voice_secs:.1f},"
                          f"volume={voice_gain:.2f}dB[p{n}]")
            idx += 1
        labels.append(f"[p{n}]")
    if not labels:
        return None
    fc.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1,alimiter=limit=0.89:level=false[seq]")
    last = "[seq]"
    if hum in (50, 60, "50", "60"):
        hz = int(hum)
        fc.append(f"aevalsrc='0.0021*sin(2*PI*{hz}*t)+0.0009*sin(2*PI*{hz * 3}*t)"
                  f"+0.0004*sin(2*PI*{hz * 5}*t)':s={_SR}:c=mono[hum]")
        fc.append(f"{last}[hum]amix=inputs=2:duration=first:normalize=0[sh]")
        last = "[sh]"
    if switch_click:
        # Relay click and a short dip as the audio switch flips over.
        fc.append(f"aevalsrc='0.22*exp(-t*350)*sin(2*PI*1700*t)':s={_SR}:c=mono:d=0.03,"
                  f"apad=pad_dur=0.01[clk]")
        fc.append(f"{last}afade=t=in:st=0.02:d=0.10[fd]")
        fc.append("[clk][fd]concat=n=2:v=0:a=1[sc]")
        last = "[sc]"
    pan = {"left": "pan=stereo|c0=c0|c1=0*c0", "right": "pan=stereo|c0=0*c0|c1=c0"}.get(
        (channels or "both").lower(), "pan=stereo|c0=c0|c1=c0")
    fc.append(f"{last}{pan}[out]")
    args += ["-filter_complex", ";".join(fc), "-map", "[out]", "-ar", str(_SR), "-ac", "2",
             "-c:a", "pcm_s16le", out_path]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) < 1000:
            logger.warning(f"[EmergencyAlertarr] EAS: audio render failed: {(r.stderr or '')[-300:]}")
            return None
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] EAS: audio render failed: {e}")
        return None
    total = _dur(out_path)
    # Where the audible sequence ends (before any trailing silence part).
    tail = 0.0
    for p in reversed(parts):
        if p.get("kind") == "silence":
            tail += float(p.get("secs") or 0.0)
        else:
            break
    logger.info(
        f"[EmergencyAlertarr] EAS: alert audio rendered ({total:.1f}s, loudness="
        f"{'off' if target is None else f'{target:.0f} LUFS'}, tones {tone_gain:+.1f} dB, "
        f"voice {voice_gain:+.1f} dB{f' (boost {boost_db:+.0f})' if boost_db else ''}, channels={channels}, hum={hum or 'off'}, click={bool(switch_click)})"
    )
    return {"path": out_path, "duration": total, "audio_end": max(0.0, total - tail)}
