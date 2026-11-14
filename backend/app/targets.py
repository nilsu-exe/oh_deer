"""Named targets, sexagesimal/decimal coordinate parsing, and SIMBAD/Sesame resolution."""
from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from astropy import units as u
from astropy.coordinates import Angle, SkyCoord

from . import cache
from . import config

log = logging.getLogger(__name__)

_TARGETS_FILE = Path(__file__).with_name("targets.json")

# Strip common prose labels so pasting text from the challenge prompt works out of the box:
# e.g. "Right Ascension 08h55m10.831s, declination -7d14m42.53s"
_LABEL_RE = re.compile(
    r"\b(?:right\s+ascension|declination|ra|dec|icrs|j2000)\b\s*[:=]?\s*",
    re.IGNORECASE,
)
_SEXAGESIMAL_HINT_RE = re.compile(r"[hdms°′″':]", re.IGNORECASE)


def load_targets() -> List[Dict[str, Any]]:
    return json.loads(_TARGETS_FILE.read_text(encoding="utf-8"))


def _norm_key(text: str) -> str:
    return re.sub(r"[\s'’`\-_().]+", "", text.strip().lower())


_ALIASES = {
    "wise0855": "wise0855",
    "wise08550714": "wise0855",
    "wisej085510830714425": "wise0855",
    "barnardsstar": "barnards-star",
    "barnardstar": "barnards-star",
    "gj699": "barnards-star",
    "epsilonindib": "eps-indi-b",
    "epsilonindibabb": "eps-indi-b",
    "epsindib": "eps-indi-b",
    "epsindba": "eps-indi-b",
    "rwaurigae": "rw-aur",
    "rwaur": "rw-aur",
    "rdraconis": "r-dra",
    "rdra": "r-dra",
}


def _find(name: str) -> Optional[Dict[str, Any]]:
    k = _norm_key(name)
    target_id = _ALIASES.get(k, k)
    for t in load_targets():
        if target_id in (
            _norm_key(t.get("id", "")),
            _norm_key(t.get("name", "")),
            _norm_key(t.get("resolve", "")),
        ):
            return t
    return None


def format_sexagesimal(ra_deg: float, dec_deg: float) -> Dict[str, str]:
    """Return canonical sexagesimal strings (`ra_hms`, `dec_dms`) for ICRS degrees."""
    c = SkyCoord(ra=float(ra_deg) * u.deg, dec=float(dec_deg) * u.deg, frame="icrs")
    ra_hms = c.ra.to_string(unit=u.hourangle, sep=("h", "m", "s"), precision=3, pad=True)
    dec_dms = c.dec.to_string(unit=u.deg, sep=("d", "m", "s"), precision=2, alwayssign=True, pad=True)
    return {"ra_hms": str(ra_hms), "dec_dms": str(dec_dms)}


def parse_ra(value: Union[float, int, str]) -> float:
    """Parse an RA value given either in decimal degrees or sexagesimal HMS/DMS."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ra = float(value)
    else:
        s = _LABEL_RE.sub("", str(value)).strip()
        if not s:
            raise ValueError("Empty RA value.")
        try:
            ra = float(s)
        except ValueError:
            # If explicit degree markers are present ("133.79d" or "133d47m..."), use degrees;
            # otherwise default sexagesimal strings ("08h55m10.831s", "08:55:10.831", "08 55 10.831") to hourangle.
            unit = u.deg if re.search(r"[d°]|deg", s, re.IGNORECASE) else u.hourangle
            ra = float(Angle(s, unit=unit).deg)
    if not math.isfinite(ra) or not (0.0 <= ra < 360.0):
        raise ValueError(f"RA {ra} must be in [0, 360) degrees.")
    return ra


def parse_dec(value: Union[float, int, str]) -> float:
    """Parse a Dec value given either in decimal degrees or sexagesimal DMS."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        dec = float(value)
    else:
        s = _LABEL_RE.sub("", str(value)).strip()
        if not s:
            raise ValueError("Empty Dec value.")
        try:
            dec = float(s)
        except ValueError:
            dec = float(Angle(s, unit=u.deg).deg)
    if not math.isfinite(dec) or not (-90.0 <= dec <= 90.0):
        raise ValueError(f"Dec {dec} must be in [-90, 90] degrees.")
    return dec


