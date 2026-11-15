"""Orchestration: IRSA query -> cutouts -> cleaned, reprojected frames / spectrum points."""
from __future__ import annotations

import logging
import warnings
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from astropy.time import Time

from . import config, diagnostics, irsa_client, processing, spectral_id
from .render_cache import cached_render

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


def _merge_flag_breakdowns(group: List[Dict[str, Any]]) -> Dict[str, float]:
    """Average per-bit flag fractions across a group of stacked frames."""
    if not group:
        return {}
    keys = sorted({k for f in group for k in (f.get("flags_breakdown") or {}).keys()})
    n = float(len(group))
    return {
        k: round(sum(float((f.get("flags_breakdown") or {}).get(k, 0.0)) for f in group) / n, 4)
        for k in keys
    }


def _bin_frames(frames: List[Dict[str, Any]], bin_days: float) -> List[Dict[str, Any]]:
    """Average frames that fall into the same time bin (improves S/N)."""
    if bin_days <= 0 or not frames:
        for f in frames:
            f["n_stacked"] = 1
            f["wavelength_range_um"] = [f.get("wavelength_um"), f.get("wavelength_um")]
            f.setdefault("flags_breakdown", {})
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
            "flags_breakdown": _merge_flag_breakdowns(g),
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
    with diagnostics.stage(log, "discovery", f"ra={ra:.6f} dec={dec:.6f} detector={det}"):
        images = irsa_client.query_images(ra, dec, det)
    discovery = getattr(images, "discovery", None)
    result = _render_frames(
        list(images), ra, dec, fov, det, bin_days=bin_days, sky=sky,
        subtract=subtract, mask=mask, scale=scale, max_frames=max_frames, stretch=stretch,
    )
    result["discovery"] = discovery
    return result


@cached_render
def _render_frames(images, ra, dec, fov, det, *, bin_days, sky, subtract,
                   mask, scale, max_frames, stretch):
    n_found = len(images)
    images = _subsample(images, max_frames)
    log.info("frames selected found=%d requested=%d max_frames=%d", n_found, len(images), max_frames)
    with diagnostics.stage(log, "downloads", f"requested={len(images)}"):
        fetched = irsa_client.fetch_all(images, ra, dec, fov * config.CUTOUT_PAD)

    grid_wcs, shape = processing.make_grid(ra, dec, fov, scale)
    frames: List[Dict[str, Any]] = []
    failed = 0
    with diagnostics.stage(log, "frame processing", f"cutouts={len(fetched)} grid={shape}"):
        for index, (meta, content) in enumerate(fetched, 1):
            diagnostics.set_progress(f"frame={index}/{len(fetched)} step=FITS_parse failed={failed}")
            if index == 1 or index % 10 == 0:
                log.info("frame processing progress frame=%d/%d usable=%d failed=%d",
                         index, len(fetched), len(frames), failed)
            if content is None:
                failed += 1
                continue
            try:
                c = processing.parse_cutout(content, meta)
                img, flag_frac = processing.clean_image(c, sky=sky, mask=mask)
                flag_breakdown = processing.summarize_flags(c.flags)
                diagnostics.set_progress(f"frame={index}/{len(fetched)} step=reprojection failed={failed}")
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
                "version": c.version,
                "flag_fraction": flag_frac,
                "flags_breakdown": flag_breakdown,
                "array": arr,
            })

    log.info("frame processing summary usable=%d failed=%d", len(frames), failed)

    with diagnostics.stage(log, "stacking and PNG encoding", f"frames={len(frames)} bin_days={bin_days} subtract={subtract}"):
        frames = _bin_frames(frames, bin_days)
        composites, candidates = _build_composites_and_candidates(frames, grid_wcs, scale)

        signed = subtract in ("median", "first", "prev")
        if subtract == "median" and len(frames) > 1:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                med = np.nanmedian(np.stack([f["array"] for f in frames]), axis=0)
            for f in frames:
                f["array"] = f["array"] - med
        elif subtract == "first" and len(frames) > 1:
            base = frames[0]["array"].copy()
            for f in frames:
                f["array"] = f["array"] - base
        elif subtract == "prev" and len(frames) > 1:
            raw_copy = [f["array"].copy() for f in frames]
            frames[0]["array"] = raw_copy[0] - raw_copy[1]
            for i in range(1, len(frames)):
                frames[i]["array"] = raw_copy[i] - raw_copy[i - 1]

        if stretch == "auto":
            stretch = "linear" if signed else "asinh"
        pngs = processing.encode_frames([f["array"] for f in frames], stretch=stretch)

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
            "flags_breakdown": f.get("flags_breakdown") or {},
            "image": png,
        })

    log.info("frames response ready returned=%d requested=%d failed=%d", len(out_frames), len(images), failed)
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
    with diagnostics.stage(log, "discovery", f"ra={ra:.6f} dec={dec:.6f} detectors=all"):
        images = irsa_client.query_selected_images(ra, dec, dets)
    discovery = getattr(images, "discovery", None)
    if dets:
        images = [m for m in images if m.get("det") in dets]
    result = _render_spectrum(
        list(images), ra, dec, size=size, aperture_px=aperture_px,
        max_frames=max_frames, z=z,
    )
    result["discovery"] = discovery
    return result


