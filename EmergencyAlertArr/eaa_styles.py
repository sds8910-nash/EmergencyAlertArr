"""EAS screen styles for EmergencyAlertarr.

Every style recreates the on-screen output of a real piece of EAS equipment
(or a common cable/set-top presentation of it). Layouts are designed on the
480-line grid those devices actually drew on (640x480 for 4:3 screens,
854x480 for 16:9 ones) and scaled to the output resolution, so proportions
match captures of the real thing.

Sources the layouts/wording are based on (see the project changelog):
  * DASDEC / DASDEC-II / DASDEC-III built-in video "details pages"
    (Digital Alert Systems; Luxi Mono Bold, purple-ish blue background with a
    thin red outline, heading line, centred lines, "1/3" or "Page 1 of 3"
    counter, optional English + Spanish translation).
  * Trilithic EASyPLUS / EASyCAST / EASyIPTV built-in CG (STV5730A OSD chip:
    28-column x 11-row character grid; header, scroll, originator, "Issued a",
    event; black / grey / grey-with-black-text; top / middle / bottom scroll;
    crawl speed 1-10).
  * Trilithic EASyCAP (blue, Bitstream Vera Sans Bold, centred paragraph,
    "- 1 of 2 -" counter).
  * Gorman-Redlich EAS-1CG ("*** EMERGENCY DETAILS ***", all-caps
    translation, "Page 1 of 2").
  * VDS 840 / MHz Sub-Alert mode 1 (background colour by alert class: green
    tests/statements/advisories, yellow watches, red warnings; crawl at the
    bottom).
  * Set-top "EMERGENCY ALERT" screen with a red ticker, and a plain ticker
    crawled over the live program (DASDEC/Chyron-style CG crawl at the top).
"""

import os
import re
import textwrap
import unicodedata
from datetime import datetime, timedelta, timezone

from eaa_common import *          # noqa: F401,F403  (RUNTIME_DIR, FONTS_DIR, _atomic_write, ...)
from eaa_common import _atomic_write, _font_for, _resolve_font

__all__ = [
    "STYLE_CATALOG", "STYLE_KEYS", "SAME_EVENT_NAMES", "style_label", "style_is_live",
    "build_style_overlay", "eas_spoken_text", "eas_event_name", "eas_event_code",
    "eas_originator_code", "eas_station_id", "eas_test_alert",
]

# ---------------------------------------------------------------------------
# Reference tables
# ---------------------------------------------------------------------------

# 47 CFR 11.31(e) event codes (+ NWS-only SQW). Names as the FCC table gives
# them; the 2022 FCC Report & Order (FCC 22-75) renamed EAN and NPT -- those
# new names are applied when the "2023 FCC wording" option is on.
SAME_EVENT_NAMES = {
    "EAN": "Emergency Action Notification", "NIC": "National Information Center",
    "NPT": "National Periodic Test", "RMT": "Required Monthly Test",
    "RWT": "Required Weekly Test", "ADR": "Administrative Message",
    "AVW": "Avalanche Warning", "AVA": "Avalanche Watch", "BZW": "Blizzard Warning",
    "BLU": "Blue Alert", "CAE": "Child Abduction Emergency", "CDW": "Civil Danger Warning",
    "CEM": "Civil Emergency Message", "CFW": "Coastal Flood Warning",
    "CFA": "Coastal Flood Watch", "DSW": "Dust Storm Warning", "EQW": "Earthquake Warning",
    "EVI": "Evacuation Immediate", "EWW": "Extreme Wind Warning", "FRW": "Fire Warning",
    "FFW": "Flash Flood Warning", "FFA": "Flash Flood Watch", "FFS": "Flash Flood Statement",
    "FLW": "Flood Warning", "FLA": "Flood Watch", "FLS": "Flood Statement",
    "HMW": "Hazardous Materials Warning", "HWW": "High Wind Warning",
    "HWA": "High Wind Watch", "HUW": "Hurricane Warning", "HUA": "Hurricane Watch",
    "HLS": "Hurricane Statement", "LEW": "Law Enforcement Warning",
    "LAE": "Local Area Emergency", "MEP": "Missing and Endangered Persons",
    "NMN": "Network Message Notification", "TOE": "911 Telephone Outage Emergency",
    "NUW": "Nuclear Power Plant Warning", "DMO": "Practice/Demo Warning",
    "RHW": "Radiological Hazard Warning", "SVR": "Severe Thunderstorm Warning",
    "SVA": "Severe Thunderstorm Watch", "SVS": "Severe Weather Statement",
    "SPW": "Shelter in Place Warning", "SMW": "Special Marine Warning",
    "SPS": "Special Weather Statement", "SSA": "Storm Surge Watch",
    "SSW": "Storm Surge Warning", "TOR": "Tornado Warning", "TOA": "Tornado Watch",
    "TRW": "Tropical Storm Warning", "TRA": "Tropical Storm Watch",
    "TSW": "Tsunami Warning", "TSA": "Tsunami Watch", "VOW": "Volcano Warning",
    "WSW": "Winter Storm Warning", "WSA": "Winter Storm Watch",
    "SQW": "Snow Squall Warning",
}
_FCC2023_EVENT_NAMES = {
    "EAN": "National Emergency Message",
    "NPT": "Nationwide Test of the Emergency Alert System",
}

