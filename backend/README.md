# SPHEREx sky flipbook: backend

FastAPI service that turns "RA, Dec, field of view" into a time-ordered series of SPHEREx
images (reprojected onto one north-up grid, so moving objects move and everything else stays
put), plus on-the-fly spectra.

## Start here (10 minutes)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest -q                          # 1. offline tests, synthetic data, no network needed
python scripts/smoke_test.py       # 2. DO THIS FIRST on a machine that can reach IRSA
uvicorn app.main:app --reload      # 3. API on http://localhost:8000  (interactive docs: /docs)
python scripts/prefetch_targets.py # 4. before the demo: warm the cache for the 5 test targets
```

**Step 2 matters most.** The IRSA parts (TAP query, cutout URLs, WCS-WAVE wavelength lookup)
are copied from the IRSA tutorials but were *not* run against IRSA when this was written.
`smoke_test.py` checks each assumption and tells you which one is wrong, if any.

## Layout

| File | Job |
|---|---|
| `app/irsa_client.py` | TAP query for images covering a position, parallel cutout download, retries |
| `app/processing.py` | Parse cutout, wavelength per pixel, flag masking, reprojection, PNG encoding, aperture photometry |
| `app/pipeline.py` | `build_frames()` and `build_spectrum()`: ties the above together, binning, subtraction |
| `app/targets.py`, `targets.json` | The 5 challenge targets, name resolution (SIMBAD), proper motion |
| `app/main.py` | HTTP endpoints (see `API.md`) |
| `app/config.py` | Every tunable: pixel scale, parallelism, flag bits, TTLs |
| `tests/` | Offline tests with synthetic rotated frames |

## How it updates with new data

Nothing is hard-coded to a release. Each request queries IRSA's TAP tables live, so weekly
ingests appear automatically. Image lists are cached for 6 h (`SPHEREX_TAP_TTL`, seconds);
cutouts are cached forever (their URLs include the processing version).

## Design decisions worth knowing

- **One detector per flipbook.** Each of the six arrays covers a different wavelength range;
  mixing them would make brightness changes look like motion or variability.
- **Reprojection.** Detector grids are rotated against the sky, differently in each exposure.
  Frames are reprojected onto a common north-up grid. Cutouts are requested 1.45x larger than
  the FOV so rotated corners are still covered.
- **Flag masking.** Persistence, blooms, halos, streaks, hot/cold pixels, etc. are set to NaN
  (shown transparent); they look like faint movers. `SOURCE` is not masked. Toggle with `mask=false`.
- **Shared brightness stretch** across all frames in a response, so frames are comparable.

## Known limitations / TODO

- **Fast movers.** The grid is centred on a fixed position, not tracked. For WISE 0855 and other
  fast movers use a larger FOV. Per-frame centring on a motion model would be a good extension.
- **Proper motion.** SIMBAD positions are J2000. Barnard's Star moves ~10"/yr, so by 2026 it is
  ~4.5' from its J2000 position; `targets.json` applies proper motion for targets that list it.
  Those proper-motion values and the SIMBAD names (`eps Ind Ba`, ...) were written from memory:
  **verify them**, and check each target actually appears in the first frames.
- **Median subtraction (`subtract=median`)** removes static sources, but since each exposure sees
  a star at a *different wavelength*, bright stars leave residuals. Best on a single narrow band.
- **Spectra are quick-look.** Aperture sum minus local background, no aperture correction or
  PSF fit (the `EPSF` extension is the route to proper photometry). A moving object or a
  neighbour in the aperture contaminates points.
- **Name resolution** needs outbound access to SIMBAD/Sesame. `ra`/`dec` always work offline of that.
- First request for a new position is slow (TAP ~20 s plus downloads). Prefetch the demo
  targets, and show a loading state in the UI.

## Deploying

Any host that runs Python: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Set
`SPHEREX_CACHE_DIR` to a persistent disk if the host has one. If a `frontend/` folder sits next to
`backend/`, the API serves it at `/` so one deployment does everything.