@cached_render
def _render_spectrum(images, ra, dec, *, size, aperture_px, max_frames, z):
    n_found = len(images)
    images = _subsample(images, max_frames)
    log.info("spectrum selected found=%d requested=%d max_frames=%d", n_found, len(images), max_frames)
    with diagnostics.stage(log, "downloads", f"requested={len(images)}"):
        fetched = irsa_client.fetch_all(images, ra, dec, size, aperture_only=True)

    points, failed = [], 0
    with diagnostics.stage(log, "spectrum processing", f"cutouts={len(fetched)} aperture={aperture_px}"):
        for index, (meta, content) in enumerate(fetched, 1):
            diagnostics.set_progress(f"exposure={index}/{len(fetched)} step=FITS_parse failed={failed}")
            if index == 1 or index % 10 == 0:
                log.info("spectrum processing progress exposure=%d/%d points=%d failed=%d",
                         index, len(fetched), len(points), failed)
            if content is None:
                failed += 1
                continue
            try:
                c = processing.parse_cutout(content, meta)
                diagnostics.set_progress(f"exposure={index}/{len(fetched)} step=photometry failed={failed}")
                p = processing.aperture_point(c, ra, dec, ap=aperture_px)
            except Exception as exc:
                log.warning("spectrum point failed (%s): %s", meta.get("base_url"), exc)
                failed += 1
                continue
            if p is not None:
                p["date"] = _iso(p["mjd"])
                points.append(p)
    points.sort(key=lambda p: p["wavelength_um"])
    with diagnostics.stage(log, "spectral analysis", f"points={len(points)} z={z}"):
        try:
            analysis = spectral_id.analyze_spectrum(points, known_z=z)
        except Exception as exc:     # analysis must never break the spectrum itself
            log.exception("spectral analysis failed")
            analysis = {"error": f"analysis failed: {exc}"}
    log.info("spectrum response ready points=%d requested=%d failed=%d", len(points), len(images), failed)
    return {
        "query": {"ra": ra, "dec": dec, "aperture_px": aperture_px, "cutout_size_deg": size},
        "counts": {"images_found": n_found, "requested": len(images),
                   "failed": failed, "points": len(points)},
        "points": points,
        "analysis": analysis,
        "notes": ("flux_jy = aperture sum minus local median background; no aperture correction. "
                  "Quick-look only, not calibrated point-source photometry."),
    }


DETECTOR_BANDS: Dict[int, Tuple[float, float, str]] = {
    1: (0.75, 1.09, "Short-Wave NIR (0.75–1.09 µm)"),
    2: (1.10, 1.62, "J/H-Band NIR (1.10–1.62 µm)"),
    3: (1.63, 2.41, "H/K-Band NIR (1.63–2.41 µm)"),
    4: (2.42, 3.82, "H2O Ice / PAH / CH4 Band (2.42–3.82 µm)"),
    5: (3.83, 4.41, "CO2 Ice / Thermal NIR (3.83–4.41 µm)"),
    6: (4.42, 5.00, "CO / Mid-IR Thermal (4.42–5.00 µm)"),
}


def build_detector_stacks(
    ra: float,
    dec: float,
    fov: float = 0.1,
    *,
    dets: Optional[List[int]] = None,
    sky: str = "median",
    mask: bool = True,
    scale: float = config.PIXEL_SCALE_ARCSEC,
    max_per_det: int = 30,
    stretch: str = "asinh",
) -> Dict[str, Any]:
    """Separately co-add all exposures for each of the 6 SPHEREx detector arrays (D1..D6),
    and combine them into an All-Detector Deep Stack + False-Color Infrared RGB composite.
    """
    target_dets = sorted(set(dets)) if dets else [1, 2, 3, 4, 5, 6]
    with diagnostics.stage(log, "discovery", f"ra={ra:.6f} dec={dec:.6f} detectors={target_dets}"):
        all_images = irsa_client.query_selected_images(ra, dec, target_dets)
    discovery = getattr(all_images, "discovery", None)
    result = _render_detector_stacks(
        list(all_images), ra, dec, fov, target_dets=target_dets, sky=sky,
        mask=mask, scale=scale, max_per_det=max_per_det, stretch=stretch,
    )
    result["discovery"] = discovery
    return result


