"""
eaa_decoder.py -- SAME (Specific Area Message Encoding) decoder for
EmergencyAlertarr.

This is the "monitored input" side of a real ENDEC: it demodulates the SAME
AFSK header burst off an audio source, parses it, finds the EOM, and can capture
the whole activation (header -> attention -> message -> EOM) to a WAV so the real
received audio can be relayed on your channels.

Start by decoding recorded EAS activation WAVs (or the ones your own generator
produces -- the numbers here match eaa_tones exactly, so it round-trips). A live
web-stream listener can feed the same functions later.

Pure-Python + numpy. No Dispatcharr/Django imports, so it's easy to test on its
own from the command line:

    python3 eaa_decoder.py some_activation.wav
"""

import math
import os
import wave
import time
import threading
import subprocess
import logging

logger = logging.getLogger(__name__)

__all__ = [
    "SAME_SR", "SAME_MARK", "SAME_SPACE", "SAME_BAUD",
    "read_wav_mono", "decode_same_stream", "decode_eom",
    "parse_same_header", "same_to_alert", "analyze_wav", "analyze_samples",
    "capture_activation", "StreamMonitor", "SAME_EVENT_NAMES",
]

# --- SAME AFSK parameters (must match eaa_tones) ---------------------------
SAME_SR    = 48000
SAME_MARK  = 2083.3     # binary 1
SAME_SPACE = 1562.5     # binary 0
SAME_BAUD  = 520.833    # bits/sec

# EOM: preamble + "NNNN"
_EOM_STR = "NNNN"

# Event-code -> human name (common EAS codes; extend as needed).
SAME_EVENT_NAMES = {
    "EAN": "Emergency Action Notification", "EAT": "Emergency Action Termination",
    "NPT": "National Periodic Test", "NIC": "National Information Center",
    "RWT": "Required Weekly Test", "RMT": "Required Monthly Test",
    "DMO": "Practice/Demo Warning", "ADR": "Administrative Message",
    "TOR": "Tornado Warning", "TOA": "Tornado Watch",
    "SVR": "Severe Thunderstorm Warning", "SVA": "Severe Thunderstorm Watch",
    "SVS": "Severe Weather Statement", "FFW": "Flash Flood Warning",
    "FFA": "Flash Flood Watch", "FFS": "Flash Flood Statement",
    "FLW": "Flood Warning", "FLA": "Flood Watch", "FLS": "Flood Statement",
    "SMW": "Special Marine Warning", "SPS": "Special Weather Statement",
    "WSW": "Winter Storm Warning", "WSA": "Winter Storm Watch",
    "BZW": "Blizzard Warning", "HWW": "High Wind Warning", "HWA": "High Wind Watch",
    "HUW": "Hurricane Warning", "HUA": "Hurricane Watch", "TRW": "Tropical Storm Warning",
    "TSW": "Tsunami Warning", "TSA": "Tsunami Watch",
    "CAE": "Child Abduction Emergency", "CDW": "Civil Danger Warning",
    "CEM": "Civil Emergency Message", "EVI": "Evacuation Immediate",
    "FRW": "Fire Warning", "HMW": "Hazardous Materials Warning",
    "LEW": "Law Enforcement Warning", "LAE": "Local Area Emergency",
    "SPW": "Shelter in Place Warning", "TOE": "911 Telephone Outage Emergency",
    "NUW": "Nuclear Power Plant Warning", "RHW": "Radiological Hazard Warning",
    "BLU": "Blue Alert", "AVW": "Avalanche Warning", "AVA": "Avalanche Watch",
    "EQW": "Earthquake Warning", "VOW": "Volcano Warning", "DSW": "Dust Storm Warning",
}


def _np():
    import numpy as np
    return np


# --- WAV I/O ----------------------------------------------------------------
def read_wav_mono(path, target_sr=SAME_SR):
    """Read a WAV file as a mono float32 array at target_sr (linear-resampled)."""
    np = _np()
    with wave.open(path, "rb") as w:
        sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 1:
        a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width: {sw*8} bit")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != target_sr and len(a) > 1:
        idx = np.linspace(0, len(a) - 1, int(round(len(a) * target_sr / sr)))
        a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
        sr = target_sr
    return a, sr