# Spanish event names as DASDEC's "English + Spanish EAS Translation" renders
# them (feminine nouns take "una", masculine "un").
_EVENT_ES = {
    "TOR": ("Advertencia de Tornado", "f"), "TOA": ("Vigilancia de Tornado", "f"),
    "SVR": ("Advertencia de Tormentas Eléctricas Severas", "f"),
    "SVA": ("Vigilancia de Tormentas Eléctricas Severas", "f"),
    "SVS": ("Declaración de Tiempo Severo", "f"),
    "FFW": ("Advertencia de Inundaciones Repentinas", "f"),
    "FFA": ("Vigilancia de Inundaciones Repentinas", "f"),
    "FFS": ("Declaración de Inundaciones Repentinas", "f"),
    "FLW": ("Advertencia de Inundación", "f"), "FLA": ("Vigilancia de Inundación", "f"),
    "FLS": ("Declaración de Inundación", "f"),
    "WSW": ("Advertencia de Tormenta Invernal", "f"), "WSA": ("Vigilancia de Tormenta Invernal", "f"),
    "BZW": ("Advertencia de Ventisca", "f"), "HWW": ("Advertencia de Vientos Fuertes", "f"),
    "HWA": ("Vigilancia de Vientos Fuertes", "f"), "EWW": ("Advertencia de Vientos Extremos", "f"),
    "HUW": ("Advertencia de Huracán", "f"), "HUA": ("Vigilancia de Huracán", "f"),
    "HLS": ("Declaración de Huracán", "f"),
    "TRW": ("Advertencia de Tormenta Tropical", "f"), "TRA": ("Vigilancia de Tormenta Tropical", "f"),
    "SSW": ("Advertencia de Marejada Ciclónica", "f"), "SSA": ("Vigilancia de Marejada Ciclónica", "f"),
    "TSW": ("Advertencia de Tsunami", "f"), "TSA": ("Vigilancia de Tsunami", "f"),
    "DSW": ("Advertencia de Tormenta de Polvo", "f"), "SQW": ("Advertencia de Ráfagas de Nieve", "f"),
    "SMW": ("Advertencia Marina Especial", "f"), "SPS": ("Declaración Especial del Tiempo", "f"),
    "FRW": ("Advertencia de Incendio", "f"), "AVW": ("Advertencia de Avalancha", "f"),
    "AVA": ("Vigilancia de Avalancha", "f"), "CFW": ("Advertencia de Inundación Costera", "f"),
    "CFA": ("Vigilancia de Inundación Costera", "f"), "EQW": ("Advertencia de Terremoto", "f"),
    "VOW": ("Advertencia de Volcán", "f"),
    "CAE": ("Emergencia de Secuestro de Menores", "f"), "CEM": ("Mensaje de Emergencia Civil", "m"),
    "CDW": ("Advertencia de Peligro Civil", "f"), "EVI": ("Evacuación Inmediata", "f"),
    "SPW": ("Advertencia de Refugio en el Lugar", "f"), "LEW": ("Advertencia de las Autoridades", "f"),
    "LAE": ("Emergencia de Área Local", "f"), "TOE": ("Emergencia de Interrupción del 911", "f"),
    "HMW": ("Advertencia de Materiales Peligrosos", "f"),
    "NUW": ("Advertencia de Planta Nuclear", "f"), "RHW": ("Advertencia de Peligro Radiológico", "f"),
    "BLU": ("Alerta Azul", "f"), "MEP": ("Alerta de Personas Desaparecidas y en Peligro", "f"),
    "ADR": ("Mensaje Administrativo", "m"), "DMO": ("Advertencia de Práctica/Demostración", "f"),
    "RMT": ("Prueba Mensual Requerida", "f"), "RWT": ("Prueba Semanal Requerida", "f"),
    "NPT": ("Prueba Periódica Nacional", "f"), "EAN": ("Notificación de Acción de Emergencia", "f"),
}

_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "VI": "Virgin Islands", "GU": "Guam", "AS": "American Samoa",
    "MP": "Northern Mariana Islands",
}

_MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
              "septiembre", "octubre", "noviembre", "diciembre"]

# Required visual text for a nationwide test (47 CFR 11.51(d), effective
# Dec 12 2023), used for NPT alerts in every style.
_NPT_SCRIPT = ("This is a nationwide test of the Emergency Alert System, issued by the "
               "Federal Emergency Management Agency, covering the United States from "
               "{start} to {end}. This is only a test. No action is required by the public.")

# Bundled fonts (fonts/ in the plugin). Liberation Sans Bold is metrically
# identical to Arial Bold; DejaVu Sans Bold is Bitstream Vera Sans Bold, the
# EASyCAP font; DejaVu Sans Condensed Bold stands in for Helvetica Narrow
# Bold (VDS 840's "Swiss 721").
_F_LUXI   = "luximb.ttf"
_F_STV    = "EASyText.ttf"
_F_ARIALB = "LiberationSans-Bold.ttf"
_F_VERAB  = "DejaVuSans-Bold.ttf"
_F_NARROW = "DejaVuSansCondensed-Bold.ttf"

# Average advance width (in em) per font, used only if Pillow can't measure.
_AVG_ADV = {_F_LUXI: 0.60, _F_STV: 0.54, _F_ARIALB: 0.56, _F_VERAB: 0.64, _F_NARROW: 0.58}

# ---------------------------------------------------------------------------
# Style catalogue
# ---------------------------------------------------------------------------

STYLE_CATALOG = [
    ("dasdec",            "DASDEC — classic details page (purple-blue, red outline, Luxi Mono)"),
    ("dasdec_legacy",     "DASDEC — 2007–2016 all-caps page (\"Emergency Alert Details\", Page 1 of 2)"),
    ("dasdec_black",      "DASDEC — black background"),
    ("dasdec_plain",      "DASDEC — plain blue, Arial Bold, no outline"),
    ("dasdec_red",        "DASDEC — red background (Alert Ready-style)"),
    ("dasdec_redscroll",  "DASDEC — classic page + red crawl bar on top"),
    ("dasdec_blackscroll","DASDEC — classic page + black crawl bar on top"),
    ("easyplus",          "EASyPLUS — black screen, scroll under header (classic)"),
    ("easyplus_middle",   "EASyPLUS — black screen, middle scroll"),
    ("easyplus_bottom",   "EASyPLUS — black screen, bottom scroll"),
    ("easyplus_gray",     "EASyPLUS — grey screen, white text"),
    ("easyplus_grayblack","EASyPLUS — light grey screen, black text"),
    ("easyplus_ticker",   "EASyPLUS — ticker only, over the live program"),
    ("easycap",           "EASyCAP — blue page, Vera Sans Bold, \"- 1 of 2 -\""),
    ("gorman_redlich",    "Gorman-Redlich EAS-1CG — \"*** EMERGENCY DETAILS ***\" pages"),
    ("gorman_redlich_overlay", "Gorman-Redlich EAS-1CG — transparent, keyed over the live program"),
    ("vds_subalert",      "VDS 840 / Sub-Alert — colour by alert type, bottom crawl"),
    ("stb_ticker",        "Set-top box — black \"EMERGENCY ALERT\" screen, red ticker"),
    ("crawl_overlay",     "Crawl over the live program (CG ticker at the top)"),
    ("sara",              "SARA set-top firmware — red bar over the program, paged (2010s–today)"),
    ("sara_classic",      "SARA set-top firmware — classic dark-red bar (2000s)"),
    ("sara_dasdec",       "SARA/DASDEC — SARA red bar on top of the DASDEC details page"),
]
STYLE_KEYS = [k for k, _ in STYLE_CATALOG]
_LIVE_STYLES = {"easyplus_ticker", "crawl_overlay", "gorman_redlich_overlay", "sara", "sara_classic"}

