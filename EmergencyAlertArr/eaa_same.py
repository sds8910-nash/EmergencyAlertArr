"""SAME (Specific Area Message Encoding) decoding.

Turns audio into EAS messages the way an ENDEC's decoder does:

  * AFSK demodulation: mark 2083 1/3 Hz, space 1562.5 Hz, 520 5/6 bit/s, LSB
    first; a burst is found by its energy at those two tones and decoded by
    correlating each bit period against both (all bit phases are tried)
  * a header is only accepted when two of its three bursts match exactly
    (47 CFR 11.33(a)(10)); with three noisy copies, a per-character majority
    vote is used as a last resort
  * the audio after the header is recorded as the message, the attention
    signal (853 + 960 Hz, or NWR's 1050 Hz) is trimmed off the front, and the
    recording stops at the EOM (NNNN) -- or after two minutes without one
    (the decoder reset, 47 CFR 11.33(a)(9))

Used by the channel monitor (a Dispatcharr channel whose audio is listened to
for EAS tones, like an ENDEC's LP-1 / LP-2 monitoring assignments).
"""

import math
import os
import re
import time
import wave

__all__ = ["SameDecoder", "parse_header", "header_to_alert", "decode_bursts_from_samples"]

MARK = 2083.0 + 1.0 / 3.0
SPACE = 1562.5
BAUD = 520.0 + 5.0 / 6.0

_HDR_RE = re.compile(r"^ZCZC-([A-Z]{3})-([A-Z]{3})-((?:\d{6}-?){1,31})\+(\d{4})-(\d{7})-([^-]{1,8})-?$")

_ORG_NAMES = {
    "WXR": "National Weather Service", "CIV": "Civil Authorities",
    "EAS": "EAS Participant", "PEP": "Primary Entry Point System", "EAN": "Emergency Action Notification Network",
}


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------

def parse_header(text):
    """'ZCZC-WXR-TOR-040143+0030-2781900-KXXX/NWS-' -> dict or None."""
    t = (text or "").strip()
    if not t.endswith("-"):
        t += "-"
    m = _HDR_RE.match(t)
    if not m:
        return None
    org, eee, locs, ttt, jjj, sid = m.groups()
    codes = [c for c in locs.strip("-").split("-") if c]
    return {"org": org, "event": eee, "codes": codes, "purge": ttt, "issued": jjj,
            "station": sid, "raw": t}


def _issue_datetime(jjjhhmm, received_ts):
    """JJJHHMM (UTC day-of-year) -> aware datetime, choosing the year that puts
    it closest to when it was received."""
    from datetime import datetime, timedelta, timezone
    jjj, hh, mm = int(jjjhhmm[:3]), int(jjjhhmm[3:5]), int(jjjhhmm[5:7])
    ref = datetime.fromtimestamp(received_ts, timezone.utc)
    best = None
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            dt = datetime(y, 1, 1, hh, mm, tzinfo=timezone.utc) + timedelta(days=jjj - 1)
        except Exception:
            continue
        if best is None or abs((dt - ref).total_seconds()) < abs((best - ref).total_seconds()):
            best = dt
    return best or ref


