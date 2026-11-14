"""Offline checks for observable waits, upstream failures, and concurrent downloads."""
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, diagnostics, irsa_client
from app.main import app


@pytest.fixture
def app_logs(caplog, monkeypatch):
    # The application's console handler deliberately does not propagate to root.
    monkeypatch.setattr(logging.getLogger("app"), "propagate", True)
    caplog.set_level(logging.INFO, logger="app")
    return caplog


def test_heartbeat_identifies_blocked_discovery_and_preserves_request_id(monkeypatch, app_logs):
    monkeypatch.setattr(config, "LOG_HEARTBEAT_SECONDS", 0.005)

    def slow_discovery(*args):
        time.sleep(0.05)  # Simulate a blocking upstream HTTP request, offline.
        return []

    monkeypatch.setattr(irsa_client, "query_images", slow_discovery)
    response = TestClient(app).get("/api/frames", params={"ra": 1, "dec": 2})
    assert response.status_code == 200
    identifier = response.headers["x-request-id"]
    records = app_logs.records
    heartbeats = [r for r in records if "request running" in r.getMessage()
                  and "stage=discovery" in r.getMessage()]
    assert heartbeats
    assert all(r.request_id == identifier for r in records)
    assert any("request finished status=200" in r.getMessage() for r in records)


@pytest.mark.parametrize("endpoint", ["frames", "spectrum"])
def test_tap_proxy_failure_is_reported_as_upstream_502(monkeypatch, app_logs, endpoint):
    from astroquery.ipac.irsa import Irsa
    from pyvo.dal.exceptions import DALServiceError

    monkeypatch.setattr(irsa_client.cache, "get_json", lambda *args, **kwargs: None)

    def failed_query(*args, **kwargs):
        raise DALServiceError("502 Server Error: Proxy Error for https://irsa.ipac.caltech.edu/TAP/sync")

    monkeypatch.setattr(Irsa, "query_tap", failed_query)
    response = TestClient(app).get(f"/api/{endpoint}", params={"ra": 1, "dec": 2})
    assert response.status_code == 502
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert "IRSA image discovery failed" in response.json()["detail"]
    assert any("TAP HTTP query failed" in r.getMessage() and "Proxy Error" in r.getMessage()
               for r in app_logs.records)
    assert any("request finished status=502" in r.getMessage() for r in app_logs.records)


def test_download_progress_is_visible_before_a_slow_first_file_finishes(monkeypatch, app_logs):
    release_first = threading.Event()
    worker_request_ids = []
    metas = [{"base_url": "slow"}, {"base_url": "fast"}]

    def fake_cutout(url, *args):
        worker_request_ids.append(diagnostics.request_id())
        if url == "slow":
            assert release_first.wait(2), "The fast download was not reported while the first was blocked"
        return url.encode()

    class ProgressHandler(logging.Handler):
        def emit(self, record):
            if "cutout progress completed=1/2" in record.getMessage():
                release_first.set()

    monkeypatch.setattr(irsa_client, "fetch_cutout", fake_cutout)
    logger = logging.getLogger("app")
    handler = ProgressHandler()
    logger.addHandler(handler)
    token = diagnostics.current_request.set(diagnostics.RequestState("download-test"))
    try:
        results = irsa_client.fetch_all(metas, 1, 2, 0.1)
    finally:
        release_first.set()
        diagnostics.current_request.reset(token)
        logger.removeHandler(handler)

    assert results == [(metas[0], b"slow"), (metas[1], b"fast")]
    assert worker_request_ids == ["download-test", "download-test"]
    assert any("completed=1/2" in r.getMessage() and "pending=1" in r.getMessage()
               for r in app_logs.records)


@pytest.mark.parametrize("recovers", [True, False])
def test_cutout_logs_timeouts_retries_and_final_outcome(monkeypatch, app_logs, recovers):
    import requests

    outcomes = iter([
        requests.Timeout("upstream read timed out"),
        SimpleNamespace(status_code=503, content=b""),
        SimpleNamespace(status_code=200 if recovers else 503, content=b"FITS" if recovers else b""),
    ])
    delays = []

    def fake_get(url, timeout):
        assert timeout == config.HTTP_TIMEOUT
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(irsa_client.requests, "get", fake_get)
    monkeypatch.setattr(irsa_client.time, "sleep", delays.append)
    monkeypatch.setattr(config, "RETRIES", 3)
    result = irsa_client._download("https://example.invalid/cutout")
    assert result == (b"FITS" if recovers else None)
    assert delays == [config.RETRY_BACKOFF, config.RETRY_BACKOFF * 2]
    messages = [r.getMessage() for r in app_logs.records]
    assert any("attempt=1/3" in m and "Timeout" in m and "elapsed=" in m for m in messages)
    assert any("attempt=2/3" in m and "status=503" in m for m in messages)
    assert sum("cutout retry in" in m for m in messages) == 2
    assert any("cutout abandoned" in m for m in messages) is not recovers
