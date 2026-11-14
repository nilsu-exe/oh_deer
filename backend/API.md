# API contract (frontend: build against this)

Base URL `http://localhost:8000`. All endpoints are `GET`, JSON, CORS open. Interactive docs at `/docs`.
Every endpoint that takes a position accepts **either** `ra` + `dec` (ICRS degrees) **or** `target` (name).

## `GET /api/frames`: the flipbook

| Param | Default | Meaning |
|---|---|---|
| `ra`, `dec` / `target` | required | Position (decimal degrees or sexagesimal HMS/DMS like `08h55m10.831s`), or a target name / coordinate string |
| `epoch` | 2026.0 | Decimal year; only used with `target` for high-proper-motion targets |
| `fov` | 0.1 | Field of view in degrees (max 1.0). Larger = longer load |
| `det` | 2 | Detector 1-6. Wavelength ranges: D1 0.75-1.09, D2 1.10-1.62, D3 1.63-2.41, D4 2.42-3.82, D5 3.83-4.41, D6 4.42-5.00 um |
| `bin_days` | 0 | Average frames inside N-day bins (higher S/N); 0 = off |
| `sky` | `median` | Background removal: `none`, `zodi` (ZODI model), `median` (per-frame median) |
| `subtract` | `none` | `none`, `median` (per-pixel median over time), `first` (minus frame 0), `prev` (minus previous frame) |
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
  ],
  "composites": {
    "coadd_mean_image": "data:image/png;base64,...",
    "max_projection_image": "data:image/png;base64,...",
    "epoch1_image": "data:image/png;base64,...",
    "epoch2_image": "data:image/png;base64,...",
    "epoch_diff_image": "data:image/png;base64,...",
    "epoch_diff_color_image": "data:image/png;base64,...",
    "epoch1_mjd_range": [60800.0, 60805.0],
    "epoch2_mjd_range": [60980.0, 60985.0],
    "epoch1_n_frames": 40,
    "epoch2_n_frames": 42
  },
  "candidates": [
    { "kind": "mover", "snr": 14.2, "x": 29.0, "y": 24.0, "from_x": 29.0, "from_y": 29.0,
      "ra": 133.795, "dec": -7.2365, "from_ra": 133.795, "from_dec": -7.245,
      "shift_px": 5.0, "shift_arcsec": 30.75, "proper_motion_arcsec_yr": 62.3 }
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
`max_frames` (600), `dets` (e.g. `1,2`), `z` (optional known redshift). Uses all six detectors by default.

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

Optional `z` (0-10) fixes the redshift instead of estimating it. The response also carries an
`analysis` block (same shape as `POST /api/spectrum/analyze` below; `{"error": ...}` if it failed).

## `POST /api/spectrum/analyze`: feature identification, redshift, distance

Runs the analysis on points you send (no IRSA query; good for cached/custom spectra).

```json
{ "points": [ { "wavelength_um": 2.17, "flux_jy": 0.012, "err_jy": 0.0003 } ], "z": null }
```

`err_jy` is optional (estimated from scatter if missing); `z` is optional (estimated if omitted).
Steps: inverse-variance binning onto the 102 channels (6 detectors x 17) -> redshift template
scan (z = 0..3.5, step 0.002) -> greedy, deblended window photometry of each reference feature ->
flat LambdaCDM distances (H0 = 70, Om0 = 0.3).

```json
{
  "n_channels": 102,
  "binned_102": [ { "channel": 1, "det": 1, "channel_center_um": 0.7583, "wavelength_um": 0.758,
                    "flux_jy": 0.0071, "err_jy": 0.00004, "n_exposures": 3 } ],
  "redshift": { "z": 0.158, "z_err": 0.0147, "confidence": "High", "regime": "extragalactic",
                "matched_lines": ["PAH 3.3 µm", "Paschen-α (Paα)", "Paschen-β (Paβ)", "H-alpha (Hα)"],
                "method": "template_scan", "candidates": [ { "z": 0.158, "score": 16032.7, "matched_lines": ["..."] } ] },
  "cosmology": { "regime": "extragalactic", "is_extragalactic": true, "redshift_z": 0.158,
                 "recession_velocity_kms": 43665, "comoving_distance_mpc": 651.9,
                 "luminosity_distance_mpc": 755.0, "luminosity_distance_err_mpc": 52.4,
                 "angular_diameter_distance_mpc": 563.0, "luminosity_distance_ly": 2.46e9,
                 "luminosity_distance_gly": 2.46, "lookback_time_gyr": 1.976,
                 "age_at_emission_gyr": 11.49, "distance_modulus_mag": 39.39, "...": "..." },
  "detections": [
    { "id": "pa_alpha", "name": "Paschen-α (Paα)", "type": "emission", "category": "atomic_emission",
      "rest_wavelength_um": 1.875, "observed_wavelength_um": 2.171, "z": 0.158,
      "continuum_flux_jy": 0.0105, "observed_flux_jy": 0.0161, "delta_flux_jy": 0.0056,
      "contrast_pct": 53.2, "optical_depth_tau": 0.0, "equivalent_width_um": -0.0265,
      "equivalent_width_rest_um": -0.0229, "equivalent_width_fit_um": -0.0362,
      "snr": 12.2, "confidence": "High", "continuum_method": "linear", "deblended": true,
      "region_points": [ { "wavelength_um": 2.07, "flux_jy": 0.0104, "continuum_jy": 0.0104, "in_core": false } ] }
  ],
  "summary_classification": "Redshifted Extragalactic AGN / Starburst"
}
```

Notes:
- `regime` is `galactic` when z < 0.003: Hubble-flow distances are `null` (use parallax instead).
- `confidence`: High S/N >= 5, Medium >= 3, Low >= 2. Equivalent widths are positive for absorption,
  negative for emission. `equivalent_width_um` is side-band photometry (slightly low for broad bands);
  `equivalent_width_fit_um` is from a Gaussian + continuum fit.
- `optical_depth_tau` = -ln(F_trough / F_continuum) for absorption features, 0 for emission.
- `region_points` holds the core and continuum points used, for shading the feature in a plot.

## `GET /api/spectral_features`

The reference feature library (rest wavelength, type, FWHM, continuum windows, category) and the
102-channel grid.

## `GET /api/overlays`: Gaia DR3 & AllWISE catalog overlays

Params: `ra`/`dec` or `target`, `fov` (0.1), `scale` (6.15), `epoch` (2026.0),
`catalogs` (`gaia,wise`), `max_sources` (150).

Projects Gaia DR3 optical sources and AllWISE infrared sources onto the exact PNG pixel grid `(x, y)`
of `/api/frames` (origin top-left):
- `gaia`: includes `x`, `y`, `g_mag`, `bp_rp`, `pm_ra_masyr`, `pm_dec_masyr`, `total_pm_arcsec_yr`,
  PNG proper-motion arrow vectors `dx_px_per_yr` and `dy_px_per_yr`, `parallax_mas`, `distance_pc`,
  `distance_ly`, and `tangential_velocity_kms`.
- `wise`: includes `x`, `y`, `w1_mag`..`w4_mag`, `w1_w2_color`, `w1_flux_mjy`..`w4_flux_mjy`,
  `gaia_match`, and `gaia_source_id`.
- `ir_only_candidates`: WISE sources with no Gaia DR3 optical counterpart within 3 arcsec (highlights
  cold brown dwarfs like WISE 0855 and deeply embedded protostars).
- `high_proper_motion`: Gaia stars with total proper motion >= 0.1 arcsec/yr.

## `GET /api/pixel_to_sky`: turn a click into coordinates

`?ra=&dec=&fov=&scale=&x=&y=`, with the same `ra`, `dec`, `fov`, `scale` as the `/api/frames` call
and `x`, `y` = pixel coordinates in the frame image (origin top-left). Returns `{ra, dec}`.
Use it to turn a click into a spectrum request.

## Others

- `GET /api/targets`: the 5 challenge targets (for preset buttons).
- `GET /api/resolve?name=...&epoch=`: name to coordinates (SIMBAD; proper motion applied for known targets).
- `GET /api/health`.

Errors: `404` name not found, `422` bad or missing parameters.
