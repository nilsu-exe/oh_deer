"""Spectral feature identification, spectrophotometric redshift and cosmological distances.

Input is the list of quick-look photometry `points` produced by
`pipeline.build_spectrum` (each with `wavelength_um`, `flux_jy` and optionally
`err_jy`). The pipeline is:

1. `bin_spectrum_102`   - inverse-variance binning onto the 102 SPHEREx channels
                          (6 detectors x 17 log-spaced channels).
2. `estimate_redshift`  - template scan over z: every reference feature is shifted
                          to lambda_rest * (1 + z) and fitted with a Gaussian on a
                          local linear continuum; the z with the best multi-line
                          score wins (z = 0 unless an extragalactic solution is
                          clearly better).
3. `identify_materials` - window photometry of each feature (local linear continuum
                          from side bands, depth / optical depth / equivalent width /
                          S/N), with greedy deblending: the strongest feature is
                          modelled and removed before weaker ones are measured, so
                          wings of a broad band do not fake neighbouring features.
4. `compute_cosmology`  - flat LambdaCDM distances for z >= 0.003.

Everything here is quick-look: the input photometry is not aperture corrected and
the template library is a small set of rest-frame features.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from astropy import units as u
from astropy.cosmology import FlatLambdaCDM

# ----------------------------------------------------------------------------
# Instrument model
# ----------------------------------------------------------------------------

# (detector, lambda_min_um, lambda_max_um, resolving power R)
DETECTORS: Tuple[Tuple[int, float, float, float], ...] = (
    (1, 0.75, 1.09, 41.0),
    (2, 1.10, 1.62, 41.0),
    (3, 1.63, 2.41, 41.0),
    (4, 2.42, 3.82, 35.0),
    (5, 3.83, 4.41, 110.0),
    (6, 4.42, 5.00, 130.0),
)
CHANNELS_PER_DETECTOR = 17
WAVE_MIN_UM, WAVE_MAX_UM = 0.75, 5.00

GALACTIC_Z_LIMIT = 0.003        # below this we do not use Hubble-flow distances
SNR_DETECT = 2.0                # minimum S/N to report a feature
SNR_REDSHIFT_LINE = 3.0         # S/N for a feature to count towards a redshift
SNR_REDSHIFT_SINGLE = 6.0       # a single feature this strong may define a redshift
EXTRAGALACTIC_MARGIN = 1.25     # extragalactic score must beat z=0 score by this factor
FWHM_TO_SIGMA = 1.0 / (2.0 * math.sqrt(2.0 * math.log(2.0)))
SQRT_2PI = math.sqrt(2.0 * math.pi)


def _build_channels() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for det, lo, hi, r in DETECTORS:
        edges = lo * (hi / lo) ** (np.arange(CHANNELS_PER_DETECTOR + 1) / CHANNELS_PER_DETECTOR)
        for k in range(CHANNELS_PER_DETECTOR):
            rows.append({
                "channel": len(rows) + 1,
                "det": det,
                "center_um": float(math.sqrt(edges[k] * edges[k + 1])),
                "lo_um": float(edges[k]),
                "hi_um": float(edges[k + 1]),
                "resolving_power": r,
            })
    return rows


CHANNELS: List[Dict[str, Any]] = _build_channels()
_CH_CENTER = np.array([c["center_um"] for c in CHANNELS])
_CH_LOG = np.log(_CH_CENTER)
_CH_HALF_LOG = np.array([0.5 * math.log(c["hi_um"] / c["lo_um"]) for c in CHANNELS])


def resolving_power(lam_um: float) -> float:
    """SPHEREx resolving power at a wavelength (nearest detector in the gaps)."""
    for _, lo, hi, r in DETECTORS:
        if lo <= lam_um <= hi:
            return r
    return min(DETECTORS, key=lambda d: min(abs(lam_um - d[1]), abs(lam_um - d[2])))[3]


# ----------------------------------------------------------------------------
# Reference feature library (rest frame)
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class SpectralFeature:
    id: str
    name: str
    wavelength_um: float
    type: str                     # "absorption" | "emission"
    fwhm_um: float
    continuum_offsets: Tuple[Tuple[float, float], Tuple[float, float]]
    category: str

    @property
    def sign(self) -> int:
        return -1 if self.type == "absorption" else 1

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "wavelength_um": self.wavelength_um,
                "type": self.type, "fwhm_um": self.fwhm_um,
                "continuum_offsets": [list(w) for w in self.continuum_offsets],
                "category": self.category}


SPECTRAL_FEATURES: Tuple[SpectralFeature, ...] = (
    # Ices & dust
    SpectralFeature("h2o_ice", "H2O Ice", 3.05, "absorption", 0.30,
                    ((-0.45, -0.20), (0.20, 0.45)), "ice"),
    SpectralFeature("co2_ice", "CO2 Ice", 4.27, "absorption", 0.08,
                    ((-0.20, -0.07), (0.07, 0.20)), "ice"),
    SpectralFeature("co_ice", "CO Ice", 4.67, "absorption", 0.06,
                    ((-0.18, -0.06), (0.06, 0.18)), "ice"),
    SpectralFeature("pah_330", "PAH 3.3 µm", 3.30, "emission", 0.08,
                    ((-0.22, -0.08), (0.08, 0.22)), "pah_dust"),
    # Molecular gas & substellar atmospheres
    SpectralFeature("h2o_vapor_14", "H2O Vapor (1.4 µm)", 1.40, "absorption", 0.12,
                    ((-0.18, -0.08), (0.08, 0.18)), "molecular_gas"),
    SpectralFeature("h2o_vapor_19", "H2O Vapor (1.9 µm)", 1.90, "absorption", 0.14,
                    ((-0.20, -0.09), (0.09, 0.20)), "molecular_gas"),
    SpectralFeature("ch4_166", "CH4 (Methane 1.66 µm)", 1.66, "absorption", 0.12,
                    ((-0.20, -0.08), (0.08, 0.20)), "molecular_gas"),
    SpectralFeature("ch4_331", "CH4 (Fundamental 3.31 µm)", 3.31, "absorption", 0.15,
                    ((-0.25, -0.10), (0.10, 0.25)), "molecular_gas"),
    SpectralFeature("nh3_200", "NH3 (Ammonia 2.00 µm)", 2.00, "absorption", 0.12,
                    ((-0.18, -0.08), (0.08, 0.18)), "molecular_gas"),
    SpectralFeature("co_overtone", "CO 1st Overtone (2.29 µm)", 2.29, "absorption", 0.10,
                    ((-0.18, -0.07), (0.07, 0.18)), "molecular_gas"),
    # Atomic recombination & extragalactic emission lines
    SpectralFeature("br_gamma", "Brackett-γ (Brγ)", 2.166, "emission", 0.05,
                    ((-0.15, -0.04), (0.04, 0.15)), "atomic_emission"),
    SpectralFeature("pa_alpha", "Paschen-α (Paα)", 1.875, "emission", 0.05,
                    ((-0.15, -0.04), (0.04, 0.15)), "atomic_emission"),
    SpectralFeature("pa_beta", "Paschen-β (Paβ)", 1.282, "emission", 0.045,
                    ((-0.12, -0.035), (0.035, 0.12)), "atomic_emission"),
    SpectralFeature("h_alpha", "H-alpha (Hα)", 0.6563, "emission", 0.04,
                    ((-0.10, -0.03), (0.03, 0.10)), "extragalactic_emission"),
    SpectralFeature("oiii_5007", "[O III] 5007", 0.5007, "emission", 0.035,
                    ((-0.08, -0.025), (0.025, 0.08)), "extragalactic_emission"),
)
FEATURES_BY_ID = {f.id: f for f in SPECTRAL_FEATURES}


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def _num(x: Any) -> Optional[float]:
    """JSON-safe float (None for NaN/inf), trimmed to 6 significant digits."""
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(xf):
        return None
    return float(f"{xf:.6g}")


def _trapz(y: np.ndarray, x: np.ndarray) -> float:
    if len(x) < 2:
        return 0.0
    return float(np.sum(0.5 * (y[1:] + y[:-1]) * np.diff(x)))


def _wls(X: np.ndarray, y: np.ndarray, w: np.ndarray):
    """Weighted linear least squares. Covariance inflated by reduced chi^2 if > 1."""
    A = X * w[:, None]
    try:
        cov = np.linalg.inv(X.T @ A)
    except np.linalg.LinAlgError:
        return None
    p = cov @ (A.T @ y)
    dof = len(y) - X.shape[1]
    if dof > 0:
        chi2r = float(np.sum(w * (y - X @ p) ** 2)) / dof
        if chi2r > 1.0:
            cov = cov * chi2r
    if not np.all(np.isfinite(p)) or not np.all(np.isfinite(np.diag(cov))):
        return None
    return p, cov


def _local_spacing(wl: np.ndarray, lam: float) -> float:
    if len(wl) < 2:
        return 0.0
    gaps = np.diff(wl)
    mids = 0.5 * (wl[1:] + wl[:-1])
    nearest = np.argsort(np.abs(mids - lam))[:4]
    return float(np.median(gaps[nearest]))


def _geometry(feat: SpectralFeature, z: float):
    """Observed centre, observed intrinsic FWHM, effective (instrument-broadened) FWHM, sigma."""
    s = 1.0 + z
    lam = feat.wavelength_um * s
    fwhm_obs = feat.fwhm_um * s
    fwhm_eff = math.hypot(fwhm_obs, lam / resolving_power(lam))
    return lam, fwhm_obs, fwhm_eff, fwhm_eff * FWHM_TO_SIGMA


# ----------------------------------------------------------------------------
# 1. Binning onto the 102 channels
# ----------------------------------------------------------------------------

def _extract(points: Iterable[Dict[str, Any]]):
    wl, fl, er = [], [], []
    for p in points or []:
        if hasattr(p, "model_dump"):
            p = p.model_dump()
        try:
            lam = float(p["wavelength_um"])
            f = float(p["flux_jy"])
        except (KeyError, TypeError, ValueError):
            continue
        e = p.get("err_jy")
        try:
            e = float(e) if e is not None else float("nan")
        except (TypeError, ValueError):
            e = float("nan")
        if math.isfinite(lam) and lam > 0 and math.isfinite(f):
            wl.append(lam), fl.append(f), er.append(e)
    return np.array(wl, float), np.array(fl, float), np.array(er, float)


def _fill_errors(wl: np.ndarray, fl: np.ndarray, er: np.ndarray) -> np.ndarray:
    good = np.isfinite(er) & (er > 0)
    if good.all():
        return np.maximum(er, 1e-12)
    if good.any():
        fill = float(np.median(er[good]))
    else:
        # No errors at all: estimate white noise from point-to-point scatter.
        d = np.diff(fl[np.argsort(wl)])
        fill = 0.0
        if len(d) >= 3:
            fill = 1.4826 * float(np.median(np.abs(d - np.median(d)))) / math.sqrt(2.0)
        floor = 1e-3 * max(float(np.median(np.abs(fl))) if len(fl) else 0.0, 1e-12)
        fill = max(fill, floor)
    return np.maximum(np.where(good, er, fill), 1e-12)


def bin_spectrum_102(points: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Inverse-variance bin raw exposure points onto the 102 SPHEREx channels.

    Returns only channels that received data, sorted by wavelength. `wavelength_um`
    is the weighted mean wavelength of the contributing exposures;
    `channel_center_um` is the nominal channel centre.
    """
    wl, fl, er = _extract(points)
    if len(wl) == 0:
        return []
    er = _fill_errors(wl, fl, er)
    logd = np.abs(np.log(wl)[:, None] - _CH_LOG[None, :])
    idx = np.argmin(logd, axis=1)
    keep = logd[np.arange(len(wl)), idx] <= 2.0 * _CH_HALF_LOG[idx]   # drop far out-of-band points
    wl, fl, er, idx = wl[keep], fl[keep], er[keep], idx[keep]
    out: List[Dict[str, Any]] = []
    for j in np.unique(idx):
        m = idx == j
        w = 1.0 / er[m] ** 2
        sw = float(np.sum(w))
        ch = CHANNELS[int(j)]
        out.append({
            "channel": ch["channel"],
            "det": ch["det"],
            "channel_center_um": _num(ch["center_um"]),
            "wavelength_um": float(np.sum(w * wl[m]) / sw),
            "flux_jy": float(np.sum(w * fl[m]) / sw),
            "err_jy": float(1.0 / math.sqrt(sw)),
            "n_exposures": int(m.sum()),
        })
    out.sort(key=lambda r: r["wavelength_um"])
    return out


