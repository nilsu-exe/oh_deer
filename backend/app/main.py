"""FastAPI app. Run from backend/ with uvicorn app.main:app --reload."""
from __future__ import annotations

from contextlib import asynccontextmanager
import sys
from pathlib import Path
from typing import Annotated, List, Literal, Optional, Union

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

if __package__ in (None, ""):
    # Support python app/main.py as well as importing app.main.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import config, demo_seed, diagnostics, discovery_mirror, irsa_client, overlays, pipeline, processing, spectral_id, targets
else:
    from . import config, demo_seed, diagnostics, discovery_mirror, irsa_client, overlays, pipeline, processing, spectral_id, targets

diagnostics.configure_logging()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    demo_seed.ensure_seeded()
    yield


app = FastAPI(title="SPHEREx sky flipbook API", version="0.1.0", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(diagnostics.RequestLoggingMiddleware)


@app.exception_handler(irsa_client.DiscoveryError)
async def discovery_error(request, exc):
    return JSONResponse(status_code=502, content={
        "detail": "IRSA image discovery failed. The upstream TAP service may be unavailable; try again shortly.",
        "request_id": diagnostics.request_id(),
    })


@app.exception_handler(irsa_client.MirrorUnavailable)
async def mirror_unavailable(request, exc):
    return JSONResponse(status_code=503, content={
        "detail": str(exc), "request_id": diagnostics.request_id(),
    })

def _center(ra: Optional[Union[float, str]], dec: Optional[Union[float, str]],
            target: Optional[str], epoch: Optional[float]):
    """Accept ra+dec (degrees or sexagesimal HMS/DMS) or a target name/coord string."""
    demo_seed.ensure_seeded()
    if target:
        try:
            with diagnostics.stage(targets.log, "resolution", f"target={target} epoch={epoch}"):
                info = targets.resolve(target, epoch)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return info.get("ra"), info.get("dec"), info
    if ra is None or dec is None:
        raise HTTPException(status_code=422, detail="Provide ra and dec (degrees or HMS/DMS) or target.")
    try:
        ra_deg = targets.parse_ra(ra)
        dec_deg = targets.parse_dec(dec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    sex = targets.format_sexagesimal(ra_deg, dec_deg)
    return ra_deg, dec_deg, {"name": None, "ra": ra_deg, "dec": dec_deg, **sex, "pm_applied": False}


@app.get("/api/health")
def health():
    return {"status": "ok", "cache_dir": str(config.CACHE_DIR)}


@app.get("/api/discovery/status")
def discovery_status():
    return {"mode": config.DISCOVERY_MODE, **discovery_mirror.status()}


@app.get("/api/targets")
def list_targets():
    """The five challenge test targets (names + notes)."""
    return targets.load_targets()


@app.get("/api/resolve")
def resolve(name: str, epoch: Optional[float] = None):
    try:
        with diagnostics.stage(targets.log, "resolution", f"name={name} epoch={epoch}"):
            return targets.resolve(name, epoch)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/frames")
def frames(
    ra: Optional[Union[float, str]] = None,
    dec: Optional[Union[float, str]] = None,
    target: Optional[str] = Query(None, description="Target name or coordinate string instead of ra/dec"),
    epoch: Optional[float] = Query(None, description="Decimal year for proper-motion targets"),
    fov: float = Query(0.1, gt=0.005, le=1.0, description="Field of view, degrees"),
    det: int = Query(2, ge=1, le=6, description="Detector array 1-6"),
    bin_days: float = Query(0.0, ge=0.0, description="Average frames within N-day bins (0 = off)"),
    sky: Literal["none", "zodi", "median"] = "median",
    subtract: Literal["none", "median", "first", "prev"] = Query(
        "none",
        description="Subtraction mode: 'none', 'median' (over all frames), 'first' (vs frame 0), 'prev' (vs previous frame)",
    ),
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
    diagnostics.set_phase("response serialization")
    return result


def _parse_dets(dets: Optional[str]) -> Optional[List[int]]:
    if not dets or not isinstance(dets, str):
        return None
    try:
        det_list = [int(d) for d in dets.split(",") if d.strip()]
    except ValueError:
        raise HTTPException(status_code=422, detail="dets must be comma-separated integers")
    if not det_list or any(d not in range(1, 7) for d in det_list):
        raise HTTPException(status_code=422, detail="dets must be detector numbers 1-6")
    return det_list


@app.get("/api/detectors")
def detector_stacks(
    ra: Optional[Union[float, str]] = None,
    dec: Optional[Union[float, str]] = None,
    target: Optional[str] = None,
    epoch: Optional[float] = None,
    fov: Annotated[float, Query(gt=0.005, le=1.0, description="Field of view, degrees")] = 0.1,
    dets: Annotated[Optional[str], Query(description="Comma-separated detectors 1-6 (default: all 6)")] = None,
    sky: Literal["none", "zodi", "median"] = "median",
    mask: bool = True,
    scale: Annotated[float, Query(ge=1.0, le=30.0)] = config.PIXEL_SCALE_ARCSEC,
    max_per_det: Annotated[int, Query(ge=1, le=300, description="Max exposures stacked per detector array")] = 30,
    stretch: Literal["asinh", "linear"] = "asinh",
):
    """Separately co-add all exposures for each of the 6 detector arrays (D1..D6)
    and return an All-Detector Deep Stack + False-Color Infrared RGB composite.
    """
    ra_deg, dec_deg, info = _center(ra, dec, target, epoch)
    det_list = _parse_dets(dets)
    result = pipeline.build_detector_stacks(
        ra_deg, dec_deg, fov, dets=det_list, sky=sky, mask=mask,
        scale=scale, max_per_det=max_per_det, stretch=stretch,
    )
    result["resolved"] = info
    diagnostics.set_phase("response serialization")
    return result


@app.get("/api/spectrum")
def spectrum(
    ra: Optional[Union[float, str]] = None,
    dec: Optional[Union[float, str]] = None,
    target: Optional[str] = None,
    epoch: Optional[float] = None,
    aperture_px: Annotated[float, Query(ge=0.5, le=5.0)] = 1.5,
    size: Annotated[float, Query(ge=0.02, le=0.2, description="Cutout size per exposure, degrees")] = 0.04,
    max_frames: Annotated[int, Query(ge=0, le=3000)] = 600,
    dets: Annotated[Optional[str], Query(description="Comma-separated detectors, e.g. '1,2'")] = None,
    z: Annotated[Optional[float], Query(ge=0.0, le=10.0, description="Optional known redshift override")] = None,
):
    ra, dec, info = _center(ra, dec, target, epoch)
    det_list = _parse_dets(dets)
    result = pipeline.build_spectrum(ra, dec, size=size, aperture_px=aperture_px,
                                     max_frames=max_frames, dets=det_list, z=z)
    result["resolved"] = info
    diagnostics.set_phase("response serialization")
    return result


@app.get("/api/lightcurve")
def lightcurve(
    ra: Optional[Union[float, str]] = None,
    dec: Optional[Union[float, str]] = None,
    target: Optional[str] = None,
    epoch: Optional[float] = None,
    aperture_px: Annotated[float, Query(ge=0.5, le=5.0)] = 1.5,
    size: Annotated[float, Query(ge=0.02, le=0.2, description="Cutout size per exposure, degrees")] = 0.04,
    max_frames: Annotated[int, Query(ge=0, le=3000)] = 600,
    dets: Annotated[Optional[str], Query(description="Comma-separated detectors, e.g. '1,2'")] = None,
    bin_days: Annotated[float, Query(ge=0.1, le=180.0, description="Epoch bin width in days")] = 5.0,
):
    """Extract a wavelength-detrended light curve and variability metrics (RW Aurigae, R Draconis)."""
    ra_deg, dec_deg, info = _center(ra, dec, target, epoch)
    det_list = _parse_dets(dets)
    spec = pipeline.build_spectrum(ra_deg, dec_deg, size=size, aperture_px=aperture_px,
                                   max_frames=max_frames, dets=det_list)
    binned = (spec.get("analysis") or {}).get("binned_102")
    var = spectral_id.analyze_variability(spec.get("points", []), binned, bin_days=bin_days)
    diagnostics.set_phase("response serialization")
    return {
        "query": {**spec["query"], "bin_days": bin_days},
        "resolved": info,
        "counts": spec["counts"],
        "variability": var,
        "discovery": spec.get("discovery"),
    }


class SpectrumPoint(BaseModel):
    model_config = ConfigDict(extra="allow")
    wavelength_um: float = Field(..., gt=0.0)
    flux_jy: float
    err_jy: Optional[float] = Field(None, ge=0.0)


class AnalyzeRequest(BaseModel):
    points: List[SpectrumPoint]
    z: Optional[float] = Field(None, ge=0.0, le=10.0, description="Known redshift (skip estimation)")


@app.post("/api/spectrum/analyze")
def analyze_spectrum(req: AnalyzeRequest):
    """Feature identification + redshift + distances for supplied points (no IRSA query)."""
    points = [p.model_dump() for p in req.points]
    return spectral_id.analyze_spectrum(points, known_z=req.z)


@app.get("/api/spectral_features")
def spectral_features():
    """Reference feature library and the 102-channel grid used by the analysis."""
    return {"features": [f.to_dict() for f in spectral_id.SPECTRAL_FEATURES],
            "channels": spectral_id.CHANNELS}


@app.get("/api/overlays")
def catalog_overlays(
    ra: Optional[Union[float, str]] = None,
    dec: Optional[Union[float, str]] = None,
    target: Optional[str] = None,
    epoch: Optional[float] = None,
    fov: float = Query(0.1, gt=0.005, le=1.0, description="Field of view, degrees"),
    scale: float = Query(config.PIXEL_SCALE_ARCSEC, ge=1.0, le=30.0),
    catalogs: str = Query("gaia,wise", description="Comma-separated catalogs: 'gaia', 'wise'"),
    max_sources: int = Query(150, ge=1, le=500),
):
    """Overlay astronomical catalog sources (Gaia DR3 & AllWISE) onto the frame pixel grid."""
    ra_deg, dec_deg, info = _center(ra, dec, target, epoch)
    cat_list = [c.strip().lower() for c in catalogs.split(",") if c.strip()]
    invalid = [c for c in cat_list if c not in ("gaia", "wise")]
    if invalid or not cat_list:
        raise HTTPException(status_code=422, detail="catalogs must be a comma-separated subset of 'gaia,wise'.")
    result = overlays.build_overlays(
        ra_deg,
        dec_deg,
        fov,
        scale=scale,
        epoch=epoch,
        catalogs=cat_list,
        max_sources=max_sources,
    )
    result["resolved"] = info
    diagnostics.set_phase("response serialization")
    return result


@app.get("/api/pixel_to_sky")
def pixel_to_sky(ra: Union[float, str], dec: Union[float, str], fov: float = 0.1,
                 scale: float = config.PIXEL_SCALE_ARCSEC, x: float = 0.0, y: float = 0.0):
    """Convert a click on a returned frame to sky coordinates.

    x, y are PNG pixel coordinates (origin top-left, y down) of a frame returned
    by /api/frames with the same ra, dec, fov, scale.
    """
    try:
        ra_deg = targets.parse_ra(ra)
        dec_deg = targets.parse_dec(dec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    grid, shape = processing.make_grid(ra_deg, dec_deg, fov, scale)
    row = shape[0] - 1 - y          # PNG rows are flipped vs array rows
    ra_out, dec_out = grid.wcs_pix2world([[x, row]], 0)[0]
    ra_f, dec_f = float(ra_out), float(dec_out)
    return {"ra": ra_f, "dec": dec_f, **targets.format_sexagesimal(ra_f, dec_f)}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
