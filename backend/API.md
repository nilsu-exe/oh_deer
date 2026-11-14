# API contract (frontend: build against this)

Base URL `http://localhost:8000`. All endpoints are `GET`, JSON, CORS open. Interactive docs at `/docs`.
Every endpoint that takes a position accepts **either** `ra` + `dec` (ICRS degrees) **or** `target` (name).

## `GET /api/frames`: the flipbook

| Param | Default | Meaning |
|---|---|---|
| `ra`, `dec` / `target` | required | Position, or a name such as `wise0855` or `Barnard's Star` |
| `epoch` | 2026.0 | Decimal year; only used with `target` for high-proper-motion targets |
| `fov` | 0.1 | Field of view in degrees (max 1.0). Larger = longer load |
| `det` | 2 | Detector 1-6. Wavelength ranges: D1 0.75-1.09, D2 1.10-1.62, D3 1.63-2.41, D4 2.42-3.82, D5 3.83-4.41, D6 4.42-5.00 um |
| `bin_days` | 0 | Average frames inside N-day bins (higher S/N); 0 = off |
| `sky` | `median` | Background removal: `none`, `zodi` (ZODI model), `median` (per-frame median) |
| `subtract` | `none` | `median` subtracts the per-pixel median over time, so static sources vanish |
| `mask` | true | Mask bad-flag pixels (transparent) |
| `max_frames` | 200 | Evenly thin the frame list (0 = all) |
| `stretch` | `auto` | `asinh`, `linear`; `auto` = linear when subtracting |
| `scale` | 6.15 | Output pixel scale, arcsec/pixel |

Response (abbreviated):

```json
{
  "query":   { "ra": 133.795, "dec": -7.245, "fov_deg": 0.1, "det": 2, "...": "..." },
  "resolved": { "name": "WISE 0855-0714", "ra": 133.79513, "dec": -7.245147, "pm_applied": false, "note": "..." },
  "grid":    { "n_pixels": 59, "pixel_scale_arcsec": 6.15, "fov_deg": 0.1008,
               "center": {"ra": 133.795, "dec": -7.245},
               "orientation": "north up, east left", "origin": "top-left" },
  "counts":  { "images_found": 84, "requested": 84, "failed": 2, "frames_returned": 82 },
  "frames": [
    { "index": 0, "mjd": 60811.1, "date": "2025-05-14T02:41:09Z", "det": 2,
      "wavelength_um": 1.31, "wavelength_range_um": [1.31, 1.31], "bandwidth_um": 0.03,
      "n_stacked": 1, "version": "...", "flag_fraction": 0.012,
      "image": "data:image/png;base64,..." }
  ]
}
```

Frontend notes:
- `image` is grayscale + alpha PNG (alpha 0 = masked/no data), row 0 = north, left = east. Draw it
  with `image-rendering: pixelated` and scale up with CSS. All frames share one brightness stretch.
- Show `date`, `wavelength_um` and `flag_fraction` as frame metadata. Show `counts.failed` ("80 of 82 loaded").
- Preload all PNGs into `Image` objects before starting playback.

## `GET /api/spectrum`: spectrum at a position

Params: `ra`/`dec` or `target`, `aperture_px` (1.5), `size` (0.04 deg cutout per exposure),
`max_frames` (600), `dets` (e.g. `1,2`). Uses all six detectors by default.

```json
{
  "points": [
    { "wavelength_um": 0.81, "bandwidth_um": 0.02, "flux_jy": 0.0021, "err_jy": 0.0004,
      "surface_brightness_mjy_sr": 0.8, "n_pix": 9, "mjd": 60811.1, "det": 1,
      "date": "2025-05-14T02:41:09Z", "version": "..." }
  ],
  "counts": { "images_found": 500, "requested": 500, "failed": 3, "points": 470 },
  "notes": "quick-look photometry..."
}
```

Points are sorted by wavelength: plot `flux_jy` vs `wavelength_um` with `err_jy` error bars.
First call for a position can take minutes (hundreds of cutouts); later calls are cached.

## `GET /api/pixel_to_sky`: turn a click into coordinates

`?ra=&dec=&fov=&scale=&x=&y=`, with the same `ra`, `dec`, `fov`, `scale` as the `/api/frames` call
and `x`, `y` = pixel coordinates in the frame image (origin top-left). Returns `{ra, dec}`.
Use it to turn a click into a spectrum request.

## Others

- `GET /api/targets`: the 5 challenge targets (for preset buttons).
- `GET /api/resolve?name=...&epoch=`: name to coordinates (SIMBAD; proper motion applied for known targets).
- `GET /api/health`.

Errors: `404` name not found, `422` bad or missing parameters, `502` IRSA TAP
discovery failed (upstream unavailable or returned an invalid response).

The `X-Request-ID` response header matches the request ID in the backend logs.
Discovery errors return `{ "detail": "IRSA image discovery failed...", "request_id": "..." }`.
Individual cutout failures still return a partial flipbook with `counts.failed`.
