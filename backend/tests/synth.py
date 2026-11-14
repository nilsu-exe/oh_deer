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


def make_spectral_mef(n=100, compressed_flags=False):
    """A real WAVE-TAB lookup, SIP distortion, unsigned flags, and large EPSF."""
    with fits.open(io.BytesIO(make_mef(133.795, -7.245, 133.795, -7.245, n=n))) as source:
        hdul = fits.HDUList([h.copy() for h in source])
    hdr = hdul["IMAGE"].header
    hdr["CTYPE1"] = "RA---TAN-SIP"
    hdr["CTYPE2"] = "DEC--TAN-SIP"
    hdr["A_ORDER"], hdr["B_ORDER"] = 2, 2
    hdr["A_2_0"], hdr["B_0_2"] = 5e-6, -2e-6
    for i in (1, 2):
        hdr[f"CRPIX{i}W"] = 0.0
        hdr[f"CRVAL{i}W"] = 1.0
        hdr[f"CDELT{i}W"] = 1.0
        hdr[f"CUNIT{i}W"] = "um"
        hdr[f"CTYPE{i}W"] = "WAVE-TAB"
        hdr[f"PV{i}_3W"] = i
        hdr[f"PS{i}_0W"] = "WCS-WAVE"
        hdr[f"PS{i}_1W"] = "VALUES"
        hdr[f"PS{i}_2W"] = "X" if i == 1 else "Y"
        hdr[f"CRPIX{i}A"] = 0.0
        hdr[f"CRVAL{i}A"] = 0.0
        hdr[f"CDELT{i}A"] = 1.0
        hdr[f"CTYPE{i}A"] = "LINEAR"
    flags = np.zeros((n, n), dtype=np.uint32)
    flags[n // 2, n // 2] = (1 << 31) | (1 << 21)
    flags[n // 2 + 1, n // 2] = 1 << 10
    flag_hdu = fits.CompImageHDU if compressed_flags else fits.ImageHDU
    hdul["FLAGS"] = flag_hdu(flags, name="FLAGS")
    values = np.empty((1, 2, 2, 2), dtype=np.float32)
    values[0, :, :, 0] = [[1.10, 1.12], [1.60, 1.62]]
    values[0, :, :, 1] = 0.03
    hdul.append(fits.BinTableHDU.from_columns([
        fits.Column(name="EPSF", format="1089D", array=np.zeros((256, 1089))),
    ], name="EPSF"))
    hdul.append(fits.BinTableHDU.from_columns([
        fits.Column(name="X", format="2J", dim="(2)", array=[[1, n]]),
        fits.Column(name="Y", format="2J", dim="(2)", array=[[1, n]]),
        fits.Column(name="VALUES", format="8E", dim="(2,2,2)", array=values),
    ], name="WCS-WAVE"))
    with hdul:
        buf = io.BytesIO()
        hdul.writeto(buf)
        return buf.getvalue()
