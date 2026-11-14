"""Offline tests for spectral feature identification, redshift and cosmology."""
import math

import numpy as np
import pytest
from pydantic import ValidationError

from app import main, pipeline, spectral_id
from synth import make_mef

ICES_AND_BRG = [(3.05, 0.30, -0.30), (4.27, 0.08, -0.35), (2.166, 0.05, +0.60)]
QSO_LINES = [(0.6563, 0.04, +0.8), (1.282, 0.045, +0.5), (1.875, 0.05, +0.6), (3.30, 0.08, +0.4)]


def synthetic_points(lines, z=0.0, noise=0.01, n_exp=3, seed=1):
    """Raw multi-exposure points on the 102-channel grid.

    lines: (rest_um, rest_fwhm_um, peak_amplitude) with amplitude < 0 for absorption.
    Each line is broadened by the SPHEREx resolution (area conserved), shifted by
    (1 + z) and multiplied onto a gentle power-law continuum; Gaussian noise of
    `noise` x continuum is added and reported as err_jy.
    """
    rng = np.random.default_rng(seed)
    pts = []
    for ch in spectral_id.CHANNELS:
        width = ch["hi_um"] - ch["lo_um"]
        for _ in range(n_exp):
            lam = ch["center_um"] + rng.uniform(-0.25, 0.25) * width
            cont = 0.01 * (lam / 2.0) ** 0.3
            prof = 1.0
            for rest, fwhm, amp in lines:
                lo, fo = rest * (1 + z), fwhm * (1 + z)
                fe = math.hypot(fo, lo / spectral_id.resolving_power(lo))
                prof += amp * fo / fe * math.exp(-0.5 * ((lam - lo) / (fe / 2.3548)) ** 2)
            pts.append({"wavelength_um": float(lam),
                        "flux_jy": float(cont * prof + rng.normal(0.0, noise * cont)),
                        "err_jy": float(noise * cont)})
    return pts


def _true_ew(fwhm, amp):
    return -amp * fwhm / 2.3548 * math.sqrt(2 * math.pi)


def test_channel_grid_has_102_channels():
    assert len(spectral_id.CHANNELS) == 102
    centers = [c["center_um"] for c in spectral_id.CHANNELS]
    assert centers == sorted(centers)
    assert 0.75 < centers[0] < 0.77 and 4.9 < centers[-1] < 5.0
    assert {c["det"] for c in spectral_id.CHANNELS} == {1, 2, 3, 4, 5, 6}


def test_binning_inverse_variance():
    pts = [{"wavelength_um": 2.0, "flux_jy": 1.0, "err_jy": 0.1},
           {"wavelength_um": 2.001, "flux_jy": 2.0, "err_jy": 0.2}]
    b = spectral_id.bin_spectrum_102(pts)
    assert len(b) == 1 and b[0]["n_exposures"] == 2
    assert b[0]["flux_jy"] == pytest.approx((1 / 0.01 + 2 / 0.04) / (1 / 0.01 + 1 / 0.04))
    assert b[0]["err_jy"] == pytest.approx(1 / math.sqrt(1 / 0.01 + 1 / 0.04))
    # missing errors and garbage entries are tolerated
    assert spectral_id.bin_spectrum_102([{"wavelength_um": 1.0, "flux_jy": 1.0},
                                         {"wavelength_um": "x"}, {}])[0]["n_exposures"] == 1


def test_identify_ices_and_brgamma_at_z0():
    res = spectral_id.analyze_spectrum(synthetic_points(ICES_AND_BRG))
    assert res["n_channels"] == 102 and res["binned_102"][0]["n_exposures"] == 3
    assert res["redshift"]["z"] == 0.0 and res["redshift"]["regime"] == "galactic"
    assert res["cosmology"]["regime"] == "galactic" and res["cosmology"]["luminosity_distance_mpc"] is None

    det = {d["id"]: d for d in res["detections"]}
    for key in ("h2o_ice", "co2_ice", "br_gamma"):
        assert det[key]["confidence"] == "High", key
    assert set(det) == {"h2o_ice", "co2_ice", "br_gamma"}          # no spurious features
    assert det["h2o_ice"]["optical_depth_tau"] > 0.15 and det["co2_ice"]["optical_depth_tau"] > 0.15
    assert det["br_gamma"]["optical_depth_tau"] == 0.0 and det["br_gamma"]["contrast_pct"] > 0

    for key, (rest, fwhm, amp) in zip(("h2o_ice", "co2_ice", "br_gamma"), ICES_AND_BRG):
        ew = _true_ew(fwhm, amp)
        assert det[key]["equivalent_width_fit_um"] == pytest.approx(ew, rel=0.10), key
        assert math.copysign(1, det[key]["equivalent_width_um"]) == math.copysign(1, ew)
    for key in ("h2o_ice", "co2_ice"):     # side-band photometry: wings bias it slightly low
        assert det[key]["equivalent_width_um"] == pytest.approx(_true_ew(*ICES_AND_BRG[0 if key == "h2o_ice" else 1][1:]), rel=0.25)
    assert "Ice-Rich" in res["summary_classification"]


