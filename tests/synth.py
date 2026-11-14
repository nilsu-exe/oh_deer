"""Build synthetic SPHEREx-like cutouts (no network needed)."""
import io

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS

SCALE = 6.15 / 3600.0   # deg / pixel


def make_wcs(ra0, dec0, rot_deg, n):
    """Detector-aligned TAN WCS, rotated by rot_deg relative to north."""
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cunit = ["deg", "deg"]
    w.wcs.crval = [ra0, dec0]
    w.wcs.crpix = [(n + 1) / 2.0, (n + 1) / 2.0]
    w.wcs.cdelt = [-SCALE, SCALE]
    t = np.radians(rot_deg)
    w.wcs.pc = [[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]]
    return w


def make_mef(star_ra, star_dec, ra0, dec0, rot_deg=0.0, n=100, hot_pixel=None, source_pixel=None):
    """Return FITS bytes shaped like a SPHEREx cutout (IMAGE/FLAGS/VARIANCE/ZODI)."""
    from astropy.coordinates import SkyCoord
    w = make_wcs(ra0, dec0, rot_deg, n)
    x0, y0 = w.world_to_pixel(SkyCoord(star_ra * u.deg, star_dec * u.deg))
    yy, xx = np.indices((n, n))
    img = 10.0 + 100.0 * np.exp(-0.5 * ((xx - x0) ** 2 + (yy - y0) ** 2) / 1.2 ** 2)
    flags = np.zeros((n, n), dtype=np.int32)
    if hot_pixel is not None:
        flags[hot_pixel] |= 1 << 10          # HOT
    if source_pixel is not None:
        flags[source_pixel] |= 1 << 21       # SOURCE (must NOT be masked)
    hdr = w.to_header()
    primary = fits.PrimaryHDU()
    primary.header["VERSION"] = "test-v1"
    hdul = fits.HDUList([
        primary,
        fits.ImageHDU(img.astype(np.float32), header=hdr, name="IMAGE"),
        fits.ImageHDU(flags, name="FLAGS"),
        fits.ImageHDU(np.ones((n, n), dtype=np.float32), name="VARIANCE"),
        fits.ImageHDU(np.zeros((n, n), dtype=np.float32), name="ZODI"),
    ])
    buf = io.BytesIO()
    hdul.writeto(buf)
    return buf.getvalue()


def fake_wavelength_maps(hdr, hdul, shape):
    """Stand-in for the WCS-WAVE lookup: wavelength rises linearly with row."""
    ny, nx = shape
    wave = np.tile((1.10 + 0.52 * np.arange(ny) / (ny - 1))[:, None], (1, nx)).astype(np.float32)
    return wave, np.full(shape, 0.03, dtype=np.float32)
