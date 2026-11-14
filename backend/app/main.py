"""FastAPI app. Run from backend/ with uvicorn app.main:app --reload."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

if __package__ in (None, ""):
    # Support python app/main.py as well as importing app.main.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import config, pipeline, processing, targets
else:
    from . import config, pipeline, processing, targets

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="SPHEREx sky flipbook API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

log = logging.getLogger(__name__)

@app.middleware("http")
async def log_request_start(request, call_next):
    log.info("request started: %s %s", request.method, request.url.path)
    response = await call_next(request)
    log.info("request completed: %s %s (%s)", request.method, request.url.path,
             response.status_code)
    return response


def _center(ra: Optional[float], dec: Optional[float], target: Optional[str],
            epoch: Optional[float]):
    """Accept either ra+dec (degrees) or a target name. Returns (ra, dec, info)."""
    if target:
        try:
            info = targets.resolve(target, epoch)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return info.get("ra"), info.get("dec"), info
    if ra is None or dec is None:
        raise HTTPException(status_code=422, detail="Provide ra and dec (degrees) or target.")
    if not (0.0 <= ra < 360.0 and -90.0 <= dec <= 90.0):
        raise HTTPException(status_code=422, detail="ra must be in [0,360) and dec in [-90,90].")
    return ra, dec, {"name": None, "ra": ra, "dec": dec, "pm_applied": False}


@app.get("/api/health")
def health():
    return {"status": "ok", "cache_dir": str(config.CACHE_DIR)}


@app.get("/api/targets")
def list_targets():
    """The five challenge test targets (names + notes)."""
    return targets.load_targets()


@app.get("/api/resolve")
def resolve(name: str, epoch: Optional[float] = None):
    try:
        return targets.resolve(name, epoch)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/frames")
def frames(
    ra: Optional[float] = None,
    dec: Optional[float] = None,
    target: Optional[str] = Query(None, description="Name instead of ra/dec"),
    epoch: Optional[float] = Query(None, description="Decimal year for proper-motion targets"),
    fov: float = Query(0.1, gt=0.005, le=1.0, description="Field of view, degrees"),
    det: int = Query(2, ge=1, le=6, description="Detector array 1-6"),
    bin_days: float = Query(0.0, ge=0.0, description="Average frames within N-day bins (0 = off)"),
    sky: Literal["none", "zodi", "median"] = "median",
    subtract: Literal["none", "median"] = Query("none", description="Subtract per-pixel median over time"),
    mask: bool = True,
    scale: float = Query(config.PIXEL_SCALE_ARCSEC, ge=1.0, le=30.0),
    max_frames: int = Query(200, ge=0, le=2000),
    stretch: Literal["auto", "linear", "asinh"] = "auto",
):
    ra, dec, info = _center(ra, dec, target, epoch)
    result = pipeline.build_frames(ra, dec, fov, det, bin_days=bin_days, sky=sky,
                                   subtract=subtract, mask=mask, scale=scale,
                                   max_frames=max_frames, stretch=stretch)
    result["resolved"] = info
    return result


@app.get("/api/spectrum")
def spectrum(
    ra: Optional[float] = None,
    dec: Optional[float] = None,
    target: Optional[str] = None,
    epoch: Optional[float] = None,
    aperture_px: float = Query(1.5, ge=0.5, le=5.0),
    size: float = Query(0.04, ge=0.02, le=0.2, description="Cutout size per exposure, degrees"),
    max_frames: int = Query(600, ge=0, le=3000),
    dets: Optional[str] = Query(None, description="Comma-separated detectors, e.g. '1,2'"),
):
    ra, dec, info = _center(ra, dec, target, epoch)
    det_list = None
    if dets:
        try:
            det_list = [int(d) for d in dets.split(",")]
        except ValueError:
            raise HTTPException(status_code=422, detail="dets must be comma-separated integers")
    result = pipeline.build_spectrum(ra, dec, size=size, aperture_px=aperture_px,
                                     max_frames=max_frames, dets=det_list)
    result["resolved"] = info
    return result


@app.get("/api/pixel_to_sky")
def pixel_to_sky(ra: float, dec: float, fov: float = 0.1,
                 scale: float = config.PIXEL_SCALE_ARCSEC, x: float = 0.0, y: float = 0.0):
    """Convert a click on a returned frame to sky coordinates.

    x, y are PNG pixel coordinates (origin top-left, y down) of a frame returned
    by /api/frames with the same ra, dec, fov, scale.
    """
    grid, shape = processing.make_grid(ra, dec, fov, scale)
    row = shape[0] - 1 - y          # PNG rows are flipped vs array rows
    ra_out, dec_out = grid.wcs_pix2world([[x, row]], 0)[0]
    return {"ra": float(ra_out), "dec": float(dec_out)}


# The frontend is a sibling of backend/ in the repository root.
_frontend = Path(__file__).resolve().parent.parent.parent / "frontend"
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