def header_to_alert(text, received_ts=None, event_names=None, describe=None, audio_file=None,
                    channel_name=""):
    """Build the plugin's alert dict from a decoded SAME header."""
    from datetime import timedelta
    h = parse_header(text)
    if not h:
        return None
    received_ts = received_ts or time.time()
    issued = _issue_datetime(h["issued"], received_ts)
    dur = timedelta(hours=int(h["purge"][:2]), minutes=int(h["purge"][2:]))
    expires = issued + dur
    ec = h["event"]
    name = (event_names or {}).get(ec) or ec
    if ec in ("EAN", "EAT", "NIC"):
        sev = "Extreme"
    elif ec in ("RMT", "RWT", "DMO", "NPT", "ADR"):
        sev = "Minor"
    elif ec.endswith("W") or ec in ("CEM", "CAE", "EVI", "LAE", "TOE", "BLU"):
        sev = "Severe"
    else:
        sev = "Moderate"
    area = (describe(h["codes"]) if describe else "") or ", ".join(h["codes"])
    import hashlib
    ident = "mon:" + hashlib.md5(h["raw"].encode("ascii", "replace")).hexdigest()[:16]
    a = {
        "id": ident, "src_ids": [ident], "refs": [],
        "event": name, "event_code": ec, "originator": h["org"],
        "sender": _ORG_NAMES.get(h["org"], h["org"]),
        "area": area, "same_codes": list(h["codes"]),
        "severity": sev,
        "sent": issued.isoformat(), "effective": issued.isoformat(), "expires": expires.isoformat(),
        "headline": f"{name} for {area}", "description": "", "instruction": "",
        "station_id": h["station"].strip(),
        "same_jjj": h["issued"], "same_purge": "+" + h["purge"],
        "source": "monitor", "monitor_channel": channel_name,
        "same_header": h["raw"], "received": received_ts,
    }
    if audio_file:
        a["audio_file"] = audio_file
    return a


# ---------------------------------------------------------------------------
# Demodulation
# ---------------------------------------------------------------------------

def _np():
    import numpy as np
    return np


def _correlators(x, sr):
    """Per-sample mark/space energies over a one-bit window, and total power."""
    np = _np()
    n = np.arange(len(x))
    L = max(4, int(round(sr / BAUD)))
    k = np.ones(L) / L

    def band(f):
        ph = 2 * np.pi * f * n / sr
        i = np.convolve(x * np.cos(ph), k, mode="same")
        q = np.convolve(x * np.sin(ph), k, mode="same")
        return i * i + q * q

    em, es = band(MARK), band(SPACE)
    pw = np.convolve(x * x, k, mode="same")
    return em, es, pw


def _bytes_from_bits(bits):
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        v = 0
        for b in range(8):
            v |= (bits[i + b] & 1) << b
        out.append(v)
    return bytes(out)


def _decode_burst(x, sr):
    """Best SAME message text in one burst of samples, or ''."""
    np = _np()
    em, es, _pw = _correlators(x, sr)
    d = em - es
    spb = sr / BAUD
    best = ""
    nbits = int(len(x) / spb) - 1
    if nbits < 8 * 20:
        return ""
    for off in np.arange(0.0, spb, max(1.0, spb / 12.0)):
        centers = (off + spb * 0.5 + np.arange(nbits) * spb).astype(int)
        centers = centers[centers < len(d)]
        bits = (d[centers] > 0).astype(np.uint8).tolist()
        for st in range(8):
            raw = _bytes_from_bits(bits[st:])
            idx = raw.find(b"\xab\xab\xab\xab")
            if idx < 0:
                continue
            j = idx
            while j < len(raw) and raw[j] == 0xAB:
                j += 1
            msg = bytearray()
            for c in raw[j:]:
                if 32 <= c < 127:
                    msg.append(c)
                else:
                    break
            txt = msg.decode("ascii")
            if txt.startswith("NNNN"):
                return "NNNN"
            if txt.startswith("ZCZC") and len(txt) > len(best):
                # cut at the end of the station field (header ends with '-')
                m = re.match(r"^(ZCZC-[A-Z]{3}-[A-Z]{3}-(?:\d{6}-?){1,31}\+\d{4}-\d{7}-[^-]{1,8}-)", txt)
                best = m.group(1) if m else txt
        if best and parse_header(best):
            return best
    return best


def decode_bursts_from_samples(x, sr):
    """Offline helper: every SAME burst in an array of samples -> [(t, text)]."""
    np = _np()
    em, es, pw = _correlators(x, sr)
    ratio = (em + es) / (pw + 1e-12)
    on = (ratio > 0.25) & (pw > 1e-6)
    out = []
    i, n = 0, len(x)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        off_run = 0
        while j < n and off_run < int(0.05 * sr):
            off_run = 0 if on[j] else off_run + 1
            j += 1
        if j - i > int(0.25 * sr):
            txt = _decode_burst(x[max(0, i - int(0.01 * sr)):j], sr)
            if txt:
                out.append((i / sr, txt))
        i = j
    return out


