"""Pixel-level work: parse a cutout, mask flags, reproject, encode PNGs, extract spectra points."""
from __future__ import annotations

import base64
import io
import logging
import math
import warnings
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_area
from PIL import Image
from reproject import reproject_interp

from . import config

log = logging.getLogger(__name__)


@dataclass
class Cutout:
    meta: Dict[str, Any]
    wcs: WCS                 # celestial WCS (SIP included)
    image: np.ndarray        # MJy/sr, zodi NOT subtracted
    flags: np.ndarray
    variance: np.ndarray     # (MJy/sr)^2
    zodi: Optional[np.ndarray]  # MJy/sr; omitted in aperture-only cutouts
    wave: np.ndarray         # central wavelength per pixel, micron
    bandwidth: np.ndarray    # bandwidth per pixel, micron
    version: str


# ---------------------------------------------------------------- parsing ---

def _wavelength_maps(hdr, hdul, shape) -> Tuple[np.ndarray, np.ndarray]:
    """Per-pixel wavelength/bandwidth from the WCS-WAVE lookup table (WCS key 'W').

    Mirrors the IRSA tutorials: SIP distortion must be switched off for this
    WCS, and astropy needs the HDU list to read the lookup table.
    """
    try:
        spec = WCS(hdr, fobj=hdul, key="W")
        spec.sip = None
        yy, xx = np.indices(shape)
        wl, bw = spec.pixel_to_world(xx, yy)
        return (wl.to(u.micrometer).value.astype(np.float32),
                bw.to(u.micrometer).value.astype(np.float32))
    except Exception as exc:  # pragma: no cover - depends on real files
        log.warning("could not build wavelength map: %s", exc)
        nan = np.full(shape, np.nan, dtype=np.float32)
        return nan, nan


def parse_cutout(content: bytes, meta: Dict[str, Any]) -> Cutout:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with fits.open(io.BytesIO(content), memmap=False) as hdul:
            hdr = hdul["IMAGE"].header
            image = np.array(hdul["IMAGE"].data, dtype=np.float32)
            flags = np.array(hdul["FLAGS"].data, dtype=np.int64)
            variance = np.array(hdul["VARIANCE"].data, dtype=np.float32)
            zodi = np.array(hdul["ZODI"].data, dtype=np.float32) if "ZODI" in hdul else None
            wcs = WCS(hdr)
            wave, bw = _wavelength_maps(hdr, hdul, image.shape)
            version = str(hdul[0].header.get("VERSION", ""))
    return Cutout(meta, wcs, image, flags, variance, zodi, wave, bw, version)


# ---------------------------------------------------------------- masking ---

def bad_mask(flags: np.ndarray, bits: Optional[Sequence[int]] = None) -> np.ndarray:
    m = 0
    for b in (config.BAD_FLAG_BITS if bits is None else bits):
        m |= 1 << b
    return (flags & m) != 0


def clean_image(c: Cutout, sky: str = "median", mask: bool = True) -> Tuple[np.ndarray, float]:
    """Return (image with bad pixels = NaN, fraction of pixels masked).

    sky: "none" | "zodi" (subtract ZODI model) | "median" (subtract frame median)
    """
    img = c.image.astype(np.float32).copy()
    if sky == "zodi":
        if c.zodi is None:
            raise ValueError("ZODI is required for zodiacal background subtraction")
        img -= c.zodi
    bad = bad_mask(c.flags) if mask else np.zeros(img.shape, dtype=bool)
    img[bad] = np.nan
    img[~np.isfinite(img)] = np.nan
    if sky == "median" and np.isfinite(img).any():
        img -= np.nanmedian(img)
    return img, float(bad.mean())


# ------------------------------------------------------- common sky grid ----