# --- AFSK demodulation ------------------------------------------------------
def _soft_demod(a, sr):
    """Non-coherent AFSK soft decision: per-sample d = |mark| - |space| using a
    one-bit sliding integrate window. Positive d -> mark (1), negative -> space (0)."""
    np = _np()
    spb = sr / SAME_BAUD
    win = max(1, int(round(spb)))
    n = np.arange(len(a))
    mark = a * np.exp(-1j * 2 * np.pi * SAME_MARK * n / sr)
    space = a * np.exp(-1j * 2 * np.pi * SAME_SPACE * n / sr)
    box = np.ones(win)
    mm = np.abs(np.convolve(mark, box, mode="same"))
    ss = np.abs(np.convolve(space, box, mode="same"))
    return (mm - ss), (mm + ss), spb


def _bits_at_phase(d, spb, off, nbits):
    np = _np()
    idx = (off + np.arange(nbits) * spb).astype(int)
    idx = idx[idx < len(d)]
    return (d[idx] > 0).astype(int), idx


def _bits_to_text(bits, start_bit):
    """Read printable SAME chars (bytes, LSB-first) from bits starting at start_bit."""
    out = []
    b = start_bit
    while b + 8 <= len(bits):
        byte = 0
        for i in range(8):
            byte |= int(bits[b + i]) << i
        ch = chr(byte)
        if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-+/ ":
            out.append(ch)
            b += 8
        else:
            break
    return "".join(out)


def _find_marker_bit(bits, marker):
    """Byte-aligned search for `marker` (ASCII) in an LSB-first bit stream.
    Returns the starting bit index of the marker, or -1."""
    target = [((ord(c) >> i) & 1) for c in marker for i in range(8)]
    tl = len(target)
    for b in range(0, len(bits) - tl + 1):
        ok = True
        for j in range(tl):
            if bits[b + j] != target[j]:
                ok = False
                break
        if ok:
            return b
    return -1


def _scan_for(d, spb, marker, want_text=True):
    """Sweep the sampling phase, demodulate the whole stream, and look for the
    byte-aligned marker ('ZCZC' or 'NNNN'). Returns (found_text, bit_index,
    sample_index) for the best phase, or (None, -1, -1)."""
    np = _np()
    nbits = int(len(d) / spb)
    best = (None, -1, -1)
    # sweep phase across one bit period; clean audio locks quickly
    step = max(1, int(spb / 12))
    for off in range(0, int(spb) + 1, step):
        bits, idx = _bits_at_phase(d, spb, off, nbits)
        mb = _find_marker_bit(bits, marker)
        if mb < 0:
            continue
        if not want_text:
            return (marker, mb, int(idx[mb]) if mb < len(idx) else -1)
        text = _bits_to_text(bits, mb)
        if text.startswith(marker) and len(text) > len(best[0] or ""):
            best = (text, mb, int(idx[mb]) if mb < len(idx) else -1)
    return best


def decode_same_stream(samples, sr):
    """Demodulate and return every distinct SAME header string found in the audio
    (headers are sent 3x; identical decodes are collapsed). Each entry:
    {'header': str, 'sample': int}."""
    d, energy, spb = _soft_demod(samples, sr)
    text, mb, si = _scan_for(d, spb, "ZCZC", want_text=True)
    results = []
    if text:
        # Trim trailing junk (noise can add a few chars past the station ID) by
        # matching the canonical SAME header shape.
        import re
        m = re.search(
            r"ZCZC-[A-Z]{3}-[A-Z0-9]{3}(?:-\d{6})+\+\d{4}-\d{7}-[A-Z0-9 /]{1,8}", text)
        hdr = (m.group(0) + "-") if m else text
        results.append({"header": hdr, "sample": si})
    # de-dup identical headers
    seen, uniq = set(), []
    for r in results:
        key = r["header"][:80]
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def decode_eom(samples, sr):
    """Return sample index of the first EOM (preamble + NNNN), or -1."""
    d, energy, spb = _soft_demod(samples, sr)
    _t, _mb, si = _scan_for(d, spb, _EOM_STR, want_text=False)
    return si


