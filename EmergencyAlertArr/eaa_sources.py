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

__all__ = ['NWS_ALERTS_URL', 'NWS_UA', '_IPAWS_EAS_BASE', '_IPAWS_FEED_PATHS', '_NATIONAL_EVENT_CODES', '_alert_content_id', '_cap_local', '_cap_named_value', '_cap_same_codes', '_cap_text', '_fetch_all_alerts', '_fetch_ipaws_alerts', '_fetch_nws_alerts', '_is_national_alert', '_merge_dedupe_alerts', '_norm_ts', '_parse_cap_alert', '_same_codes_match']

NWS_ALERTS_URL  = "https://api.weather.gov/alerts/active"

NWS_UA          = "EmergencyAlertarr/0.2"

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
    # Origin: only weather-service alerts get WXR (which keeps the WHAT/WHERE/WHEN
    # formatting); civil/AMBER/law-enforcement alerts get CIV so they render as
    # plain prose. Don't use _same_org_for here -- its WXR default would wrongly
    # treat every non-matching civil sender as NWS.
    _sn = sender_name.lower()
    if any(k in _sn for k in ("nws", "national weather", "weather service", "noaa")):
        originator = "WXR"
        sender_name = "National Weather Service"
    elif "primary entry" in _sn or "pep" in _sn.replace("/", " ").split():
        originator = "PEP"   # Primary Entry Point -- national activation
    else:
        originator = "CIV"
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

def _fetch_all_alerts(settings, zones, severity_threshold="Moderate"):
    """Fetch the active alert set from whichever source(s) are enabled
    (NWS api.weather.gov and/or IPAWS-OPEN), merged into one list. Each source
    is isolated so a failure in one never blocks the other. The same alert
    arriving from both sources is de-duplicated to a single entry."""
    source = (settings.get("eas_source") or "nws").lower()
    alerts = []
    if source in ("nws", "both") and zones:
        try:
            alerts.extend(_fetch_nws_alerts(zones, severity_threshold) or [])
        except Exception as e:
            logger.warning(f"[EmergencyAlertarr] EAS: NWS fetch failed: {e}")
    if source in ("ipaws", "both"):
        feeds = [f.strip() for f in (settings.get("eas_ipaws_feeds") or "eas").split(",") if f.strip()]
        raw_codes = (settings.get("eas_ipaws_same_codes") or "").strip()
        if raw_codes.upper() == "ALL":
            same_filter = ["ALL"]   # nationwide -- take everything (testing)
        else:
            same_filter = [c.strip() for c in raw_codes.replace(" ", "").split(",") if c.strip()]
        if not same_filter:
            logger.warning(
                "[EmergencyAlertarr] EAS: IPAWS is enabled but no county SAME codes are set "
                "(EAS IPAWS County Codes) -- set 6-digit codes, or ALL for nationwide. "
                "Skipping IPAWS."
            )
        else:
            try:
                alerts.extend(_fetch_ipaws_alerts(feeds, same_filter, severity_threshold) or [])
            except Exception as e:
                logger.warning(f"[EmergencyAlertarr] EAS: IPAWS fetch failed: {e}")
    return _merge_dedupe_alerts(alerts)