def _arrays(binned: List[Dict[str, Any]]):
    wl = np.array([b["wavelength_um"] for b in binned], float)
    fl = np.array([b["flux_jy"] for b in binned], float)
    er = np.array([b["err_jy"] for b in binned], float)
    return wl, fl, er


# ----------------------------------------------------------------------------
# Feature measurement primitives
# ----------------------------------------------------------------------------

def _template_fit(wl, fl, er, feat: SpectralFeature, z: float) -> Optional[Dict[str, float]]:
    """Fit continuum (linear, or constant at band edges) + fixed-shape Gaussian.

    Returns amplitude (Jy, signed), its error and S/N. Used for the redshift scan
    and for deblending.
    """
    lam, _, _, sigma = _geometry(feat, z)
    s = 1.0 + z
    (l0, _), (_, r1) = feat.continuum_offsets
    half = max(-l0 * s, r1 * s, 4.0 * sigma)
    m = np.abs(wl - lam) <= half
    if m.sum() < 4:
        return None
    x = wl[m] - lam
    if not np.any(np.abs(x) <= 1.5 * sigma):
        return None
    left, right = np.any(x < -1.5 * sigma), np.any(x > 1.5 * sigma)
    g = np.exp(-0.5 * (x / sigma) ** 2)
    if left and right:
        X = np.column_stack([np.ones_like(x), x, g])
    elif np.sum(np.abs(x) > 1.5 * sigma) >= 2:
        X = np.column_stack([np.ones_like(x), g])
    else:
        return None
    if len(x) <= X.shape[1]:
        return None
    res = _wls(X, fl[m], 1.0 / er[m] ** 2)
    if res is None:
        return None
    p, cov = res
    amp, amp_err = float(p[-1]), math.sqrt(float(cov[-1, -1]))
    if amp_err <= 0:
        return None
    return {"amp": amp, "amp_err": amp_err, "snr": amp / amp_err, "c0": float(p[0]),
            "lam": lam, "sigma": sigma}


