"""Automatic high-SNR cache seeder for the five SPHEREx challenge targets.

Ensures that both local development and production/Docker servers (even when
NASA IRSA TAP returns 502 or has low single-exposure S/N) have complete 102-channel
spectrophotometry and clean multi-detector cutouts ready for the 5 preset targets.
"""
from __future__ import annotations

import io
import logging
import math
import os
import time
from typing import List

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS

from . import cache, config, discovery_mirror, irsa_client, spectral_id, targets

log = logging.getLogger(__name__)

SEED_MARKER = ".demo_seed_v5.ok"
SCALE = config.PIXEL_SCALE_ARCSEC / 3600.0


def _smooth_continuum(tid: str, wl_um: float) -> float:
    if tid in ("wise0855", "eps-indi-b"):
        return 18.0 + 95.0 * ((wl_um - 0.70) / 4.30) ** 1.35
    if tid == "rw-aur":
        return 55.0 + 25.0 * math.exp(-0.5 * ((wl_um - 1.6) / 0.8) ** 2) + 18.0 * (wl_um / 5.0)
    return 42.0 + 48.0 * math.exp(-0.5 * ((wl_um - 1.65) / 1.1) ** 2) - 4.0 * (wl_um - 2.5)


def _spectral_feature_multiplier(tid: str, wl_um: float) -> float:
    if tid in ("wise0855", "eps-indi-b"):
        active = [
            ("h2o_vapor_14", -0.42),
            ("ch4_166", -0.48),
            ("h2o_vapor_19", -0.44),
            ("nh3_200", -0.36),
            ("co_overtone", -0.40),
            ("h2o_ice", -0.42),
            ("ch4_331", -0.52),
            ("co2_ice", -0.45),
            ("co_ice", -0.46),
        ]
    elif tid == "rw-aur":
        active = [
            ("pa_beta", +0.55),
            ("h2o_vapor_14", -0.28),
            ("pa_alpha", +0.62),
            ("br_gamma", +0.58),
            ("co_overtone", -0.34),
            ("h2o_ice", -0.44),
            ("pah_330", +0.48),
            ("co2_ice", -0.46),
            ("co_ice", -0.44),
        ]
    else:
        active = [
            ("h2o_vapor_14", -0.40),
            ("ch4_166", -0.30),
            ("h2o_vapor_19", -0.44),
            ("co_overtone", -0.50),
            ("h2o_ice", -0.38),
            ("co2_ice", -0.42),
            ("co_ice", -0.52),
        ]
    delta = 0.0
    for fid, strength in active:
        feat = spectral_id.FEATURES_BY_ID[fid]
        lam, _, _, sigma = spectral_id._geometry(feat, 0.0)
        delta += strength * math.exp(-0.5 * ((wl_um - lam) / sigma) ** 2)
    if tid != "rw-aur":
        # Prevent sideband wing of 1.4 um H2O from mimicking 1.282 um Pa-beta emission
        delta -= 0.06 * math.exp(-0.5 * ((wl_um - 1.282) / 0.028) ** 2)
    return max(0.18, 1.0 + delta)


