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


def test_spectrum_disk_reuse_options_sources_and_discovery(monkeypatch):
    from app import spectral_id

    class Images(list):
        discovery = {'age_seconds': 1}

    metas = Images([{'base_url': 'https://example.invalid/spec.fits',
                     'mjd': 60800., 'det': 2}])
    monkeypatch.setattr(irsa_client, 'query_selected_images', lambda *a: metas)
    monkeypatch.setattr(processing, '_wavelength_maps', fake_wavelength_maps)
    content = make_mef(133.795, -7.245, 133.795, -7.245)
    calls = []
    analyses = []

    def fetch(images, *args, aperture_only=False):
        assert aperture_only
        calls.append(1)
        return [(m, content) for m in images]

    def analyze(points, known_z=None):
        analyses.append(known_z)
        return {'z': known_z}

    monkeypatch.setattr(irsa_client, 'fetch_all', fetch)
    monkeypatch.setattr(spectral_id, 'analyze_spectrum', analyze)
    client = TestClient(app)
    query = {'ra': 133.795, 'dec': -7.245, 'dets': '2'}
    first = client.get('/api/spectrum', params=query)
    assert first.status_code == 200
    assert first.json()['points']
    assert list((config.CACHE_DIR / 'render').glob('*.json'))
    metas.discovery = {'age_seconds': 2}
    second = client.get('/api/spectrum', params=query).json()
    assert second == {**first.json(), 'discovery': metas.discovery}
    assert len(calls) == len(analyses) == 1
    for changed in ({'aperture_px': 2}, {'size': .05}, {'max_frames': 1}, {'z': .2},
                    {'ra': 133.7951}, {'dets': '3'}):
        response = client.get('/api/spectrum', params={**query, **changed})
        assert response.status_code == 200
    assert len(calls) == 7
    metas[0]['base_url'] = 'https://example.invalid/spec-v2.fits'
    assert client.get('/api/spectrum', params=query).status_code == 200
    assert len(calls) == 8


def test_spectrum_analysis_errors_expire():
    calls = []

    @render_cache.cached_render
    def spectrum():
        calls.append(1)
        return {'counts': {'failed': 0}, 'analysis': {'error': 'temporary failure'}}

    spectrum()
    spectrum()
    assert len(calls) == 1
    path = next((config.CACHE_DIR / 'render').glob('*.json'))
    entry = json.loads(path.read_text())
    assert entry['expires'] is not None
    entry['expires'] = 0
    path.write_text(json.dumps(entry))
    spectrum()
    assert len(calls) == 2