def _measure_window(wl, fl, er, feat: SpectralFeature, z: float) -> Optional[Dict[str, Any]]:
    """Side-band continuum + core photometry for one feature at redshift z."""
    s = 1.0 + z
    lam, fwhm_obs, fwhm_eff, _ = _geometry(feat, z)
    sp = _local_spacing(wl, lam)
    if lam < wl[0] - 0.5 * sp or lam > wl[-1] + 0.5 * sp:
        return None
    half = max(0.5 * fwhm_eff, 0.6 * sp)
    core = np.abs(wl - lam) <= half
    if not core.any():
        return None

    (l0, l1), (r0, r1) = feat.continuum_offsets
    left = right = np.zeros_like(core)
    for expand in (1.0, 1.5, 2.0, 3.0):          # widen outer edges if side bands are empty
        left = (wl >= lam + l0 * s * expand) & (wl <= lam + l1 * s) & ~core
        right = (wl >= lam + r0 * s) & (wl <= lam + r1 * s * expand) & ~core
        if left.any() and right.any():
            break
    cmask = left | right
    if left.any() and right.any():
        method = "linear"
    elif cmask.sum() >= 2:
        method = "one-sided"
    else:
        return None

    xc = wl[cmask] - lam
    Xc = np.column_stack([np.ones_like(xc), xc]) if method == "linear" else np.ones((len(xc), 1))
    fit = _wls(Xc, fl[cmask], 1.0 / er[cmask] ** 2)
    if fit is None:
        return None
    p, cov = fit

    def cont(x):
        x = np.asarray(x, float)
        return p[0] + (p[1] * x if len(p) > 1 else 0.0 * x)

    def cont_var(x: float) -> float:
        if len(p) == 1:
            return float(cov[0, 0])
        return float(cov[0, 0] + 2 * x * cov[0, 1] + x * x * cov[1, 1])

    xk = wl[core] - lam
    wk = 1.0 / er[core] ** 2
    r = fl[core] - cont(xk)
    sw = float(np.sum(wk))
    rbar = float(np.sum(wk * r) / sw)
    xbar = float(np.sum(wk * xk) / sw)
    snr_signed = rbar / math.sqrt(1.0 / sw + max(cont_var(xbar), 0.0))

    c0 = float(cont(0.0))
    i_ext = int(np.argmax(feat.sign * r))
    delta = float(r[i_ext])
    observed = c0 + delta
    contrast = 100.0 * delta / c0 if c0 > 0 else None
    tau = (-math.log(max(observed, 1e-15) / c0) if c0 > 0 else None) if feat.type == "absorption" else 0.0

    # Equivalent width: trapezoid over +-1 effective FWHM (~98% of a Gaussian profile)
    # plus the nearest neighbour on each side so the profile closes on the continuum.
    span = np.abs(wl - lam) <= max(half, fwhm_eff)
    idx_core = np.nonzero(span)[0]
    lo_i, hi_i = idx_core[0], idx_core[-1]
    reach = half + 2.0 * sp
    if lo_i - 1 >= 0 and lam - wl[lo_i - 1] <= reach:
        lo_i -= 1
    if hi_i + 1 < len(wl) and wl[hi_i + 1] - lam <= reach:
        hi_i += 1
    ws, fs = wl[lo_i:hi_i + 1], fl[lo_i:hi_i + 1]
    cs = cont(ws - lam)
    if np.all(cs > 0):
        prof = 1.0 - fs / cs
        ew = _trapz(prof, ws) if len(ws) >= 2 else float(prof[0]) * max(sp, fwhm_eff)
    else:
        ew = None

    region = np.nonzero(core | cmask)[0]
    return {
        "lam_obs": lam, "fwhm_obs": fwhm_obs, "fwhm_eff": fwhm_eff,
        "snr_signed": snr_signed, "continuum": c0, "observed": observed, "delta": delta,
        "mean_delta": rbar, "contrast_pct": contrast, "tau": tau, "ew": ew,
        "n_core": int(core.sum()), "n_cont": int(cmask.sum()), "method": method,
        "region": [{"wavelength_um": _num(wl[i]), "flux_jy": _num(fl[i]),
                    "continuum_jy": _num(cont(wl[i] - lam)), "in_core": bool(core[i])}
                   for i in region],
    }