def style_label(key):
    return dict(STYLE_CATALOG).get(key, key)

def style_is_live(key):
    """True for styles drawn over the live program (they decode the channel)."""
    return key in _LIVE_STYLES

# ---------------------------------------------------------------------------
# Alert -> translation pieces
# ---------------------------------------------------------------------------

def eas_event_code(alert):
    ec = (alert.get("event_code") or "").strip().upper()
    if len(ec) == 3:
        return ec
    name = re.sub(r"\s+", " ", (alert.get("event") or "").strip().lower())
    for code, nm in SAME_EVENT_NAMES.items():
        if nm.lower() == name:
            return code
    for code, nm in _FCC2023_EVENT_NAMES.items():
        if nm.lower() == name:
            return code
    return ""

def eas_event_name(alert, fcc2023=True):
    code = eas_event_code(alert)
    if fcc2023 and code in _FCC2023_EVENT_NAMES:
        return _FCC2023_EVENT_NAMES[code]
    ev = re.sub(r"\s+", " ", (alert.get("event") or "")).strip()
    if ev:
        return ev
    return SAME_EVENT_NAMES.get(code, "Emergency Alert")

def eas_originator_code(alert):
    org = (alert.get("originator") or "").strip().upper()
    if org in ("WXR", "CIV", "EAS", "PEP"):
        return org
    s = (alert.get("sender") or "").lower()
    if any(k in s for k in ("weather", "nws", "noaa")):
        return "WXR"
    if "primary entry" in s or "united states government" in s or "fema" in s:
        return "PEP"
    if "participant" in s or "broadcast" in s or "cable" in s:
        return "EAS"
    return "CIV"

def eas_station_id(alert, default="EAA/IPTV"):
    """LLLLLLLL station ID: as delivered (NWS office "KXXX/NWS", "IPAWSCAP"),
    else the configured one."""
    sid = (alert.get("station_id") or "").strip()
    return sid or default

def _parse_dt(s):
    try:
        dt = datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

_US_TZ = {(-4, True): "EDT", (-5, True): "CDT", (-6, True): "MDT", (-7, True): "PDT",
          (-8, True): "AKDT", (-5, False): "EST", (-6, False): "CST", (-7, False): "MST",
          (-8, False): "PST", (-9, False): "AKST", (-10, False): "HST", (-4, False): "AST"}

def _local(dt):
    """Show times in the server's local zone when it matches the alert's offset
    (or the alert is UTC); otherwise keep the issuing office's offset."""
    if dt is None:
        return None
    loc = dt.astimezone()
    if dt.utcoffset() == loc.utcoffset() or dt.utcoffset().total_seconds() == 0:
        return loc
    return dt

