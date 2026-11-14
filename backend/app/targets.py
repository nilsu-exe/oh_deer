"""Named targets: the five challenge test targets plus free-text name resolution (SIMBAD/Sesame)."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import cache, config

log = logging.getLogger(__name__)

_TARGETS_FILE = Path(__file__).with_name("targets.json")


def load_targets() -> List[Dict[str, Any]]:
    return json.loads(_TARGETS_FILE.read_text())


def _find(name: str) -> Optional[Dict[str, Any]]:
    n = name.strip().lower()
    for t in load_targets():
        if n in (t["id"].lower(), t["name"].lower()):
            return t
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
    from astropy.coordinates import SkyCoord
    c = SkyCoord.from_name(name)
    out = {"ra": float(c.ra.deg), "dec": float(c.dec.deg)}
    cache.set_json(key, out)
    return out


def resolve(name: str, epoch: Optional[float] = None) -> Dict[str, Any]:
    """Resolve a name to ICRS degrees. Raises LookupError if it cannot be resolved.

    SIMBAD positions are J2000. For targets with a known proper motion we
    propagate to `epoch` (default: config.DEFAULT_EPOCH) so the SPHEREx
    footprint query lands on the object, not where it was in 2000.
    """
    t = _find(name)
    try:
        if t and "ra" in t:
            base = {"ra": t["ra"], "dec": t["dec"]}
        else:
            base = _sesame(t["resolve"] if t else name)
    except Exception as exc:
        raise LookupError(f"could not resolve '{name}': {exc}") from exc

    info: Dict[str, Any] = {"name": t["name"] if t else name, **base,
                            "pm_applied": False, "note": (t or {}).get("note")}
    if t and "pm_ra_masyr" in t:
        ep = config.DEFAULT_EPOCH if epoch is None else epoch
        info.update(apply_proper_motion(base["ra"], base["dec"], t["pm_ra_masyr"],
                                        t["pm_dec_masyr"], 2000.0, ep))
        info.update(pm_applied=True, epoch=ep)
    return info