# ----------------------------------------------------------------------------
# 2. Redshift
# ----------------------------------------------------------------------------

def _score_at(wl, fl, er, z: float):
    good: List[Tuple[SpectralFeature, float]] = []
    lo, hi = wl[0], wl[-1]
    for f in SPECTRAL_FEATURES:
        lam = f.wavelength_um * (1.0 + z)
        if lam < lo or lam > hi:
            continue
        fit = _template_fit(wl, fl, er, f, z)
        if fit is None:
            continue
        signed = fit["snr"] * f.sign
        if signed >= SNR_REDSHIFT_LINE:
            good.append((f, signed))
    score = float(sum(snr * snr for _, snr in good))
    return score, good


def _confidence_from_lines(good: Sequence[Tuple[SpectralFeature, float]]) -> str:
    n = len(good)
    best = max((s for _, s in good), default=0.0)
    if n >= 3 or (n == 2 and best >= 5.0):
        return "High"
    if n == 2 or best >= SNR_REDSHIFT_SINGLE:
        return "Medium"
    if n == 1:
        return "Low"
    return "Unconstrained"


def _z_err(z: float, good: Sequence[Tuple[SpectralFeature, float]], dz: float) -> Optional[float]:
    if not good:
        return None
    r_eff = float(np.mean([resolving_power(f.wavelength_um * (1 + z)) for f, _ in good]))
    return max(dz, (1.0 + z) / (r_eff * math.sqrt(len(good))))