def _vote(texts):
    """Two-of-three exact match; else a per-character majority of three."""
    from collections import Counter
    c = Counter(t for t in texts if t)
    if not c:
        return None
    txt, n = c.most_common(1)[0]
    if n >= 2:
        return txt
    if len(texts) >= 3:
        L = max(len(t) for t in texts)
        chars = []
        for i in range(L):
            col = Counter(t[i] for t in texts if i < len(t))
            ch, k = col.most_common(1)[0]
            if k < 2:
                return None
            chars.append(ch)
        v = "".join(chars)
        return v if parse_header(v) else None
    return None


class SameDecoder:
    """Streaming decoder. Feed mono float samples with feed(); completed
    messages come back as dicts:
        {"type": "header", "text": ..., "time": ts}                    (2-of-3 valid)
        {"type": "message", "text": header, "audio": wav_path|None,
         "time": ts, "eom": bool}                                       (after EOM / timeout)
    """

    def __init__(self, sr=22050, workdir="/tmp", max_message_secs=120.0, reset_secs=150.0):
        self.sr = sr
        self.workdir = workdir
        self.max_msg = max_message_secs
        self.reset_secs = reset_secs
        self.buf = []
        self.buf_len = 0
        self.t0 = time.time()
        self.samples_seen = 0
        self.hdr_bursts = []          # (t, text)
        self.eom_bursts = []
        self.active = None            # current message being recorded
        self.tail = None              # leftover samples between chunks
        self.burst = None             # samples of an open burst

    # -- helpers --------------------------------------------------------
    def _now(self):
        return self.t0 + self.samples_seen / float(self.sr)

    def _finish_message(self, eom, eom_time=None):
        np = _np()
        m = self.active
        self.active = None
        if not m:
            return None
        audio = None
        try:
            rec = np.concatenate(m["rec"]) if m["rec"] else np.zeros(0)
            if eom and eom_time is not None:
                # stop just before the first NNNN burst
                rec = rec[:max(0, int((eom_time - m["rec_t0"] - 0.25) * self.sr))]
            rec = self._trim_attention(rec)
            rec = rec[:int(self.max_msg * self.sr)]
            if len(rec) > self.sr * 1.0 and float(np.sqrt(np.mean(rec * rec))) > 0.003:
                os.makedirs(self.workdir, exist_ok=True)
                audio = os.path.join(self.workdir, f"monitor_{int(m['time'])}.wav")
                pcm = (np.clip(rec, -1, 1) * 32767).astype("<i2").tobytes()
                with wave.open(audio, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(self.sr)
                    w.writeframes(pcm)
        except Exception:
            audio = None
        return {"type": "message", "text": m["header"], "audio": audio, "time": m["time"], "eom": eom}

    def _trim_attention(self, rec):
        """Drop the attention signal (853+960 Hz or 1050 Hz) and the silence
        around it from the front of the recording."""
        np = _np()
        sr = self.sr
        win = int(0.05 * sr)
        if len(rec) < win * 4:
            return rec
        n = np.arange(win)
        cut = 0
        seen_tone = False
        for start in range(0, min(len(rec) - win, int(30 * sr)), win):
            seg = rec[start:start + win]
            pw = float(np.mean(seg * seg)) + 1e-12
            tone = 0.0
            for f in (853.0, 960.0, 1050.0):
                ph = 2 * np.pi * f * n / sr
                i = float(np.mean(seg * np.cos(ph)))
                q = float(np.mean(seg * np.sin(ph)))
                tone += 4 * (i * i + q * q) / 2
            if tone / pw > 0.5 and pw > 1e-6:
                seen_tone = True
                cut = start + win
            elif seen_tone:
                break
            elif pw < 1e-6:
                cut = start + win          # leading silence
            elif start > 3 * sr:
                break                      # no attention tone: keep as is
        rec = rec[cut:]
        # strip leading near-silence after the tone (relative to the message level)
        if len(rec) > win * 4:
            body = float(np.percentile(np.abs(rec[:int(20 * sr)]), 95)) + 1e-9
            lead = 0
            for start in range(0, min(len(rec) - win, int(5 * sr)), win):
                seg = rec[start:start + win]
                if float(np.max(np.abs(seg))) > 0.15 * body:
                    break
                lead = start + win
            rec = rec[max(0, lead - int(0.1 * sr)):]
            # and trailing near-silence (the gap before the EOM)
            end = len(rec)
            while end > win and float(np.max(np.abs(rec[end - win:end]))) < 0.15 * body:
                end -= win
            rec = rec[:min(len(rec), end + int(0.2 * sr))]
        return rec

    # -- main entry -----------------------------------------------------
    def feed(self, samples):
        """samples: numpy float array (mono, -1..1). Returns a list of events."""
        np = _np()
        events = []
        if self.tail is not None and len(self.tail):
            x = np.concatenate([self.tail, samples])
            base = self.samples_seen - len(self.tail)
        else:
            x = samples
            base = self.samples_seen
        self.samples_seen += len(samples)
        if self.active is not None and self.active["len"] < int((self.max_msg + 35) * self.sr):
            self.active["rec"].append(samples.astype(np.float32))
            self.active["len"] += len(samples)
        em, es, pw = _correlators(x, self.sr)
        ratio = (em + es) / (pw + 1e-12)
        on = (ratio > 0.25) & (pw > 1e-6)
        # bursts entirely inside x; keep an unfinished one for the next call
        i, n = 0, len(x)
        keep_from = max(0, n - int(0.06 * self.sr))
        while i < n:
            if not on[i]:
                i += 1
                continue
            j = i
            off_run = 0
            while j < n and off_run < int(0.05 * self.sr):
                off_run = 0 if on[j] else off_run + 1
                j += 1
            if j >= n:                  # burst still running at the chunk end
                keep_from = min(keep_from, max(0, i - int(0.02 * self.sr)))
                break
            if j - i > int(0.25 * self.sr):
                txt = _decode_burst(x[max(0, i - int(0.01 * self.sr)):j], self.sr)
                t = self.t0 + (base + j) / float(self.sr)      # burst END time
                if txt == "NNNN":
                    self.eom_bursts.append(self.t0 + (base + i) / float(self.sr))   # start
                elif txt.startswith("ZCZC"):
                    self.hdr_bursts.append((t, txt))
            i = j
        if keep_from < n - int(5 * self.sr):
            keep_from = n - int(5 * self.sr)       # never hold more than 5 s
        self.tail = x[keep_from:].copy()

        now = self._now()
        # header complete: 3 bursts, or 2+ and 3 s since the last one
        if self.hdr_bursts:
            last = self.hdr_bursts[-1][0]
            if len(self.hdr_bursts) >= 3 or now - last > 2.5:
                texts = [t for _, t in self.hdr_bursts]
                v = _vote(texts) if len(texts) >= 2 else None
                self.hdr_bursts = []
                if v and parse_header(v):
                    if self.active is not None:
                        ev = self._finish_message(eom=False)
                        if ev:
                            events.append(ev)
                    events.append({"type": "header", "text": v, "time": last})
                    self.active = {"header": v, "time": last, "rec": [], "len": 0,
                                   "rec_t0": self._now()}
        # EOM: any one valid NNNN ends the message
        if self.eom_bursts:
            t_eom = self.eom_bursts[0]
            self.eom_bursts = []
            if self.active is not None:
                ev = self._finish_message(eom=True, eom_time=t_eom)
                if ev:
                    events.append(ev)
        # decoder reset: no EOM within reset_secs (EAN never times out)
        if self.active is not None:
            ean = "-EAN-" in self.active["header"]
            if not ean and now - self.active["time"] > self.reset_secs:
                ev = self._finish_message(eom=False)
                if ev:
                    events.append(ev)
            elif self.active and self.active["len"] > int(self.max_msg * self.sr * 1.5) and not ean:
                pass   # finish_message caps the length; keep recording until EOM/reset
        return events
