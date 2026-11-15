"""Offline tests for spectral feature identification, redshift and cosmology."""
import math

import numpy as np
import pytest
from pydantic import ValidationError

from app import main, overlays, pipeline, spectral_id, targets
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
    meta = {"base_url": "https://example.invalid/0.fits",
            "mjd": 60800.0, "det": 2, "bandpass": "SPHEREx-D2"}
    content = make_mef(ra0, dec0, ra0, dec0, rot_deg=0.0, n=40)
    monkeypatch.setattr(pipeline.irsa_client, "query_images", lambda ra, dec, det=None: [meta])
    def fetch_all(images, ra, dec, size, *, aperture_only=False):
        assert aperture_only
        return [(meta, content)]

    monkeypatch.setattr(pipeline.irsa_client, "fetch_all", fetch_all)
    spec = main.spectrum(ra=ra0, dec=dec0, aperture_px=1.5, size=0.04, max_frames=600, dets="2", z=0.158)
    assert "analysis" in spec
    assert spec["analysis"]["redshift"]["z"] == pytest.approx(0.158)
    assert spec["analysis"]["cosmology"]["regime"] == "extragalactic"


def test_sexagesimal_and_coordinate_resolution():
    # Exact string from the NASA Space Apps Challenge prompt
    queries = [
        "08h55m10.831s -7d14m42.53s",
        "08h55m10.831s, -7d14m42.53s",
        "Right Ascension 08h55m10.831s, declination -7d14m42.53s",
        "08:55:10.831 -07:14:42.53",
        "08 55 10.831 -07 14 42.53",
        "133.795129, -7.245147",
        "WISE 0855",
    ]
    for q in queries:
        info = targets.resolve(q)
        assert info["ra"] == pytest.approx(133.795129, abs=2e-5), q
        assert info["dec"] == pytest.approx(-7.245147, abs=2e-5), q
        assert info["ra_hms"].startswith("08h55m10.83")
        assert info["dec_dms"].startswith("-07d14m42.5")

    # Separate sexagesimal ra & dec parameters in _center and pixel_to_sky
    ra_deg, dec_deg, info = main._center("08h55m10.831s", "-7d14m42.53s", None, None)
    assert ra_deg == pytest.approx(133.795129, abs=2e-5)
    assert dec_deg == pytest.approx(-7.245147, abs=2e-5)
    assert info["ra_hms"] == "08h55m10.831s"

    sky = main.pixel_to_sky(ra="08h55m10.831s", dec="-7d14m42.53s", fov=0.1, x=29, y=29)
    assert sky["ra"] == pytest.approx(133.795129, abs=2e-5)
    assert "ra_hms" in sky and "dec_dms" in sky


def test_all_five_challenge_targets_resolve_offline(monkeypatch):
    monkeypatch.setattr(targets, "_sesame", lambda name: pytest.fail(f"SIMBAD called for {name}"))
    expected_ids = ["wise0855", "barnards-star", "eps-indi-b", "rw-aur", "r-dra"]
    for tid in expected_ids:
        info = targets.resolve(tid, epoch=2026.0)
        assert 0.0 <= info["ra"] < 360.0
        assert -90.0 <= info["dec"] <= 90.0
        assert "ra_hms" in info and "dec_dms" in info
    # Proper motion must be applied for Barnard's Star and Epsilon Indi B
    assert targets.resolve("barnards-star", epoch=2026.0)["pm_applied"] is True
    assert targets.resolve("eps-indi-b", epoch=2026.0)["pm_applied"] is True


