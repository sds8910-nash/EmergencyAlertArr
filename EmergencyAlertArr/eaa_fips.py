"""SAME location codes (PSSCCC) <-> place names.

Data: US Census Bureau county and state FIPS codes (2020), public domain,
bundled as data/fips.json. Used to show county names for alerts that arrive
as a bare SAME header (a monitored channel, a relayed test) and to turn NWS
county zone codes (TXC113) into SAME codes for test headers.
"""

import json
import os

__all__ = ["county_name", "describe_codes", "zones_to_same", "state_postal_to_fips", "state_name"]

_DATA = {"loaded": False, "states": {}, "counties": {}, "postal": {}}

# P digit of PSSCCC: which part of the county (47 CFR 11.31(c)).
_PARTS = {"1": "Northwest", "2": "North Central", "3": "Northeast", "4": "West Central",
          "5": "Central", "6": "East Central", "7": "Southwest", "8": "South Central",
          "9": "Southeast"}


def _load():
    if _DATA["loaded"]:
        return
    _DATA["loaded"] = True
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "data", "fips.json"), encoding="utf-8") as f:
            d = json.load(f)
        _DATA["states"] = d.get("states") or {}
        _DATA["counties"] = d.get("counties") or {}
        _DATA["postal"] = {v[1]: k for k, v in _DATA["states"].items()}
    except Exception:
        pass


def state_postal_to_fips(postal):
    _load()
    return _DATA["postal"].get((postal or "").upper())


def state_name(ss, postal=False):
    _load()
    v = _DATA["states"].get(str(ss).zfill(2))
    if not v:
        return ""
    return v[1] if postal else v[0]


def county_name(code):
    """'048113' -> 'Dallas County, TX'; '048000' -> 'All of Texas';
    '348113' -> 'Northeast Dallas County, TX'; '000000' -> 'the United States'."""
    _load()
    c = "".join(ch for ch in str(code) if ch.isdigit()).zfill(6)[:6]
    if c == "000000":
        return "the United States"
    p, ss, ccc = c[0], c[1:3], c[3:]
    st = _DATA["states"].get(ss)
    if not st:
        return c
    if ccc == "000":
        return f"All of {st[0]}"
    name = _DATA["counties"].get(ss + ccc)
    if not name:
        return f"{c} ({st[1]})"
    part = _PARTS.get(p)
    return f"{part + ' ' if part else ''}{name}, {st[1]}"


def describe_codes(codes, limit=31):
    """'Dallas County, TX; Tarrant County, TX' for a list of PSSCCC codes."""
    names = []
    for c in list(codes or [])[:limit]:
        n = county_name(c)
        if n and n not in names:
            names.append(n)
    return "; ".join(names)


def zones_to_same(zones):
    """NWS county zones (e.g. TXC113) -> SAME codes ('048113'). Forecast zones
    (TXZ119) have no county FIPS and are skipped."""
    _load()
    out = []
    for z in zones or []:
        z = (z or "").strip().upper()
        if len(z) == 6 and z[2] == "C" and z[3:].isdigit():
            ss = _DATA["postal"].get(z[:2])
            if ss:
                code = "0" + ss + z[3:]
                if code not in out:
                    out.append(code)
    return out