@cached_render
def _render_detector_stacks(all_images, ra, dec, fov, *, target_dets, sky,
                            mask, scale, max_per_det, stretch):
    n_found = len(all_images)

    by_det: Dict[int, List[Dict[str, Any]]] = {d: [] for d in target_dets}
    for m in all_images:
        d = m.get("det")
        if d in by_det:
            by_det[d].append(m)

    selected: List[Dict[str, Any]] = []
    for d in target_dets:
        selected.extend(_subsample(by_det[d], max_per_det))

    with diagnostics.stage(log, "downloads", f"requested={len(selected)}"):
        fetched = irsa_client.fetch_all(selected, ra, dec, fov * config.CUTOUT_PAD)

    grid_wcs, shape = processing.make_grid(ra, dec, fov, scale)
    per_det_frames: Dict[int, List[Dict[str, Any]]] = {d: [] for d in target_dets}
    failed = 0

    with diagnostics.stage(log, "detector stacking", f"cutouts={len(fetched)} grid={shape}"):
        for meta, content in fetched:
            if content is None:
                failed += 1
                continue
            d = meta.get("det")
            if d not in per_det_frames:
                continue
            try:
                c = processing.parse_cutout(content, meta)
                img, flag_frac = processing.clean_image(c, sky=sky, mask=mask)
                flag_breakdown = processing.summarize_flags(c.flags)
                arr = processing.reproject_to_grid(img, c.wcs, grid_wcs, shape)
            except Exception as exc:
                log.warning("detector frame failed (%s): %s", meta.get("base_url"), exc)
                failed += 1
                continue
            per_det_frames[d].append({
                "mjd": meta.get("mjd"),
                "flag_fraction": flag_frac,
                "flags_breakdown": flag_breakdown,
                "array": arr,
            })

    det_summaries: List[Dict[str, Any]] = []
    det_coadds: Dict[int, np.ndarray] = {}
    det_diffs: List[np.ndarray] = []
    for d in target_dets:
        lo_um, hi_um, label = DETECTOR_BANDS.get(d, (0.75, 5.0, f"Detector {d}"))
        flist = per_det_frames[d]
        if not flist:
            det_summaries.append({
                "det": d,
                "band_label": label,
                "wavelength_range_um": [lo_um, hi_um],
                "n_found": len(by_det[d]),
                "n_stacked": 0,
                "mjd_range": None,
                "mean_flag_fraction": None,
                "flags_breakdown": {},
                "coadd_image": None,
                "max_projection_image": None,
                "epoch_diff_color_image": None,
            })
            continue
        stack = np.stack([f["array"] for f in flist])
        coadd_arr = _nanmean(stack)
        max_arr = _nanmax(stack)
        det_coadds[d] = coadd_arr
        coadd_png = processing.encode_frames([coadd_arr], stretch=stretch)[0]
        max_png = processing.encode_frames([max_arr], stretch=stretch)[0]
        diff_color_png = None
        if len(flist) >= 2:
            ep1_f, ep2_f = processing.split_epochs(flist)
            if ep1_f and ep2_f:
                ep1_a = _nanmean(np.stack([f["array"] for f in ep1_f]))
                ep2_a = _nanmean(np.stack([f["array"] for f in ep2_f]))
                d_arr = ep2_a - ep1_a
                diff_color_png = processing.encode_diverging_png(d_arr)
                finite_d = d_arr[np.isfinite(d_arr)]
                if finite_d.size > 0:
                    sc = float(np.percentile(np.abs(finite_d - np.median(finite_d)), 99.0)) or 1.0
                    det_diffs.append((d_arr - float(np.median(finite_d))) / sc)
        mjds = [float(f["mjd"]) for f in flist if f.get("mjd") is not None]
        det_summaries.append({
            "det": d,
            "band_label": label,
            "wavelength_range_um": [lo_um, hi_um],
            "n_found": len(by_det[d]),
            "n_stacked": len(flist),
            "mjd_range": [min(mjds), max(mjds)] if mjds else None,
            "mean_flag_fraction": round(float(np.mean([f["flag_fraction"] for f in flist])), 4),
            "flags_breakdown": _merge_flag_breakdowns(flist),
            "coadd_image": coadd_png,
            "max_projection_image": max_png,
            "epoch_diff_color_image": diff_color_png,
        })

    # Combine normalized per-detector stacks into an all-detector deep white-light image
    all_coadd_png = None
    if det_coadds:
        norm_stacks = [processing.normalize_array(arr, stretch="linear") for arr in det_coadds.values()]
        deep_arr = _nanmean(np.stack(norm_stacks))
        all_coadd_png = processing.encode_frames([deep_arr], stretch=stretch)[0]

    epoch_diff_all_png = None
    if det_diffs:
        epoch_diff_all_png = processing.encode_diverging_png(_nanmean(np.stack(det_diffs)))

    # Build False-Color Infrared RGB composite:
    # Blue = D1/D2 (0.75-1.62 um), Green = D3/D4 (1.63-3.82 um), Red = D5/D6 (3.83-5.00 um)
    def _group_mean(group_dets: List[int]) -> Tuple[Optional[np.ndarray], List[int]]:
        used = [d for d in group_dets if d in det_coadds]
        if not used:
            return None, []
        return _nanmean(np.stack([det_coadds[d] for d in used])), used

    b_arr, b_dets = _group_mean([1, 2])
    g_arr, g_dets = _group_mean([3, 4])
    r_arr, r_dets = _group_mean([5, 6])

    # If fewer than 2 standard bands are populated but >= 2 detectors exist, spread them across B/G/R
    populated_groups = sum(x is not None for x in (r_arr, g_arr, b_arr))
    avail_sorted = sorted(det_coadds.keys())
    if populated_groups < 2 and len(avail_sorted) >= 2:
        b_dets = [avail_sorted[0]]
        b_arr = det_coadds[b_dets[0]]
        r_dets = [avail_sorted[-1]]
        r_arr = det_coadds[r_dets[0]]
        mid = avail_sorted[len(avail_sorted) // 2]
        g_dets = [mid]
        g_arr = det_coadds[mid]

    rgb_png = processing.encode_rgb_png(r_arr, g_arr, b_arr, stretch=stretch)
    # Pairwise 2-band RGB composites & tinted detector-pair stacks
    blue_pair_png = processing.encode_rgb_png(None, b_arr * 0.45 if b_arr is not None else None, b_arr, stretch=stretch) if b_arr is not None else None
    green_pair_png = processing.encode_rgb_png(g_arr * 0.20 if g_arr is not None else None, g_arr, g_arr * 0.35 if g_arr is not None else None, stretch=stretch) if g_arr is not None else None
    red_pair_png = processing.encode_rgb_png(r_arr, r_arr * 0.35 if r_arr is not None else None, None, stretch=stretch) if r_arr is not None else None
    bichrome_short_long_png = (
        processing.encode_rgb_png(r_arr, _nanmean(np.stack([b_arr, r_arr])) * 0.5, b_arr, stretch=stretch)
        if (b_arr is not None and r_arr is not None) else None
    )
    bichrome_short_mid_png = (
        processing.encode_rgb_png(g_arr, g_arr * 0.75, b_arr, stretch=stretch)
        if (b_arr is not None and g_arr is not None) else None
    )
    bichrome_mid_long_png = (
        processing.encode_rgb_png(r_arr, g_arr, g_arr * 0.65, stretch=stretch)
        if (g_arr is not None and r_arr is not None) else None
    )

    n = shape[0]
    return {
        "query": {
            "ra": ra, "dec": dec, "fov_deg": fov, "dets": target_dets,
            "sky": sky, "mask": mask, "max_per_det": max_per_det, "stretch": stretch,
        },
        "grid": {
            "n_pixels": n, "pixel_scale_arcsec": scale,
            "fov_deg": n * scale / 3600.0,
            "center": {"ra": ra, "dec": dec},
            "orientation": "north up, east left", "origin": "top-left",
        },
        "counts": {
            "images_found": n_found,
            "requested": len(selected),
            "failed": failed,
            "detectors_with_data": len(det_coadds),
        },
        "detectors": det_summaries,
        "all_detectors_coadd_image": all_coadd_png,
        "epoch_diff_color_image": epoch_diff_all_png,
        "false_color_rgb_image": rgb_png,
        "blue_pair_image": blue_pair_png,
        "green_pair_image": green_pair_png,
        "red_pair_image": red_pair_png,
        "bichrome_short_long_image": bichrome_short_long_png,
        "bichrome_short_mid_image": bichrome_short_mid_png,
        "bichrome_mid_long_image": bichrome_mid_long_png,
        "rgb_channels": {
            "blue_short_nir_dets": b_dets,
            "green_mid_nir_dets": g_dets,
            "red_long_nir_dets": r_dets,
        },
    }
