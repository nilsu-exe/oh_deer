"""Talking to IRSA: find SPHEREx spectral images and download cutouts.

Discovery prefers local snapshots of ``spherex.artifact`` + ``spherex.plane``.
Missing coverage falls back to a bounded TAP query in auto mode. Full-sky mirror
refreshes are explicit, so snapshot provenance must accompany local results.

Frame cutouts use IRSA's URL service: append ``?center=<ra>,<dec>d&size=<deg>``
to an image URL. Aperture spectra use partial reads from the public S3 mirror,
with IRSA cutouts as a fallback.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import as_completed, ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import cache, config, diagnostics, discovery_mirror, s3_cutouts, tap_metadata

log = logging.getLogger(__name__)

_download_slots = threading.BoundedSemaphore(config.MAX_PARALLEL_DOWNLOADS)


class DiscoveryError(RuntimeError):
    """The upstream TAP service could not return a usable image list."""


class MirrorUnavailable(DiscoveryError):
    """Local-only discovery was requested without a complete matching snapshot."""


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
    if config.DISCOVERY_MODE != "tap":
        try:
            local = discovery_mirror.query_images(ra, dec, det)
        except (sqlite3.Error, OSError, ValueError) as exc:
            log.warning("local discovery mirror unavailable error=%s: %s", type(exc).__name__, exc)
            local = None
        if local is not None:
            log.info("local discovery hit images=%d detector=%s scope=%s snapshot=%s stale=%s elapsed_network=0",
                     len(local), det or "all", local.discovery["scope"],
                     local.discovery["oldest_snapshot_utc"], local.discovery["stale"])
            return local
        if config.DISCOVERY_MODE == "local":
            raise MirrorUnavailable("No complete local discovery snapshot covers this position and detector selection.")

    key = f"tap_{round(ra, 4)}_{round(dec, 4)}_{det or 'all'}"
    cached = cache.get_json(key, ttl=config.TAP_TTL_SECONDS)
    if cached is not None:
        log.info("TAP cache hit key=%s images=%d", key, len(cached))
        return cached

    log.info("TAP cache miss key=%s ra=%.6f dec=%.6f detector=%s",
             key, ra, dec, det or "all")

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
        table = tap_metadata.query_table(adql)
        if not {"uri", "mjd", "bandpass"}.issubset(table.colnames):
            raise ValueError("TAP response is missing required discovery columns")
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
    if config.DISCOVERY_MODE != "tap":
        try:
            discovery_mirror.replace_snapshot(out, ra=ra, dec=dec, det=det)
        except (sqlite3.Error, OSError, ValueError) as exc:
            log.warning("could not save discovery snapshot: %s", exc)
    log.info("TAP discovery cached key=%s images=%d", key, len(out))
    return out


def query_selected_images(ra: float, dec: float, dets: Optional[List[int]] = None):
    """Require coverage only for requested detectors, including local-only spectra."""
    if not dets:
        return query_images(ra, dec)
    detectors = sorted(set(dets))
    groups = [query_images(ra, dec, det) for det in detectors]
    images = {m["base_url"]: m for det, group in zip(detectors, groups)
              for m in group if m.get("det") == det}
    images = sorted(images.values(), key=lambda m: (m["mjd"], m["base_url"]))
    infos = [g.discovery for g in groups if getattr(g, "discovery", None)]
    if infos:
        images = discovery_mirror.MirrorImages(images, [
            {"updated": datetime.fromisoformat(info["oldest_snapshot_utc"]).timestamp(),
             "scope": info["scope"]} for info in infos])
        if len(infos) != len(groups):
            images.discovery.update(source="mixed", scope="mixed")
    return images


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


def fetch_aperture_cutout(base_url: str, ra: float, dec: float,
                          size_deg: float) -> Optional[bytes]:
    """Prefer cached data, then S3 partial reads; fall back to the IRSA service."""
    url = cutout_url(base_url, ra, dec, size_deg)
    key = "aperture_s3_v1_" + cache.key_hash(url)
    for cached_key in (key, "cut_" + cache.key_hash(url)):
        data = cache.get_bytes(cached_key)
        if data is not None:
            log.debug("aperture cache hit bytes=%d key=%s", len(data), cached_key)
            return data

    s3_url = s3_cutouts.mirror_url(base_url)
    if s3_url is not None:
        started = time.perf_counter()
        for attempt in range(config.RETRIES):
            attempt_started = time.perf_counter()
            try:
                with _download_slots:
                    waited = time.perf_counter() - attempt_started
                    log.debug("S3 aperture started attempt=%d/%d slot_wait=%.2fs url=%s",
                              attempt + 1, config.RETRIES, waited, s3_url)
                    data = s3_cutouts.read_cutout(s3_url, ra, dec, size_deg)
                cache.set_bytes(key, data)
                log.debug("S3 aperture finished cutout_bytes=%d elapsed=%.2fs url=%s",
                          len(data), time.perf_counter() - started, s3_url)
                return data
            except Exception as exc:
                log.warning("S3 aperture failed attempt=%d/%d elapsed=%.2fs error=%s: %s url=%s",
                            attempt + 1, config.RETRIES, time.perf_counter() - attempt_started,
                            type(exc).__name__, exc, s3_url)
                if (isinstance(exc, (ImportError, KeyError, ValueError, FileNotFoundError, PermissionError))
                        or getattr(exc, "status", None) in (400, 403, 404, 416)):
                    break  # Missing dependencies/HDUs or incompatible FITS, not a transient error.
            if attempt + 1 < config.RETRIES:
                delay = config.RETRY_BACKOFF * (attempt + 1)
                log.info("S3 aperture retry in %.1fs url=%s", delay, s3_url)
                time.sleep(delay)
        log.warning("S3 aperture falling back to IRSA cutout elapsed=%.2fs url=%s",
                    time.perf_counter() - started, url)
    return fetch_cutout(base_url, ra, dec, size_deg)


def fetch_all(images: List[Dict[str, Any]], ra: float, dec: float,
              size_deg: float, *, aperture_only: bool = False
              ) -> List[Tuple[Dict[str, Any], Optional[bytes]]]:
    """Fetch cutouts for every image in parallel. Order is preserved.

    ``aperture_only`` uses S3 range reads that omit EPSF/ZODI, with IRSA fallback.
    Failed downloads come back as ``(meta, None)`` so the caller can report
    "N of M frames loaded" instead of failing the whole request.
    """
    def one(meta: Dict[str, Any]):
        fetch = fetch_aperture_cutout if aperture_only else fetch_cutout
        return meta, fetch(meta["base_url"], ra, dec, size_deg)

    total = len(images)
    started = time.perf_counter()
    log.info("cutout batch started requested=%d workers=%d timeout=%ss attempts=%d size=%.5fdeg source=%s",
             total, config.MAX_PARALLEL_DOWNLOADS, config.HTTP_TIMEOUT, config.RETRIES, size_deg,
             "S3 aperture ranges (IRSA fallback)" if aperture_only else "IRSA cutouts")
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