# --- Attention tone + message segmentation ---------------------------------
# The two-tone EAS attention signal is 853 Hz + 960 Hz.
_ATTN_A = 853.0
_ATTN_B = 960.0


def _attention_region(a, sr):
    """Find the [start, end] samples of the sustained 853/960 Hz attention tone,
    or None. Detected as the longest contiguous region where BOTH tones dominate
    the signal energy (which voice and the AFSK bursts never do), gated to exclude
    silence (where a tiny/tiny ratio would otherwise spike)."""
    np = _np()
    win = max(1, int(sr * 0.04))
    n = np.arange(len(a))
    box = np.ones(win)
    e_a = np.abs(np.convolve(a * np.exp(-1j * 2 * np.pi * _ATTN_A * n / sr), box, mode="same"))
    e_b = np.abs(np.convolve(a * np.exp(-1j * 2 * np.pi * _ATTN_B * n / sr), box, mode="same"))
    total = np.abs(np.convolve(np.abs(a), box, mode="same")) + 1e-9
    score = np.minimum(e_a, e_b) / total       # high only when both tones present
    energy_gate = total > (total.max() * 0.15)  # ignore silence
    strong = (score > 0.35) & energy_gate
    if not strong.any():
        return None
    # longest contiguous run of "strong" = the attention tone
    sd = strong.astype(int)
    edges = np.diff(np.concatenate(([0], sd, [0])))
    starts = np.where(edges == 1)[0]
    ends = np.where(edges == -1)[0]
    if len(starts) == 0:
        return None
    s, e = max(zip(starts, ends), key=lambda r: r[1] - r[0])
    if (e - s) < int(sr * 1.0):     # must be a sustained (>=1s) tone
        return None
    return int(s), int(e)


def _message_span(a, sr, eom_sample):
    """Samples [start, end] of just the voice/audio message: after the attention
    tone, before the first EOM burst. Returns None if there's no message (e.g. a
    tones-only test)."""
    preamble_samples = int(round((16 * 8) / SAME_BAUD * sr))   # 16-byte 0xAB preamble
    if eom_sample and eom_sample > 0:
        msg_end = max(0, eom_sample - preamble_samples - int(sr * 0.05))
    else:
        msg_end = len(a)
    att = _attention_region(a, sr)
    if att:
        msg_start = min(att[1] + int(sr * 0.10), msg_end)   # just after the attention tone
    else:
        return None    # no attention tone -> treat as no relayable message
    if msg_end - msg_start < int(sr * 0.30):
        return None
    return (int(msg_start), int(msg_end))


# --- Header parsing ---------------------------------------------------------
def parse_same_header(hdr):
    """Parse a SAME header string into fields.

    Format: ZCZC-ORG-EEE-PSSCCC-PSSCCC...+TTTT-JJJHHMM-LLLLLLLL-
    Returns a dict, or None if it doesn't look like SAME."""
    if not hdr or "ZCZC" not in hdr:
        return None
    body = hdr[hdr.index("ZCZC"):]
    # split off the '+TTTT-JJJHHMM-LLLLLLLL' tail at the '+'
    if "+" not in body:
        return None
    head, tail = body.split("+", 1)
    head_parts = head.strip("-").split("-")   # ['ZCZC','ORG','EEE','LOC','LOC',...]
    if len(head_parts) < 4 or head_parts[0] != "ZCZC":
        return None
    org = head_parts[1]
    event = head_parts[2]
    locations = [p for p in head_parts[3:] if p]
    tail_parts = tail.strip("-").split("-")    # ['TTTT','JJJHHMM','LLLLLLLL']
    purge = tail_parts[0] if len(tail_parts) > 0 else ""
    issued = tail_parts[1] if len(tail_parts) > 1 else ""
    station = tail_parts[2] if len(tail_parts) > 2 else ""
    return {
        "raw": body,
        "originator": org,
        "event_code": event,
        "event": SAME_EVENT_NAMES.get(event, event),
        "locations": locations,     # 6-digit PSSCCC codes
        "purge": purge,             # HHMM valid duration
        "issued": issued,           # JJJHHMM (UTC day-of-year + time)
        "station": station,         # 8-char sender ID
    }


