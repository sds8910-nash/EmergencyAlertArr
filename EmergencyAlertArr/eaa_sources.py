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
from eaa_tones import *

__all__ = ['NWS_ALERTS_URL', 'NWS_UA', '_IPAWS_EAS_BASE', '_IPAWS_FEED_PATHS', '_NATIONAL_EVENT_CODES', '_alert_content_id', '_cap_local', '_cap_named_value', '_cap_same_codes', '_cap_text', '_eas_only_filter', '_fetch_all_alerts', '_fetch_ipaws_alerts', '_fetch_nws_alerts', '_is_national_alert', '_merge_dedupe_alerts', '_norm_ts', '_parse_cap_alert', '_same_codes_match', '_summarize_alerts']

NWS_ALERTS_URL  = "https://api.weather.gov/alerts/active"

NWS_UA          = "EmergencyAlertarr/0.4 (https://github.com/sds8910-nash/EmergencyAlertArr)"

def _fetch_nws_alerts(zones, severity_threshold="Moderate"):
    # Special sentinel: "ALL" means nationwide -- query every active US alert
    # with no zone filter at all, instead of requiring a list of county codes.
    nationwide = len(zones) == 1 and zones[0].strip().upper() == "ALL"
    if nationwide:
        url = NWS_ALERTS_URL
    else:
        zone_str = ",".join(z.upper() for z in zones if z.strip())
        if not zone_str:
            return []
        url = f"{NWS_ALERTS_URL}?zone={zone_str}"
    req = urllib.request.Request(url, headers={
        "User-Agent": NWS_UA,
        "Accept": "application/geo+json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    min_sev = _EAS_SEV.get(severity_threshold, 2)
    alerts = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        if props.get("status") != "Actual":
            continue
        if props.get("urgency") in ("Past", "Unknown"):
            continue
        if _EAS_SEV.get(props.get("severity", "Unknown"), 0) < min_sev:
            continue
        # SAME event code (e.g. "TOR") and affected county SAME/FIPS codes -- used
        # to build a realistic SAME header when dynamic tone generation is on.
        ev_code = ""
        try:
            same_ev = (props.get("eventCode") or {}).get("SAME") or []
            if same_ev:
                ev_code = str(same_ev[0])
        except Exception:
            pass
        same_codes = []
        try:
            same_codes = list((props.get("geocode") or {}).get("SAME") or [])
        except Exception:
            pass
        # BLOCKCHANNEL lists the dissemination channels an alert is BLOCKED from.
        # "EAS" here means the NWS did not send this product to EAS (e.g. heat
        # advisories, air-quality alerts) -- used by the EAS-only filter.
        block_channels = []
        try:
            block_channels = [str(x).upper() for x in
                              ((props.get("parameters") or {}).get("BLOCKCHANNEL") or [])]
        except Exception:
            pass
        alerts.append({
            "id":          props.get("id", ""),
            "event":       props.get("event", "Weather Alert"),
            "area":        props.get("areaDesc", ""),
            "severity":    props.get("severity", "Unknown"),
            "effective":   props.get("effective") or props.get("onset") or "",
            "expires":     props.get("expires", ""),
            "headline":    props.get("headline", ""),
            "description": props.get("description", ""),
            "instruction": props.get("instruction", ""),
            # Everything from this feed IS the National Weather Service, so show
            # that consistently rather than the per-office senderName (which can
            # read like "NWS Tulsa OK"). Tests build their own sender elsewhere.
            "sender":      "National Weather Service",
            "event_code":  ev_code,
            "same_codes":  same_codes,
            "blockchannel": block_channels,
            "originator":  "WXR",   # NWS originator code
        })
    return alerts

_IPAWS_EAS_BASE = "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest"

_IPAWS_FEED_PATHS = {          # friendly name -> endpoint path segment
    "eas":    "eas",           # EAS CAP feed (what ENDEC/decoder devices poll)
    "wea":    "PublicWEA",     # public Wireless Emergency Alerts
    "public": "public",        # general public feed
}

def _cap_local(tag):
    """Local element name with any XML namespace stripped."""
    return tag.rsplit("}", 1)[-1]

def _cap_text(alert_el, name):
    """First non-empty text of a descendant element with the given local name."""
    for e in alert_el.iter():
        if _cap_local(e.tag) == name and (e.text or "").strip():
            return e.text.strip()
    return ""

def _cap_named_value(alert_el, container, want_name):
    """For CAP <container><valueName>X</valueName><value>Y</value></container>
    pairs (geocode / eventCode / parameter), return the first Y whose X matches
    want_name, or '' ."""
    for c in alert_el.iter():
        if _cap_local(c.tag) != container:
            continue
        vn = val = ""
        for child in c:
            ln = _cap_local(child.tag)
            if ln == "valueName":
                vn = (child.text or "").strip()
            elif ln == "value":
                val = (child.text or "").strip()
        if vn.upper() == want_name.upper() and val:
            return val
    return ""

def _cap_same_codes(alert_el):
    """All SAME/FIPS geocodes on the alert (6-digit PSSCCC)."""
    codes = []
    for gc in alert_el.iter():
        if _cap_local(gc.tag) != "geocode":
            continue
        vn = val = ""
        for child in gc:
            ln = _cap_local(child.tag)
            if ln == "valueName":
                vn = (child.text or "").strip()
            elif ln == "value":
                val = (child.text or "").strip()
        if vn.upper() in ("SAME", "FIPS6") and val and val not in codes:
            codes.append(val)
    return codes

def _same_codes_match(codes, filt):
    """True if any of the alert's SAME codes falls within the user's filter.
    A filter entry ending in 000 (e.g. 040000) is a whole-state wildcard that
    matches every county in that state (SS = digits 2-3 of the 6-digit code)."""
    fset = {c for c in filt if c}
    states = {c for c in fset if len(c) == 6 and c[3:] == "000"}
    for raw in codes:
        c = "".join(ch for ch in str(raw) if ch.isdigit()).zfill(6)[:6]
        if c in fset:
            return True
        for st in states:
            if c[1:3] == st[1:3]:
                return True
    return False

_NATIONAL_EVENT_CODES = {"EAN", "EAT", "NPT", "NIC"}

def _is_national_alert(a):
    """True for national-scope alerts (Emergency Action Notification, National
    Periodic Test, Primary Entry Point activations, or anything carrying the
    entire-US SAME code 000000). These always pass the county filter and the
    severity floor, even when specific FIPS codes are set."""
    ec = (a.get("event_code") or "").strip().upper()
    if ec in _NATIONAL_EVENT_CODES:
        return True
    if (a.get("originator") or "").strip().upper() == "PEP":
        return True
    for c in (a.get("same_codes") or []):
        if "".join(ch for ch in str(c) if ch.isdigit()).zfill(6)[:6] == "000000":
            return True
    return False

def _parse_cap_alert(el):
    """Map one CAP <alert> element to EmergencyAlertarr's alert dict shape."""
    ident = _cap_text(el, "identifier")
    if not ident:
        return None
    sender_name = _cap_text(el, "senderName") or _cap_text(el, "sender") or "Emergency Alert System"
    severity = _cap_text(el, "severity") or "Unknown"
    # Prefer the SAME originator the feed states explicitly in the EAS-ORG
    # parameter (PEP/CIV/WXR/EAS). Only fall back to inferring it from the
    # sender name when EAS-ORG is missing. Originator drives formatting: WXR
    # keeps the WHAT/WHERE/WHEN layout; CIV/PEP/EAS render as plain prose.
    eas_org = (_cap_named_value(el, "parameter", "EAS-ORG") or "").strip().upper()
    _sn = sender_name.lower()
    if eas_org in ("PEP", "CIV", "WXR", "EAS"):
        originator = eas_org
        if eas_org == "WXR":
            sender_name = "National Weather Service"
    elif any(k in _sn for k in ("nws", "national weather", "weather service", "noaa")):
        originator = "WXR"
        sender_name = "National Weather Service"
    elif "primary entry" in _sn or "pep" in _sn.replace("/", " ").split():
        originator = "PEP"   # Primary Entry Point -- national activation
    else:
        originator = "CIV"
    # Tidy the display sender: IPAWS COG names look like
    # "201057,Public - Chelan County, WA,CHELAN COUNTY" -- strip the leading COG
    # id and "Public - " prefix for a cleaner "Message from ..." line.
    if originator != "WXR":
        _disp = re.sub(r"^\s*\d+\s*,\s*", "", sender_name)          # drop leading COG id
        _m = re.search(r"Public\s*-\s*(.+)", _disp)
        if _m:
            _disp = _m.group(1)
        _disp = ", ".join([p.strip() for p in _disp.split(",")[:2] if p.strip()])
        if _disp:
            sender_name = _disp
    return {
        "id":          "ipaws:" + ident,
        "event":       _cap_text(el, "event") or "Emergency Alert",
        "area":        _cap_text(el, "areaDesc"),
        "severity":    severity if severity in _EAS_SEV else "Unknown",
        "effective":   _cap_text(el, "effective") or _cap_text(el, "onset") or _cap_text(el, "sent"),
        "expires":     _cap_text(el, "expires"),
        "headline":    _cap_text(el, "headline"),
        "description": _cap_text(el, "description"),
        "instruction": _cap_text(el, "instruction"),
        "sender":      sender_name,
        "event_code":  _cap_named_value(el, "eventCode", "SAME"),
        "same_codes":  _cap_same_codes(el),
        "originator":  originator,
        "_status":     _cap_text(el, "status") or "Actual",
        "_msgtype":    _cap_text(el, "msgType") or "Alert",
    }

def _fetch_ipaws_alerts(feeds, same_filter, severity_threshold="Moderate"):
    """Poll the IPAWS-OPEN public feeds and return alert dicts. `same_filter` is
    the list of 6-digit SAME/FIPS codes to keep; pass ["ALL"] to take every alert
    nationwide (testing). National alerts (EAN/NPT/000000) always pass, and in
    ALL mode nothing is filtered by area or severity."""
    import xml.etree.ElementTree as ET
    from datetime import datetime, timezone, timedelta
    if not same_filter:
        return []   # nationwide with no filter would be a firehose -- caller warns
    allow_all = any((c or "").strip().upper() == "ALL" for c in same_filter)
    min_sev = _EAS_SEV.get(severity_threshold, 2)
    # Feed retains alerts ~30 min; polling "recent since now-30m" each sweep gets
    # the full current active set. The seen-set queue dedups across polls.
    since = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, seen = [], set()
    for feed in feeds:
        path = _IPAWS_FEED_PATHS.get((feed or "").strip().lower())
        if not path:
            continue
        url = f"{_IPAWS_EAS_BASE}/{path}/recent/{since}"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": NWS_UA, "Accept": "application/xml",
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            root = ET.fromstring(raw)
        except Exception as e:
            logger.warning(f"[EmergencyAlertarr] EAS: IPAWS '{feed}' fetch/parse failed: {e}")
            continue
        for el in (e for e in root.iter() if _cap_local(e.tag) == "alert"):
            a = _parse_cap_alert(el)
            if not a:
                continue
            if a["_status"] != "Actual":                     # skip Test/Exercise/System
                continue
            if a["_msgtype"] not in ("Alert", "Update"):      # skip Cancel/Ack/Error
                continue
            national = _is_national_alert(a)
            # National alerts (EAN/NPT/000000) bypass the severity floor and the
            # county filter; ALL mode bypasses both for everything.
            if not (allow_all or national) and _EAS_SEV.get(a["severity"], 0) < min_sev:
                continue
            if not (allow_all or national) and not _same_codes_match(a["same_codes"], same_filter):
                continue
            if a["id"] in seen:
                continue
            seen.add(a["id"])
            a.pop("_status", None)
            a.pop("_msgtype", None)
            out.append(a)
    return out

def _norm_ts(s):
    """Canonicalize an ISO timestamp to UTC for comparison; best-effort."""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return (s or "").strip()

def _alert_content_id(a):
    """Stable ID derived from an alert's CONTENT (event + expiry + affected
    county SAME codes), so the same alert coming from two sources (e.g. NWS and
    its IPAWS relay) collapses to one entry. Independent of which source's
    identifier it carried, so it also survives one source dropping out."""
    import hashlib
    event = re.sub(r"\s+", " ", (a.get("event") or "").strip().lower())
    exp = _norm_ts(a.get("expires"))
    codes = ",".join(sorted(
        "".join(ch for ch in str(c) if ch.isdigit())
        for c in (a.get("same_codes") or []) if str(c).strip()
    ))
    key = f"{event}|{exp}|{codes}"
    return "alert:" + hashlib.md5(key.encode("utf-8")).hexdigest()[:16]

def _merge_dedupe_alerts(alerts):
    """Collapse duplicate alerts (same content) to a single entry and give each
    a stable content ID. First occurrence wins -- callers add NWS before IPAWS,
    so the richer NWS copy (with WHAT/WHERE/WHEN text) is the one kept."""
    out, seen = [], set()
    for a in alerts or []:
        cid = _alert_content_id(a)
        if cid in seen:
            continue
        seen.add(cid)
        a = dict(a)
        a["id"] = cid
        out.append(a)
    return out

def _summarize_alerts(alerts, limit=8):
    """Short human-readable summary of an alert list for the logs / diagnostics."""
    alerts = alerts or []
    if not alerts:
        return "none active"
    parts = []
    for a in alerts[:limit]:
        area = (a.get("area") or "")
        area = (area[:34] + "…") if len(area) > 34 else area
        parts.append(f'{a.get("event","?")} [{a.get("severity","?")}]' + (f" — {area}" if area else ""))
    extra = f" (+{len(alerts) - limit} more)" if len(alerts) > limit else ""
    return f"{len(alerts)}: " + " | ".join(parts) + extra


def _eas_only_filter(alerts, settings):
    """'Behave like a real ENDEC' filtering for NWS products:
      - eas_only_real: drop alerts BLOCKED from EAS (BLOCKCHANNEL contains EAS)
        or lacking a real SAME event code (non-EAS products like heat/air-quality
        carry event_code "NWS" or none).
      - eas_event_block: drop alerts whose SAME event code is in this list.
      - eas_event_allow: if set, keep ONLY alerts whose SAME event code is listed.
    National alerts (EAN/NPT/PEP/000000) always pass. Returns (kept, dropped_desc)."""
    val = settings.get("eas_only_real")
    only_real = (val is True) or (str(val).strip().lower() in ("1", "true", "yes", "on"))
    allow = {c.strip().upper() for c in (settings.get("eas_event_allow") or "").replace(" ", "").split(",") if c.strip()}
    block = {c.strip().upper() for c in (settings.get("eas_event_block") or "").replace(" ", "").split(",") if c.strip()}
    if not (only_real or allow or block):
        return alerts, []
    kept, dropped = [], []
    for a in alerts:
        if _is_national_alert(a):
            kept.append(a)
            continue
        ec = (a.get("event_code") or "").strip().upper()
        reason = None
        if only_real:
            if "EAS" in (a.get("blockchannel") or []):
                reason = "blocked from EAS"
            elif ec in ("", "NWS"):
                reason = "no real SAME event code"
        if reason is None and block and ec in block:
            reason = f"event {ec} blocked"
        if reason is None and allow and ec not in allow:
            reason = f"event {ec} not in allow list"
        if reason:
            dropped.append(f'{a.get("event")} ({reason})')
        else:
            kept.append(a)
    return kept, dropped


def _fetch_all_alerts(settings, zones, severity_threshold="Moderate"):
    """Fetch the active alert set from whichever source(s) are enabled
    (NWS api.weather.gov and/or IPAWS-OPEN), merged into one list. Each source
    is isolated so a failure in one never blocks the other. The same alert
    arriving from both sources is de-duplicated to a single entry. Every poll is
    logged per source so you can confirm polling is actually happening."""
    source = (settings.get("eas_source") or "nws").lower()
    nws_alerts, ipaws_alerts = [], []
    if source in ("nws", "both"):
        if zones:
            try:
                nws_alerts = _fetch_nws_alerts(zones, severity_threshold) or []
            except Exception as e:
                logger.warning(f"[EmergencyAlertarr] EAS: NWS fetch failed: {e}")
            nws_alerts, _dropped = _eas_only_filter(nws_alerts, settings)
            if _dropped:
                logger.info(f"[EmergencyAlertarr] NWS EAS-only filter dropped {len(_dropped)}: "
                            + "; ".join(_dropped[:8]) + (" …" if len(_dropped) > 8 else ""))
            logger.info(f"[EmergencyAlertarr] NWS poll (zones={','.join(zones)}): {_summarize_alerts(nws_alerts)}")
        else:
            logger.info("[EmergencyAlertarr] NWS poll skipped: no zone/county codes set")
    if source in ("ipaws", "both"):
        feeds = [f.strip() for f in (settings.get("eas_ipaws_feeds") or "eas").split(",") if f.strip()]
        raw_codes = (settings.get("eas_ipaws_same_codes") or "").strip()
        if raw_codes.upper() == "ALL":
            same_filter = ["ALL"]   # nationwide -- take everything (testing)
        else:
            same_filter = [c.strip() for c in raw_codes.replace(" ", "").split(",") if c.strip()]
        if not same_filter:
            logger.info(
                "[EmergencyAlertarr] IPAWS poll skipped: no county SAME codes set "
                "(set 6-digit codes, or ALL for nationwide)"
            )
        else:
            try:
                ipaws_alerts = _fetch_ipaws_alerts(feeds, same_filter, severity_threshold) or []
            except Exception as e:
                logger.warning(f"[EmergencyAlertarr] EAS: IPAWS fetch failed: {e}")
            _codes = "ALL" if same_filter == ["ALL"] else ",".join(same_filter)
            logger.info(f"[EmergencyAlertarr] IPAWS poll (codes={_codes}, feeds={','.join(feeds)}): {_summarize_alerts(ipaws_alerts)}")
    return _merge_dedupe_alerts(nws_alerts + ipaws_alerts)
