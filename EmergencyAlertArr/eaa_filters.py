"""ENDEC-style alert filters: what happens to each received alert.

Modelled on the Sage ENDEC 3644 filter table (and the equivalent DASDEC
"forwarding" setup):

  * every filter matches on originator / event / location and has an ACTION
    and a PRIORITY (0-63, higher wins when several match)
  * actions: Automatic relay, Timed relay (hold N minutes, then send),
    Log only, Ignore
  * the factory filters are: EAN/EAT from PEP -> automatic, priority 63;
    RMT -> timed relay (5 min hold), priority 60; RWT -> log only;
    everything else -> timed, priority 40
  * an alert of priority 61 or more cannot be aborted once it is on the air;
    a national alert takes over from anything lower

Differences from the Sage defaults (on purpose, for a TV setup):
  * received Required Weekly Tests are RELAYED by default (header + EOM)
  * ordinary alerts relay immediately instead of waiting 10 minutes
  * warnings may interrupt a test that is still playing

Custom rules (one per line, '#' starts a comment):

    TOR, SVR, FFW = relay            # air immediately
    SVA, TOA      = hold 10          # wait 10 minutes, then air
    FLS, SPS      = log              # history only, never aired
    DMO           = ignore           # drop silently
    CEM           = relay priority 55
"""

import re

__all__ = ["classify", "decide", "parse_rules", "PRIORITY", "can_preempt", "FILTER_ACTIONS"]

PRIORITY = {
    "national": 63,   # EAN / EAT / NIC -- Presidential / national activation
    "eat":      63,   # Emergency Action Termination -- ends a national alert
    "npt":      61,   # National Periodic Test -- sent immediately, not abortable
    "warning":  50,
    "other":    40,
    "rmt":      30,
    "rwt":      20,
    "dmo":      10,
}

FILTER_ACTIONS = ("relay", "hold", "log", "ignore")

_NATIONAL = {"EAN", "NIC"}
_WARNING_EXTRA = {"CAE", "CEM", "EVI", "SPW", "CDW", "LEW", "NUW", "RHW", "HMW", "BLU",
                  "LAE", "TOE", "SMW", "EQW", "VOW", "AVW", "DSW", "FRW"}


def _code(alert):
    return (alert.get("event_code") or "").strip().upper()


def classify(alert):
    """Category used for the default action and priority."""
    ec = _code(alert)
    org = (alert.get("originator") or "").strip().upper()
    ev = (alert.get("event") or "").lower()
    if ec == "EAT":
        return "eat"
    if ec in _NATIONAL or (org == "PEP" and ec not in ("NPT", "RMT", "RWT", "DMO", "EAT")):
        return "national"
    if ec == "NPT":
        return "npt"
    if ec == "RMT" or "monthly test" in ev:
        return "rmt"
    if ec == "RWT" or "weekly test" in ev:
        return "rwt"
    if ec == "DMO" or "practice/demo" in ev:
        return "dmo"
    if ec in _WARNING_EXTRA or (len(ec) == 3 and ec.endswith("W")) or "warning" in ev or "emergency" in ev:
        return "warning"
    return "other"


def parse_rules(text):
    """Parse the custom rule lines -> {CODE: (action, minutes, priority)}."""
    rules = {}
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        left, right = line.split("=", 1)
        codes = [c.strip().upper() for c in re.split(r"[,\s]+", left) if c.strip()]
        words = right.strip().lower().split()
        if not words or words[0] not in FILTER_ACTIONS:
            continue
        action = words[0]
        minutes = None
        prio = None
        rest = words[1:]
        i = 0
        while i < len(rest):
            w = rest[i]
            if w.replace(".", "", 1).isdigit() and minutes is None and action == "hold":
                minutes = float(w)
            elif w == "priority" and i + 1 < len(rest) and rest[i + 1].isdigit():
                prio = max(0, min(63, int(rest[i + 1])))
                i += 1
            i += 1
        for c in codes:
            if len(c) == 3:
                rules[c] = (action, minutes, prio)
    return rules


def decide(alert, settings):
    """{"action", "hold_min", "priority", "category"} for one received alert."""
    cat = classify(alert)
    prio = PRIORITY[cat]

    def _f(key, default, lo, hi):
        try:
            v = settings.get(key)
            return max(lo, min(hi, float(default if v in (None, "") else v)))
        except Exception:
            return default

    if cat in ("national", "npt", "eat"):
        return {"action": "relay", "hold_min": 0.0, "priority": prio, "category": cat}
    if cat == "rmt":
        action = (settings.get("eas_rmt_action") or "hold").lower()
        hold = _f("eas_rmt_hold_min", 5, 0, 60)            # FCC: relay within 60 min
    elif cat == "rwt":
        action = (settings.get("eas_rwt_action") or "relay").lower()
        hold = 0.0
    else:
        action = (settings.get("eas_other_action") or "relay").lower()
        hold = _f("eas_other_hold_min", 10, 0, 120)
    custom = parse_rules(settings.get("eas_filter_rules") or "").get(_code(alert))
    if custom:
        action = custom[0]
        if custom[1] is not None:
            hold = custom[1]
        if custom[2] is not None:
            prio = custom[2]
    if action not in FILTER_ACTIONS:
        action = "relay"
    if action != "hold":
        hold = 0.0
    return {"action": action, "hold_min": float(hold), "priority": int(prio), "category": cat}


def can_preempt(new_priority, running_priority):
    """A national alert (61+) takes over from anything lower; a warning (50+)
    may cut into a test (30 or less). Nothing else interrupts."""
    try:
        n, r = int(new_priority), int(running_priority)
    except Exception:
        return False
    if r >= 61:
        return False if n <= r else n >= 61
    if n >= 61:
        return n > r
    return n >= 50 and r <= 30
