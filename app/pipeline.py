"""Orchestration: IRSA query -> cutouts -> cleaned, reprojected frames / spectrum points."""
from __future__ import annotations

import logging
import warnings
from typing import Any, Dict, List, Optional

import numpy as np
from astropy.time import Time

from . import config, irsa_client, processing

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


def _bin_frames(frames: List[Dict[str, Any]], bin_days: float) -> List[Dict[str, Any]]:
    """Average frames that fall into the same time bin (improves S/N)."""
    if bin_days <= 0 or not frames:
        for f in frames:
            f["n_stacked"] = 1
            f["wavelength_range_um"] = [f["wavelength_um"], f["wavelength_um"]]
        return frames
    frames = sorted(frames, key=lambda f: f["mjd"])
    t0 = frames[0]["mjd"]
    groups: Dict[int, List[Dict[str, Any]]] = {}
    for f in frames:
        groups.setdefault(int((f["mjd"] - t0) // bin_days), []).append(f)
    out = []
    for _, g in sorted(groups.items()):
        waves = [f["wavelength_um"] for f in g if f["wavelength_um"] is not None]
        out.append({
            "mjd": float(np.mean([f["mjd"] for f in g])),
            "det": g[0]["det"],
            "wavelength_um": float(np.mean(waves)) if waves else None,
            "wavelength_range_um": [float(min(waves)), float(max(waves))] if waves else [None, None],
            "bandwidth_um": None,
            "version": ",".join(sorted({f["version"] for f in g if f["version"]})),
            "flag_fraction": float(np.mean([f["flag_fraction"] for f in g])),
            "n_stacked": len(g),
            "array": _nanmean(np.stack([f["array"] for f in g])),
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
            log.warning("frame failed (%s): %s", meta.get("base_url"), exc)
            failed += 1
            continue
        frames.append({
            "mjd": meta["mjd"], "det": meta.get("det"),
            "wavelength_um": None if not np.isfinite(wl) else wl,
            "bandwidth_um": None if not np.isfinite(bw) else bw,
            "version": c.version, "flag_fraction": flag_frac, "array": arr,
        })

    frames = _bin_frames(frames, bin_days)

    signed = subtract == "median"
    if subtract == "median" and len(frames) > 1:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            med = np.nanmedian(np.stack([f["array"] for f in frames]), axis=0)
        for f in frames:
            f["array"] = f["array"] - med

    if stretch == "auto":
        stretch = "linear" if signed else "asinh"
    pngs = processing.encode_frames([f["array"] for f in frames], stretch=stretch)

    out_frames = []
    for i, (f, png) in enumerate(zip(frames, pngs)):
        out_frames.append({
            "index": i,
            "mjd": f["mjd"],
            "date": _iso(f["mjd"]),
            "det": f["det"],
            "wavelength_um": f["wavelength_um"],
            "wavelength_range_um": f["wavelength_range_um"],
            "bandwidth_um": f["bandwidth_um"],
            "n_stacked": f["n_stacked"],
            "version": f["version"],
            "flag_fraction": round(f["flag_fraction"], 4),
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
    }


def build_spectrum(ra: float, dec: float, *, size: float = 0.04, aperture_px: float = 1.5,
                   max_frames: int = 600, dets: Optional[List[int]] = None) -> Dict[str, Any]:
    """Quick-look spectrum at (ra, dec) from every exposure on all (or chosen) detectors.

    Each exposure samples the position at a different wavelength (the filters
    vary across the detector), so stacking all exposures from all six arrays
    traces out the 0.75-5 micron spectrum.
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
            log.warning("spectrum point failed (%s): %s", meta.get("base_url"), exc)
            failed += 1
            continue
        if p is not None:
            p["date"] = _iso(p["mjd"])
            points.append(p)
    points.sort(key=lambda p: p["wavelength_um"])
    return {
        "query": {"ra": ra, "dec": dec, "aperture_px": aperture_px, "cutout_size_deg": size},
        "counts": {"images_found": n_found, "requested": len(images),
                   "failed": failed, "points": len(points)},
        "points": points,
        "notes": ("flux_jy = aperture sum minus local median background; no aperture correction. "
                  "Quick-look only, not calibrated point-source photometry."),
    }