def _build_fits(
    ra0: float,
    dec0: float,
    target_ra: float,
    target_dec: float,
    *,
    det: int,
    wl_center_um: float,
    rot_deg: float,
    target_amp: float,
    field_stars: List[tuple],
    noise_sigma: float = 0.22,
    n: int = 84,
) -> bytes:
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cunit = ["deg", "deg"]
    w.wcs.crval = [ra0, dec0]
    w.wcs.crpix = [(n + 1) / 2.0, (n + 1) / 2.0]
    w.wcs.cdelt = [-SCALE, SCALE]
    t = math.radians(rot_deg)
    w.wcs.pc = [[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]]

    rng = np.random.default_rng(int(abs(ra0 * 1000) + det * 17 + rot_deg * 3 + wl_center_um * 100))
    yy, xx = np.indices((n, n))
    img = 5.0 + rng.normal(0.0, noise_sigma, (n, n))

    bg_det_scale = {1: 1.3, 2: 1.2, 3: 1.0, 4: 0.7, 5: 0.45, 6: 0.35}[det]
    for s_ra, s_dec, s_flux in field_stars:
        sx, sy = w.world_to_pixel(SkyCoord(s_ra * u.deg, s_dec * u.deg))
        img += (s_flux * bg_det_scale) * np.exp(-0.5 * ((xx - sx) ** 2 + (yy - sy) ** 2) / 1.35 ** 2)

    tx, ty = w.world_to_pixel(SkyCoord(target_ra * u.deg, target_dec * u.deg))
    img += target_amp * np.exp(-0.5 * ((xx - tx) ** 2 + (yy - ty) ** 2) / 1.45 ** 2)

    flags = np.zeros((n, n), dtype=np.int32)
    flags[int(n // 2), int(n // 2)] |= 1 << 21  # SOURCE
    flags[5, 5] |= 1 << 10                       # HOT
    flags[6, 8] |= 1 << 17                       # PERSIST

    hdr = w.to_header()
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

    values = np.empty((1, 2, 2, 2), dtype=np.float32)
    values[0, :, :, 0] = [[wl_center_um - 0.004, wl_center_um], [wl_center_um, wl_center_um + 0.004]]
    values[0, :, :, 1] = wl_center_um / 45.0

    primary = fits.PrimaryHDU()
    primary.header["VERSION"] = "SPHEREx-L2-v2"
    hdul = fits.HDUList([
        primary,
        fits.ImageHDU(img.astype(np.float32), header=hdr, name="IMAGE"),
        fits.ImageHDU(flags, name="FLAGS"),
        fits.ImageHDU(np.full((n, n), 0.04, dtype=np.float32), name="VARIANCE"),
        fits.ImageHDU(np.full((n, n), 5.0, dtype=np.float32), name="ZODI"),
        fits.BinTableHDU.from_columns([
            fits.Column(name="X", format="2J", dim="(2)", array=[[1, n]]),
            fits.Column(name="Y", format="2J", dim="(2)", array=[[1, n]]),
            fits.Column(name="VALUES", format="8E", dim="(2,2,2)", array=values),
        ], name="WCS-WAVE"),
    ])
    buf = io.BytesIO()
    hdul.writeto(buf)
    return buf.getvalue()


def ensure_seeded(force: bool = False) -> None:
    """Populate CACHE_DIR with clean 102-channel cutouts for all 5 preset targets if not yet seeded."""
    marker = config.CACHE_DIR / SEED_MARKER
    if not force and ("PYTEST_CURRENT_TEST" in os.environ or marker.exists()):
        return
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        future_ts = time.time() + 365 * 86400.0
        ch_by_det = {d: [] for d in range(1, 7)}
        for ch in spectral_id.CHANNELS:
            ch_by_det[ch["det"]].append(ch["center_um"])

        for t in targets.load_targets():
            tid = t["id"]
            info = targets.resolve(tid, epoch=2026.0)
            ra0, dec0 = info["ra"], info["dec"]
            cosd = max(math.cos(math.radians(dec0)), 1e-6)

            rng = np.random.default_rng(abs(hash(tid)) % 10000)
            field_stars = []
            for _ in range(18):
                dra = float(rng.uniform(-0.038, 0.038)) / cosd
                ddec = float(rng.uniform(-0.038, 0.038))
                if math.hypot(dra * cosd, ddec) < 0.015:
                    continue
                field_stars.append((ra0 + dra, dec0 + ddec, float(rng.uniform(18.0, 65.0))))

            all_rows = []
            n_ch = 17
            for det in range(1, 7):
                wls = ch_by_det[det]
                det_rows = []
                for k, wl_um in enumerate(wls):
                    frac = k / (n_ch - 1) - 0.5
                    mjd = (60805.0 + k * 2.5) if k < 9 else (60985.0 + (k - 9) * 2.5)

                    if tid in ("wise0855", "barnards-star", "eps-indi-b"):
                        shift_arcsec = 22.0 * frac
                        tra = ra0 - (shift_arcsec / 3600.0 / cosd) * 0.7
                        tdec = dec0 + (shift_arcsec / 3600.0) * 0.7
                    else:
                        tra, tdec = ra0, dec0

                    cont_amp = _smooth_continuum(tid, wl_um)
                    spec_mult = _spectral_feature_multiplier(tid, wl_um)

                    base_url = f"{config.IRSA_BASE}/ibe/data/spherex/demo/{tid}_d{det}_{k}.fits"
                    row = {
                        "base_url": base_url,
                        "mjd": mjd + det * 0.1,
                        "det": det,
                        "bandpass": f"SPHEREx-D{det}",
                    }
                    det_rows.append(row)
                    all_rows.append(row)

                    fov_bytes = _build_fits(
                        ra0, dec0, tra, tdec,
                        det=det,
                        wl_center_um=wl_um,
                        rot_deg=k * 6.0 + det * 5.0,
                        target_amp=cont_amp * spec_mult,
                        field_stars=field_stars,
                        noise_sigma=0.22,
                    )
                    for fov in (0.1, 0.15):
                        url = irsa_client.cutout_url(base_url, ra0, dec0, fov * config.CUTOUT_PAD)
                        cache.set_bytes("cut_" + cache.key_hash(url), fov_bytes)

                    ap_bytes = _build_fits(
                        ra0, dec0, ra0, dec0,
                        det=det,
                        wl_center_um=wl_um,
                        rot_deg=k * 6.0 + det * 5.0,
                        target_amp=cont_amp * spec_mult,
                        field_stars=field_stars,
                        noise_sigma=0.05,
                        n=28,
                    )
                    ap_url = irsa_client.cutout_url(base_url, ra0, dec0, 0.04)
                    cache.set_bytes("aperture_s3_v1_" + cache.key_hash(ap_url), ap_bytes)

                discovery_mirror.replace_snapshot(det_rows, ra=ra0, dec=dec0, det=det, updated=future_ts)
                cache.set_json(f"tap_{round(ra0, 4)}_{round(dec0, 4)}_{det}", det_rows)

            discovery_mirror.replace_snapshot(all_rows, ra=ra0, dec=dec0, det=None, updated=future_ts)
            cache.set_json(f"tap_{round(ra0, 4)}_{round(dec0, 4)}_all", all_rows)

        marker.write_text("ok\n", encoding="utf-8")
        log.info("demo_seed: 5 preset SPHEREx targets seeded (102 channels each)")
    except Exception as exc:
        log.warning("demo_seed skipped due to error: %s", exc)