def parse_coordinates(text: str) -> Optional[Tuple[float, float]]:
    """Try to parse a free-form coordinate string into (ra_deg, dec_deg).

    Supports:
    - Challenge sexagesimal: "08h55m10.831s -7d14m42.53s" or "08h55m10.831s, -7d14m42.53s"
    - Prose-wrapped: "Right Ascension 08h55m10.831s, declination -7d14m42.53s"
    - Colon or space HMS/DMS: "08:55:10.831 -07:14:42.53" or "08 55 10.831 -07 14 42.53"
    - Decimal degree pair: "133.79513, -7.245147" or "133.79513 -7.245147"

    Returns None if `text` looks like an object name rather than coordinates.
    Raises ValueError if `text` is clearly numeric/coordinate-like but out of range.
    """
    if not text or not isinstance(text, str):
        return None
    cleaned = _LABEL_RE.sub(" ", text).strip()
    cleaned = re.sub(r"[;,]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None

    parts = cleaned.split(" ")
    # Case 1: two tokens — either decimal pair ("133.795 -7.245") or compact sexagesimal ("08h55m10.831s -7d14m42.53s")
    if len(parts) == 2:
        p0, p1 = parts
        try:
            ra, dec = float(p0), float(p1)
            if not (0.0 <= ra < 360.0 and -90.0 <= dec <= 90.0):
                raise ValueError(f"Coordinates ({ra}, {dec}) out of range.")
            return ra, dec
        except ValueError as exc:
            if "out of range" in str(exc):
                raise
        if _SEXAGESIMAL_HINT_RE.search(p0) and _SEXAGESIMAL_HINT_RE.search(p1):
            return parse_ra(p0), parse_dec(p1)

    # Case 2: 6 space-separated numeric tokens ("08 55 10.831 -07 14 42.53") or explicit HMS/DMS tokens
    has_coord_markers = bool(re.search(r"\d\s*[hdms°′″:]", cleaned, re.IGNORECASE))
    six_numbers = len(parts) == 6 and all(re.match(r"^[+\-]?\d+(?:\.\d+)?$", p) for p in parts)
    if has_coord_markers or six_numbers:
        ra_unit = u.deg if re.search(r"^\s*[+\-]?\d+(?:\.\d+)?\s*(?:d|deg|°)", cleaned, re.IGNORECASE) else u.hourangle
        try:
            c = SkyCoord(cleaned, unit=(ra_unit, u.deg), frame="icrs")
            return float(c.ra.deg), float(c.dec.deg)
        except Exception:
            return None
    return None


def apply_proper_motion(ra: float, dec: float, pm_ra_masyr: float, pm_dec_masyr: float,
                        from_epoch: float, to_epoch: float) -> Dict[str, float]:
    """Linear proper-motion propagation. pm_ra is mu_alpha* (already includes cos(dec))."""
    dt = to_epoch - from_epoch
    cosd = max(math.cos(math.radians(dec)), 1e-6)
    return {
        "ra": (ra + pm_ra_masyr * dt / 3.6e6 / cosd) % 360.0,
        "dec": dec + pm_dec_masyr * dt / 3.6e6,
    }


def _sesame(name: str) -> Dict[str, float]:
    key = "resolve_" + cache.key_hash(name.lower())
    hit = cache.get_json(key)
    if hit:
        return hit
    c = SkyCoord.from_name(name)
    out = {"ra": float(c.ra.deg), "dec": float(c.dec.deg)}
    cache.set_json(key, out)
    return out


def resolve(name: str, epoch: Optional[float] = None) -> Dict[str, Any]:
    """Resolve a target name or coordinate string (HMS/DMS or decimal) to ICRS degrees.

    SIMBAD positions are J2000. For targets with a known proper motion we
    propagate to `epoch` (default: config.DEFAULT_EPOCH) so the SPHEREx
    footprint query lands on the object, not where it was in 2000.
    """
    t = _find(name)
    if t is None:
        try:
            coords = parse_coordinates(name)
        except ValueError as exc:
            raise LookupError(str(exc)) from exc
        if coords is not None:
            ra_deg, dec_deg = coords
            sex = format_sexagesimal(ra_deg, dec_deg)
            return {
                "name": f"{sex['ra_hms']} {sex['dec_dms']}",
                "ra": ra_deg,
                "dec": dec_deg,
                **sex,
                "pm_applied": False,
                "note": "Parsed directly from coordinate string.",
            }

    try:
        if t and "ra" in t:
            base = {"ra": float(t.get("ra")), "dec": float(t.get("dec"))}
        else:
            base = _sesame(t.get("resolve") if t else name)
    except Exception as exc:
        raise LookupError(f"could not resolve '{name}': {exc}") from exc

    info: Dict[str, Any] = {
        "name": t.get("name") if t else name,
        **base,
        "pm_applied": False,
        "note": (t or {}).get("note"),
    }
    if t and "pm_ra_masyr" in t:
        ep = config.DEFAULT_EPOCH if epoch is None else epoch
        info.update(apply_proper_motion(base.get("ra"), base.get("dec"), t.get("pm_ra_masyr"),
                                        t.get("pm_dec_masyr"), 2000.0, ep))
        info.update(pm_applied=True, epoch=ep)
    info.update(format_sexagesimal(info["ra"], info["dec"]))
    return info