def test_coadds_epoch_diff_and_candidate_detection(monkeypatch):
    ra0, dec0 = 133.795, -7.245
    # 4 exposures across 2 epochs (180 days apart): star shifts north by 5 pixels in Epoch 2
    shift_deg = 5 * 6.15 / 3600.0
    mjds = [60800.0, 60801.0, 60980.0, 60981.0]
    decs = [dec0, dec0, dec0 + shift_deg, dec0 + shift_deg]
    metas, contents = [], []
    for i, (mjd, dec_s) in enumerate(zip(mjds, decs)):
        metas.append({
            "base_url": f"https://example.invalid/{i}.fits",
            "mjd": mjd, "det": 2, "bandpass": "SPHEREx-D2",
        })
        contents.append(make_mef(
            ra0, dec_s, ra0, dec0, rot_deg=10.0 * i,
            hot_pixel=(10, 10), source_pixel=(50, 50),
        ))

    monkeypatch.setattr(pipeline.irsa_client, "query_images", lambda ra, dec, det=None: metas)
    monkeypatch.setattr(pipeline.irsa_client, "fetch_all", lambda images, ra, dec, size: list(zip(metas, contents)))

    res = pipeline.build_frames(ra0, dec0, 0.1, 2)
    assert "HOT" in res["frames"][0]["flags_breakdown"]
    assert "SOURCE" in res["frames"][0]["flags_breakdown"]
    comp = res["composites"]
    assert comp["coadd_mean_image"].startswith("data:image/png;base64,")
    assert comp["max_projection_image"].startswith("data:image/png;base64,")
    assert comp["epoch1_image"].startswith("data:image/png;base64,")
    assert comp["epoch2_image"].startswith("data:image/png;base64,")
    assert comp["epoch_diff_image"].startswith("data:image/png;base64,")
    assert comp["epoch_diff_color_image"].startswith("data:image/png;base64,")
    assert comp["epoch1_n_frames"] == 2 and comp["epoch2_n_frames"] == 2

    # Moving star must be detected as a dipole ("mover") shifting ~5 pixels
    movers = [c for c in res["candidates"] if c["kind"] == "mover"]
    assert len(movers) >= 1
    top = movers[0]
    assert top["shift_px"] == pytest.approx(5.0, abs=1.5)
    assert top["shift_arcsec"] == pytest.approx(5.0 * 6.15, abs=10.0)
    assert top["proper_motion_arcsec_yr"] is not None and top["proper_motion_arcsec_yr"] > 30.0
    assert top["y"] < top["from_y"]  # moved north -> smaller PNG y (top-left origin)

    # Also test subtract="first" and subtract="prev"
    res_first = pipeline.build_frames(ra0, dec0, 0.1, 2, subtract="first")
    assert res_first["query"]["stretch"] == "linear"
    res_prev = pipeline.build_frames(ra0, dec0, 0.1, 2, subtract="prev")
    assert res_prev["query"]["stretch"] == "linear"


def test_gaia_and_wise_overlays(monkeypatch):
    ra0, dec0 = 133.79513, -7.245147
    fake_gaia = [
        {
            "source_id": "1234567890",
            "ra": ra0 + 0.01,
            "dec": dec0 + 0.01,
            "g_mag": 12.4,
            "bp_mag": 13.1,
            "rp_mag": 11.6,
            "bp_rp": 1.5,
            "pm_ra_masyr": -798.6,
            "pm_dec_masyr": 10328.1,
            "parallax_mas": 546.98,
        }
    ]
    fake_wise = [
        # Optical star also seen in WISE
        {
            "designation": "J085513.20-071406.5",
            "ra": ra0 + 0.01,
            "dec": dec0 + 0.01,
            "w1_mag": 9.8,
            "w2_mag": 9.7,
            "w3_mag": 9.6,
            "w4_mag": 9.0,
            "w1_snr": 45.0,
            "w2_snr": 42.0,
            "pm_ra_masyr": -800.0,
            "pm_dec_masyr": 10300.0,
        },
        # Cold brown dwarf (WISE 0855-like): bright in W2, red W1-W2, NO Gaia optical counterpart!
        {
            "designation": "J085510.83-071442.5",
            "ra": ra0,
            "dec": dec0,
            "w1_mag": 17.8,
            "w2_mag": 13.9,
            "w3_mag": 11.5,
            "w4_mag": 9.0,
            "w1_snr": 6.0,
            "w2_snr": 28.0,
            "pm_ra_masyr": -8120.0,
            "pm_dec_masyr": 700.0,
        },
    ]
    monkeypatch.setattr(overlays, "fetch_gaia_sources", lambda ra, dec, rad, max_sources=150: fake_gaia)
    monkeypatch.setattr(overlays, "fetch_wise_sources", lambda ra, dec, rad, max_sources=150: fake_wise)

    out = main.catalog_overlays(
        ra="08h55m10.831s",
        dec="-7d14m42.53s",
        fov=0.1,
        scale=6.15,
        catalogs="gaia,wise",
        max_sources=150,
    )
    assert out["counts"]["gaia_dr3"] == 1
    assert out["counts"]["wise"] == 2
    assert out["counts"]["high_proper_motion"] == 1
    assert out["counts"]["ir_only_candidates"] == 1

    g0 = out["gaia"][0]
    assert g0["distance_pc"] == pytest.approx(1000.0 / 546.98, abs=0.05)
    assert g0["distance_ly"] == pytest.approx(5.96, abs=0.05)
    assert g0["is_high_proper_motion"] is True
    assert g0["dy_px_per_yr"] < 0  # +pm_dec (north) -> -y in top-left PNG coordinates

    ir_only = out["ir_only_candidates"][0]
    assert ir_only["designation"] == "J085510.83-071442.5"
    assert ir_only["gaia_match"] is False
    assert ir_only["w1_w2_color"] == pytest.approx(3.9, abs=0.01)
    assert ir_only["w2_flux_mjy"] > ir_only["w1_flux_mjy"]
    # Located right at the center of the 59x59 grid (x=29, y=29)
    assert ir_only["x"] == pytest.approx(29.0, abs=0.5)
    assert ir_only["y"] == pytest.approx(29.0, abs=0.5)