def _issue_to_iso(issued, year=None):
    """Convert a JJJHHMM SAME timestamp (UTC day-of-year + HHMM) to an ISO string."""
    from datetime import datetime, timedelta, timezone
    try:
        if len(issued) < 7:
            return ""
        doy = int(issued[0:3]); hh = int(issued[3:5]); mm = int(issued[5:7])
        y = year or datetime.now(timezone.utc).year
        base = datetime(y, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1, hours=hh, minutes=mm)
        return base.isoformat()
    except Exception:
        return ""


def same_to_alert(parsed):
    """Convert a parsed SAME header into the alert dict EmergencyAlertarr's relay
    path expects (same shape as the NWS/IPAWS parsers produce)."""
    if not parsed:
        return None
    from datetime import datetime, timedelta, timezone
    eff = _issue_to_iso(parsed.get("issued"))
    exp = ""
    try:
        if eff and len(parsed.get("purge") or "") == 4:
            ph = int(parsed["purge"][0:2]); pm = int(parsed["purge"][2:4])
            exp = (datetime.fromisoformat(eff) + timedelta(hours=ph, minutes=pm)).isoformat()
    except Exception:
        pass
    org = (parsed.get("originator") or "").upper()
    sev = "Extreme" if parsed.get("event_code") in ("TOR", "FFW", "EAN", "TSW") else "Severe"
    return {
        "id": f"same:{parsed.get('event_code','')}:{parsed.get('issued','')}:{'-'.join(parsed.get('locations',[]))}",
        "event": parsed.get("event") or "Emergency Alert",
        "event_code": parsed.get("event_code", ""),
        "area": "",                       # SAME carries FIPS, not names; resolve elsewhere
        "severity": sev,
        "effective": eff,
        "expires": exp,
        "headline": "",
        "description": "",
        "instruction": "",
        "sender": parsed.get("station") or "EAS",
        "originator": org if org in ("PEP", "CIV", "WXR", "EAS") else "EAS",
        "same_codes": parsed.get("locations", []),
        "source": "same-decode",
    }


# --- Full-activation analysis + capture ------------------------------------
def analyze_samples(samples, sr):
    """Decode an in-memory activation (mono float array): header(s), parsed
    fields, alert dict, EOM position, full activation span, and message span."""
    headers = decode_same_stream(samples, sr)
    eom_sample = decode_eom(samples, sr)
    parsed = parse_same_header(headers[0]["header"]) if headers else None
    alert = same_to_alert(parsed) if parsed else None
    start = headers[0]["sample"] if headers else 0
    start = max(0, start - int(sr * 0.05))
    end = min(len(samples), eom_sample + int(sr * 0.20)) if eom_sample > 0 else len(samples)
    return {
        "sr": sr,
        "duration_s": len(samples) / sr,
        "headers": headers,
        "parsed": parsed,
        "alert": alert,
        "eom_sample": eom_sample,
        "activation_span": (int(start), int(end)),
        "message_span": _message_span(samples, sr, eom_sample),
    }


def analyze_wav(path):
    """Decode a recorded activation WAV end to end (see analyze_samples)."""
    samples, sr = read_wav_mono(path)
    return analyze_samples(samples, sr)


