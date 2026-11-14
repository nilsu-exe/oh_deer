"""Talking to IRSA: find SPHEREx spectral images and download cutouts.

Discovery uses the TAP tables ``spherex.artifact`` + ``spherex.plane`` (the same
query as the IRSA cutouts tutorial). TAP sees new weekly ingests immediately,
whereas SIA lags about a day -- so new data releases need no code changes.

Cutouts use IRSA's URL service: append ``?center=<ra>,<dec>d&size=<deg>`` to
an image URL and you get a small multi-extension FITS file back.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import config
from . import cache

log = logging.getLogger(__name__)

_IRSA_SPHEREX_PATH = "ibe/data/spherex/"
_SPHEREX_S3_BASE = "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com"

_download_slots = threading.BoundedSemaphore(config.MAX_PARALLEL_DOWNLOADS)


def _det_from_bandpass(name: str) -> Optional[int]:
    # "SPHEREx-D2" -> 2
    try:
        return int(str(name).strip()[-1])
    except (ValueError, IndexError):
        return None


def query_images(ra: float, dec: float, det: Optional[int] = None) -> List[Dict[str, Any]]:
    """All spectral images whose footprint contains (ra, dec), oldest first.

    ``det`` is the detector number 1-6 (``None`` = all six).
    Returns dicts with an ``image`` source mapping (``s3`` then ``irsa``),
    plus ``mjd``, ``det``, and ``bandpass``.
    """
    key = f"tap_{round(ra, 4)}_{round(dec, 4)}_{det or 'all'}"
    cached = cache.get_json(key, ttl=config.TAP_TTL_SECONDS)
    if cached is not None:
        return cached

    from astroquery.ipac.irsa import Irsa  # imported lazily: slow import

    band_clause = f"AND p.energy_bandpassname = 'SPHEREx-D{int(det)}'" if det else ""
    adql = f"""
        SELECT a.uri AS uri,
               p.time_bounds_lower AS mjd,
               p.energy_bandpassname AS bandpass
        FROM spherex.artifact a
        JOIN spherex.plane p ON a.planeid = p.planeid
        WHERE 1 = CONTAINS(POINT('ICRS', {ra}, {dec}), p.poly)
          {band_clause}
        ORDER BY p.time_bounds_lower
    """
    t0 = time.time()
    table = Irsa.query_tap(adql).to_table()
    log.info("TAP query returned %d rows in %.1fs", len(table), time.time() - t0)

    out: List[Dict[str, Any]] = []
    for row in table:
        try:
            mjd = float(row.get("mjd"))
        except (TypeError, ValueError):
            continue
        uri = str(row.get("uri")).lstrip("/")
        bandpass = str(row.get("bandpass"))
        s3_path = uri.removeprefix(_IRSA_SPHEREX_PATH)
        out.append({
            "image": {
                "s3": f"{_SPHEREX_S3_BASE}/{s3_path}",
                "irsa": f"{config.IRSA_BASE}/{uri}",
            },
            "mjd": mjd,
            "det": _det_from_bandpass(bandpass),
            "bandpass": bandpass,
        })
    out.sort(key=lambda r: r.get("mjd"))
    cache.set_json(key, out)
    return out


def cutout_url(base_url: str, ra: float, dec: float, size_deg: float) -> str:
    return f"{base_url}?center={ra:.6f},{dec:.6f}d&size={size_deg:.5f}"


def _download(url: str) -> Optional[bytes]:
    for attempt in range(config.RETRIES):
        try:
            with _download_slots:
                r = requests.get(url, timeout=config.HTTP_TIMEOUT)
            if r.status_code == 200 and r.content:
                return r.content
            log.warning("HTTP %s for %s (attempt %d)", r.status_code, url, attempt + 1)
        except requests.RequestException as exc:
            log.warning("download error %s for %s (attempt %d)", exc, url, attempt + 1)
        time.sleep(config.RETRY_BACKOFF * (attempt + 1))
    return None


def fetch_cutout(image: Dict[str, str], ra: float, dec: float,
                 size_deg: float) -> Optional[bytes]:
    """Try the S3 image first, then an IRSA cutout; return raw FITS bytes."""
    base_url = image.get("irsa")
    s3_url = image.get("s3")
    url = cutout_url(base_url, ra, dec, size_deg)
    cutout_key = "cut_" + cache.key_hash(url)
    data = cache.get_bytes(cutout_key)
    if data is not None:
        return data

    if s3_url:
        s3_key = "s3_" + cache.key_hash(s3_url)
        data = cache.get_bytes(s3_key)
        if data is not None:
            return data
        data = _download(s3_url)
        if data is not None:
            cache.set_bytes(s3_key, data)
            return data
        log.info("S3 download failed; falling back to IRSA cutout for %s", base_url)

    data = _download(url)
    if data is not None:
        cache.set_bytes(cutout_key, data)
    return data


def fetch_all(images: List[Dict[str, Any]], ra: float, dec: float,
              size_deg: float) -> List[Tuple[Dict[str, Any], Optional[bytes]]]:
    """Fetch cutouts for every image in parallel. Order is preserved.

    Failed downloads come back as ``(meta, None)`` so the caller can report
    "N of M frames loaded" instead of failing the whole request.
    """
    def one(meta: Dict[str, Any]):
        return meta, fetch_cutout(meta.get("image"), ra, dec, size_deg)

    with ThreadPoolExecutor(max_workers=config.MAX_PARALLEL_DOWNLOADS) as pool:
        return list(pool.map(one, images))
