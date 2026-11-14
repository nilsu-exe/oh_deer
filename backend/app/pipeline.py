"""Orchestration: IRSA query -> cutouts -> cleaned, reprojected frames / spectrum points."""
from __future__ import annotations

import logging
import warnings
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from astropy.time import Time

from . import processing
from . import config
from . import irsa_client
from . import spectral_id

log = logging.getLogger(__name__)


def _subsample(items: List[Dict[str, Any]], max_n: int) -> List[Dict[str, Any]]:
    """Evenly thin a time-ordered list down to max_n (0 = keep all)."""
    if max_n <= 0 or len(items) <= max_n:
        return items
    idx = np.unique(np.linspace(0, len(items) - 1, max_n).round().astype(int))
    return [items[i] for i in idx]


def _iso(mjd: float) -> str:
    return Time(mjd, format="mjd").isot[:19] + "Z"


def _nanmean(stack: np.ndarray) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return np.nanmean(stack, axis=0)


def _nanmax(stack: np.ndarray) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return np.nanmax(stack, axis=0)


def _build_composites_and_candidates(
    frames: List[Dict[str, Any]],
    grid_wcs,
    scale: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Build multi-epoch co-adds ('add together'), epoch difference images
    ('subtract images taken at different times'), and moving/variable candidates.
    """
    if not frames:
        return {
            "coadd_mean_image": None,
            "max_projection_image": None,
            "epoch1_image": None,
            "epoch2_image": None,
            "epoch_diff_image": None,
            "epoch_diff_color_image": None,
            "epoch1_mjd_range": None,
            "epoch2_mjd_range": None,
            "epoch1_n_frames": 0,
            "epoch2_n_frames": 0,
        }, []

    raw_arrays = [f.get("array").copy() for f in frames]
    stack = np.stack(raw_arrays)
    coadd_mean = _nanmean(stack)
    max_proj = _nanmax(stack)
    coadd_png = processing.encode_frames([coadd_mean], stretch="asinh")[0]
    max_png = processing.encode_frames([max_proj], stretch="asinh")[0]

    if len(frames) < 2:
        mjd0 = float(frames[0].get("mjd") or 0.0)
        return {
            "coadd_mean_image": coadd_png,
            "max_projection_image": max_png,
            "epoch1_image": coadd_png,
            "epoch2_image": None,
            "epoch_diff_image": None,
            "epoch_diff_color_image": None,
            "epoch1_mjd_range": [mjd0, mjd0],
            "epoch2_mjd_range": None,
            "epoch1_n_frames": 1,
            "epoch2_n_frames": 0,
        }, []

    ep1_frames, ep2_frames = processing.split_epochs(frames)
    ep1_arr = _nanmean(np.stack([f.get("array") for f in ep1_frames]))
    ep2_arr = _nanmean(np.stack([f.get("array") for f in ep2_frames]))
    ep1_png, ep2_png = processing.encode_frames([ep1_arr, ep2_arr], stretch="asinh")

    diff_arr = ep2_arr - ep1_arr
    diff_gray_png = processing.encode_frames([diff_arr], stretch="linear")[0]
    diff_color_png = processing.encode_diverging_png(diff_arr)

    mjd1_list = [float(f.get("mjd")) for f in ep1_frames if f.get("mjd") is not None]
    mjd2_list = [float(f.get("mjd")) for f in ep2_frames if f.get("mjd") is not None]
    mjd1_mean = float(np.mean(mjd1_list)) if mjd1_list else None
    mjd2_mean = float(np.mean(mjd2_list)) if mjd2_list else None

    candidates = processing.detect_candidates(
        ep1_arr,
        ep2_arr,
        grid_wcs,
        mjd1=mjd1_mean,
        mjd2=mjd2_mean,
        pixel_scale_arcsec=scale,
    )
    composites = {
        "coadd_mean_image": coadd_png,
        "max_projection_image": max_png,
        "epoch1_image": ep1_png,
        "epoch2_image": ep2_png,
        "epoch_diff_image": diff_gray_png,
        "epoch_diff_color_image": diff_color_png,
        "epoch1_mjd_range": [min(mjd1_list), max(mjd1_list)] if mjd1_list else None,
        "epoch2_mjd_range": [min(mjd2_list), max(mjd2_list)] if mjd2_list else None,
        "epoch1_n_frames": len(ep1_frames),
        "epoch2_n_frames": len(ep2_frames),
    }
    return composites, candidates


def _bin_frames(frames: List[Dict[str, Any]], bin_days: float) -> List[Dict[str, Any]]:
    """Average frames that fall into the same time bin (improves S/N)."""
    if bin_days <= 0 or not frames:
        for f in frames:
            f["n_stacked"] = 1
            f["wavelength_range_um"] = [f.get("wavelength_um"), f.get("wavelength_um")]
        return frames
    frames = sorted(frames, key=lambda f: f.get("mjd"))
    t0 = frames[0]["mjd"]
    groups: Dict[int, List[Dict[str, Any]]] = {}
    for f in frames:
        groups.setdefault(int((f.get("mjd") - t0) // bin_days), []).append(f)
    out = []
    for _, g in sorted(groups.items()):
        waves = [f.get("wavelength_um") for f in g if f.get("wavelength_um") is not None]
        out.append({
            "mjd": float(np.mean([f.get("mjd") for f in g])),
            "det": g[0]["det"],
            "wavelength_um": float(np.mean(waves)) if waves else None,
            "wavelength_range_um": [float(min(waves)), float(max(waves))] if waves else [None, None],
            "bandwidth_um": None,
            "version": ",".join(sorted({f.get("version") for f in g if f.get("version")})),
            "flag_fraction": float(np.mean([f.get("flag_fraction") for f in g])),
            "n_stacked": len(g),
            "array": _nanmean(np.stack([f.get("array") for f in g])),
        })
    return out


def build_frames(ra: float, dec: float, fov: float, det: int, *,
                 bin_days: float = 0.0, sky: str = "median", subtract: str = "none",
                 mask: bool = True, scale: float = config.PIXEL_SCALE_ARCSEC,
                 max_frames: int = 200, stretch: str = "auto") -> Dict[str, Any]:
    """Frames for one detector, reprojected onto a common north-up grid.

    Frames are per detector on purpose: each detector covers a different
    wavelength range, and mixing them in one flipbook would make brightness
    changes look like variability or motion.
    """
    images = irsa_client.query_images(ra, dec, det)
    n_found = len(images)
    images = _subsample(images, max_frames)
    fetched = irsa_client.fetch_all(images, ra, dec, fov * config.CUTOUT_PAD)

    grid_wcs, shape = processing.make_grid(ra, dec, fov, scale)
    frames: List[Dict[str, Any]] = []
    failed = 0
    for meta, content in fetched:
        if content is None:
            failed += 1
            continue
        try:
            c = processing.parse_cutout(content, meta)
            img, flag_frac = processing.clean_image(c, sky=sky, mask=mask)
            arr = processing.reproject_to_grid(img, c.wcs, grid_wcs, shape)
            x, y = processing.pixel_of(c, ra, dec)
            wl = processing.sample(c.wave, x, y)
            bw = processing.sample(c.bandwidth, x, y)
        except Exception as exc:
            source = (meta.get("image") or {}).get("irsa")
            log.warning("frame failed (%s): %s", source, exc)
            failed += 1
            continue
        frames.append({
            "mjd": meta.get("mjd"), "det": meta.get("det"),
            "wavelength_um": None if not np.isfinite(wl) else wl,
            "bandwidth_um": None if not np.isfinite(bw) else bw,
            "version": c.version, "flag_fraction": flag_frac, "array": arr,
        })

    frames = _bin_frames(frames, bin_days)
    composites, candidates = _build_composites_and_candidates(frames, grid_wcs, scale)

    signed = subtract in ("median", "first", "prev")
    if len(frames) > 1:
        if subtract == "median":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                med = np.nanmedian(np.stack([f.get("array") for f in frames]), axis=0)
            for f in frames:
                f["array"] = f.get("array") - med
        elif subtract == "first":
            base = frames[0].get("array").copy()
            for f in frames:
                f["array"] = f.get("array") - base
        elif subtract == "prev":
            raw_copy = [f.get("array").copy() for f in frames]
            frames[0]["array"] = raw_copy[0] - raw_copy[1]
            for i in range(1, len(frames)):
                frames[i]["array"] = raw_copy[i] - raw_copy[i - 1]

    if stretch == "auto":
        stretch = "linear" if signed else "asinh"
    pngs = processing.encode_frames([f.get("array") for f in frames], stretch=stretch)

    out_frames = []
    for i, (f, png) in enumerate(zip(frames, pngs)):
        out_frames.append({
            "index": i,
            "mjd": f.get("mjd"),
            "date": _iso(f.get("mjd")),
            "det": f.get("det"),
            "wavelength_um": f.get("wavelength_um"),
            "wavelength_range_um": f.get("wavelength_range_um"),
            "bandwidth_um": f.get("bandwidth_um"),
            "n_stacked": f.get("n_stacked"),
            "version": f.get("version"),
            "flag_fraction": round(f.get("flag_fraction"), 4),
            "image": png,
        })

    n = shape[0]
    return {
        "query": {"ra": ra, "dec": dec, "fov_deg": fov, "det": det, "bin_days": bin_days,
                  "sky": sky, "subtract": subtract, "mask": mask, "stretch": stretch},
        "grid": {"n_pixels": n, "pixel_scale_arcsec": scale,
                 "fov_deg": n * scale / 3600.0,
                 "center": {"ra": ra, "dec": dec},
                 "orientation": "north up, east left", "origin": "top-left"},
        "counts": {"images_found": n_found, "requested": len(images),
                   "failed": failed, "frames_returned": len(out_frames)},
        "frames": out_frames,
        "composites": composites,
        "candidates": candidates,
    }


def build_spectrum(ra: float, dec: float, *, size: float = 0.04, aperture_px: float = 1.5,
                   max_frames: int = 600, dets: Optional[List[int]] = None,
                   z: Optional[float] = None) -> Dict[str, Any]:
    """Quick-look spectrum at (ra, dec) from every exposure on all (or chosen) detectors.

    Each exposure samples the position at a different wavelength (the filters
    vary across the detector), so stacking all exposures from all six arrays
    traces out the 0.75-5 micron spectrum. The result carries an `analysis`
    block (feature identification, redshift, distances); pass `z` to fix the
    redshift instead of estimating it.
    """
    images = irsa_client.query_images(ra, dec, None)
    if dets:
        images = [m for m in images if m.get("det") in dets]
    n_found = len(images)
    images = _subsample(images, max_frames)
    fetched = irsa_client.fetch_all(images, ra, dec, size)

    points, failed = [], 0
    for meta, content in fetched:
        if content is None:
            failed += 1
            continue
        try:
            c = processing.parse_cutout(content, meta)
            p = processing.aperture_point(c, ra, dec, ap=aperture_px)
        except Exception as exc:
            source = (meta.get("image") or {}).get("irsa")
            log.warning("spectrum point failed (%s): %s", source, exc)
            failed += 1
            continue
        if p is not None:
            p["date"] = _iso(p.get("mjd"))
            points.append(p)
    points.sort(key=lambda p: p.get("wavelength_um"))
    try:
        analysis = spectral_id.analyze_spectrum(points, known_z=z)
    except Exception as exc:     # analysis must never break the spectrum itself
        log.exception("spectral analysis failed")
        analysis = {"error": f"analysis failed: {exc}"}
    return {
        "query": {"ra": ra, "dec": dec, "aperture_px": aperture_px, "cutout_size_deg": size},
        "counts": {"images_found": n_found, "requested": len(images),
                   "failed": failed, "points": len(points)},
        "points": points,
        "analysis": analysis,
        "notes": ("flux_jy = aperture sum minus local median background; no aperture correction. "
                  "Quick-look only, not calibrated point-source photometry."),
    }
