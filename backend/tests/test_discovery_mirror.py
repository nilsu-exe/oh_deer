"""Coverage, spherical geometry, atomic refreshes, and offline discovery routing."""
import io
import json
import os
import time

from astropy.io.votable import from_table, writeto
from astropy.table import Table
from fastapi.testclient import TestClient
import pytest
import requests

from app import config, discovery_mirror as mirror, irsa_client, processing, tap_metadata
from app.main import app
from synth import fake_wavelength_maps, make_mef

RA, DEC = 133.795, -7.245
POLYGON = "POLYGON ICRS 359 -1 1 -1 1 1 359 1"


@pytest.fixture(autouse=True)
def isolated_mirror(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DISCOVERY_MIRROR_PATH", tmp_path / "mirror.sqlite3")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "DISCOVERY_MODE", "auto")


def image(name="exposure", det=2, mjd=60800.0, footprint=POLYGON):
    return {"base_url": f"{config.IRSA_BASE}/ibe/data/spherex/release/{name}.fits",
            "mjd": mjd, "det": det, "bandpass": f"SPHEREx-D{det}", "footprint": footprint}


def test_point_snapshots_do_not_claim_nearby_or_missing_detector_coverage():
    assert mirror.query_images(RA, DEC, 2) is None
    mirror.replace_snapshot([image()], ra=RA, dec=DEC, det=2, updated=1)
    result = mirror.query_images(RA, DEC, 2)
    assert len(result) == 1 and result.discovery["stale"]
    assert mirror.query_images(RA + 0.001, DEC, 2) is None
    assert mirror.query_images(RA, DEC, 3) is None
    assert mirror.query_images(RA, DEC) is None


def test_all_six_detectors_required_for_spectrum_and_sorted_by_time():
    for det in range(1, 7):
        mirror.replace_snapshot([image(str(det), det=det, mjd=60810 - det)],
                                ra=RA, dec=DEC, det=det)
    result = mirror.query_images(RA, DEC)
    assert [r["det"] for r in result] == [6, 5, 4, 3, 2, 1]
    assert result.discovery["scope"] == "point"


@pytest.mark.parametrize("footprint", [POLYGON, "POLYGON ICRS 359 1 1 1 1 -1 359 -1"])
def test_full_sky_query_handles_ra_wrap_both_windings_edges_and_antipodes(footprint):
    mirror.replace_snapshot([image(footprint=footprint)], det=2)
    for ra, dec in [(0, 0), (359.5, 0), (0.5, 0), (1, 0)]:
        assert len(mirror.query_images(ra, dec, 2)) == 1
    for ra, dec in [(180, 0), (4, 0), (0, 10), (0, -10)]:
        assert mirror.query_images(ra, dec, 2) == []


def test_polar_footprint_and_complete_empty_snapshot():
    mirror.replace_snapshot([image(footprint="POLYGON ICRS 0 89 90 89 180 89 270 89")], det=2)
    assert len(mirror.query_images(123, 90, 2)) == 1
    assert mirror.query_images(123, -90, 2) == []
    mirror.replace_snapshot([], det=2)
    assert mirror.query_images(123, 90, 2) == []
    assert mirror.query_images(123, 90, 3) is None


def test_atomic_refresh_readers_keep_old_snapshot_until_commit_and_failure_rolls_back():
    mirror.replace_snapshot([image("old")], det=2, updated=10)

    def replacement():
        yield image("new")
        assert mirror.query_images(0, 0, 2)[0]["base_url"].endswith("old.fits")

    mirror.replace_snapshot(replacement(), det=2, updated=20)
    assert mirror.query_images(0, 0, 2)[0]["base_url"].endswith("new.fits")
    with pytest.raises(ValueError, match="STC-S"):
        mirror.replace_snapshot([image("other"), image("broken", footprint="not a polygon")], det=2)
    assert mirror.query_images(0, 0, 2)[0]["base_url"].endswith("new.fits")
    assert mirror.status()["snapshots"][0]["updated"] == 20


def test_newest_snapshot_wins_and_seeding_preserves_original_age(tmp_path):
    old = tmp_path / "tap_0.0_0.0_2.json"
    old.write_text(json.dumps([image("old")]))
    os.utime(old, (1, 1))
    assert mirror.seed_cache(tmp_path) == {"snapshots_imported": 1, "images_imported": 1}
    assert mirror.status()["snapshots"][0]["updated"] == 1
    mirror.replace_snapshot([image("new")], det=2, updated=time.time())
    result = mirror.query_images(0, 0, 2)
    assert result[0]["base_url"].endswith("new.fits")
    assert not result.discovery["stale"]


def test_point_precision_wraps_zero_ra():
    mirror.replace_snapshot([image()], ra=360.0, dec=0.0, det=2)
    assert len(mirror.query_images(359.99999, 0.0, 2)) == 1


def test_mirrored_frames_and_spectrum_expose_snapshot_age_without_tap(monkeypatch):
    rows = [image(str(det), det=det) for det in range(1, 7)]
    mirror.replace_snapshot(rows, ra=RA, dec=DEC, updated=1)
    monkeypatch.setattr(tap_metadata, "query_table", lambda *a, **kw: pytest.fail("TAP contacted"))
    monkeypatch.setattr(processing, "_wavelength_maps", fake_wavelength_maps)
    content = make_mef(RA, DEC, RA, DEC, n=40)
    monkeypatch.setattr(irsa_client, "fetch_all",
                        lambda images, *a, **kw: [(m, content) for m in images])
    client = TestClient(app)
    for endpoint, count_key, count in [("frames", "frames_returned", 1), ("spectrum", "points", 6)]:
        response = client.get(f"/api/{endpoint}", params={"ra": RA, "dec": DEC})
        assert response.status_code == 200, response.text
        assert response.json()["counts"][count_key] == count
        assert response.json()["discovery"]["source"] == "local_mirror"
        assert response.json()["discovery"]["stale"]
    state = client.get("/api/discovery/status").json()
    assert state["images"] == 6 and state["mode"] == "auto"


def test_local_only_missing_coverage_returns_503_without_contacting_tap(monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_MODE", "local")
    monkeypatch.setattr(tap_metadata, "query_table", lambda *a, **kw: pytest.fail("TAP contacted"))
    response = TestClient(app).get("/api/frames", params={"ra": RA, "dec": DEC})
    assert response.status_code == 503
    assert "No complete local" in response.json()["detail"]
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_selected_detector_spectrum_uses_only_that_detectors_mirror(monkeypatch):
    monkeypatch.setattr(config, "DISCOVERY_MODE", "local")
    mirror.replace_snapshot([image()], ra=RA, dec=DEC, det=2)
    monkeypatch.setattr(tap_metadata, "query_table", lambda *a, **kw: pytest.fail("TAP contacted"))
    monkeypatch.setattr(processing, "_wavelength_maps", fake_wavelength_maps)
    content = make_mef(RA, DEC, RA, DEC, n=40)
    monkeypatch.setattr(irsa_client, "fetch_all", lambda images, *a, **kw: [(m, content) for m in images])
    client = TestClient(app)
    response = client.get("/api/spectrum", params={"ra": RA, "dec": DEC, "dets": "2"})
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["points"] == 1
    assert response.json()["discovery"]["source"] == "local_mirror"
    assert client.get("/api/spectrum", params={"ra": RA, "dec": DEC, "dets": "7"}).status_code == 422


def test_live_success_is_saved_to_mirror_and_tap_mode_bypasses_local_data(monkeypatch):
    monkeypatch.setattr(irsa_client.cache, "get_json", lambda *a, **kw: None)
    calls = []

    def live(*a, **kw):
        calls.append(a)
        return Table({"uri": ["ibe/data/spherex/release/live.fits"], "mjd": [60800.0],
                      "bandpass": ["SPHEREx-D2"]})

    monkeypatch.setattr(tap_metadata, "query_table", live)
    assert irsa_client.query_images(RA, DEC, 2)[0]["base_url"].endswith("live.fits")
    assert irsa_client.query_images(RA, DEC, 2).discovery["source"] == "local_mirror"
    assert len(calls) == 1
    monkeypatch.setattr(config, "DISCOVERY_MODE", "tap")
    irsa_client.query_images(RA, DEC, 2)
    assert len(calls) == 2


def votable_bytes(status="OK"):
    table = from_table(Table({"uri": ["ibe/data/spherex/release/test.fits"],
                             "mjd": [60800.0], "bandpass": ["SPHEREx-D2"],
                             "footprint": [POLYGON]}))
    out = io.BytesIO()
    writeto(table, out)
    return out.getvalue().replace(b"</RESOURCE>",
                                 f'<INFO name="QUERY_STATUS" value="{status}">status</INFO></RESOURCE>'.encode())


def mock_response(monkeypatch, payload):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield payload

    def get(url, **kwargs):
        assert url.endswith("/TAP/sync")
        assert kwargs["timeout"] == (10, config.TAP_HTTP_TIMEOUT)
        assert kwargs["stream"] and kwargs["params"]["MAXREC"] > 0
        return Response()

    monkeypatch.setattr(tap_metadata.requests, "get", get)


@pytest.mark.parametrize("status", ["ERROR", "OVERFLOW"])
def test_tap_errors_and_late_overflow_cannot_replace_good_snapshot(monkeypatch, status):
    from scripts.sync_discovery import sync_detector

    mirror.replace_snapshot([image("old")], det=2, updated=10)
    mock_response(monkeypatch, votable_bytes(status))
    with pytest.raises(ValueError, match=status):
        sync_detector(2)
    assert mirror.query_images(0, 0, 2)[0]["base_url"].endswith("old.fits")
    assert mirror.status()["snapshots"][0]["updated"] == 10


def test_successful_export_builds_spatial_snapshot_and_enforces_byte_limit(monkeypatch):
    from scripts.sync_discovery import sync_detector

    mock_response(monkeypatch, votable_bytes())
    assert sync_detector(2) == 1
    assert len(mirror.query_images(0, 0, 2)) == 1
    assert mirror.query_images(10, 10, 2) == []
    with pytest.raises(ValueError, match="byte limit"):
        tap_metadata.query_table("SELECT", max_bytes=1)


def test_wrong_empty_export_schema_is_rejected_without_publishing_empty_coverage(monkeypatch):
    from scripts.sync_discovery import sync_detector

    mirror.replace_snapshot([image("old")], det=2, updated=10)
    monkeypatch.setattr(tap_metadata, "query_table", lambda *a, **kw: Table({"wrong": []}))
    with pytest.raises(ValueError, match="missing required"):
        sync_detector(2)
    assert mirror.query_images(0, 0, 2)[0]["base_url"].endswith("old.fits")


def test_tap_read_timeout_reaches_existing_upstream_error_response(monkeypatch):
    def timed_out(*a, **kw):
        assert kw["timeout"] == (10, config.TAP_HTTP_TIMEOUT)
        raise requests.Timeout("TAP stalled")

    monkeypatch.setattr(tap_metadata.requests, "get", timed_out)
    response = TestClient(app).get("/api/frames", params={"ra": RA, "dec": DEC})
    assert response.status_code == 502
