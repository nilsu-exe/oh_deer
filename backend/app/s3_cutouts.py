"""Small aperture cutouts from the public SPHEREx S3 mirror.

Astropy seeks past unused HDUs and reads contiguous row strips with HTTP Range
requests. Disable read-ahead so traversing the EPSF header never fetches its data.
"""
from __future__ import annotations

import io
import re
import warnings
from typing import Optional
from urllib.parse import urlsplit

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.nddata import Cutout2D
from astropy.wcs import WCS

from . import config


def mirror_url(base_url: str) -> Optional[str]:
    """Map IRSA image paths to S3 without pinning the release or version."""
    url = urlsplit(base_url)
    prefix = "/ibe/data/spherex/"
    if (url.scheme != "https" or url.netloc != "irsa.ipac.caltech.edu"
            or not url.path.startswith(prefix) or not url.path.endswith(".fits")
            or url.query or url.fragment):
        return None
    return config.SPHEREX_S3_BASE + "/" + url.path[len(prefix):]


def read_cutout(url: str, ra: float, dec: float, size_deg: float) -> bytes:
    """Read IMAGE/FLAGS/VARIANCE regions and WCS-WAVE, returning compact FITS.

    ZODI and EPSF are unused by aperture photometry. Only their headers may be
    read while seeking to the wavelength lookup table later in the file.
    """
    from aiohttp import ClientTimeout

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with fits.open(
            url, use_fsspec=True, memmap=False, lazy_load_hdus=True,
            fsspec_kwargs={
                "cache_type": "none",
                "client_kwargs": {"timeout": ClientTimeout(total=config.HTTP_TIMEOUT)},
            },
        ) as remote:
            image = remote["IMAGE"]
            # A zero-stride view lets Cutout2D compute bounds without reading the
            # image or allocating a detector-sized array.
            bounds = Cutout2D(
                np.broadcast_to(0, image.shape), SkyCoord(ra * u.deg, dec * u.deg),
                size_deg * u.deg, wcs=WCS(image.header), mode="trim",
            )
            ys, xs = bounds.slices_original
            primary = fits.PrimaryHDU(header=remote[0].header.copy())
            hdus = [primary]
            for name in ("IMAGE", "FLAGS", "VARIANCE"):
                hdu = remote[name]
                # Whole rows are contiguous on disk: one request per strip,
                # instead of a separate range request for each small row slice.
                data = hdu.section[ys, :][:, xs].copy()
                hdr = hdu.header.copy()
                for key in list(hdr):
                    if re.fullmatch(r"CRPIX[12][A-Z]?", key):
                        offset = xs.start if key[5] == "1" else ys.start
                        hdr[key] -= offset
                # Section returns physical values, including unsigned FLAGS.
                # Do not apply the original scaling twice on re-serialization.
                for key in ("BSCALE", "BZERO", "CHECKSUM", "DATASUM"):
                    hdr.remove(key, ignore_missing=True)
                hdus.append(fits.ImageHDU(data=data, header=hdr, name=name))
            # Required for the alternate spectral WCS; EPSF.data is never used.
            hdus.append(remote["WCS-WAVE"].copy())
            with fits.HDUList(hdus) as compact:
                output = io.BytesIO()
                compact.writeto(output)
                return output.getvalue()
