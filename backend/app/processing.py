"""Pixel-level work: parse a cutout, mask flags, reproject, encode PNGs, extract spectra points."""
from __future__ import annotations

import base64
import io
import logging
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
        "mjd": c.meta["mjd"],
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
