"""Astronomical catalog overlays (Gaia DR3 & AllWISE) mapped onto the SPHEREx frame grid.

Supports the NASA Space Apps Challenge consideration:
"Users could benefit from having the option to overlay astronomical data from
other telescopes, such as the Gaia mission or the WISE mission."

For a given `(ra, dec, fov, scale)` patch of sky, this module queries:
1. **Gaia DR3** (optical positions, G/BP/RP magnitudes, proper-motion vectors
   `pm_ra_masyr`, `pm_dec_masyr`, parallax `parallax_mas`, and geometric distance
   in parsecs / light-years), and
2. **AllWISE** (infrared 3.4 µm W1 and 4.6 µm W2 magnitudes and flux densities in mJy,
   which bracket SPHEREx Arrays 4–6),

projects every catalog entry onto the exact PNG pixel grid `(x, y)` of `/api/frames`,
computes proper-motion arrow vectors in PNG pixels/year, and flags infrared-only
sources (WISE detections with no Gaia optical counterpart within 3 arcsec).
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import requests

from . import cache
from . import config
from . import processing
from . import targets

log = logging.getLogger(__name__)

# Standard WISE Vega zero-point flux densities (Wright et al. 2010)
WISE_W1_ZP_JY = 309.540     # 3.3526 µm
WISE_W2_ZP_JY = 171.787     # 4.6028 µm
WISE_W3_ZP_JY = 31.674      # 11.5608 µm
WISE_W4_ZP_JY = 8.363       # 22.0883 µm
LY_PER_PC = 3.2615638

_ESA_GAIA_TAP_SYNC = "https://gea.esac.esa.int/tap-server/tap/sync"


def _num(val: Any, digits: int = 6) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, digits)


def _mag_to_mjy(mag: Optional[float], zp_jy: float) -> Optional[float]:
    if mag is None or not math.isfinite(mag):
        return None
    return round(zp_jy * 1000.0 * (10.0 ** (-0.4 * float(mag))), 4)


# ----------------------------------------------------------------------------
# Raw TAP queries (cached on disk)
# ----------------------------------------------------------------------------

def _query_gaia_irsa(ra: float, dec: float, radius_deg: float, max_sources: int) -> List[Dict[str, Any]]:
    from astroquery.ipac.irsa import Irsa

    adql = f"""
        SELECT TOP {int(max_sources)}
               source_id, ra, dec,
               phot_g_mean_mag, phot_bp_mean_mag, phot_rp_mean_mag, bp_rp,
               pmra, pmdec, parallax
        FROM gaia_dr3_source
        WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra:.6f}, {dec:.6f}, {radius_deg:.6f}))
        ORDER BY phot_g_mean_mag ASC
    """
    tbl = Irsa.query_tap(adql).to_table()
    rows: List[Dict[str, Any]] = []
    for r in tbl:
        rows.append({
            "source_id": str(r.get("source_id")),
            "ra": _num(r.get("ra"), 7),
            "dec": _num(r.get("dec"), 7),
            "g_mag": _num(r.get("phot_g_mean_mag"), 3),
            "bp_mag": _num(r.get("phot_bp_mean_mag"), 3),
            "rp_mag": _num(r.get("phot_rp_mean_mag"), 3),
            "bp_rp": _num(r.get("bp_rp"), 3),
            "pm_ra_masyr": _num(r.get("pmra"), 3),
            "pm_dec_masyr": _num(r.get("pmdec"), 3),
            "parallax_mas": _num(r.get("parallax"), 3),
        })
    return rows


def _query_gaia_esa(ra: float, dec: float, radius_deg: float, max_sources: int) -> List[Dict[str, Any]]:
    adql = f"""
        SELECT TOP {int(max_sources)}
               source_id, ra, dec,
               phot_g_mean_mag, phot_bp_mean_mag, phot_rp_mean_mag, bp_rp,
               pmra, pmdec, parallax
        FROM gaiadr3.gaia_source
        WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra:.6f}, {dec:.6f}, {radius_deg:.6f}))
        ORDER BY phot_g_mean_mag ASC
    """
    resp = requests.get(
        _ESA_GAIA_TAP_SYNC,
        params={"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "json", "QUERY": adql},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    cols = [c["name"] for c in payload.get("metadata", [])]
    rows: List[Dict[str, Any]] = []
    for item in payload.get("data", []):
        rec = dict(zip(cols, item))
        rows.append({
            "source_id": str(rec.get("source_id")),
            "ra": _num(rec.get("ra"), 7),
            "dec": _num(rec.get("dec"), 7),
            "g_mag": _num(rec.get("phot_g_mean_mag"), 3),
            "bp_mag": _num(rec.get("phot_bp_mean_mag"), 3),
            "rp_mag": _num(rec.get("phot_rp_mean_mag"), 3),
            "bp_rp": _num(rec.get("bp_rp"), 3),
            "pm_ra_masyr": _num(rec.get("pmra"), 3),
            "pm_dec_masyr": _num(rec.get("pmdec"), 3),
            "parallax_mas": _num(rec.get("parallax"), 3),
        })
    return rows


def fetch_gaia_sources(ra: float, dec: float, radius_deg: float, max_sources: int = 150) -> List[Dict[str, Any]]:
    """Query Gaia DR3 within `radius_deg` of `(ra, dec)` with disk caching."""
    key = f"gaia_dr3_{round(ra, 4)}_{round(dec, 4)}_{round(radius_deg, 4)}_{int(max_sources)}"
    cached = cache.get_json(key)
    if cached is not None:
        return cached

    rows: List[Dict[str, Any]] = []
    try:
        rows = _query_gaia_irsa(ra, dec, radius_deg, max_sources)
    except Exception as exc_irsa:
        log.info("IRSA Gaia DR3 TAP failed (%s); trying ESA Gaia TAP", exc_irsa)
        try:
            rows = _query_gaia_esa(ra, dec, radius_deg, max_sources)
        except Exception as exc_esa:
            log.warning("ESA Gaia DR3 TAP also failed: %s", exc_esa)
            return []

    cache.set_json(key, rows)
    return rows


def fetch_wise_sources(ra: float, dec: float, radius_deg: float, max_sources: int = 150) -> List[Dict[str, Any]]:
    """Query IRSA AllWISE Source Catalog (`allwise_p3as_psd`) within `radius_deg`."""
    key = f"wise_allwise_{round(ra, 4)}_{round(dec, 4)}_{round(radius_deg, 4)}_{int(max_sources)}"
    cached = cache.get_json(key)
    if cached is not None:
        return cached

    try:
        from astroquery.ipac.irsa import Irsa

        adql = f"""
            SELECT TOP {int(max_sources)}
                   designation, ra, dec,
                   w1mpro, w2mpro, w3mpro, w4mpro,
                   w1snr, w2snr, pmra, pmdec
            FROM allwise_p3as_psd
            WHERE 1 = CONTAINS(POINT('ICRS', ra, dec), CIRCLE('ICRS', {ra:.6f}, {dec:.6f}, {radius_deg:.6f}))
            ORDER BY w2mpro ASC
        """
        tbl = Irsa.query_tap(adql).to_table()
        rows: List[Dict[str, Any]] = []
        for r in tbl:
            rows.append({
                "designation": str(r.get("designation")),
                "ra": _num(r.get("ra"), 7),
                "dec": _num(r.get("dec"), 7),
                "w1_mag": _num(r.get("w1mpro"), 3),
                "w2_mag": _num(r.get("w2mpro"), 3),
                "w3_mag": _num(r.get("w3mpro"), 3),
                "w4_mag": _num(r.get("w4mpro"), 3),
                "w1_snr": _num(r.get("w1snr"), 1),
                "w2_snr": _num(r.get("w2snr"), 1),
                "pm_ra_masyr": _num(r.get("pmra"), 2),
                "pm_dec_masyr": _num(r.get("pmdec"), 2),
            })
        cache.set_json(key, rows)
        return rows
    except Exception as exc:
        log.warning("IRSA AllWISE TAP query failed: %s", exc)
        return []


# ----------------------------------------------------------------------------
# Projection, kinematics, and Gaia-WISE cross-matching
# ----------------------------------------------------------------------------

def enrich_gaia_sources(
    raw_gaia: Iterable[Dict[str, Any]],
    grid_wcs,
    shape: Sequence[int],
    *,
    epoch: float = config.DEFAULT_EPOCH,
    scale_arcsec: float = config.PIXEL_SCALE_ARCSEC,
) -> List[Dict[str, Any]]:
    """Project Gaia DR3 entries onto the PNG frame and compute distance & motion vectors."""
    ny, nx = int(shape[0]), int(shape[1])
    out: List[Dict[str, Any]] = []
    for g in raw_gaia or []:
        ra0, dec0 = g.get("ra"), g.get("dec")
        if ra0 is None or dec0 is None:
            continue
        pm_ra = g.get("pm_ra_masyr")
        pm_dec = g.get("pm_dec_masyr")
        # Gaia DR3 reference epoch is J2016.0
        if pm_ra is not None and pm_dec is not None and epoch is not None:
            pos = targets.apply_proper_motion(ra0, dec0, pm_ra, pm_dec, 2016.0, float(epoch))
            ra_ep, dec_ep = pos["ra"], pos["dec"]
        else:
            ra_ep, dec_ep = float(ra0), float(dec0)

        col, row = grid_wcs.wcs_world2pix([[ra_ep, dec_ep]], 0)[0]
        x = float(col)
        y = float(ny - 1 - row)
        if not (-0.5 <= x <= nx - 0.5 and -0.5 <= y <= ny - 0.5):
            continue

        total_pm_masyr = None
        total_pm_arcsec_yr = None
        dx_px_yr = 0.0
        dy_px_yr = 0.0
        if pm_ra is not None and pm_dec is not None:
            total_pm_masyr = math.hypot(pm_ra, pm_dec)
            total_pm_arcsec_yr = total_pm_masyr / 1000.0
            # East is left (-x in PNG), North is up (-y in PNG)
            dx_px_yr = -(pm_ra / 1000.0) / scale_arcsec
            dy_px_yr = -(pm_dec / 1000.0) / scale_arcsec

        plx = g.get("parallax_mas")
        dist_pc = None
        dist_ly = None
        v_tan_kms = None
        if plx is not None and plx > 0.1:
            dist_pc = 1000.0 / plx
            dist_ly = dist_pc * LY_PER_PC
            if total_pm_arcsec_yr is not None:
                v_tan_kms = 4.74047 * total_pm_arcsec_yr * dist_pc

        out.append({
            "catalog": "Gaia DR3",
            "id": f"Gaia DR3 {g.get('source_id')}",
            "source_id": g.get("source_id"),
            "ra": _num(ra_ep, 6),
            "dec": _num(dec_ep, 6),
            "ra_j2016": _num(ra0, 6),
            "dec_j2016": _num(dec0, 6),
            "x": _num(x, 2),
            "y": _num(y, 2),
            "g_mag": g.get("g_mag"),
            "bp_mag": g.get("bp_mag"),
            "rp_mag": g.get("rp_mag"),
            "bp_rp": g.get("bp_rp"),
            "pm_ra_masyr": pm_ra,
            "pm_dec_masyr": pm_dec,
            "total_pm_arcsec_yr": _num(total_pm_arcsec_yr, 4),
            "dx_px_per_yr": _num(dx_px_yr, 4),
            "dy_px_per_yr": _num(dy_px_yr, 4),
            "parallax_mas": plx,
            "distance_pc": _num(dist_pc, 2),
            "distance_ly": _num(dist_ly, 2),
            "tangential_velocity_kms": _num(v_tan_kms, 1),
            "is_high_proper_motion": bool(total_pm_arcsec_yr and total_pm_arcsec_yr >= 0.1),
        })
    return out


def enrich_wise_sources(
    raw_wise: Iterable[Dict[str, Any]],
    gaia_sources: Sequence[Dict[str, Any]],
    grid_wcs,
    shape: Sequence[int],
    *,
    match_radius_arcsec: float = 3.0,
) -> List[Dict[str, Any]]:
    """Project AllWISE entries onto the PNG frame, compute mJy fluxes, and cross-match with Gaia."""
    ny, nx = int(shape[0]), int(shape[1])
    out: List[Dict[str, Any]] = []
    for w in raw_wise or []:
        ra_w, dec_w = w.get("ra"), w.get("dec")
        if ra_w is None or dec_w is None:
            continue
        col, row = grid_wcs.wcs_world2pix([[ra_w, dec_w]], 0)[0]
        x = float(col)
        y = float(ny - 1 - row)
        if not (-0.5 <= x <= nx - 0.5 and -0.5 <= y <= ny - 0.5):
            continue

        w1 = w.get("w1_mag")
        w2 = w.get("w2_mag")
        w3 = w.get("w3_mag")
        w4 = w.get("w4_mag")
        w1_w2 = (w1 - w2) if (w1 is not None and w2 is not None) else None

        # Cross-match against Gaia sources (both at J2016 and propagated positions)
        cosd = max(math.cos(math.radians(dec_w)), 1e-6)
        matched_gaia_id = None
        best_sep = match_radius_arcsec
        for g in gaia_sources:
            for gra, gdec in ((g["ra"], g["dec"]), (g["ra_j2016"], g["dec_j2016"])):
                if gra is None or gdec is None:
                    continue
                sep = math.hypot((ra_w - gra) * cosd, dec_w - gdec) * 3600.0
                if sep <= best_sep:
                    best_sep = sep
                    matched_gaia_id = g["id"]

        desig = w.get("designation") or ""
        out.append({
            "catalog": "AllWISE",
            "id": f"WISE {desig}".strip(),
            "designation": desig,
            "ra": _num(ra_w, 6),
            "dec": _num(dec_w, 6),
            "x": _num(x, 2),
            "y": _num(y, 2),
            "w1_mag": w1,
            "w2_mag": w2,
            "w3_mag": w3,
            "w4_mag": w4,
            "w1_w2_color": _num(w1_w2, 3),
            "w1_flux_mjy": _mag_to_mjy(w1, WISE_W1_ZP_JY),
            "w2_flux_mjy": _mag_to_mjy(w2, WISE_W2_ZP_JY),
            "w3_flux_mjy": _mag_to_mjy(w3, WISE_W3_ZP_JY),
            "w4_flux_mjy": _mag_to_mjy(w4, WISE_W4_ZP_JY),
            "gaia_match": matched_gaia_id is not None,
            "gaia_source_id": matched_gaia_id,
            "is_ir_excess_or_substellar": bool(
                matched_gaia_id is None or (w1_w2 is not None and w1_w2 >= 0.8)
            ),
        })
    return out


def build_overlays(
    ra: float,
    dec: float,
    fov: float = 0.1,
    *,
    scale: float = config.PIXEL_SCALE_ARCSEC,
    epoch: Optional[float] = None,
    catalogs: Sequence[str] = ("gaia", "wise"),
    max_sources: int = 150,
) -> Dict[str, Any]:
    """Fetch and project Gaia DR3 and AllWISE catalog overlays for a sky patch."""
    ep = config.DEFAULT_EPOCH if epoch is None else float(epoch)
    grid_wcs, shape = processing.make_grid(ra, dec, fov, scale)
    n = int(shape[0])
    # Half-diagonal radius so corners of the square FOV are covered
    radius_deg = min(1.0, 0.5 * fov * math.sqrt(2.0) * 1.05)

    cats = {c.strip().lower() for c in catalogs}
    raw_gaia = fetch_gaia_sources(ra, dec, radius_deg, max_sources=max_sources) if "gaia" in cats else []
    raw_wise = fetch_wise_sources(ra, dec, radius_deg, max_sources=max_sources) if "wise" in cats else []

    gaia = enrich_gaia_sources(raw_gaia, grid_wcs, shape, epoch=ep, scale_arcsec=scale)
    wise = enrich_wise_sources(raw_wise, gaia, grid_wcs, shape)

    ir_only = [w for w in wise if not w["gaia_match"]]
    high_pm = [g for g in gaia if g["is_high_proper_motion"]]

    return {
        "query": {
            "ra": ra,
            "dec": dec,
            "fov_deg": fov,
            "scale_arcsec": scale,
            "epoch": ep,
            "catalogs": sorted(cats),
        },
        "grid": {
            "n_pixels": n,
            "pixel_scale_arcsec": scale,
            "fov_deg": n * scale / 3600.0,
            "center": {"ra": ra, "dec": dec},
            "orientation": "north up, east left",
            "origin": "top-left",
        },
        "counts": {
            "gaia_dr3": len(gaia),
            "wise": len(wise),
            "high_proper_motion": len(high_pm),
            "ir_only_candidates": len(ir_only),
        },
        "gaia": gaia,
        "wise": wise,
        "high_proper_motion": high_pm,
        "ir_only_candidates": ir_only,
        "notes": (
            "Pixel coordinates (x, y) match /api/frames (origin top-left). "
            "dx_px_per_yr and dy_px_per_yr give the proper-motion vector in PNG pixels/year. "
            "ir_only_candidates are WISE infrared detections with no Gaia DR3 optical counterpart within 3 arcsec."
        ),
    }