def make_grid(ra: float, dec: float, fov_deg: float,
              pixel_scale_arcsec: float = config.PIXEL_SCALE_ARCSEC) -> Tuple[WCS, Tuple[int, int]]:
    """North-up, east-left TAN grid centred on (ra, dec). Returns (wcs, (ny, nx))."""
    n = max(5, int(np.ceil(fov_deg * 3600.0 / pixel_scale_arcsec)))
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cunit = ["deg", "deg"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [(n + 1) / 2.0, (n + 1) / 2.0]
    s = pixel_scale_arcsec / 3600.0
    w.wcs.cdelt = [-s, s]
    return w, (n, n)


def reproject_to_grid(img: np.ndarray, wcs_in: WCS, grid: WCS, shape: Tuple[int, int]) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = reproject_interp((img, wcs_in), grid, shape_out=shape,
                               order="bilinear", return_footprint=False)
    return out.astype(np.float32)


# -------------------------------------------------------------- sampling ----

def pixel_of(c: Cutout, ra: float, dec: float) -> Tuple[float, float]:
    x, y = c.wcs.world_to_pixel(SkyCoord(ra * u.deg, dec * u.deg))
    return float(x), float(y)


def sample(arr: np.ndarray, x: float, y: float) -> float:
    """Nearest-pixel value, clamped to the array bounds."""
    ny, nx = arr.shape
    xi = int(np.clip(round(x), 0, nx - 1))
    yi = int(np.clip(round(y), 0, ny - 1))
    v = float(arr[yi, xi])
    return v if np.isfinite(v) else float("nan")


def aperture_point(c: Cutout, ra: float, dec: float, ap: float = 1.5,
                   ann: Tuple[float, float] = (4.0, 7.0)) -> Optional[Dict[str, Any]]:
    """Quick-look photometry at (ra, dec) in one cutout.

    Aperture sum minus local-median background, converted to Jy with the pixel
    solid angle. No aperture correction and no PSF fit: this is a first look,
    not calibrated point-source photometry.
    """
    x, y = pixel_of(c, ra, dec)
    ny, nx = c.image.shape
    if not (0 <= x < nx and 0 <= y < ny):
        return None
    yy, xx = np.indices(c.image.shape)
    r = np.hypot(xx - x, yy - y)
    good = ~bad_mask(c.flags) & np.isfinite(c.image)
    in_ap = (r <= ap) & good
    in_ann = (r >= ann[0]) & (r <= ann[1]) & good
    if in_ap.sum() < 2 or in_ann.sum() < 10:
        return None
    bg = float(np.median(c.image[in_ann]))
    sb = c.image[in_ap] - bg
    omega = proj_plane_pixel_area(c.wcs) * (np.pi / 180.0) ** 2   # sr / pixel
    wl = sample(c.wave, x, y)
    if not np.isfinite(wl):
        return None
    return {
        "wavelength_um": wl,
        "bandwidth_um": _nan_to_none(sample(c.bandwidth, x, y)),
        "flux_jy": float(sb.sum() * 1e6 * omega),
        "err_jy": float(np.sqrt(c.variance[in_ap].sum()) * 1e6 * omega),
        "surface_brightness_mjy_sr": float(sb.mean()),
        "n_pix": int(in_ap.sum()),
        "mjd": c.meta.get("mjd"),
        "det": c.meta.get("det"),
        "version": c.version,
    }


def _nan_to_none(v: float) -> Optional[float]:
    return float(v) if np.isfinite(v) else None


# --------------------------------------------------------- PNG encoding -----

def encode_frames(arrays: List[np.ndarray], stretch: str = "asinh",
                  p_lo: float = 1.0, p_hi: float = 99.5) -> List[str]:
    """Encode frames as grayscale+alpha PNG data URLs with ONE shared stretch,
    so brightness is comparable between frames. NaN pixels become transparent.
    Image row 0 is the top (north), so arrays are flipped vertically."""
    if not arrays:
        return []
    stack = np.stack(arrays)
    finite = stack[np.isfinite(stack)]
    if finite.size == 0:
        lo, hi = 0.0, 1.0
    else:
        lo, hi = np.percentile(finite, [p_lo, p_hi])
        if hi <= lo:
            hi = lo + 1e-9
    urls = []
    for arr in arrays:
        norm = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
        if stretch == "asinh":
            norm = np.arcsinh(norm * 10.0) / np.arcsinh(10.0)
        alpha = np.isfinite(arr)
        gray = np.where(alpha, norm, 0.0)
        la = np.dstack([(gray * 255).astype(np.uint8),
                        (alpha * 255).astype(np.uint8)])
        buf = io.BytesIO()
        Image.fromarray(np.flipud(la), mode="LA").save(buf, format="PNG", optimize=True)
        urls.append("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode())
    return urls


def encode_diverging_png(diff_arr: np.ndarray, p_abs: float = 99.5) -> str:
    """Encode a signed difference image (Epoch 2 - Epoch 1) as an RGBA PNG data URL.

    Positive residuals (new position of a mover, or brightening source) are rendered
    in warm red/amber; negative residuals (old position of a mover, or dimming source)
    are rendered in cool cyan/blue; near-zero background is dark slate.
    """
    finite = diff_arr[np.isfinite(diff_arr)]
    if finite.size == 0:
        scale = 1.0
    else:
        med = float(np.median(finite))
        diff_arr = diff_arr - med
        scale = float(np.percentile(np.abs(finite - med), p_abs))
        if scale <= 0:
            scale = 1.0

    # Signed normalized amplitude in [-1, 1] with gentle asinh compression
    t = np.clip(diff_arr / scale, -1.0, 1.0)
    t = np.arcsinh(t * 4.0) / np.arcsinh(4.0)
    pos = np.clip(t, 0.0, 1.0)
    neg = np.clip(-t, 0.0, 1.0)
    alpha = np.isfinite(diff_arr)

    # Dark space background (15, 23, 42) -> Red/Amber (255, 95, 65) for + and Cyan (56, 195, 255) for -
    r = np.clip(15.0 + pos * 240.0 + neg * 41.0, 0.0, 255.0)
    g = np.clip(23.0 + pos * 72.0 + neg * 172.0, 0.0, 255.0)
    b = np.clip(42.0 + pos * 23.0 + neg * 213.0, 0.0, 255.0)
    a = np.where(alpha, 255.0, 0.0)

    rgba = np.dstack([r, g, b, a]).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(np.flipud(rgba), mode="RGBA").save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ----------------------------------------- Epoch stacking & candidate hunt ---

def split_epochs(frames: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split time-ordered frames into early (Epoch 1) and late (Epoch 2) groups.

    Uses the largest MJD gap when >= 2 frames exist (which naturally separates
    6-month SPHEREx survey passes), falling back to a midpoint split.
    """
    if len(frames) < 2:
        return list(frames), []
    ordered = sorted(frames, key=lambda f: float(f.get("mjd") or 0.0))
    mjds = np.array([float(f.get("mjd") or 0.0) for f in ordered], dtype=float)
    gaps = np.diff(mjds)
    if len(gaps) > 0 and float(np.max(gaps)) > 1.5 * float(np.median(gaps) + 1e-6):
        split_idx = int(np.argmax(gaps)) + 1
    else:
        split_idx = len(ordered) // 2
    split_idx = max(1, min(len(ordered) - 1, split_idx))
    return ordered[:split_idx], ordered[split_idx:]


def _local_extrema(snr_map: np.ndarray, thresh: float, radius: int = 2) -> List[Tuple[float, float, float]]:
    """Find local maxima >= thresh in `snr_map` with sub-pixel centroiding.

    Returns list of (col_x, row_y, peak_snr) sorted by peak_snr descending.
    """
    ny, nx = snr_map.shape
    work = np.where(np.isfinite(snr_map), snr_map, -np.inf)
    peaks: List[Tuple[float, float, float]] = []
    margin = max(1, radius)
    for r in range(margin, ny - margin):
        for c in range(margin, nx - margin):
            val = float(work[r, c])
            if val < thresh:
                continue
            patch = work[r - radius:r + radius + 1, c - radius:c + radius + 1]
            if val < float(np.max(patch)):
                continue
            # Strictly greater than earlier ties using neighborhood suppression
            sub = np.maximum(work[r - 1:r + 2, c - 1:c + 2], 0.0)
            wsum = float(np.sum(sub))
            if wsum > 0:
                yy, xx = np.mgrid[r - 1:r + 2, c - 1:c + 2]
                cy = float(np.sum(yy * sub) / wsum)
                cx = float(np.sum(xx * sub) / wsum)
            else:
                cx, cy = float(c), float(r)
            peaks.append((cx, cy, val))

    peaks.sort(key=lambda p: p[2], reverse=True)
    kept: List[Tuple[float, float, float]] = []
    for cx, cy, val in peaks:
        if all(math.hypot(cx - kx, cy - ky) > radius + 0.5 for kx, ky, _ in kept):
            kept.append((cx, cy, val))
    return kept


def detect_candidates(
    epoch1_arr: np.ndarray,
    epoch2_arr: np.ndarray,
    grid_wcs: WCS,
    *,
    mjd1: Optional[float] = None,
    mjd2: Optional[float] = None,
    pixel_scale_arcsec: float = config.PIXEL_SCALE_ARCSEC,
    snr_thresh: float = 4.5,
    max_candidates: int = 10,
) -> List[Dict[str, Any]]:
    """Detect moving sources (dipoles) and variable sources from Epoch 2 - Epoch 1.

    Pixel coordinates (`x`, `y`, `from_x`, `from_y`) use the top-left PNG convention
    matching `/api/pixel_to_sky`.
    """
    diff = np.asarray(epoch2_arr, dtype=float) - np.asarray(epoch1_arr, dtype=float)
    finite = diff[np.isfinite(diff)]
    if finite.size < 16:
        return []
    med = float(np.median(finite))
    mad = float(np.median(np.abs(finite - med)))
    sigma = max(1.4826 * mad, float(np.std(finite)) * 0.25, 1e-6)
    snr_map = (diff - med) / sigma

    pos_peaks = _local_extrema(snr_map, snr_thresh, radius=2)[:max_candidates * 2]
    neg_peaks = _local_extrema(-snr_map, snr_thresh, radius=2)[:max_candidates * 2]

    ny = diff.shape[0]

    def to_sky(col: float, row: float) -> Tuple[float, float]:
        ra_out, dec_out = grid_wcs.wcs_pix2world([[col, row]], 0)[0]
        return float(ra_out), float(dec_out)

    used_neg = set()
    candidates: List[Dict[str, Any]] = []
    dt_yr = ((float(mjd2) - float(mjd1)) / 365.25) if (mjd1 is not None and mjd2 is not None and mjd2 > mjd1) else None

    # 1. Pair positive (Epoch 2) and negative (Epoch 1) peaks into moving-object dipoles
    for px, py, psnr in pos_peaks:
        best_j = None
        best_dist = 999.0
        for j, (nx_c, ny_c, nsnr) in enumerate(neg_peaks):
            if j in used_neg:
                continue
            dist = math.hypot(px - nx_c, py - ny_c)
            ratio = max(psnr, nsnr) / max(min(psnr, nsnr), 1e-6)
            if 1.2 <= dist <= 25.0 and ratio <= 4.0 and dist < best_dist:
                best_dist = dist
                best_j = j
        if best_j is not None:
            used_neg.add(best_j)
            nx_c, ny_c, nsnr = neg_peaks[best_j]
            ra2, dec2 = to_sky(px, py)
            ra1, dec1 = to_sky(nx_c, ny_c)
            shift_arcsec = best_dist * pixel_scale_arcsec
            pm_arcsec_yr = (shift_arcsec / dt_yr) if (dt_yr and dt_yr > 1e-4) else None
            comb_snr = math.hypot(psnr, nsnr)
            candidates.append({
                "kind": "mover",
                "snr": round(comb_snr, 2),
                "x": round(px, 2),
                "y": round(ny - 1 - py, 2),
                "from_x": round(nx_c, 2),
                "from_y": round(ny - 1 - ny_c, 2),
                "ra": round(ra2, 6),
                "dec": round(dec2, 6),
                "from_ra": round(ra1, 6),
                "from_dec": round(dec1, 6),
                "shift_px": round(best_dist, 2),
                "shift_arcsec": round(shift_arcsec, 2),
                "proper_motion_arcsec_yr": round(pm_arcsec_yr, 2) if pm_arcsec_yr is not None else None,
            })
        else:
            ra2, dec2 = to_sky(px, py)
            candidates.append({
                "kind": "variable",
                "direction": "brightening",
                "snr": round(psnr, 2),
                "x": round(px, 2),
                "y": round(ny - 1 - py, 2),
                "ra": round(ra2, 6),
                "dec": round(dec2, 6),
            })

    # 2. Remaining unpaired negative troughs are dimming variable sources
    for j, (nx_c, ny_c, nsnr) in enumerate(neg_peaks):
        if j in used_neg:
            continue
        ra1, dec1 = to_sky(nx_c, ny_c)
        candidates.append({
            "kind": "variable",
            "direction": "dimming",
            "snr": round(nsnr, 2),
            "x": round(nx_c, 2),
            "y": round(ny - 1 - ny_c, 2),
            "ra": round(ra1, 6),
            "dec": round(dec1, 6),
        })

    candidates.sort(key=lambda c: c["snr"], reverse=True)
    return candidates[:max_candidates]
