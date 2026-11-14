# SPHEREx Sky Flipbook

A public web tool that shows how a patch of sky changes over time in NASA's SPHEREx infrared survey, like a little video, so anyone can spot moving objects such as asteroids, comets, nearby stars and brown dwarfs.

Built for the **NASA Space Apps Challenge** by **Oh Deer, We're in Orbit**.

## The challenge

Since 2025, NASA's SPHEREx mission has been mapping the whole sky every six months in 102 bands of near-infrared light. Objects that move, like Pluto when it was discovered, reveal themselves by shifting position between images taken at different times. Volunteer projects such as Backyard Worlds have found most known comets and brown dwarfs this way, but there is far more data than any one person can examine.

The challenge: build a public-facing web tool where a user enters the coordinates of a sky patch (right ascension and declination) and sees a series of SPHEREx images of that patch played like a video.

## What our tool does

- **Flipbook player.** Enter RA/Dec (or pick a target), and play, pause or scrub through the SPHEREx images of that patch in time order.
- **Choose the field of view.** Larger fields keep fast movers in frame, at the cost of longer loading.
- **Choose the detector.** SPHEREx has six detector arrays, each covering a different wavelength range. We show one array at a time, so brightness changes between frames are not mistaken for motion.
- **Add or subtract images.** Average frames within a time bin to improve signal-to-noise, or subtract the median over time so static sources vanish and movers stand out.
- **Spectra on the fly.** Pick a position and get a quick-look spectrum from all exposures across the six arrays (0.75 to 5 micron).
- **Contamination flags.** Bad pixels (persistence, hot pixels, blooms, halos and so on) are masked by default, and each frame shows its flagged fraction.
- **Shareable links.** Coordinates in the URL so the tool can be embedded in other sites and projects.
- **Easy to update.** Each request queries IRSA live, so new weekly data releases appear without code changes.

## Test targets

Barnard's Star, Epsilon Indi B, WISE 0855, RW Aurigae and R Draconis, available as preset buttons.

## How it works

```
Browser (frontend/)  ->  FastAPI backend (backend/)  ->  NASA/IPAC IRSA archive
   flipbook, controls        queries, cutouts, cleaning,       SPHEREx spectral images
   spectrum chart            reprojection, PNG frames          (TAP query + cutout service)
```

1. The backend finds every SPHEREx image covering the position (IRSA TAP query) and downloads small cutouts instead of full images.
2. Each exposure's detector grid is rotated differently on the sky, so every cutout is reprojected onto one common north-up grid. Static objects stay put and movers move.
3. Bad-flag pixels are masked, a shared brightness stretch is applied, and frames are returned as PNGs with date, wavelength and flag metadata.

The full API is described in [`backend/API.md`](backend/API.md).

## Run it locally

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements-dev.txt

pytest -q                       # offline tests
python scripts/smoke_test.py    # checks the real connection to IRSA
uvicorn app.main:app --reload   # http://127.0.0.1:8000  (API docs at /docs)
```

If a `frontend/` folder sits next to `backend/`, the server serves it at `/`. Before a demo, warm the cache with `python scripts/prefetch_targets.py`.

## Project structure

```
backend/    FastAPI service, IRSA client, image processing, tests
frontend/   Web interface
```

## Limitations

- Spectra are quick-look aperture photometry, not calibrated point-source photometry.
- The field is centred on a fixed position, so very fast movers need a larger field of view.
- The first request for a new position is slow (IRSA query plus downloads); demo targets are pre-cached.

## Team
Burak Yeniceri
Emre Fikri Kocak
Nilsu Doganci
Orkun Demirci
Zeynep Azra Yildirim

## Data and credits

- Data: NASA's SPHEREx mission, served by the NASA/IPAC Infrared Science Archive (IRSA) at Caltech/IPAC.
- Tools: Astropy, astroquery, reproject, FastAPI, [Aladin Lite](https://aladin.cds.unistra.fr/AladinLite/) from CDS if used.
- Built during the NASA Space Apps Challenge. This project is not endorsed by NASA.
