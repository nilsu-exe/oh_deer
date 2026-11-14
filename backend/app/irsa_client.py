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
from concurrent.futures import as_completed, ThreadPoolExecutor
from contextvars import copy_context
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import cache, config, diagnostics

log = logging.getLogger(__name__)

_download_slots = threading.BoundedSemaphore(config.MAX_PARALLEL_DOWNLOADS)


class DiscoveryError(RuntimeError):
    """The upstream TAP service could not return a usable image list."""


def _det_from_bandpass(name: str) -> Optional[int]:
    # "SPHEREx-D2" -> 2
    try:
        return int(str(name).strip()[-1])
    except (ValueError, IndexError):
        return None


def query_images(ra: float, dec: float, det: Optional[int] = None) -> List[Dict[str, Any]]:
    """All spectral images whose footprint contains (ra, dec), oldest first.

    ``det`` is the detector number 1-6 (``None`` = all six).
    Returns dicts: ``{"base_url", "mjd", "det", "bandpass"}``.
    """
    key = f"tap_{round(ra, 4)}_{round(dec, 4)}_{det or 'all'}"
    cached = cache.get_json(key, ttl=config.TAP_TTL_SECONDS)
    if cached is not None:
        log.info("TAP cache hit key=%s images=%d", key, len(cached))
        return cached

    log.info("TAP cache miss key=%s ra=%.6f dec=%.6f detector=%s",
             key, ra, dec, det or "all")

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
    t0 = time.perf_counter()
    log.info("TAP HTTP query started endpoint=https://irsa.ipac.caltech.edu/TAP/sync")
    try:
        table = Irsa.query_tap(adql).to_table()
    except Exception as exc:
        log.error("TAP HTTP query failed elapsed=%.2fs error=%s: %s",
                  time.perf_counter() - t0, type(exc).__name__, exc)
        raise DiscoveryError(str(exc)) from exc
    log.info("TAP HTTP query finished rows=%d elapsed=%.2fs", len(table), time.perf_counter() - t0)

    out: List[Dict[str, Any]] = []
    for row in table:
        try:
            mjd = float(row["mjd"])
        except (TypeError, ValueError):
            continue
        uri = str(row["uri"]).lstrip("/")
        bandpass = str(row["bandpass"])
        out.append({
            "base_url": f"{config.IRSA_BASE}/{uri}",
            "mjd": mjd,
            "det": _det_from_bandpass(bandpass),
            "bandpass": bandpass,
        })
    out.sort(key=lambda r: r["mjd"])
    cache.set_json(key, out)
    log.info("TAP discovery cached key=%s images=%d", key, len(out))
    return out


def cutout_url(base_url: str, ra: float, dec: float, size_deg: float) -> str:
    return f"{base_url}?center={ra:.6f},{dec:.6f}d&size={size_deg:.5f}"


def _download(url: str) -> Optional[bytes]:
    total_started = time.perf_counter()
    for attempt in range(config.RETRIES):
        attempt_started = time.perf_counter()
        try:
            log.debug("cutout waiting for slot attempt=%d/%d url=%s", attempt + 1, config.RETRIES, url)
            with _download_slots:
                waited = time.perf_counter() - attempt_started
                if waited >= config.LOG_HEARTBEAT_SECONDS > 0:
                    log.warning("cutout slot acquired after %.2fs waiting url=%s", waited, url)
                log.debug("cutout HTTP started attempt=%d/%d timeout=%ss slot_wait=%.2fs url=%s",
                          attempt + 1, config.RETRIES, config.HTTP_TIMEOUT, waited, url)
                http_started = time.perf_counter()
                r = requests.get(url, timeout=config.HTTP_TIMEOUT)
            if r.status_code == 200 and r.content:
                log.debug("cutout HTTP finished bytes=%d http_elapsed=%.2fs total_elapsed=%.2fs url=%s",
                          len(r.content), time.perf_counter() - http_started,
                          time.perf_counter() - total_started, url)
                return r.content
            log.warning("cutout HTTP failed status=%s attempt=%d/%d elapsed=%.2fs url=%s",
                        r.status_code, attempt + 1, config.RETRIES,
                        time.perf_counter() - attempt_started, url)
        except requests.RequestException as exc:
            log.warning("cutout HTTP failed attempt=%d/%d elapsed=%.2fs error=%s: %s url=%s",
                        attempt + 1, config.RETRIES, time.perf_counter() - attempt_started,
                        type(exc).__name__, exc, url)
        if attempt + 1 < config.RETRIES:
            delay = config.RETRY_BACKOFF * (attempt + 1)
            log.info("cutout retry in %.1fs url=%s", delay, url)
            time.sleep(delay)
    log.error("cutout abandoned attempts=%d elapsed=%.2fs url=%s", config.RETRIES,
              time.perf_counter() - total_started, url)
    return None


def fetch_cutout(base_url: str, ra: float, dec: float, size_deg: float) -> Optional[bytes]:
    """Download one cutout (raw FITS bytes), using the disk cache. None on failure."""
    url = cutout_url(base_url, ra, dec, size_deg)
    key = "cut_" + cache.key_hash(url)
    data = cache.get_bytes(key)
    if data is not None:
        log.debug("cutout cache hit bytes=%d url=%s", len(data), url)
        return data
    log.debug("cutout cache miss url=%s", url)
    data = _download(url)
    if data is not None:
        cache.set_bytes(key, data)
    return data


def fetch_all(images: List[Dict[str, Any]], ra: float, dec: float,
              size_deg: float) -> List[Tuple[Dict[str, Any], Optional[bytes]]]:
    """Fetch cutouts for every image in parallel. Order is preserved.

    Failed downloads come back as ``(meta, None)`` so the caller can report
    "N of M frames loaded" instead of failing the whole request.
    """
    def one(meta: Dict[str, Any]):
        return meta, fetch_cutout(meta["base_url"], ra, dec, size_deg)

    total = len(images)
    started = time.perf_counter()
    log.info("cutout batch started requested=%d workers=%d timeout=%ss attempts=%d size=%.5fdeg",
             total, config.MAX_PARALLEL_DOWNLOADS, config.HTTP_TIMEOUT, config.RETRIES, size_deg)
    diagnostics.set_progress(f"completed=0/{total} pending={total}")
    results = [None] * total
    failed = 0
    with ThreadPoolExecutor(max_workers=config.MAX_PARALLEL_DOWNLOADS) as pool:
        # Report whichever download finishes first, while keeping chronological
        # return order. Propagate the request ID into every pool worker.
        futures = {pool.submit(copy_context().run, one, meta): index
                   for index, meta in enumerate(images)}
        for completed, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results[futures[future]] = result
            failed += result[1] is None
            diagnostics.set_progress(f"completed={completed}/{total} failed={failed} pending={total - completed}")
            if completed == 1 or completed % 10 == 0 or completed == total:
                log.info("cutout progress completed=%d/%d failed=%d pending=%d elapsed=%.2fs",
                         completed, total, failed, total - completed, time.perf_counter() - started)
    log.info("cutout batch finished loaded=%d failed=%d elapsed=%.2fs",
             total - failed, failed, time.perf_counter() - started)
    return results