def test_ch4_absorption_vs_pah_emission_disambiguation():
    absorb = spectral_id.analyze_spectrum(synthetic_points([(3.31, 0.15, -0.35)]), known_z=0.0)
    ids = {d["id"] for d in absorb["detections"]}
    assert "ch4_331" in ids and "pah_330" not in ids
    assert "Methane" in absorb["summary_classification"]

    emit = spectral_id.analyze_spectrum(synthetic_points([(3.30, 0.08, +0.5)]), known_z=0.0)
    ids = {d["id"] for d in emit["detections"]}
    assert "pah_330" in ids and "ch4_331" not in ids and "h2o_ice" not in ids


def test_redshift_and_distance_3c273_analog():
    z_true = 0.158
    pts = synthetic_points(QSO_LINES, z=z_true)
    rz = spectral_id.estimate_redshift(pts)
    assert abs(rz["z"] - z_true) < 0.01
    assert rz["regime"] == "extragalactic" and rz["confidence"] == "High"
    assert len(rz["matched_lines"]) >= 3

    res = spectral_id.analyze_spectrum(pts)
    det = {d["id"]: d for d in res["detections"]}
    assert {"pa_beta", "pa_alpha", "pah_330"} <= set(det)
    assert det["pa_alpha"]["rest_wavelength_um"] == 1.875
    assert det["pa_alpha"]["observed_wavelength_um"] == pytest.approx(1.875 * (1 + z_true), abs=0.02)
    assert res["cosmology"]["regime"] == "extragalactic"
    assert "Extragalactic" in res["summary_classification"]

    cos = spectral_id.compute_cosmology(z_true, 0.01)
    assert cos["luminosity_distance_mpc"] == pytest.approx(754, abs=50)
    assert cos["lookback_time_gyr"] == pytest.approx(1.98, abs=0.1)
    assert cos["comoving_distance_mpc"] == pytest.approx(cos["luminosity_distance_mpc"] / (1 + z_true), rel=1e-4)
    assert cos["angular_diameter_distance_mpc"] == pytest.approx(cos["comoving_distance_mpc"] / (1 + z_true), rel=1e-4)
    assert cos["recession_velocity_kms"] == pytest.approx(43665, abs=50)
    assert cos["luminosity_distance_gly"] == pytest.approx(2.46, abs=0.05)
    assert cos["luminosity_distance_err_mpc"] > 0


def test_cosmology_galactic_regime_and_flat_spectrum():
    cos = spectral_id.compute_cosmology(0.001)
    assert cos["regime"] == "galactic" and not cos["is_extragalactic"]
    assert cos["luminosity_distance_mpc"] is None and "parallax" in cos["note"]

    flat = spectral_id.analyze_spectrum(synthetic_points([]))
    assert flat["redshift"]["z"] == 0.0 and flat["detections"] == []
    assert flat["summary_classification"] == "Continuum-Dominated Source"

    empty = spectral_id.analyze_spectrum([])
    assert empty["n_channels"] == 0 and empty["detections"] == []


def test_analyze_and_spectrum_handlers(monkeypatch):
    pts = synthetic_points(QSO_LINES, z=0.158)
    body = main.analyze_spectrum(main.AnalyzeRequest(points=pts))
    assert body["redshift"]["regime"] == "extragalactic"
    assert abs(body["redshift"]["z"] - 0.158) < 0.01
    assert body["cosmology"]["luminosity_distance_mpc"] > 600

    fixed = main.analyze_spectrum(main.AnalyzeRequest(points=pts, z=0.0))
    assert fixed["redshift"]["method"] == "user_supplied" and fixed["cosmology"]["regime"] == "galactic"

    with pytest.raises(ValidationError):
        main.AnalyzeRequest(points=pts, z=-1)
    with pytest.raises(ValidationError):
        main.AnalyzeRequest(points=[{"flux_jy": 1}])

    empty = main.analyze_spectrum(main.AnalyzeRequest(points=[]))
    assert empty["n_channels"] == 0

    lib = main.spectral_features()
    assert len(lib["channels"]) == 102 and any(f["id"] == "h2o_ice" for f in lib["features"])

    # Also verify pipeline.build_spectrum attaches `analysis` and respects `z`
    ra0, dec0 = 133.795, -7.245
    meta = {"image": {"s3": "https://example.invalid/s3/0.fits", "irsa": "https://example.invalid/0.fits"},
            "mjd": 60800.0, "det": 2, "bandpass": "SPHEREx-D2"}
    content = make_mef(ra0, dec0, ra0, dec0, rot_deg=0.0, n=40)
    monkeypatch.setattr(pipeline.irsa_client, "query_images", lambda ra, dec, det=None: [meta])
    monkeypatch.setattr(pipeline.irsa_client, "fetch_all", lambda images, ra, dec, size: [(meta, content)])
    spec = main.spectrum(ra=ra0, dec=dec0, aperture_px=1.5, size=0.04, max_frames=600, dets="2", z=0.158)
    assert "analysis" in spec
    assert spec["analysis"]["redshift"]["z"] == pytest.approx(0.158)
    assert spec["analysis"]["cosmology"]["regime"] == "extragalactic"
