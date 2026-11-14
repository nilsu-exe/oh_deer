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
It also compares the aperture download's wavelength, flux, and uncertainty with
an IRSA cutout from the same exposure.

## Layout

| File | Job |
|---|---|
| `app/irsa_client.py` | TAP query for images covering a position, parallel cutout download, retries |
| `app/discovery_mirror.py` | Local SQLite snapshots and spherical footprint index |
| `app/tap_metadata.py` | Bounded TAP metadata exports, including overflow checks |
| `scripts/sync_discovery.py` | Bootstrap, refresh, and inspect the discovery mirror |
| `app/s3_cutouts.py` | S3 range reads for aperture spectra, skipping EPSF and ZODI data |
| `app/processing.py` | Parse cutout, wavelength per pixel, flag masking, reprojection, PNG encoding, aperture photometry |
| `app/pipeline.py` | `build_frames()` and `build_spectrum()`: ties the above together, binning, subtraction |
| `app/targets.py`, `targets.json` | The 5 challenge targets, name resolution (SIMBAD), proper motion |
| `app/main.py` | HTTP endpoints (see `API.md`) |
| `app/config.py` | Every tunable: pixel scale, parallelism, flag bits, TTLs |
| `tests/` | Offline tests with synthetic rotated frames |

## How it updates with new data

Nothing is hard-coded to a release. Discovery first checks the local SQLite mirror.
An available snapshot is served immediately, including during an IRSA outage.
Outside mirrored coverage, the default `auto` mode uses the six-hour TAP cache
and then live TAP, saving successful queries into the mirror. Cutouts are cached
forever (their URLs include the processing version).

## Local discovery mirror

The mirror stores only the image URLs, times, detectors, and footprints this app
needs. It does not implement a general TAP/ADQL server or copy image data.
Initialize it from saved discovery queries without contacting IRSA:

```bash
python scripts/sync_discovery.py --seed-cache --status
```

Saved point queries cover only their original coordinate key (rounded to four
decimal places, matching the old cache) and detector selection. Their original
timestamps are retained. They do not imply coverage of adjacent coordinates.
Six detector snapshots are required for an all-detector spectrum.

Build full-sky coverage when TAP is available:

```bash
python scripts/sync_discovery.py --sync --det 2 --timeout 300
python scripts/sync_discovery.py --sync --timeout 600 --attempts 10
```

The second command exports each of the six detectors separately. Completed,
recent detectors are skipped on rerun; `--force` refreshes them again. Each
detector import commits atomically. HTTP failures, interrupted exports, invalid
footprints, and TAP `QUERY_STATUS=ERROR` or `OVERFLOW` leave the previous snapshot
intact. Exports are capped at one million rows and 512 MiB per detector; raise
`--maxrec` / `--max-mb` if necessary. These limits fail explicitly rather than
publishing truncated coverage. Temporary parsing/import memory can exceed the
download size. Full-sky exports still depend on an operational upstream service.

Full-sky queries use a SQLite 3-D R-tree followed by spherical polygon containment,
including fields crossing RA=0 and the poles. An empty result is authoritative
only within a completed snapshot. A partial set of detector exports never claims
to cover all detectors.

