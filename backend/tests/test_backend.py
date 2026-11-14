"""Offline tests with synthetic data. Run:  cd backend && pytest -q"""
from app import irsa_client, processing
import numpy as np
import pytest
from astropy import units as u
from astropy.coordinates import SkyCoord
from fastapi.testclient import TestClient

from app import targets
from app.main import app
from synth import fake_wavelength_maps, make_mef

RA0, DEC0 = 133.795, -7.245


@pytest.fixture(autouse=True)
def _fake_wavelengths(monkeypatch):
    monkeypatch.setattr(processing, "_wavelength_maps", fake_wavelength_maps)


def _metas(n, det=2, dt_days=1.0, t0=60800.0):
    return [{"base_url": f"https://example.invalid/img{i}.fits",
             "mjd": t0 + i * dt_days,
             "det": det, "bandpass": f"SPHEREx-D{det}"} for i in range(n)]


def _patch_irsa(monkeypatch, metas, contents, fail_index=None):
    monkeypatch.setattr(irsa_client, "query_images", lambda ra, dec, det=None: metas)
    fetch_modes = []

    def fake_fetch_all(images, ra, dec, size, *, aperture_only=False):
        fetch_modes.append(aperture_only)
        out = []
        for i, m in enumerate(images):
            out.append((m, None if i == fail_index else contents[i]))
        return out
    monkeypatch.setattr(irsa_client, "fetch_all", fake_fetch_all)
    return fetch_modes


# ------------------------------------------------------------ processing ---

def test_grid_is_north_up_east_left():
    grid, shape = processing.make_grid(RA0, DEC0, 0.1)
    c = shape[0] // 2
    ra_c, dec_c = grid.wcs_pix2world([[c, c]], 0)[0]
    ra_r, _ = grid.wcs_pix2world([[c + 5, c]], 0)[0]
    _, dec_u = grid.wcs_pix2world([[c, c + 5]], 0)[0]
    assert ra_r < ra_c      # +x is toward decreasing RA (east is left)
    assert dec_u > dec_c    # +row is north


@pytest.mark.parametrize("rot", [0.0, 33.0, 90.0, -47.0])
def test_static_star_stays_put_despite_detector_rotation(rot):
    grid, shape = processing.make_grid(RA0, DEC0, 0.1)
    content = make_mef(RA0, DEC0, RA0, DEC0, rot_deg=rot)
    c = processing.parse_cutout(content, {"mjd": 1.0, "det": 2})
    img, _ = processing.clean_image(c, sky="median")
    out = processing.reproject_to_grid(img, c.wcs, grid, shape)
    row, col = np.unravel_index(np.nanargmax(out), out.shape)
    ex, ey = grid.world_to_pixel(SkyCoord(RA0 * u.deg, DEC0 * u.deg))
    assert abs(col - ex) <= 1 and abs(row - ey) <= 1


def test_moving_star_moves_in_common_grid():
    grid, shape = processing.make_grid(RA0, DEC0, 0.1)
    star2_dec = DEC0 + 4 * 6.15 / 3600.0    # 4 pixels north
    pos = []
    for dec_s, rot in [(DEC0, 10.0), (star2_dec, 55.0)]:
        content = make_mef(RA0, dec_s, RA0, DEC0, rot_deg=rot)
        c = processing.parse_cutout(content, {"mjd": 1.0, "det": 2})
        img, _ = processing.clean_image(c)
        out = processing.reproject_to_grid(img, c.wcs, grid, shape)
        pos.append(np.unravel_index(np.nanargmax(out), out.shape))
    assert abs((pos[1][0] - pos[0][0]) - 4) <= 1     # rows increase northward
    assert abs(pos[1][1] - pos[0][1]) <= 1


def test_flag_masking_keeps_source_pixels():
    content = make_mef(RA0, DEC0, RA0, DEC0, hot_pixel=(10, 10), source_pixel=(20, 20))
    c = processing.parse_cutout(content, {"mjd": 1.0, "det": 2})
    img, frac = processing.clean_image(c, sky="none", mask=True)
    assert np.isnan(img[10, 10])         # HOT is masked
    assert np.isfinite(img[20, 20])      # SOURCE is not
    assert frac == pytest.approx(1 / img.size)
    img_off, _ = processing.clean_image(c, sky="none", mask=False)
    assert np.isfinite(img_off[10, 10])