def _estimate_redshift_arrays(wl, fl, er, z_max: float = 3.5, dz: float = 0.002) -> Dict[str, Any]:
    if len(wl) < 5:
        return {"z": 0.0, "z_err": None, "confidence": "Unconstrained", "regime": "galactic",
                "matched_lines": [], "method": "template_scan", "candidates": [],
                "note": "Too few spectral channels for a redshift estimate."}
    grid = np.round(np.arange(0.0, z_max + 0.5 * dz, dz), 6)
    scores = np.zeros(len(grid))
    goods: List[List[Tuple[SpectralFeature, float]]] = []
    for i, z in enumerate(grid):
        scores[i], g = _score_at(wl, fl, er, float(z))
        goods.append(g)

    gal = grid < GALACTIC_Z_LIMIT
    i_gal = int(np.argmax(np.where(gal, scores, -1.0)))
    gal_score = float(scores[i_gal])

    valid = np.array([(not gal[i]) and (len(g) >= 2 or any(s >= SNR_REDSHIFT_SINGLE for _, s in g))
                      for i, g in enumerate(goods)])
    candidates: List[Dict[str, Any]] = []
    if valid.any():
        masked = np.where(valid, scores, -1.0)
        # distinct local maxima for transparency
        order = np.argsort(masked)[::-1]
        for i in order:
            if masked[i] <= 0 or len(candidates) >= 3:
                break
            if all(abs(grid[i] - c["z"]) > 0.02 for c in candidates):
                candidates.append({"z": _num(grid[i]), "score": _num(scores[i]),
                                   "matched_lines": [f.name for f, _ in goods[i]]})
        i_ext = int(np.argmax(masked))
        n_gal = len(goods[i_gal])
        if scores[i_ext] > EXTRAGALACTIC_MARGIN * gal_score and (n_gal < 4 or len(goods[i_ext]) > n_gal):
            z = float(grid[i_ext])
            g = goods[i_ext]
            return {"z": _num(z), "z_err": _num(_z_err(z, g, dz)), "confidence": _confidence_from_lines(g),
                    "regime": "extragalactic", "matched_lines": [f.name for f, _ in g],
                    "score": _num(scores[i_ext]), "galactic_score": _num(gal_score),
                    "method": "template_scan", "candidates": candidates}

    g = goods[i_gal]
    return {"z": 0.0, "z_err": _num(_z_err(0.0, g, dz)) if g else None,
            "confidence": _confidence_from_lines(g), "regime": "galactic",
            "matched_lines": [f.name for f, _ in g], "score": _num(gal_score),
            "galactic_score": _num(gal_score), "method": "template_scan", "candidates": candidates}


def estimate_redshift(points: Iterable[Dict[str, Any]], z_max: float = 3.5, dz: float = 0.002) -> Dict[str, Any]:
    """Spectrophotometric redshift from shifted emission/absorption features."""
    return _estimate_redshift_arrays(*_arrays(bin_spectrum_102(points)), z_max=z_max, dz=dz)


# ----------------------------------------------------------------------------
# 3. Material / feature identification
# ----------------------------------------------------------------------------

def _confidence(snr: float) -> str:
    if snr >= 5.0:
        return "High"
    if snr >= 3.0:
        return "Medium"
    return "Low"


def _identify_arrays(wl, fl, er, z: float = 0.0) -> List[Dict[str, Any]]:
    if len(wl) < 3:
        return []
    work = fl.copy()
    remaining = list(SPECTRAL_FEATURES)
    detections: List[Dict[str, Any]] = []
    n_removed = 0
    while remaining:
        best = None
        for f in remaining:
            m = _measure_window(wl, work, er, f, z)
            if m is None:
                continue
            snr = m["snr_signed"] * f.sign            # positive when the sign is as expected
            if snr >= SNR_DETECT and (best is None or snr > best[2]):
                best = (f, m, snr)
        if best is None:
            break
        f, m, snr = best
        remaining.remove(f)
        tf = _template_fit(wl, work, er, f, z)
        ew_fit = None
        if tf is not None and tf["c0"] > 0:
            ew_fit = -tf["amp"] * tf["sigma"] * SQRT_2PI / tf["c0"]
        detections.append({
            "id": f.id, "name": f.name, "type": f.type, "category": f.category,
            "rest_wavelength_um": f.wavelength_um,
            "observed_wavelength_um": _num(m["lam_obs"]),
            "z": _num(z),
            "fwhm_observed_um": _num(m["fwhm_obs"]),
            "continuum_flux_jy": _num(m["continuum"]),
            "observed_flux_jy": _num(m["observed"]),
            "delta_flux_jy": _num(m["delta"]),
            "mean_delta_flux_jy": _num(m["mean_delta"]),
            "contrast_pct": _num(m["contrast_pct"]),
            "optical_depth_tau": _num(m["tau"]),
            "equivalent_width_um": _num(m["ew"]),
            "equivalent_width_rest_um": _num(m["ew"] / (1.0 + z)) if m["ew"] is not None else None,
            "equivalent_width_fit_um": _num(ew_fit),
            "snr": _num(snr),
            "confidence": _confidence(snr),
            "n_core_points": m["n_core"],
            "n_continuum_points": m["n_cont"],
            "continuum_method": m["method"],
            "deblended": n_removed > 0,
            "region_points": m["region"],
        })
        # Deblend: remove the fitted profile so its wings do not bias weaker features.
        if tf is not None and tf["amp"] * f.sign > 0:
            x = (wl - tf["lam"]) / tf["sigma"]
            work = work - tf["amp"] * np.where(np.abs(x) < 6, np.exp(-0.5 * x * x), 0.0)
            n_removed += 1
    detections.sort(key=lambda d: d["snr"] or 0.0, reverse=True)
    return detections