def _write_wav(out_path, seg, sr):
    np = _np()
    pcm = (np.clip(seg, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def capture_activation(in_path, out_path, full=False):
    """Capture the received audio from in_path to out_path as 16-bit mono WAV.

    By default this writes JUST THE MESSAGE -- the voice/audio between the
    attention tone and the EOM -- which is what a real ENDEC relays (it
    regenerates the header/attention/EOM itself and forwards the message). Pass
    full=True to capture the whole activation (header -> EOM) instead.

    Returns the analysis dict (with 'out' added), or None if there's nothing to
    capture (no SAME header, or -- for the default -- no message segment)."""
    info = analyze_wav(in_path)
    if not info["headers"]:
        return None
    samples, sr = read_wav_mono(in_path)
    if full:
        s, e = info["activation_span"]
    else:
        span = info["message_span"]
        if not span:
            return None
        s, e = span
    _write_wav(out_path, samples[s:e], sr)
    info["out"] = out_path
    info["captured"] = "activation" if full else "message"
    return info


# --- Live stream monitor ----------------------------------------------------
class StreamMonitor:
    """Monitors a live audio stream (e.g. a PEP/LP station web stream) for SAME
    activations, exactly like a real ENDEC's monitored input.

    It pulls the stream with ffmpeg, runs the SAME detector over a rolling buffer,
    and when it decodes a header it captures through the EOM, extracts the voice
    message, and calls on_activation(info) -- where info is the analyze_samples()
    dict plus 'message_wav', 'activation_wav', 'msg_secs', and 'header'.

    The detect/capture core is driven through feed(), which is public so you can
    replay a recorded WAV through it to test without a live stream.
    """

    STATE_SCAN = "scan"
    STATE_CAPTURE = "capture"

    def __init__(self, url, on_activation, sr=SAME_SR, out_dir="/tmp",
                 scan_window_s=15.0, max_activation_s=180.0, dedup_s=300.0,
                 logger=None):
        self.url = url
        self.on_activation = on_activation
        self.sr = int(sr)
        self.out_dir = out_dir
        self.scan_window = int(scan_window_s * sr)
        self.max_activation = int(max_activation_s * sr)
        self.dedup_s = dedup_s
        self.log = logger or logging.getLogger(__name__)
        self._state = self.STATE_SCAN
        self._buf = None
        self._recent = {}          # header -> last-fired monotonic time (dedup)
        self._proc = None
        self._thread = None
        self._stop = threading.Event()

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="eaa-stream-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        p = self._proc
        if p:
            try:
                p.terminate()
            except Exception:
                pass

    def _run(self):
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", self.url,
               "-f", "s16le", "-ar", str(self.sr), "-ac", "1", "-"]
        chunk = self.sr * 2   # ~1s of s16le mono
        np = _np()
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except Exception as e:
                self.log.error(f"[stream-monitor] cannot start ffmpeg: {e}")
                return
            self.log.info(f"[stream-monitor] listening to {self.url}")
            got_bytes = 0
            while not self._stop.is_set():
                data = self._proc.stdout.read(chunk)
                if not data:
                    break
                got_bytes += len(data)
                pcm = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
                try:
                    self.feed(pcm)
                except Exception as e:
                    self.log.error(f"[stream-monitor] processing error: {e}", exc_info=True)
            try:
                self._proc.terminate()
            except Exception:
                pass
            if self._stop.is_set():
                break
            if got_bytes < self.sr:   # < ~0.5s of audio before it ended -> bad URL
                self.log.error(
                    "[stream-monitor] no audio from the URL. It must be a DIRECT audio stream "
                    "(Icecast/HLS/mp3 that ffmpeg can open), not a web player page. "
                    "Reconnecting in 15s.")
                self._stop.wait(15)
            else:
                self.log.warning("[stream-monitor] stream ended/dropped; reconnecting in 5s")
                self._stop.wait(5)

    # -- detect/capture core (testable) --------------------------------------
    def feed(self, pcm):
        """Append PCM samples (mono float in [-1,1]) and advance the state
        machine. Public so a recorded WAV can be replayed through it for testing."""
        np = _np()
        self._buf = pcm.copy() if self._buf is None or len(self._buf) == 0 else np.concatenate([self._buf, pcm])

        if self._state == self.STATE_SCAN:
            if len(self._buf) > self.scan_window:
                self._buf = self._buf[-self.scan_window:]
            heads = decode_same_stream(self._buf, self.sr)
            if heads:
                hs = max(0, heads[0]["sample"] - int(self.sr * 1.5))
                self._buf = self._buf[hs:]
                self._state = self.STATE_CAPTURE
                self.log.info("[stream-monitor] SAME header detected — capturing activation")

        elif self._state == self.STATE_CAPTURE:
            eom = decode_eom(self._buf, self.sr)
            done = (eom > 0 and (len(self._buf) - eom) > int(self.sr * 0.6))
            if len(self._buf) > self.max_activation:
                done = True
                self.log.warning("[stream-monitor] activation capture timed out")
            if done:
                try:
                    self._finalize(self._buf)
                except Exception as e:
                    self.log.error(f"[stream-monitor] finalize error: {e}", exc_info=True)
                self._state = self.STATE_SCAN
                self._buf = self._buf[-self.scan_window:]

    def _finalize(self, buf):
        info = analyze_samples(buf, self.sr)
        if not info["headers"]:
            self.log.info("[stream-monitor] capture ended with no decodable header — ignoring")
            return
        header = info["headers"][0]["header"]
        now = time.monotonic()
        # de-dup: same header re-heard within dedup window (headers are sent 3x,
        # and stations often repeat) -> ignore.
        last = self._recent.get(header)
        if last is not None and (now - last) < self.dedup_s:
            self.log.info("[stream-monitor] duplicate activation ignored (recently relayed)")
            return
        self._recent[header] = now
        self._recent = {h: t for h, t in self._recent.items() if now - t < self.dedup_s}

        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.makedirs(self.out_dir, exist_ok=True)
        act_path = os.path.join(self.out_dir, f"activation-{stamp}.wav")
        msg_path = os.path.join(self.out_dir, f"message-{stamp}.wav")
        a0, a1 = info["activation_span"]
        _write_wav(act_path, buf[a0:a1], self.sr)
        msg_secs = 0.0
        if info["message_span"]:
            m0, m1 = info["message_span"]
            _write_wav(msg_path, buf[m0:m1], self.sr)
            msg_secs = (m1 - m0) / self.sr
        else:
            msg_path = None
        info.update({"header": header, "activation_wav": act_path,
                     "message_wav": msg_path, "msg_secs": msg_secs})
        p = info["parsed"] or {}
        self.log.info(f"[stream-monitor] decoded {p.get('event_code','?')} "
                      f"{p.get('event','')} from {p.get('originator','?')} "
                      f"({', '.join(p.get('locations', []))}) — relaying")
        try:
            self.on_activation(info)
        except Exception as e:
            self.log.error(f"[stream-monitor] on_activation callback error: {e}", exc_info=True)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) < 2:
        print("usage: python3 eaa_decoder.py <activation.wav> [captured_out.wav]")
        raise SystemExit(1)
    info = analyze_wav(sys.argv[1])
    print(f"duration: {info['duration_s']:.1f}s @ {info['sr']} Hz")
    if info["headers"]:
        print("SAME header:", info["headers"][0]["header"])
        p = info["parsed"]
        if p:
            print(f"  originator: {p['originator']}   event: {p['event']} ({p['event_code']})")
            print(f"  locations:  {', '.join(p['locations'])}")
            print(f"  purge/valid: {p['purge']}   issued: {p['issued']}   station: {p['station']}")
    else:
        print("no SAME header decoded")
    print("EOM at sample:", info["eom_sample"], f"({info['eom_sample']/info['sr']:.1f}s)" if info["eom_sample"] > 0 else "")
    ms = info.get("message_span")
    if ms:
        print(f"message (voice) span: {ms[0]/info['sr']:.1f}s -> {ms[1]/info['sr']:.1f}s ({(ms[1]-ms[0])/info['sr']:.1f}s)")
    else:
        print("message span: none (tones-only / no attention tone)")
    if len(sys.argv) >= 3 and info["headers"]:
        out = capture_activation(sys.argv[1], sys.argv[2])   # message only by default
        if out:
            print(f"captured {out['captured']} -> {sys.argv[2]}")
        else:
            print("nothing to capture (no message segment; try full capture in code)")