def test_variability_lightcurve_rw_aur_and_r_dra():
    # Build multi-epoch exposures across wavelengths: Epoch 2 is 2.2x brighter than Epoch 1 (~0.86 mag)
    var_pts = []
    const_pts = []
    for i, ch in enumerate(spectral_id.CHANNELS[:36]):
        lam = ch["center_um"]
        base_flux = 0.05 * (lam / 1.5) ** (-1.2)
        # Epoch 1 (MJD ~ 60800)
        var_pts.append({
            "mjd": 60800.0 + (i % 5) * 0.5,
            "date": "2025-05-05T00:00:00Z",
            "det": ch["det"],
            "wavelength_um": lam,
            "flux_jy": base_flux * 0.70,
            "err_jy": base_flux * 0.02,
        })
        # Epoch 2 (MJD ~ 60980): brightened by 2.2x
        var_pts.append({
            "mjd": 60980.0 + (i % 5) * 0.5,
            "date": "2025-11-01T00:00:00Z",
            "det": ch["det"],
            "wavelength_um": lam,
            "flux_jy": base_flux * 1.54,
            "err_jy": base_flux * 0.02,
        })
        const_pts.append({
            "mjd": 60800.0 + i * 2.0,
            "date": "2025-05-05T00:00:00Z",
            "det": ch["det"],
            "wavelength_um": lam,
            "flux_jy": base_flux,
            "err_jy": base_flux * 0.03,
        })

    res_var = spectral_id.analyze_spectrum(var_pts, known_z=0.0)
    v = res_var["variability"]
    assert v["is_variable"] is True
    assert v["variability_confidence"] == "High"
    assert v["delta_mag_range"] == pytest.approx(2.5 * math.log10(1.54 / 0.70), abs=0.1)
    assert v["epoch_trend_pct"] > 100.0
    assert len(v["lightcurve"]) == len(var_pts)
    assert len(v["epoch_bins"]) >= 2
    assert "Variable Source" in res_var["summary_classification"]

    res_const = spectral_id.analyze_spectrum(const_pts, known_z=0.0)
    assert res_const["variability"]["is_variable"] is False
    assert res_const["variability"]["variability_confidence"] == "None"


def test_six_detector_stacks_and_false_color_rgb(monkeypatch):
    ra0, dec0 = 133.79513, -7.245147
    metas = []
    contents = []
    for det in range(1, 7):
        meta = {
            "base_url": f"https://example.invalid/d{det}.fits",
            "mjd": 60800.0 + det,
            "det": det,
            "bandpass": f"SPHEREx-D{det}",
        }
        metas.append(meta)
        contents.append(make_mef(ra0, dec0, ra0, dec0, rot_deg=5.0 * det, n=40))

    monkeypatch.setattr(pipeline.irsa_client, "query_selected_images", lambda ra, dec, dets=None: metas)
    monkeypatch.setattr(pipeline.irsa_client, "fetch_all", lambda images, ra, dec, size: list(zip(metas, contents)))

    out = main.detector_stacks(target="wise0855", fov=0.1, max_per_det=10)
    assert out["counts"]["detectors_with_data"] == 6
    assert len(out["detectors"]) == 6
    for d_info in out["detectors"]:
        assert d_info["n_stacked"] == 1
        assert d_info["coadd_image"].startswith("data:image/png;base64,")
        assert d_info["max_projection_image"].startswith("data:image/png;base64,")
    assert out["all_detectors_coadd_image"].startswith("data:image/png;base64,")
    assert out["false_color_rgb_image"].startswith("data:image/png;base64,")
    assert out["rgb_channels"]["blue_short_nir_dets"] == [1, 2]
    assert out["rgb_channels"]["green_mid_nir_dets"] == [3, 4]
    assert out["rgb_channels"]["red_long_nir_dets"] == [5, 6]




