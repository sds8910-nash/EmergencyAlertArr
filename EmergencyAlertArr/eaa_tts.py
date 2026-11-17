"""Text-to-speech for EAS readouts.

Real EAS readout voices (research notes in the project docs):
  * DASDEC / One-Net (most US cable headends): IPAWS/CAP alerts are read by
    Festival's "Kal" diphone voice (festvox kal) unless the alert carries its
    own audio; optional Cepstral William / David / Allison.
  * NOAA Weather Radio: DECtalk "Paul" (CRS, 1997), Speechify "Craig" /
    "Donna" / "Tom" (VIP, 2002-2003), NeoSpeech "Paul" (BMH, 2016-today).
    The ENDEC relays that audio off a narrowband FM receiver.

This plugin bundles a static build of CMU Flite (BSD-style licence, see
bin/FLITE-COPYING), which contains the very same CMU "kal" diphone voice that
Festival's festvox-kal uses -- the classic DASDEC IPAWS voice -- plus the
8 kHz kal, RMS (male) and SLT (female) voices. No install needed.

Other engines are used when available/configured: espeak-ng, Piper, or any
custom command (for example a self-installed DECtalk "say" for Perfect Paul).
"""

import os
import platform
import shlex
import shutil
import subprocess
import tempfile

from eaa_common import _PLUGIN_DIR, RUNTIME_DIR, _ensure_dirs  # noqa: F401
import logging

logger = logging.getLogger(__name__)

__all__ = ["TTS_ENGINES", "TTS_VOICES", "TTS_FX", "tts_synthesize", "tts_status", "tts_available",
           "tts_cache_key"]

TTS_ENGINES = [
    ("auto",    "Auto — bundled Flite (Kal), else espeak-ng"),
    ("flite",   "Flite (bundled) — CMU voices incl. Kal, the DASDEC/Festival voice"),
    ("espeak",  "espeak-ng (if installed in the container)"),
    ("piper",   "Piper neural TTS (needs the piper binary + a .onnx voice)"),
    ("custom",  "Custom command (e.g. a self-installed DECtalk for 'Perfect Paul')"),
]
TTS_VOICES = [
    ("kal16", "Kal 16 kHz — DASDEC / One-Net default IPAWS voice (Festival 'Kal')"),
    ("kal",   "Kal 8 kHz — same voice, telephone-band (older ENDEC sound)"),
    ("rms",   "RMS — deeper male voice"),
    ("slt",   "SLT — female voice"),
]
TTS_FX = [
    ("endec", "ENDEC / cable — clean broadcast audio, levelled (DASDEC-style)"),
    ("nwr",   "NOAA Weather Radio — narrowband FM receiver sound"),
    ("nwr_hiss", "NOAA Weather Radio + receiver hiss"),
    ("none",  "None — raw engine output"),
]

# ffmpeg -af chains per sound preset (output: 48 kHz mono s16)
_FX_CHAINS = {
    "endec": ("highpass=f=90,lowpass=f=7500,"
              "acompressor=threshold=-22dB:ratio=3:attack=5:release=150:makeup=2,"
              "alimiter=limit=0.89"),
    "nwr": ("highpass=f=300,lowpass=f=3100,"
            "acompressor=threshold=-26dB:ratio=6:attack=3:release=90:makeup=4,"
            "alimiter=limit=0.85"),
}

_flite_path_cache = {"path": None, "checked": False}

def _bundled_flite():
    """Path to the bundled static flite for this CPU (made executable)."""
    if _flite_path_cache["checked"]:
        return _flite_path_cache["path"]
    _flite_path_cache["checked"] = True
    arch = platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(arch, arch)
    src = os.path.join(_PLUGIN_DIR, "bin", f"flite-{arch}")
    if not os.path.isfile(src):
        sys_flite = shutil.which("flite")
        _flite_path_cache["path"] = sys_flite
        if not sys_flite:
            logger.warning(f"[EmergencyAlertarr] TTS: no bundled flite for CPU '{arch}' and none installed")
        return sys_flite
    path = src
    if not os.access(path, os.X_OK):
        # Plugin zips are extracted without the execute bit.
        try:
            os.chmod(path, 0o755)
        except Exception:
            pass
    if not os.access(path, os.X_OK):
        try:
            _ensure_dirs()
            dst = os.path.join(RUNTIME_DIR, f"flite-{arch}")
            if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copyfile(src, dst)
            os.chmod(dst, 0o755)
            path = dst
        except Exception as e:
            logger.warning(f"[EmergencyAlertarr] TTS: cannot make flite executable: {e}")
            path = None
    _flite_path_cache["path"] = path
    return path

def _espeak():
    return shutil.which("espeak-ng") or shutil.which("espeak")

def tts_available(opts=None):
    engine = (opts or {}).get("engine", "auto")
    if engine == "custom":
        return bool((opts or {}).get("custom_cmd"))
    if engine == "piper":
        return bool(shutil.which("piper") or (opts or {}).get("piper_bin"))
    if engine == "espeak":
        return bool(_espeak())
    return bool(_bundled_flite() or _espeak())