def test_aperture_point_detects_star_and_wavelength():
    content = make_mef(RA0, DEC0, RA0, DEC0, rot_deg=20.0, n=40)
    c = processing.parse_cutout(content, {"mjd": 5.0, "det": 3})
    p = processing.aperture_point(c, RA0, DEC0)
    assert p is not None and p.get("flux_jy") > 0
    assert 1.10 <= p.get("wavelength_um") <= 1.62
    off = processing.aperture_point(c, RA0 + 0.01, DEC0)     # empty sky nearby
    assert off is None or off.get("flux_jy") < 0.1 * p.get("flux_jy")


def test_proper_motion_barnards_star():
    out = targets.apply_proper_motion(269.45, 4.69, -798.6, 10328.1, 2000.0, 2026.0)
    assert out.get("dec") - 4.69 == pytest.approx(10328.1 * 26 / 3.6e6, rel=1e-6)   # ~0.0746 deg


def test_wise0855_resolves_offline_from_challenge_coordinates():
    info = targets.resolve("wise0855")
    assert info.get("ra") == pytest.approx(133.79513, abs=1e-5)
    assert info.get("dec") == pytest.approx(-7.245147, abs=1e-5)
    assert not info.get("pm_applied")


# ------------------------------------------------------------------- API ---

@pytest.mark.parametrize("name,preset_id", [
    ("wise0855", "wise0855"),
    ("WISE 0855-0714", "wise0855"),
    ("barnards-star", "barnards-star"),
    ("Barnard's Star", "barnards-star"),
    ("Barnard’s Star", "barnards-star"),
    ("GJ 699", "barnards-star"),
    ("eps-indi-b", "eps-indi-b"),
    ("Epsilon Indi B", "eps-indi-b"),
    ("Epsilon Indi B (Ba/Bb)", "eps-indi-b"),
    ("eps Ind B", "eps-indi-b"),
    ("rw-aur", "rw-aur"),
    ("RW Aurigae", "rw-aur"),
    ("RW Aur", "rw-aur"),
    ("r-dra", "r-dra"),
    ("R Draconis", "r-dra"),
    ("R Dra", "r-dra"),
])
def test_resolve_presets_by_id_display_name_and_alias(monkeypatch, name, preset_id):
    preset = next(t for t in targets.load_targets() if t["id"] == preset_id)
    queries = []

    def resolve_catalog_name(catalog_name):
        queries.append(catalog_name)
        assert catalog_name == preset["resolve"]
        return {"ra": RA0, "dec": DEC0}

    monkeypatch.setattr(targets, "_sesame", resolve_catalog_name)
    response = TestClient(app).get("/api/resolve", params={"name": name, "epoch": 2026.0})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == preset["name"]
    assert body["pm_applied"] == ("pm_ra_masyr" in preset)
    assert body["ra_hms"] and body["dec_dms"]
    if "ra" in preset:
        assert queries == []
        assert body["ra"] == pytest.approx(preset["ra"])
        assert body["dec"] == pytest.approx(preset["dec"])
    else:
        assert queries == [preset["resolve"]]
        if body["pm_applied"]:
            assert body["epoch"] == 2026.0
            assert body["ra"] != RA0 and body["dec"] != DEC0
        else:
            assert body["ra"] == pytest.approx(RA0)
            assert body["dec"] == pytest.approx(DEC0)