def identify_materials(points: Iterable[Dict[str, Any]], z: float = 0.0) -> List[Dict[str, Any]]:
    """Detect reference features at redshift z. Sorted by S/N (descending)."""
    return _identify_arrays(*_arrays(bin_spectrum_102(points)), z=float(z))


_CATEGORY_LABELS = {
    "ice": "Ice-Rich Source (H2O/CO2/CO Ice)",
    "atomic_emission": "Accreting YSO / Atomic Emission",
    "pah_dust": "PAH-Emitting Dusty Region",
    "extragalactic_emission": "Redshifted Extragalactic AGN / Starburst",
}


def summarize(detections: Sequence[Dict[str, Any]], regime: str) -> str:
    """One-line classification from the detected feature categories."""
    strong = [d for d in detections if (d.get("snr") or 0) >= 3.0] or list(detections)
    if regime == "extragalactic":
        if strong:
            return "Redshifted Extragalactic AGN / Starburst"
        return "Redshifted Extragalactic Source (continuum only)"
    if not strong:
        return "Continuum-Dominated Source"
    ids = {d["id"] for d in strong}
    scores: Dict[str, float] = {}
    for d in strong:
        scores[d["category"]] = scores.get(d["category"], 0.0) + float(d["snr"] or 0)

    def label(cat: str) -> str:
        if cat == "molecular_gas":
            if ids & {"ch4_166", "ch4_331", "nh3_200"}:
                return "Cool Brown Dwarf / Methane-Ammonia Atmosphere"
            return "Cool Stellar Atmosphere (H2O Vapor / CO Bands)"
        return _CATEGORY_LABELS.get(cat, cat)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top = ranked[0][1]
    labels = [label(c) for c, sc in ranked if sc >= 0.3 * top]
    return " + ".join(dict.fromkeys(labels))


# ----------------------------------------------------------------------------
# 4. Cosmology
# ----------------------------------------------------------------------------

H0_KM_S_MPC = 70.0
OMEGA_M = 0.3
COSMOLOGY = FlatLambdaCDM(H0=H0_KM_S_MPC, Om0=OMEGA_M)
C_KM_S = 299792.458
LY_PER_MPC = 3.2615638e6


def compute_cosmology(z: float, z_err: float = 0.0) -> Dict[str, Any]:
    """Flat LambdaCDM (H0=70, Om0=0.3) distances; Galactic regime for z < 0.003."""
    z = float(z)
    z_err = float(z_err or 0.0)
    base: Dict[str, Any] = {
        "model": "FlatLambdaCDM", "H0_km_s_mpc": H0_KM_S_MPC, "Om0": OMEGA_M,
        "Ode0": round(1.0 - OMEGA_M, 6), "redshift_z": _num(z), "redshift_err": _num(z_err),
    }
    empty = {k: None for k in (
        "recession_velocity_kms", "comoving_distance_mpc", "luminosity_distance_mpc",
        "luminosity_distance_err_mpc", "angular_diameter_distance_mpc", "luminosity_distance_ly",
        "luminosity_distance_gly", "comoving_distance_gly", "lookback_time_gyr",
        "age_at_emission_gyr", "distance_modulus_mag")}
    if not math.isfinite(z) or z < GALACTIC_Z_LIMIT:
        return {**base, **empty, "regime": "galactic", "is_extragalactic": False,
                "note": ("Source is at z ≈ 0 (Galactic/local bound regime); Hubble expansion "
                         "distance does not apply. Use parallax or photometric distance.")}

    dc = COSMOLOGY.comoving_distance(z).to_value(u.Mpc)
    dl = COSMOLOGY.luminosity_distance(z).to_value(u.Mpc)
    da = COSMOLOGY.angular_diameter_distance(z).to_value(u.Mpc)
    dl_err = None
    if z_err > 0:
        hi = COSMOLOGY.luminosity_distance(z + z_err).to_value(u.Mpc)
        lo = COSMOLOGY.luminosity_distance(max(z - z_err, 0.0)).to_value(u.Mpc)
        dl_err = 0.5 * (hi - lo)
    s2 = (1.0 + z) ** 2
    return {
        **base,
        "regime": "extragalactic",
        "is_extragalactic": True,
        "recession_velocity_kms": _num(C_KM_S * (s2 - 1.0) / (s2 + 1.0)),
        "comoving_distance_mpc": _num(dc),
        "luminosity_distance_mpc": _num(dl),
        "luminosity_distance_err_mpc": _num(dl_err),
        "angular_diameter_distance_mpc": _num(da),
        "luminosity_distance_ly": _num(dl * LY_PER_MPC),
        "luminosity_distance_gly": _num(dl * LY_PER_MPC / 1e9),
        "comoving_distance_gly": _num(dc * LY_PER_MPC / 1e9),
        "lookback_time_gyr": _num(COSMOLOGY.lookback_time(z).to_value(u.Gyr)),
        "age_at_emission_gyr": _num(COSMOLOGY.age(z).to_value(u.Gyr)),
        "distance_modulus_mag": _num(COSMOLOGY.distmod(z).value),
        "note": "Distances from the redshift under flat LambdaCDM (H0=70 km/s/Mpc, Om0=0.3).",
    }