def tts_cache_key(opts):
    o = opts or {}
    return "|".join(str(o.get(k, "")) for k in
                    ("engine", "voice", "fx", "speed", "custom_cmd", "piper_model"))

def _run(cmd, timeout=60, stdin_text=None):
    r = subprocess.run(cmd, input=(stdin_text.encode("utf-8") if stdin_text is not None else None),
                       capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode(errors="replace")[-300:])

def _synth_raw(text, raw_path, opts):
    """Run the selected engine into raw_path (any WAV). Returns engine name."""
    engine = (opts.get("engine") or "auto").lower()
    speed = max(0.6, min(1.6, float(opts.get("speed") or 1.0)))
    voice = opts.get("voice") or "kal16"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(text)
        txt_path = tf.name
    try:
        if engine == "custom":
            tmpl = opts.get("custom_cmd") or ""
            parts = [p.replace("{textfile}", txt_path).replace("{out}", raw_path).replace("{text}", text)
                     for p in shlex.split(tmpl)]
            _run(parts, timeout=120)
            return "custom"
        if engine == "piper":
            exe = opts.get("piper_bin") or shutil.which("piper")
            model = opts.get("piper_model") or ""
            if not exe or not model:
                raise RuntimeError("piper binary or voice model not set")
            _run([exe, "--model", model, "--output_file", raw_path,
                  "--length_scale", f"{1.0 / speed:.3f}"], timeout=120, stdin_text=text)
            return "piper"
        flite = _bundled_flite() if engine in ("auto", "flite") else None
        if flite:
            _run([flite, "-voice", voice, "--setf", f"duration_stretch={1.0 / speed:.3f}",
                  "-f", txt_path, "-o", raw_path], timeout=120)
            return f"flite:{voice}"
        esp = _espeak() if engine in ("auto", "espeak") else None
        if esp:
            _run([esp, "-v", "en-us", "-s", str(int(165 * speed)), "-w", raw_path, "-f", txt_path],
                 timeout=60)
            return "espeak-ng"
        raise RuntimeError(f"TTS engine '{engine}' is not available")
    finally:
        try:
            os.remove(txt_path)
        except Exception:
            pass

def tts_synthesize(text, out_path, opts=None):
    """Synthesize `text` to out_path (48 kHz mono WAV) with the configured
    engine/voice, then apply the sound preset. Returns True on success."""
    opts = dict(opts or {})
    if not text or not text.strip():
        return False
    raw = out_path + ".raw.wav"
    try:
        engine = _synth_raw(text, raw, opts)
        if not os.path.isfile(raw) or os.path.getsize(raw) < 200:
            raise RuntimeError("engine produced no audio")
        fx = (opts.get("fx") or "endec").lower()
        chain = None
        extra_in = []
        if fx in _FX_CHAINS:
            chain = _FX_CHAINS[fx]
        elif fx == "nwr_hiss":
            chain = None
        ff = shutil.which("ffmpeg") or "ffmpeg"
        if fx == "nwr_hiss":
            cmd = [ff, "-v", "error", "-y", "-i", raw,
                   "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.012:sample_rate=48000",
                   "-filter_complex",
                   f"[0:a]aresample=48000,{_FX_CHAINS['nwr']}[v];"
                   "[1:a]highpass=f=300,lowpass=f=3100[n];"
                   "[v][n]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.9",
                   "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", out_path]
        else:
            af = "aresample=48000" + (("," + chain) if chain else "")
            cmd = [ff, "-v", "error", "-y", "-i", raw] + extra_in + \
                  ["-af", af, "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", out_path]
        try:
            _run(cmd, timeout=60)
        except Exception as e:
            logger.warning(f"[EmergencyAlertarr] TTS: sound preset '{fx}' failed ({e}); using raw audio")
            shutil.copyfile(raw, out_path)
        ok = os.path.isfile(out_path) and os.path.getsize(out_path) > 200
        if ok:
            logger.info(f"[EmergencyAlertarr] TTS: readout synthesized ({engine}, fx={fx}, {len(text)} chars)")
        return ok
    except Exception as e:
        logger.warning(f"[EmergencyAlertarr] TTS: synthesis failed: {e}")
        return False
    finally:
        try:
            os.remove(raw)
        except Exception:
            pass

def tts_status(opts=None):
    """Human-readable engine status for Redis Diagnostics."""
    lines = []
    fl = _bundled_flite()
    lines.append(f"TTS: bundled Flite: {fl or 'NOT available for this CPU'}")
    lines.append(f"TTS: espeak-ng: {_espeak() or 'not installed'}")
    lines.append(f"TTS: piper: {shutil.which('piper') or 'not installed'}")
    o = opts or {}
    lines.append(f"TTS: selected engine={o.get('engine', 'auto')} voice={o.get('voice', 'kal16')} "
                 f"sound={o.get('fx', 'endec')} speed={o.get('speed', 1.0)}")
    return lines