def test_frames_endpoint(monkeypatch):
    n = 4
    metas = _metas(n)
    contents = [make_mef(RA0, DEC0 + i * 2 * 6.15 / 3600, RA0, DEC0, rot_deg=15.0 * i) for i in range(n)]
    fetch_modes = _patch_irsa(monkeypatch, metas, contents)
    client = TestClient(app)
    r = client.get("/api/frames", params={"ra": RA0, "dec": DEC0, "fov": 0.1, "det": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["frames_returned"] == n
    assert body["grid"]["n_pixels"] == 59
    f0 = body["frames"][0]
    assert f0["image"].startswith("data:image/png;base64,")
    assert 1.10 <= f0["wavelength_um"] <= 1.62
    assert f0["date"].endswith("Z") and f0["version"] == "test-v1"
    assert fetch_modes == [False]

    r2 = client.get("/api/frames", params={"ra": RA0, "dec": DEC0, "bin_days": 2})
    assert r2.json()["counts"]["frames_returned"] == 2
    assert r2.json()["frames"][0]["n_stacked"] == 2

    r3 = client.get("/api/frames", params={"ra": RA0, "dec": DEC0, "subtract": "median"})
    assert r3.status_code == 200 and r3.json()["query"]["stretch"] == "linear"


def test_frames_reports_failed_downloads(monkeypatch):
    metas = _metas(3)
    contents = [make_mef(RA0, DEC0, RA0, DEC0) for _ in range(3)]
    _patch_irsa(monkeypatch, metas, contents, fail_index=1)
    body = TestClient(app).get("/api/frames", params={"ra": RA0, "dec": DEC0}).json()
    assert body.get("counts")["failed"] == 1 and body.get("counts")["frames_returned"] == 2


def test_frames_with_no_images(monkeypatch):
    _patch_irsa(monkeypatch, [], [])
    r = TestClient(app).get("/api/frames", params={"ra": RA0, "dec": DEC0})
    assert r.status_code == 200 and r.json()["frames"] == []


def test_frames_by_target_name(monkeypatch):
    _patch_irsa(monkeypatch, _metas(1), [make_mef(RA0, DEC0, RA0, DEC0)])
    r = TestClient(app).get("/api/frames", params={"target": "wise0855"})
    assert r.status_code == 200
    assert r.json()["resolved"]["name"] == "WISE 0855-0714"


def test_frames_requires_coordinates_or_target():
    assert TestClient(app).get("/api/frames").status_code == 422
    assert TestClient(app).get("/api/frames", params={"ra": 400, "dec": 0}).status_code == 422


def test_spectrum_endpoint_sorted_by_wavelength(monkeypatch):
    # 6 exposures, star sits at different rows so the fake wavelength differs
    contents, metas = [], []
    for i in range(6):
        ra0, dec0 = RA0, DEC0 + (i - 3) * 3 * 6.15 / 3600.0
        contents.append(make_mef(RA0, DEC0, ra0, dec0, rot_deg=7.0 * i, n=40))
        metas.append({"base_url": f"https://example.invalid/{i}.fits",
                      "mjd": 60800.0 + i,
                      "det": 1 + i, "bandpass": f"SPHEREx-D{1 + i}"})
    fetch_modes = _patch_irsa(monkeypatch, metas, contents)
    body = TestClient(app).get("/api/spectrum", params={"ra": RA0, "dec": DEC0}).json()
    waves = [p.get("wavelength_um") for p in body.get("points")]
    assert len(waves) == 6 and waves == sorted(waves)
    assert all(p.get("flux_jy") > 0 for p in body.get("points"))
    assert {p.get("det") for p in body.get("points")} == {1, 2, 3, 4, 5, 6}
    assert fetch_modes == [True]
    assert "analysis" in body and "redshift" in body["analysis"]

    fixed = TestClient(app).get("/api/spectrum", params={"ra": RA0, "dec": DEC0, "z": 0.158}).json()
    assert fixed["analysis"]["redshift"]["z"] == pytest.approx(0.158)
    assert fixed["analysis"]["cosmology"]["regime"] == "extragalactic"


def test_pixel_to_sky_center_is_the_center():
    r = TestClient(app).get("/api/pixel_to_sky",
                            params={"ra": RA0, "dec": DEC0, "fov": 0.1, "x": 29, "y": 29}).json()
    assert r.get("ra") == pytest.approx(RA0, abs=1e-6) and r.get("dec") == pytest.approx(DEC0, abs=1e-6)
    north = TestClient(app).get("/api/pixel_to_sky",
                                params={"ra": RA0, "dec": DEC0, "fov": 0.1, "x": 29, "y": 20}).json()
    assert north.get("dec") > DEC0      # smaller y (higher in the picture) = north


def test_health_and_targets():
    c = TestClient(app)
    assert c.get("/api/health").json()["status"] == "ok"
    assert len(c.get("/api/targets").json()) == 5