Settings, read at startup:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPHEREX_DISCOVERY_MIRROR` | `<cache_dir>/discovery.sqlite3` | Persistent mirror database |
| `SPHEREX_DISCOVERY_MODE` | `auto` | Prefer local snapshots, use TAP for missing coverage |
| `SPHEREX_TAP_TIMEOUT` | `30` seconds | Live TAP HTTP timeout and export time budget |

Set `SPHEREX_DISCOVERY_MODE=local` to prohibit remote discovery entirely; missing
coverage returns HTTP 503. Set it to `tap` to bypass the mirror and use the
original six-hour cache/live discovery path. Existing snapshots are not refreshed
implicitly: rerun `--sync` to incorporate new ingests or reprocessing. Responses
from the mirror include `discovery.source`, `scope`, `oldest_snapshot_utc`,
`age_seconds`, and `stale` (older than `SPHEREX_TAP_TTL`). Stale snapshots remain
usable during outages, but do not include data ingested after their snapshot.
`GET /api/discovery/status` reports the currently completed snapshots and counts.

Spectrum downloads first reuse cached cutouts, then read small regions directly
from the public SPHEREx S3 mirror over HTTPS. Only IMAGE, FLAGS, VARIANCE, and
the WCS-WAVE lookup table are retained; aperture photometry needs neither EPSF
nor ZODI. Astropy uses lazy HDUs and range reads with read-ahead disabled to
avoid transferring unused extensions. Whole image row strips reduce request
count; compressed FLAGS may still require multiple tile reads. Celestial/SIP
and alternate wavelength WCS reference pixels are shifted to the cropped origin.
Compact aperture cutouts have a separate, versioned cache key so they cannot be
used accidentally for frame rendering with zodiacal subtraction. Missing mirror
objects and failed partial reads fall back to the existing IRSA cutout service,
with bounded parallelism, timeouts, retries, and partial-failure reporting.
See [IRSA's cloud layout](https://irsa.ipac.caltech.edu/cloud_access/) and
[Astropy's remote FITS guidance](https://docs.astropy.org/en/stable/io/fits/usage/cloud.html).

## Request diagnostics

The backend logs each API request immediately, with a short request ID and total
elapsed time on completion. The same ID appears in the response's `X-Request-ID`
header. Discovery failures return HTTP 502 with that ID in the JSON body.

Stage start/finish logs cover name resolution, TAP discovery, cutout downloads,
FITS processing/reprojection or photometry, and stacking/PNG encoding. Every
10 seconds, an unfinished request logs its current stage, stage duration, and
progress. A request waiting for a free API worker is identified separately.
Download progress reports completed/failed/pending counts as files finish,
while the returned frames keep their chronological order. TAP cache hits and
misses are logged at INFO. Cutout cache hits and individual HTTP starts/successes
are available at DEBUG. Failed HTTP attempts include their duration, URL, attempt
number, and next retry delay.

```text
18:40:00 INFO [a12b34cd] app.pipeline: discovery started ra=133.795130 dec=-7.245147 detector=2
18:40:10 INFO [a12b34cd] app.diagnostics: request running elapsed=10.0s stage=discovery stage_elapsed=10.0s
18:40:20 ERROR [a12b34cd] app.irsa_client: TAP HTTP query failed elapsed=20.1s error=DALServiceError: 502 Server Error: Proxy Error ...
18:40:20 INFO [a12b34cd] app.diagnostics: request finished status=502 elapsed=20.10s
```

These settings are read at startup:

- `SPHEREX_LOG_LEVEL=INFO` (default), or `DEBUG` for per-file details.
- `SPHEREX_LOG_HEARTBEAT=10` (seconds); set `0` to disable periodic messages.

For example: `SPHEREX_LOG_LEVEL=DEBUG uvicorn app.main:app --reload`.

A TAP `502 Proxy Error` occurs before cutouts or image processing begin. It is
an upstream discovery failure. Cutout requests retain their 120-second timeout
and three attempts, so a failing cold request can take minutes, with additional
time waiting for the shared download slots. Heartbeats expose those waits; they
do not retry live TAP discovery. Live TAP calls now have a 30-second timeout
(`SPHEREX_TAP_TIMEOUT`); mirrored requests make no TAP call.

An aperture batch logs `source=S3 aperture ranges (IRSA fallback)`. Individual
range-cutout timings, output sizes, and cache hits are available at DEBUG; S3
failures, retries, and fallbacks are logged at INFO/WARNING. S3 changes the
download stage only. The local discovery mirror is what avoids TAP calls during
an outage for already mirrored coordinates/detectors.

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
  Those proper-motion values were written from memory: **verify them**, and check each target
  actually appears in the first frames. Epsilon Indi B uses the working CDS identifier
  `eps Ind B`; the former `eps Ind Ba` identifier does not resolve.
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