def _tz_abbr(dt):
    if dt is None:
        return ""
    name = dt.tzname() or ""
    if name and not name.startswith("UTC") and not name.startswith("+") and not name.startswith("-"):
        return name
    hours = int(dt.utcoffset().total_seconds() // 3600)
    dst = 3 <= dt.month <= 11
    return _US_TZ.get((hours, dst)) or _US_TZ.get((hours, not dst)) or name

def _hm(dt, lower=False):
    s = dt.strftime("%I:%M %p").lstrip("0")
    return s.lower() if lower else s

def _areas(alert):
    """[(name, state_or_None)] from areaDesc ("Dallas, TX; Tarrant, TX")."""
    out = []
    for part in re.split(r"\s*;\s*", alert.get("area") or ""):
        part = part.strip().rstrip(".")
        if not part:
            continue
        m = re.match(r"^(.*?),\s*([A-Z]{2})$", part)
        if m:
            out.append((m.group(1).strip(), m.group(2)))
        else:
            out.append((part, None))
    return out or [("This area", None)]

def _cap_text(alert):
    """Free-text portion of a CAP alert (description + instruction) as clean
    prose paragraphs, NWS "* WHAT..." bullets flattened to "WHAT: ..."."""
    paras = []
    for key in ("description", "instruction"):
        raw = (alert.get(key) or "").strip()
        if not raw:
            continue
        for chunk in re.split(r"\n\s*\n|\*\s+", raw):
            chunk = re.sub(r"\s+", " ", chunk).strip()
            if not chunk:
                continue
            chunk = re.sub(r"^([A-Z][A-Z /&'-]{2,})\.{2,}\s*", r"\1: ", chunk)
            paras.append(chunk)
    return paras

def _ctx(alert, opts):
    fcc = opts.get("fcc2023", True)
    code = eas_event_code(alert)
    org = eas_originator_code(alert)
    event = eas_event_name(alert, fcc)
    start = _local(_parse_dt(alert.get("effective")) or datetime.now(timezone.utc))
    end = _local(_parse_dt(alert.get("expires")) or (start + timedelta(hours=1)))
    cap = []
    if code == "NPT":
        cap = [_NPT_SCRIPT.format(start=f"{_hm(start)} {_tz_abbr(start)}".strip(),
                                  end=f"{_hm(end)} {_tz_abbr(end)}".strip())]
    elif opts.get("show_cap_text", True):
        cap = _cap_text(alert)
    return {
        "code": code, "org": org, "event": event,
        "article": "an" if event[:1].upper() in "AEIOU" else "a",
        "areas": _areas(alert), "start": start, "end": end,
        "station": eas_station_id(alert, opts.get("station_id") or "EAA/IPTV"),
        "operator": (opts.get("operator_name") or "").strip(),
        "cap": cap, "fcc2023": fcc,
        "national": code in ("EAN", "NPT") or org == "PEP",
    }

def _org_phrase(c, variant):
    """Originator wording per device family."""
    org, fcc = c["org"], c["fcc2023"]
    pep_new = "United States Government"
    table = {
        # DASDEC sentence subject (mixed case)
        "dasdec": {"WXR": "The National Weather Service", "CIV": "A civil authority",
                   "EAS": "A broadcast station or cable system",
                   "PEP": "The " + pep_new if fcc else "The Primary Entry Point System"},
        # TFT-style legacy caps (DASDEC legacy / Gorman-Redlich)
        "caps":   {"WXR": "THE NATIONAL WEATHER SERVICE", "CIV": "A CIVIL AUTHORITY",
                   "EAS": "A BROADCAST STATION OR CABLE SYSTEM",
                   "PEP": "THE " + pep_new.upper() if fcc else "THE PRIMARY ENTRY POINT SYSTEM"},
        # EASyPLUS originator line (FCC 11.31 originator table names)
        "easy":   {"WXR": "National Weather Service", "CIV": "Civil Authorities",
                   "EAS": "EAS Participant",
                   "PEP": pep_new if fcc else "Primary Entry Point System"},
        # EASyCAP sentence subject
        "easycap": {"WXR": "The National Weather Service", "CIV": "A Civil Authority",
                    "EAS": "An EAS Participant",
                    "PEP": "The " + pep_new if fcc else "A Primary Entry Point System"},
        "es":     {"WXR": "El Servicio Meteorológico Nacional ha emitido",
                   "CIV": "Una autoridad civil ha emitido",
                   "EAS": "Una estación emisora o un operador de cable a emitido",
                   "PEP": "El Gobierno de los Estados Unidos ha emitido" if fcc
                          else "El Sistema de Punto de Entrada Primaria ha emitido"},
    }
    # A cable headend's DASDEC lets the operator name itself as the EAS
    # Participant originator ("<operator> has issued A REQUIRED WEEKLY TEST ...").
    if org == "EAS" and c.get("operator") and variant in ("dasdec", "caps", "easycap"):
        return c["operator"].upper() if variant == "caps" else c["operator"]
    return table[variant].get(org, table[variant]["CIV"])

def _grouped_area_lines(areas, sep=";"):
    """DASDEC county list: one per line, state only after the last county of
    each state run ("Wayne;", "Genesee;", "Livingston, NY;")."""
    lines = []
    for i, (name, st) in enumerate(areas):
        nxt = areas[i + 1][1] if i + 1 < len(areas) else "__end__"
        if st and nxt != st:
            lines.append(f"{name}, {st}{sep}")
        else:
            lines.append(f"{name}{sep}")
    return lines

def _plain_areas(areas, comma=True):
    return ", ".join(f"{n} {s}" if s else n for n, s in areas) if comma else \
        "; ".join(f"{n}, {s}" if s else n for n, s in areas)

def _crawl_translation(c, tz=False):
    """One-line ENDEC translation used by crawls / tickers."""
    end = c["end"]
    until = f"{end.strftime('%m/%d')} {_hm(end)}" + (f" {_tz_abbr(end)}" if tz else "")
    org = _org_phrase(c, "easy")
    verb = "have" if c["org"] == "CIV" else "has"
    org_s = ("The " + org) if c["org"] == "WXR" else org
    s = (f"{org_s} {verb} issued {c['article']} {c['event']} for the following counties: "
         f"{_plain_areas(c['areas'])}. Effective Until {until}.")
    return s

def eas_spoken_text(alert, opts=None):
    """Readout text, ENDEC-style: the header translation, then the CAP text."""
    c = _ctx(alert, opts or {})
    org = _org_phrase(c, "dasdec")
    areas = "; ".join(f"{n}, {_STATE_NAMES.get(s, s)}" if s else n for n, s in c["areas"])
    s = (f"{org} has issued {c['article']} {c['event']} for the following counties or areas: "
         f"{areas}. At {_hm(c['start'])} on {c['start'].strftime('%B')} {c['start'].day}, "
         f"effective until {_hm(c['end'])}.")
    if c["code"] == "RWT":
        return s
    if c["cap"]:
        s += " " + " ".join(c["cap"])
    return s

def eas_test_alert(kind, now_utc, area=None, station_id=None, same_codes=None):
    """RMT/RWT as an encoder originates it: EAS Participant originator, the
    standard test script, and a realistic valid time (RMT +0100, RWT +0015)."""
    weekly = (kind or "").lower() == "weekly"
    label = "Required Weekly Test" if weekly else "Required Monthly Test"
    location = (area or "").strip() or "this viewing area"
    if weekly:
        desc = ""
        instr = ""
        dur = 15
    else:
        desc = ("This is a Required Monthly Test of the Emergency Alert System. "
                "This test is conducted by your local broadcasters and cable systems in "
                "cooperation with the Federal Communications Commission. If this had been an "
                "actual emergency, official messages would have followed the alert tone.")
        instr = "This concludes this test of the Emergency Alert System."
        dur = 60
    return {
        "event": label, "area": location, "severity": "Minor",
        "effective": now_utc.isoformat(),
        "expires": (now_utc + timedelta(minutes=dur)).isoformat(),
        "headline": f"{label} for {location}", "description": desc, "instruction": instr,
        "sender": "EAS Participant", "event_code": "RWT" if weekly else "RMT",
        "originator": "EAS", "same_codes": list(same_codes or ["000000"])[:31],
        "station_id": station_id or "",
    }

# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _ffq(path):
    return "'" + str(path).replace("'", "'\\''") + "'"

def _font(name, override=None):
    """Bundled font path, honoring a user override for the primary font."""
    if override:
        return _resolve_font(override, "dasdec" if name == _F_LUXI else "easyplus")
    p = os.path.join(FONTS_DIR, name)
    if os.path.isfile(p):
        return p
    return _font_for("dasdec" if name == _F_LUXI else "easyplus")

_glyph_cache = {}

def _ascii_fallback(text, font_path):
    """Swap characters the font can't draw (the STV5730A-style EASyText has
    no accents, bullets or middots) for ones it can."""
    if os.path.basename(font_path or "") != _F_STV:
        return text
    t = text.replace("·", "-").replace("•", "-").replace("–", "-").replace("’", "'")
    t = unicodedata.normalize("NFKD", t)
    return "".join(ch for ch in t if ord(ch) < 128)

def _measure(font_path, size, text):
    key = (font_path, int(size))
    try:
        f = _glyph_cache.get(key)
        if f is None:
            from PIL import ImageFont
            f = ImageFont.truetype(font_path, int(size))
            _glyph_cache[key] = f
        return float(f.getlength(text))
    except Exception:
        return len(text) * size * _AVG_ADV.get(os.path.basename(font_path or ""), 0.6)

def _wrap(text, font_path, size, max_w):
    """Greedy word wrap by rendered width."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if cur and _measure(font_path, size, cand) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines or [""]

class _Canvas:
    """Accumulates an FFmpeg filter chain drawing on a W x H frame laid out in
    480-line "SD units" (y scaled by k = H/480, x by the same k)."""

    def __init__(self, cid, width, height, base_w, scale=1.0):
        self.cid = cid
        self.W, self.H = width, height
        self.k = height / 480.0
        self.base_w = base_w
        self.ts = max(0.5, min(3.0, float(scale or 1.0)))   # user text-size multiplier
        self.parts = []
        self.n = 0

    def px(self, v):
        return int(round(v * self.k))

    def _file(self, text):
        name = f"eas_{self.cid}_s{self.n:03d}.txt"
        self.n += 1
        _atomic_write(name, text if text.strip() else " ")
        return os.path.join(RUNTIME_DIR, name)

    def rect(self, x, y, w, h, color, enable=None):
        en = f":enable='{enable}'" if enable else ""
        self.parts.append(
            f"drawbox=x={self.px(x)}:y={self.px(y)}:w={self.px(w)}:h={self.px(h)}"
            f":color={color}:t=fill{en}")

    def outline(self, inset, thick, color):
        """Rectangular outline as four filled bars -- exact pixel edges at any
        resolution (the old single drawbox t= border drifted when scaled)."""
        W = self.base_w
        H = 480
        self.rect(inset, inset, W - 2 * inset, thick, color)
        self.rect(inset, H - inset - thick, W - 2 * inset, thick, color)
        self.rect(inset, inset, thick, H - 2 * inset, color)
        self.rect(W - inset - thick, inset, thick, H - 2 * inset, color)

    def text(self, s, font, size, baseline, color="white", x="center", enable=None,
             shadow=None, edge=None):
        s = _ascii_fallback(s, font)
        fs = max(6, int(round(size * self.k * self.ts)))
        xexpr = "(w-text_w)/2" if x == "center" else str(self.px(x))
        en = f":enable='{enable}'" if enable else ""
        sh = f":shadowcolor={shadow}:shadowx={max(1, self.px(1.5))}:shadowy={max(1, self.px(1.5))}" if shadow else ""
        if edge:
            sh += f":borderw={max(1, self.px(edge))}:bordercolor=black"
        self.parts.append(
            f"drawtext=fontfile={_ffq(font)}:textfile={_ffq(self._file(s))}:reload=0:expansion=none"
            f":fontsize={fs}:fontcolor={color}:x={xexpr}:y={self.px(baseline)}-max_glyph_a{sh}{en}")

    def crawl(self, s, font, size, baseline, speed, color="white", gap=60, enable=None):
        """Right-to-left crawl that repeats continuously at a fixed speed
        (SD px/s), entering from the right edge at t=0."""
        s = _ascii_fallback(re.sub(r"\s+", " ", s).strip(), font)
        fs = max(6, int(round(size * self.k * self.ts)))
        spd = max(10.0, speed * self.k)
        en = f":enable='{enable}'" if enable else ""
        self.parts.append(
            f"drawtext=fontfile={_ffq(font)}:textfile={_ffq(self._file(s))}:reload=0:expansion=none"
            f":fontsize={fs}:fontcolor={color}"
            f":x=w-mod(t*{spd:.1f}\\,text_w+w+{self.px(gap)}):y={self.px(baseline)}-max_glyph_a{en}")

    def filter(self):
        return ",".join(self.parts) if self.parts else "null"

def _page_enables(n, secs):
    if n <= 1:
        return [None]
    tot = n * secs
    return [f"gte(mod(t\\,{tot:.2f})\\,{i * secs:.2f})*lt(mod(t\\,{tot:.2f})\\,{(i + 1) * secs:.2f})"
            for i in range(n)]

def _paginate(lines, per_page):
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)]
    # don't start a page with a blank spacer line
    return [[ln for j, ln in enumerate(p) if not (j == 0 and not ln.strip())] or [""]
            for p in pages] or [[""]]

# ---------------------------------------------------------------------------
# DASDEC family
# ---------------------------------------------------------------------------

_DASDEC_BG = {
    "dasdec": "0x2A20BC", "dasdec_legacy": "0x22229E", "dasdec_black": "0x000000",
    "dasdec_plain": "0x1A2DE0", "dasdec_red": "0xB0161B",
    "dasdec_redscroll": "0x2A20BC", "dasdec_blackscroll": "0x2A20BC",
}
_BG_CHOICES = {"blue": "0x1A2DE0", "purpleblue": "0x2A20BC", "black": "0x000000",
               "red": "0xB0161B", "maroon": "0x6E0F14", "orange": "0xD2691E"}

def _dasdec_english(c, legacy, wrap):
    if legacy:
        first = (f"{_org_phrase(c, 'caps')} HAS ISSUED {c['article'].upper()} {c['event'].upper()} "
                 f"FOR THE FOLLOWING COUNTIES/AREAS:")
        lines = wrap(first)
        lines += _grouped_area_lines(c["areas"])
        lines += [f"AT {_hm(c['start'])}", f"ON {c['start'].strftime('%b').upper()} {c['start'].day}, {c['start'].year}",
                  f"EFFECTIVE UNTIL {_hm(c['end'])}.", f"MESSAGE FROM {c['station'].upper()}."]
    else:
        first = (f"{_org_phrase(c, 'dasdec')} has issued {c['article'].upper()} {c['event'].upper()} "
                 f"for the following counties or areas:")
        lines = wrap(first)
        lines += _grouped_area_lines(c["areas"])
        lines += [f"at {_hm(c['start'])}", f"on {c['start'].strftime('%b').upper()} {c['start'].day}, {c['start'].year}",
                  f"Effective until {_hm(c['end'])}.", f"Message from {c['station']}."]
    for para in c["cap"]:
        lines += wrap(para.upper() if legacy else para)
    return lines

def _dasdec_spanish(c, wrap):
    ev, g = _EVENT_ES.get(c["code"], (c["event"], "f"))
    art = "una" if g == "f" else "un"
    s = c["start"]
    first = f"{_org_phrase(c, 'es')} {art} {ev} para los siguientes condados:"
    lines = wrap(first) + _grouped_area_lines(c["areas"])
    lines += [f"En {_hm(s)}", f"de {_MONTHS_ES[s.month - 1]} {s.day}, {s.year}",
              f"Efectivo hasta {_hm(c['end'])}.", f"Un mensaje de {c['station']}."]
    return lines

def _build_dasdec(cv, c, style, opts):
    legacy = style == "dasdec_legacy"
    font = _font(_F_ARIALB if style == "dasdec_plain" else _F_LUXI, opts.get("font_override"))
    bar = style in ("dasdec_redscroll", "dasdec_blackscroll")
    # Layout (SD units on the 640x480 4:3 page)
    size = 30 if legacy else 21
    pitch = 34 if legacy else 25
    top = 46 if not bar else 76
    head_base = 34 if not bar else 64
    counter_base = 455
    usable_w = 560 * cv.k
    wrap = lambda s: _wrap(s, font, size * cv.k * cv.ts, usable_w)
    per_page = max(4, int((counter_base - 44 - top) // (pitch * cv.ts)) - 1)

    heading = opts.get("heading")
    if heading is None:
        heading = "Emergency Alert Details" if legacy else "Emergency Alert System"
    if (opts.get("heading_mode") or "auto") == "auto" and c["station"].upper() == "IPAWSCAP" and not legacy:
        heading = ""          # IPAWS-originated pages are shown without a heading

    pages = [(heading, p) for p in _paginate(_dasdec_english(c, legacy, wrap), per_page)]
    if opts.get("spanish") and not legacy:
        pages += [("ALERTA DE EMERGENCIA", p) for p in _paginate(_dasdec_spanish(c, wrap), per_page)]

    if style not in ("dasdec_plain", "dasdec_black", "dasdec_red"):
        cv.outline(0, 3, "0xE0141E")
    elif style == "dasdec_black":
        cv.outline(0, 3, "0xE0141E")
    if bar:
        cv.rect(0, 0, 640, 40, "0xC8141E" if style == "dasdec_redscroll" else "0x000000")
        cv.crawl(_crawl_translation(c) + ("   " + " ".join(c["cap"]) if c["cap"] else ""),
                 _font(_F_ARIALB), 22, 28, opts.get("crawl_px", 150))

    n = len(pages)
    enables = _page_enables(n, opts.get("page_secs", 7.0))
    for i, (head, lines) in enumerate(pages):
        en = enables[i]
        if head:
            cv.text(head, font, size, head_base, enable=en)
        y = top + pitch if head else top + 6
        for ln in lines:
            if ln.strip():
                cv.text(ln, font, size, y, enable=en)
            y += pitch * cv.ts
        counter = f"Page {i + 1} of {n}" if legacy else f"{i + 1}/{n}"
        cv.text(counter, font, size, counter_base - 6, enable=en)
    return n

# ---------------------------------------------------------------------------
# EASyPLUS family (STV5730A 28x11 grid)
# ---------------------------------------------------------------------------

def _stv_row(row):
    """Baseline (SD units) of STV5730A text row 0..10."""
    return 48 + row * 40

def _build_easyplus(cv, c, style, opts):
    font = _font(_F_STV, opts.get("font_override"))
    fg = "black" if style == "easyplus_grayblack" else "white"
    size = 32
    pos = {"easyplus_middle": "middle", "easyplus_bottom": "bottom"}.get(style, "top")
    if style in ("easyplus_gray", "easyplus_grayblack"):
        pos = opts.get("scroll_pos") or pos
    code = c["code"]
    crawl_txt = _crawl_translation(c)
    if c["cap"] and code != "NPT":
        crawl_txt += "  " + " ".join(c["cap"])
    elif code == "NPT":
        crawl_txt = c["cap"][0]
    speed = 30.0 * max(1, min(10, int(opts.get("crawl_speed", 5))))

    def wrap28(s):
        return textwrap.wrap(s, 28) or [""]

    if code == "NPT":
        rows = {0: ["NATIONAL EAS TEST"], 3: ["THIS IS A TEST"], 5: ["THIS IS ONLY A TEST"]}
    else:
        header = "NATIONAL ALERT" if c["national"] and code != "RMT" else "EMERGENCY ALERT SYSTEM"
        if opts.get("local_access"):
            header = "LOCAL ACCESS ALERT"
        org = _org_phrase(c, "easy")
        if c["org"] == "EAS" and opts.get("legacy_names"):
            org_lines = ["Broadcast Station", "or Cable System"]
        else:
            org_lines = wrap28(org)
        rows = {0: [header], 3: org_lines,
                5: ["Issued an" if c["article"] == "an" else "Issued a"],
                7: wrap28(c["event"])}
        if opts.get("local_access"):
            rows = {0: ["LOCAL ACCESS ALERT"], 3: ["A LOCAL AUTHORITY"], 5: ["HAS INITIATED A"],
                    7: ["DIRECT COMMUNITY ACCESS"]}
        tune = (opts.get("tune_to") or "").strip()
        if tune and pos != "bottom":
            rows[9] = wrap28(tune)[:2]
    scroll_row = {"top": 1, "middle": 4, "bottom": 10}[pos]
    for r, lines in rows.items():
        for j, ln in enumerate(lines):
            rr = r + j
            if rr == scroll_row:
                rr += 1
            cv.text(ln, font, size, _stv_row(rr), color=fg)
    cv.crawl(crawl_txt, font, size, _stv_row(scroll_row), speed, color=fg, gap=80)
    return 1

def _build_easyplus_ticker(cv, c, opts):
    font = _font(_F_STV, opts.get("font_override"))
    txt = _crawl_translation(c) + ("  " + " ".join(c["cap"]) if c["cap"] else "")
    speed = 30.0 * max(1, min(10, int(opts.get("crawl_speed", 5))))
    cv.rect(0, 22, cv.base_w, 40, "black")
    cv.crawl(txt, font, 32, 52, speed, gap=80)
    return 1

# ---------------------------------------------------------------------------
# EASyCAP
# ---------------------------------------------------------------------------

def _build_easycap(cv, c, opts):
    font = _font(_F_VERAB, opts.get("font_override"))
    hsize, bsize, pitch = 27, 21, 25
    usable = 600 * cv.k
    wrap = lambda s, sz: _wrap(s, font, sz * cv.k * cv.ts, usable)
    org = _org_phrase(c, "easycap")
    end = c["end"]
    until = f"{end.strftime('%B %d')}, {end.strftime('%I:%M %p')} {_tz_abbr(end)}".strip()
    if c["code"] == "RWT":
        org_l = org[0].lower() + org[1:]
        body = f"A Required Weekly Test has been issued by {org_l}."
    else:
        states = {s for _, s in c["areas"] if s}
        if c["national"] and c["code"] in ("EAN", "NPT"):
            where = "The United States"
            body = f"{org} has issued {c['article']} {c['event']} for {where}. Effective until {until}."
        else:
            names = ", ".join(n for n, _ in c["areas"])
            st = _STATE_NAMES.get(next(iter(states)), "") if len(states) == 1 else ""
            body = (f"{org} has issued {c['article']} {c['event']} for the following "
                    f"{(st + ' ') if st else ''}counties: {names}. Effective until {until}.")
    lines = wrap(body, bsize)
    for p in c["cap"]:
        lines += wrap(p, bsize)
    if c["cap"]:
        lines[-1] = lines[-1] + " " + c["station"]
    head = ["Emergency Alert System"] + wrap(c["event"], hsize)
    top = 30 + len(head) * 32 + 8
    per_page = max(4, int((425 - top) // (pitch * cv.ts)))
    pages = _paginate(lines, per_page)
    enables = _page_enables(len(pages), opts.get("page_secs", 7.0))
    for i, pl in enumerate(pages):
        en = enables[i]
        for j, h in enumerate(head):
            cv.text(h, font, hsize, 40 + j * 32, enable=en)
        y = top + pitch
        for ln in pl:
            if ln.strip():
                cv.text(ln, font, bsize, y, enable=en)
            y += pitch * cv.ts
        if len(pages) > 1:
            cv.text(f"- {i + 1} of {len(pages)} -", font, bsize, 458, enable=en)
    return len(pages)

# ---------------------------------------------------------------------------
# Gorman-Redlich EAS-1CG
# ---------------------------------------------------------------------------

def _build_gorman(cv, c, opts, keyed=False):
    font = _font(_F_ARIALB, opts.get("font_override"))
    size, pitch = 25, 30
    # Keyed over video the EAS-1CG draws white characters with a dark edge so
    # they stay readable on any picture.
    edge = 2.0 if keyed else None
    usable = 610 * cv.k
    wrap = lambda s: _wrap(s, font, size * cv.k * cv.ts, usable)
    lines = _dasdec_english(c, True, wrap)
    per_page = max(4, int((415 - 60) // (pitch * cv.ts)))
    pages = _paginate(lines, per_page)
    enables = _page_enables(len(pages), opts.get("page_secs", 7.0))
    for i, pl in enumerate(pages):
        en = enables[i]
        cv.text("*** EMERGENCY DETAILS ***", font, 23, 40, enable=en, edge=edge)
        y = 60 + pitch
        for ln in pl:
            if ln.strip():
                cv.text(ln, font, size, y, enable=en, edge=edge)
            y += pitch * cv.ts
        if len(pages) > 1:
            cv.text(f"Page {i + 1} of {len(pages)}", font, size, 458, enable=en, edge=edge)
    return len(pages)

# ---------------------------------------------------------------------------
# VDS 840 / MHz Sub-Alert (mode 1)
# ---------------------------------------------------------------------------

def _vds_bg(c):
    code, ev = c["code"], c["event"].lower()
    if code in ("EAN",) or "warning" in ev or code in ("CAE", "CEM", "EVI", "LAE", "BLU", "MEP"):
        return "0xA3161A"          # red: warnings / EAN
    if "watch" in ev:
        return "0xB28B1C"          # yellow: watches
    return "0x1E8C2E"              # green: tests, statements, advisories

def _build_vds(cv, c, opts):
    font = _font(_F_NARROW, opts.get("font_override"))
    cv.text("Emergency Alert System", font, 25, 50)
    tune = (opts.get("tune_to") or "").strip()
    if tune:
        for j, ln in enumerate(textwrap.wrap(tune, 30)[:2]):
            cv.text(ln, font, 25, 112 + j * 30)
    ev_lines = _wrap(c["event"], font, 27 * cv.k * cv.ts, 560 * cv.k)
    y0 = 250 if tune else 170
    for j, ln in enumerate(ev_lines):
        cv.text(ln, font, 27, y0 + j * 34)
    if opts.get("spanish") and c["code"] in _EVENT_ES:
        for j, ln in enumerate(_wrap(_EVENT_ES[c["code"]][0], font, 27 * cv.k * cv.ts, 560 * cv.k)):
            cv.text(ln, font, 27, y0 + (len(ev_lines) + j) * 34 + 10)
    if c["code"] != "RWT":           # Sub-Alert crawls are exempt on RWTs
        s, e = c["start"], c["end"]
        fmt = lambda d: f"{_hm(d, lower=True)} {d.strftime('%a %b')} {d.day}"
        org = _org_phrase(c, "easy")
        org = ("The " + org) if c["org"] == "WXR" else org
        txt = (f"{org} has issued {c['article']} {c['event']} for {_plain_areas(c['areas'])} "
               f"beginning at {fmt(s)} and ending at {fmt(e)} ({_tz_abbr(e)})")
        if c["cap"]:
            txt += "   " + " ".join(c["cap"])
        cv.crawl(txt, font, 25, 452, opts.get("crawl_px", 150), gap=90)
    return 1

# ---------------------------------------------------------------------------
# Set-top "EMERGENCY ALERT" + red ticker, and crawl over the live program
# ---------------------------------------------------------------------------

def _build_stb(cv, c, opts):
    font = _font(_F_ARIALB, opts.get("font_override"))
    cv.text("EMERGENCY ALERT", font, 54, 200)
    cv.rect(0, 268, cv.base_w, 58, "0xC8102E")
    txt = _crawl_translation(c, tz=True) + ("   " + " ".join(c["cap"]) if c["cap"] else "")
    cv.crawl(txt, font, 32, 309, opts.get("crawl_px", 170), gap=120)
    return 1

def _sara_text(c, legacy):
    """The SCTE-18 alert text a headend DASDEC sends to the set-top boxes --
    the DASDEC translation as one line, then the CAP text."""
    wrap_all = lambda x: [x]
    lines = _dasdec_english(c, legacy, wrap_all)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()

def _sara_chunks(text, size, mid_word):
    """SARA doesn't crawl: it shows the text one bar-full at a time, no page
    counter, looping until the alert ends. Newer firmware fills the bar to a
    fixed character count and cuts mid-word ("...for th" / "...Message fro");
    older firmware breaks between words."""
    if mid_word:
        out = []
        i = 0
        while i < len(text):
            chunk = text[i:i + size]
            i += size
            chunk = chunk.lstrip()
            if chunk:
                out.append(chunk)
        return out or [""]
    return textwrap.wrap(text, size) or [""]

def _build_sara(cv, c, opts, classic=False, over_page=False):
    """SARA (Scientific-Atlanta / Cisco Explorer set-top firmware) EAS bar.

    From captures: a full-width bar across the top of the picture (~10% of
    the height), white bold sans text with a dark drop shadow starting ~8%
    in from the left, paged rather than scrolled. Modern firmware: bright red
    bar, DASDEC wording; classic (2000s): darker red, condensed font."""
    secs = float(opts.get("sara_secs", 3.0))
    if classic:
        # 2000s firmware: darker red bar set inside title-safe (~10% down).
        font = _font(_F_NARROW, opts.get("font_override"))
        bar, top, h, size, base, chars = "0x8E1B1F", 46, 48, 24, 80, 46
        text = _sara_text(c, legacy=True)
        chunks = _sara_chunks(text, chars, mid_word=False)
    else:
        # Current firmware: bright red bar flush with the top of the picture.
        font = _font(_F_ARIALB, opts.get("font_override"))
        bar, top, h, size, base, chars = "0xE3161B", 0, 50, 22, 36, 48
        text = _sara_text(c, legacy=False)
        chunks = _sara_chunks(text, chars, mid_word=True)
    x = 66
    if over_page:
        # Captured on a cable box tuned to the DASDEC details channel: the
        # box's bar spans the whole 16:9 picture, sits just below the top of
        # the 4:3 page and covers its heading line.
        top, h, base, x = 8, 58, 46, 76
    cv.rect(0, top, cv.base_w, h, bar)
    enables = _page_enables(len(chunks), secs)
    for i, ch in enumerate(chunks):
        cv.text(ch, font, size, base, x=x, enable=enables[i], shadow="0x300000")
    return len(chunks)

def _build_crawl_overlay(cv, c, opts):
    font = _font(_F_ARIALB, opts.get("font_override"))
    cv.rect(0, 20, cv.base_w, 46, "black@0.80")
    txt = (_crawl_translation(c, tz=True) + ("   " + " ".join(c["cap"]) if c["cap"] else ""))
    cv.crawl(txt, font, 30, 54, opts.get("crawl_px", 240), gap=160)
    return 1

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_style_overlay(cid, alert, style, out_w, out_h, opts=None):
    """Build the video side of an alert.

    Returns dict:
      live       -- True: draw over the decoded live program (input 0)
      canvas     -- (w, h, bg) of the generated canvas when not live
      filter     -- filter chain applied to the canvas / scaled program video
      finish     -- chain that brings the canvas to out_w x out_h
      post       -- (optional) set-top graphics drawn over the final output,
                    including any retune lead-in (SARA/DASDEC)
      pages      -- number of pages (1 for crawl styles)
    """
    opts = dict(opts or {})
    style = style if style in STYLE_KEYS else "dasdec"
    if style == "sara_dasdec":
        # DASDEC page (4:3, pillarboxed/stretched) + the set-top's SARA bar
        # drawn afterwards across the full output frame.
        page = build_style_overlay(cid, alert, "dasdec", out_w, out_h, opts)
        bar = _Canvas(cid, out_w, out_h, 854, opts.get("scale", 1.0))
        bar.n = 900                      # keep this canvas's text files distinct
        _build_sara(bar, _ctx(alert, opts), opts, over_page=True)
        # The SARA bar is drawn by the set-top box itself, so it is applied
        # last ("post"): over the channel's freeze and the black retune too,
        # not just over the DASDEC page.
        page["post"] = bar.filter()
        return page
    c = _ctx(alert, opts)
    aspect = opts.get("aspect") or "pillarbox"

    if style in _LIVE_STYLES:
        base_w = 854
        cv = _Canvas(cid, out_w, out_h, base_w, opts.get("scale", 1.0))
        if style == "easyplus_ticker":
            pages = _build_easyplus_ticker(cv, c, opts)
        elif style == "gorman_redlich_overlay":
            pages = _build_gorman(cv, c, opts, keyed=True)
        elif style in ("sara", "sara_classic"):
            pages = _build_sara(cv, c, opts, classic=(style == "sara_classic"))
        else:
            pages = _build_crawl_overlay(cv, c, opts)
        return {"live": True, "canvas": None, "filter": cv.filter(), "finish": "setsar=1",
                "pages": pages}

    # Pages are drawn at no more than 720 lines and scaled up: the hardware
    # being imitated was SD anyway, and drawing ~60 text layers at 1080 cost
    # more than twice the CPU for no visible gain.
    rh = min(out_h, 720)
    if style == "stb_ticker":
        rw = int(round(rh * 16 / 9 / 2)) * 2
        cv = _Canvas(cid, rw, rh, 854, opts.get("scale", 1.0))
        pages = _build_stb(cv, c, opts)
        fin = "setsar=1" if (rw, rh) == (out_w, out_h) else f"scale={out_w}:{out_h}:flags=bicubic,setsar=1"
        return {"live": False, "canvas": (rw, rh, "black"), "filter": cv.filter(),
                "finish": fin, "pages": pages}

    # 4:3 devices: draw the page at 4:3 and pillarbox or stretch it to 16:9.
    cw = int(round(rh * 4 / 3 / 2)) * 2
    cv = _Canvas(cid, cw, rh, 640, opts.get("scale", 1.0))
    if style.startswith("dasdec"):
        bg = _DASDEC_BG[style]
        if opts.get("bg") in _BG_CHOICES and style in ("dasdec", "dasdec_legacy",
                                                       "dasdec_redscroll", "dasdec_blackscroll"):
            bg = _BG_CHOICES[opts["bg"]]
        pages = _build_dasdec(cv, c, style, opts)
    elif style.startswith("easyplus"):
        bg = {"easyplus_gray": "0x7A7A7A", "easyplus_grayblack": "0xC4C4C4"}.get(style, "black")
        pages = _build_easyplus(cv, c, style, opts)
    elif style == "easycap":
        bg = "0x2A22E0"
        pages = _build_easycap(cv, c, opts)
    elif style == "gorman_redlich":
        bg = "0x6A6A6A" if opts.get("bg") == "gray" else "black"
        pages = _build_gorman(cv, c, opts)
    else:  # vds_subalert
        bg = _vds_bg(c)
        pages = _build_vds(cv, c, opts)
    if aspect == "stretch":
        finish = f"scale={out_w}:{out_h}:flags=bicubic,setsar=1"
    else:
        pw = int(round(out_h * 4 / 3 / 2)) * 2
        up = "" if (pw, out_h) == (cw, rh) else f"scale={pw}:{out_h}:flags=bicubic,"
        finish = f"{up}pad={out_w}:{out_h}:({out_w}-iw)/2:0:color=black,setsar=1"
    return {"live": False, "canvas": (cw, rh, bg), "filter": cv.filter(),
            "finish": finish, "pages": pages}
