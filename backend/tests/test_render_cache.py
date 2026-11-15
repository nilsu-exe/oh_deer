"""Offline rendered-cache regression tests."""
from concurrent.futures import ThreadPoolExecutor
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config, irsa_client, processing, render_cache
from app.main import app
from synth import fake_wavelength_maps, make_mef


@pytest.mark.parametrize('endpoint,params', [
    ('frames', {'det': 2}), ('detectors', {'dets': '2'}),
])
def test_image_endpoints_reuse_disk_results(monkeypatch, endpoint, params):
    metas = [{'base_url': 'https://example.invalid/a.fits', 'mjd': 60800., 'det': 2}]
    monkeypatch.setattr(irsa_client, 'query_images', lambda *a: metas)
    monkeypatch.setattr(irsa_client, 'query_selected_images', lambda *a: metas)
    monkeypatch.setattr(processing, '_wavelength_maps', fake_wavelength_maps)
    content = make_mef(133.795, -7.245, 133.795, -7.245)
    calls = []

    def fetch(images, *args):
        calls.append(1)
        return [(m, content) for m in images]

    monkeypatch.setattr(irsa_client, 'fetch_all', fetch)
    client = TestClient(app)
    query = {'ra': 133.795, 'dec': -7.245, **params}
    first = client.get('/api/' + endpoint, params=query)
    assert first.status_code == 200
    assert 'data:image/png;base64,' in first.text
    files = list((config.CACHE_DIR / 'render').glob('*.json'))
    assert len(files) == 1
    assert 'data:image/png;base64,' in files[0].read_text()
    # Any FITS processing on the second call would produce failed frames.
    original_parse = processing.parse_cutout
    monkeypatch.setattr(processing, 'parse_cutout', lambda *a: pytest.fail('re-rendered'))
    assert client.get('/api/' + endpoint, params=query).json() == first.json()
    assert len(calls) == 1
    monkeypatch.setattr(processing, 'parse_cutout', original_parse)
    client.get('/api/' + endpoint, params={**query, 'mask': False})
    assert len(calls) == 2
    metas[0]['base_url'] = 'https://example.invalid/new-version.fits'
    client.get('/api/' + endpoint, params=query)
    assert len(calls) == 3


def test_concurrent_misses_partial_retry_and_corruption(monkeypatch):
    calls = []

    @render_cache.cached_render
    def render(value=1):
        calls.append(value)
        time.sleep(.03)
        return {'counts': {'failed': 1}, 'image': 'png'}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: render(), range(4)))
    assert len(calls) == 1
    assert all(r == results[0] for r in results)
    path = next((config.CACHE_DIR / 'render').glob('*.json'))
    entry = json.loads(path.read_text())
    entry['expires'] = 0
    path.write_text(json.dumps(entry))
    render()
    assert len(calls) == 2
    path.write_text('broken JSON')
    render()
    assert len(calls) == 3