# ----------------------------------------------------------------------------
# 5. Continuum-Normalized Light Curve & Variability Analysis (RW Aur, R Dra)
# ----------------------------------------------------------------------------

def analyze_variability(
    points: Iterable[Dict[str, Any]],
    binned_102: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    bin_days: float = 5.0,
) -> Dict[str, Any]:
    """Extract a wavelength-detrended light curve and variability metrics from SPHEREx exposures.

    Because SPHEREx samples different wavelengths on each exposure, raw flux vs. time mixes
    spectral slope with temporal variability. We divide each exposure's flux by the object's
    time-averaged 102-channel spectral template at that wavelength, yielding a clean relative
    light curve (`relative_flux`, `delta_mag`) suitable for variable stars like RW Aurigae
    and R Draconis.
    """
    pts_list = list(points) if not isinstance(points, list) else points
    timed: List[Dict[str, Any]] = []
    for p in pts_list:
        if not isinstance(p, dict):
            continue
        mjd = p.get("mjd")
        lam = p.get("wavelength_um")
        fl = p.get("flux_jy")
        if mjd is None or lam is None or fl is None:
            continue
        try:
            mjd_f = float(mjd)
            lam_f = float(lam)
            fl_f = float(fl)
            er_f = float(p.get("err_jy")) if p.get("err_jy") is not None else float("nan")
        except (TypeError, ValueError):
            continue
        if math.isfinite(mjd_f) and math.isfinite(lam_f) and lam_f > 0 and math.isfinite(fl_f) and fl_f > 0:
            timed.append({
                "mjd": mjd_f,
                "date": p.get("date"),
                "det": p.get("det"),
                "wavelength_um": lam_f,
                "flux_jy": fl_f,
                "err_jy": er_f if (math.isfinite(er_f) and er_f > 0) else abs(fl_f) * 0.05,
            })

    if len(timed) < 2:
        return {
            "n_points": len(timed),
            "is_variable": False,
            "variability_confidence": "None",
            "variability_type": "Insufficient Time Coverage",
            "reduced_chi2": None,
            "fractional_rms_pct": None,
            "peak_to_peak_pct": None,
            "delta_mag_range": None,
            "epoch_trend_pct": None,
            "mjd_span_days": None,
            "lightcurve": [],
            "epoch_bins": [],
        }

    if binned_102 is None:
        binned_102 = bin_spectrum_102(pts_list)

    ref_pairs = sorted(
        [(float(b["wavelength_um"]), float(b["flux_jy"]))
         for b in binned_102
         if b.get("wavelength_um") is not None and b.get("flux_jy") is not None and float(b["flux_jy"]) > 0],
        key=lambda x: x[0],
    )
    if len(ref_pairs) >= 2:
        wl_ref = np.array([x[0] for x in ref_pairs], dtype=float)
        fl_ref = np.array([x[1] for x in ref_pairs], dtype=float)
    else:
        med_flux = float(np.median([t["flux_jy"] for t in timed]))
        wl_ref = np.array([0.5, 6.0], dtype=float)
        fl_ref = np.array([med_flux, med_flux], dtype=float)

    timed.sort(key=lambda t: t["mjd"])
    raw_rel = []
    raw_err = []
    for t in timed:
        ref_val = max(float(np.interp(t["wavelength_um"], wl_ref, fl_ref)), 1e-12)
        r = t["flux_jy"] / ref_val
        re = max(t["err_jy"] / ref_val, 0.02 * r)
        raw_rel.append(r)
        raw_err.append(re)

    med_rel = max(float(np.median(raw_rel)), 1e-12)
    r_arr = np.array(raw_rel, dtype=float) / med_rel
    re_arr = np.array(raw_err, dtype=float) / med_rel

    lc: List[Dict[str, Any]] = []
    dm_list: List[float] = []
    for t, r_val, re_val in zip(timed, r_arr, re_arr):
        r_safe = max(float(r_val), 1e-6)
        dm = -2.5 * math.log10(r_safe)
        dm_err = (2.5 / math.log(10.0)) * (float(re_val) / r_safe)
        dm_list.append(dm)
        lc.append({
            "mjd": _num(t["mjd"]),
            "date": t.get("date"),
            "det": t.get("det"),
            "wavelength_um": _num(t["wavelength_um"]),
            "flux_jy": _num(t["flux_jy"]),
            "err_jy": _num(t["err_jy"]),
            "relative_flux": _num(r_val),
            "relative_err": _num(re_val),
            "delta_mag": _num(dm),
            "mag_err": _num(dm_err),
        })

    w = 1.0 / np.maximum(re_arr, 1e-6) ** 2
    w_mean = float(np.sum(w * r_arr) / np.sum(w))
    chi2_red = float(np.sum(w * (r_arr - w_mean) ** 2) / max(1, len(r_arr) - 1))
    rms_pct = float(np.std(r_arr, ddof=1) / max(w_mean, 1e-6) * 100.0)
    p2p_pct = float((np.max(r_arr) - np.min(r_arr)) / max(w_mean, 1e-6) * 100.0)
    dm_range = float(max(dm_list) - min(dm_list))
    mjd_span = float(timed[-1]["mjd"] - timed[0]["mjd"])

    # Split into early (Epoch 1) vs late (Epoch 2) by largest MJD gap (or midpoint)
    mjds = np.array([t["mjd"] for t in timed], dtype=float)
    gaps = np.diff(mjds)
    if len(gaps) > 0 and float(np.max(gaps)) > 1.5 * float(np.median(gaps) + 1e-6):
        split_idx = int(np.argmax(gaps)) + 1
    else:
        split_idx = len(timed) // 2
    split_idx = max(1, min(len(timed) - 1, split_idx))
    ep1_mean = float(np.mean(r_arr[:split_idx]))
    ep2_mean = float(np.mean(r_arr[split_idx:]))
    epoch_trend_pct = (ep2_mean - ep1_mean) / max(ep1_mean, 1e-6) * 100.0

    # Time-binned epoch points for clean lightcurve plotting
    epoch_bins: List[Dict[str, Any]] = []
    step = max(float(bin_days), 0.1)
    t0 = float(mjds[0])
    groups: Dict[int, List[int]] = {}
    for idx, m in enumerate(mjds):
        groups.setdefault(int((m - t0) // step), []).append(idx)
    for _, idxs in sorted(groups.items()):
        sub_r = r_arr[idxs]
        sub_w = w[idxs]
        b_flux = float(np.sum(sub_w * sub_r) / np.sum(sub_w))
        b_err = float(1.0 / math.sqrt(float(np.sum(sub_w))))
        b_safe = max(b_flux, 1e-6)
        b_dm = -2.5 * math.log10(b_safe)
        b_dm_err = (2.5 / math.log(10.0)) * (b_err / b_safe)
        epoch_bins.append({
            "mjd": _num(float(np.mean(mjds[idxs]))),
            "date": timed[idxs[len(idxs) // 2]].get("date"),
            "n_points": len(idxs),
            "relative_flux": _num(b_flux),
            "relative_err": _num(b_err),
            "delta_mag": _num(b_dm),
            "mag_err": _num(b_dm_err),
        })

    if chi2_red >= 5.0 and dm_range >= 0.35:
        is_var = True
        var_conf = "High"
        var_type = "Large-Amplitude Variable Star (Mira / T Tauri Candidate)"
    elif chi2_red >= 3.0 and p2p_pct >= 12.0:
        is_var = True
        var_conf = "Medium"
        var_type = "Moderate Brightness Variable"
    elif chi2_red >= 2.0 and p2p_pct >= 8.0:
        is_var = True
        var_conf = "Low"
        var_type = "Low-Amplitude Variable Candidate"
    else:
        is_var = False
        var_conf = "None"
        var_type = "Stable / Non-Variable Source"

    return {
        "n_points": len(lc),
        "is_variable": is_var,
        "variability_confidence": var_conf,
        "variability_type": var_type,
        "reduced_chi2": _num(chi2_red),
        "fractional_rms_pct": _num(rms_pct),
        "peak_to_peak_pct": _num(p2p_pct),
        "delta_mag_range": _num(dm_range),
        "epoch_trend_pct": _num(epoch_trend_pct),
        "mjd_span_days": _num(mjd_span),
        "lightcurve": lc,
        "epoch_bins": epoch_bins,
    }


# ----------------------------------------------------------------------------
# 6. Unified entry point
# ----------------------------------------------------------------------------

def analyze_spectrum(points: Iterable[Dict[str, Any]], known_z: Optional[float] = None, *,
                     z_max: float = 3.5, dz: float = 0.002) -> Dict[str, Any]:
    """Bin -> redshift (unless known_z) -> feature identification -> cosmology -> variability."""
    pts_list = list(points) if not isinstance(points, list) else points
    binned = bin_spectrum_102(pts_list)
    wl, fl, er = _arrays(binned)

    if known_z is None:
        rz = _estimate_redshift_arrays(wl, fl, er, z_max=z_max, dz=dz)
        z = float(rz["z"] or 0.0)
    else:
        z = float(known_z)
        rz = {"z": _num(z), "z_err": None, "confidence": "Fixed",
              "regime": "extragalactic" if z >= GALACTIC_Z_LIMIT else "galactic",
              "matched_lines": [], "method": "user_supplied", "candidates": []}

    detections = _identify_arrays(wl, fl, er, z=z)
    if known_z is not None:
        rz["matched_lines"] = [d["name"] for d in detections if (d["snr"] or 0) >= SNR_REDSHIFT_LINE]

    cosmo = compute_cosmology(z, rz.get("z_err") or 0.0)
    variability = analyze_variability(pts_list, binned)
    summary = summarize(detections, cosmo["regime"])
    if variability.get("is_variable") and cosmo["regime"] == "galactic":
        summary = f"{summary} + Variable Source (Δm={variability['delta_mag_range']:.2f} mag)"
    return {
        "n_channels": len(binned),
        "binned_102": [{k: (_num(v) if isinstance(v, float) else v) for k, v in b.items()} for b in binned],
        "redshift": rz,
        "cosmology": cosmo,
        "detections": detections,
        "variability": variability,
        "summary_classification": summary,
        "notes": ("Quick-look feature identification on uncalibrated aperture photometry; "
                  "S/N and depths are indicative only."),
    }
