# Agent guide

## Purpose and scope

Build toward the hackathon's public-facing SPHEREx sky flipbook: users enter Right
Ascension/declination, view the same sky patch over time, and spot motion or brightness
changes. The challenge describes an all-sky survey every six months in 102 near-infrared
bands. Favor an approachable discovery experience without hiding scientific caveats.

This repository currently contains the **Python/FastAPI backend only**, not a frontend.
Do not describe proposed UI features as already implemented. Useful future directions
include accessible playback/blinking, shareable coordinate URLs, selectable FOV,
stacking/differencing, spectra, contamination metadata, and Gaia/WISE overlays.

## Getting started

Run commands from this repository root. Existing docs and script docstrings mention
`cd backend`, but there is no nested `backend/` directory here.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
uvicorn app.main:app --reload
```

The API runs at `http://localhost:8000`; interactive OpenAPI docs are at `/docs`.
`requirements-dev.txt` includes runtime dependencies plus pytest and httpx. There is
no configured formatter, linter, CI workflow, or frontend build system in this checkout.
Follow the surrounding Python style and avoid adding unrelated tooling.

## Repository map

- `app/main.py`: FastAPI routes, parameter validation, coordinate/name selection, CORS.
- `app/irsa_client.py`: IRSA TAP discovery, cutout URLs, bounded parallel downloads,
  retries, and raw-data caching.
- `app/processing.py`: FITS parsing, wavelength lookup, masks, WCS reprojection,
  shared-stretch PNG encoding, and aperture photometry.
- `app/pipeline.py`: frame/spectrum orchestration, time binning, median subtraction,
  subsampling, metadata, and partial-failure accounting.
- `app/targets.py` / `app/targets.json`: challenge presets, Sesame name resolution,
  and linear proper-motion propagation.
- `app/config.py`: processing/network constants and environment configuration.
- `app/cache.py`: atomic disk-cache writes for JSON and bytes.
- `tests/synth.py`: synthetic rotated multi-extension FITS fixtures.
- `tests/test_backend.py`: offline processing and API tests; `tests/conftest.py`
  sets import paths and defaults the test cache to a temporary directory.
- `scripts/smoke_test.py`: live IRSA integration check.
- `scripts/prefetch_targets.py`: warm raw-data caches for demo targets.
- `README.md`: design rationale and limitations. `API.md`: frontend-facing contract.

Keep route changes, `API.md`, tests, and relevant README guidance consistent.
The optional static mount in `app/main.py` looks for a **sibling** `../frontend/`
relative to this repository root, not `./frontend/`. Update that deliberately if
introducing a frontend here.

## Scientific and API invariants

- Positions are ICRS degrees; `/api/frames` and `/api/spectrum` accept `ra` + `dec`
  or `target`. A supplied target takes precedence. Preserve documented units and
  validation when extending endpoints.
- A flipbook uses **one detector at a time** (1–6; default 2). Each array spans a
  different wavelength range; mixing arrays can mimic variability. Wavelength also
  varies within an array, so even same-detector differences can leave spectral residuals.
- Reproject exposures onto one common north-up, east-left TAN grid. Raw detector
  rotation must not make stationary sources appear to move. Cutouts use a 1.45×
  FOV margin to cover rotated corners; the default output scale is 6.15 arcsec/pixel.
- Array rows increase northward, but PNGs are vertically flipped: PNG origin is
  top-left. `/api/pixel_to_sky` reverses that flip. Keep click coordinates aligned
  with the returned image grid, including when displayed at a different CSS size.
- Mask bad flag bits to NaN, rendered transparent. Do **not** mask `SOURCE` (bit 21).
  Mask definitions live in `app/config.py`; preserve the `mask=false` option.
- All frames in a response share one brightness stretch. Per-frame normalization
  would misrepresent brightness changes. Automatic stretch is asinh normally and
  linear for median-subtracted frames.
- Preserve chronological frames, MJD/UTC dates, wavelengths in micrometers,
  processing versions, flag fractions, stack counts, and download-failure counts.
  Empty coverage and individual failed cutouts should not crash an entire flipbook.
- Spectra are quick-look aperture sums minus local background, not calibrated
  point-source photometry. Images are MJy/sr; returned flux/error values are Jy.
  Spectrum points are sorted by wavelength, not time. Retain the caveat about no
  aperture correction/PSF fitting and possible motion/neighbour contamination.

## Data access and caching

Discovery queries `spherex.artifact` joined to `spherex.plane` through IRSA TAP;
cutouts are downloaded as multi-extension FITS. Keep discovery release-independent
so new ingests become available without hard-coded exposure lists.

Environment variables (read when configuration is imported):

| Variable | Default | Purpose |
| --- | --- | --- |
| `SPHEREX_CACHE_DIR` | `.cache/` in this repository | Persistent JSON/cutout cache |
| `SPHEREX_TAP_TTL` | `21600` seconds | Refresh discovery results after six hours |
| `SPHEREX_MAX_PARALLEL` | `10` | Download worker count and global semaphore limit |

Cutout bytes are cached indefinitely by URL hash; URLs are expected to identify
processing versions. Only discovery results have a TTL; name resolutions are also
cached indefinitely. Do not commit `.cache/`, `.venv/`, downloaded FITS, or test caches.
Retain atomic writes, bounded concurrency, timeouts, retries, and partial-failure reporting.

## Validation and demo readiness

- Run `python -m pytest -q` after changes. Add offline synthetic tests for changed
  processing behavior and TestClient coverage for changed API behavior.
- Offline tests monkeypatch IRSA calls **and wavelength-map generation**; passing
  them does not validate real TAP schemas, cutout URLs, or WCS-WAVE lookup tables.
- On a machine with outbound IRSA access, run `python scripts/smoke_test.py` before
  claiming the live pipeline works. It checks discovery, FITS download/parsing,
  detector wavelength sanity, and end-to-end PNG output. Report network checks
  separately from offline tests; do not silently substitute synthetic data.
- Before a demo, optionally run `python scripts/prefetch_targets.py --fov 0.1`
  (add `--spectra` for spectra). This can download many files. Match the UI's FOV,
  coordinates/epoch, and detector choices; the script's default FOV is 0.15°, while
  the API default is 0.1°. It warms raw-data caches, not pre-rendered responses.

Validate all five challenge presets: **Barnard's Star**, **Epsilon Indi B (Ba/Bb)**,
**WISE 0855-0714**, **RW Aurigae**, and **R Draconis**. WISE 0855's challenge position
is 08h55m10.831s, −07°14′42.53″ (about 133.79513°, −7.245147°), stored directly for
offline resolution. Other presets require external name resolution unless cached.

Proper-motion values and resolver names in `targets.json` are explicitly unverified
in the README: check authoritative sources before changing them or presenting them
as validated. The current model treats preset base coordinates as J2000 and defaults
to epoch 2026.0; `pm_ra_masyr` includes cos(dec). WISE 0855 currently has no motion
propagation. Grids stay centered on a fixed position, not a per-exposure motion
model, so fast movers may require a larger FOV. Confirm targets are actually in the
returned frames rather than assuming successful resolution proves coverage.
