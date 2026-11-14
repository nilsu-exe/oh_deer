"""Offline verification of range selection, wavelength WCS, and fallbacks."""
import io
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits

from app import config, irsa_client, processing, s3_cutouts
from synth import make_spectral_mef

BASE = ("https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/"
        "2025W17_4B/version/2/exposure.fits")
RA, DEC = 133.795, -7.245


@pytest.mark.parametrize("release", ["dr1", "qr3", "future-release"])
def test_mirror_preserves_release_and_processing_path(release):
    url = BASE.replace("dr1", release)
    assert s3_cutouts.mirror_url(url) == (
        config.SPHEREX_S3_BASE + "/" + url.split("/ibe/data/spherex/")[1])


@pytest.mark.parametrize("url", ["https://other.invalid/image.fits", BASE + ".gz", BASE + "?cutout=1"])
def test_unknown_or_compressed_urls_use_irsa(url):
    assert s3_cutouts.mirror_url(url) is None


@pytest.mark.parametrize("pixel", [(59.2, 62.7), (2.0, 3.0)])
@pytest.mark.parametrize("compressed_flags", [False, True])
def test_partial_cutout_skips_epsf_and_preserves_photometry(monkeypatch, pixel, compressed_flags):
    content = make_spectral_mef(compressed_flags=compressed_flags)
    meta = {"mjd": 60800.0, "det": 2}
    original = processing.parse_cutout(content, meta)
    ra, dec = original.wcs.pixel_to_world_values(*pixel)
    assert np.isfinite(original.wave).all()  # Exercise real WAVE-TAB, not the stub.
    forbidden = []
    with fits.open(io.BytesIO(content)) as h:
        for name in ("EPSF", "ZODI"):
            info = h[name].fileinfo()
            forbidden.append((info["datLoc"], info["datLoc"] + info["datSpan"]))

    class RangeFile(io.BytesIO):
        transferred = 0

        def read(self, size=-1):
            start = self.tell()
            end = len(content) if size < 0 else start + size
            assert not any(start < stop and end > begin for begin, stop in forbidden)
            data = super().read(size)
            self.transferred += len(data)
            return data

    remote = RangeFile(content)
    real_open = fits.open

    def open_remote(url, **kwargs):
        assert url == "https://s3.invalid/exposure.fits"
        assert kwargs["use_fsspec"] and kwargs["lazy_load_hdus"]
        assert kwargs["fsspec_kwargs"]["cache_type"] == "none"
        return real_open(remote, memmap=False, lazy_load_hdus=True)

    with monkeypatch.context() as patch:
        patch.setattr(s3_cutouts.fits, "open", open_remote)
        compact = s3_cutouts.read_cutout("https://s3.invalid/exposure.fits", ra, dec, 0.04)

    assert remote.transferred < len(content) / 10
    with fits.open(io.BytesIO(compact)) as h:
        assert [hdu.name for hdu in h] == ["PRIMARY", "IMAGE", "FLAGS", "VARIANCE", "WCS-WAVE"]
        assert h["IMAGE"].header["CRPIX1A"] < 0 or pixel[0] < 5
    cropped = processing.parse_cutout(compact, meta)
    assert cropped.zodi is None
    x, y = processing.pixel_of(cropped, ra, dec)
    x0, y0 = int(round(pixel[0] - x)), int(round(pixel[1] - y))
    ny, nx = cropped.image.shape
    region = np.s_[y0:y0 + ny, x0:x0 + nx]
    for name in ("image", "flags", "variance", "wave", "bandwidth"):
        np.testing.assert_allclose(getattr(cropped, name), getattr(original, name)[region])
    expected = processing.aperture_point(original, ra, dec)
    actual = processing.aperture_point(cropped, ra, dec)
    assert actual is not None and expected is not None
    for key, value in expected.items():
        assert actual[key] == (pytest.approx(value) if isinstance(value, float) else value)
    with pytest.raises(ValueError, match="ZODI is required"):
        processing.clean_image(cropped, sky="zodi")


def test_aperture_download_cached_separately_and_full_cutouts_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    calls = []

    def partial(*args):
        calls.append(args)
        return b"compact FITS"

    monkeypatch.setattr(s3_cutouts, "read_cutout", partial)
    assert irsa_client.fetch_aperture_cutout(BASE, RA, DEC, 0.04) == b"compact FITS"
    assert irsa_client.fetch_aperture_cutout(BASE, RA, DEC, 0.04) == b"compact FITS"
    assert len(calls) == 1
    url = irsa_client.cutout_url(BASE, RA, DEC, 0.04)
    assert irsa_client.cache.get_bytes("cut_" + irsa_client.cache.key_hash(url)) is None
    irsa_client.cache.set_bytes("cut_" + irsa_client.cache.key_hash(
        irsa_client.cutout_url(BASE, RA, DEC, 0.1)), b"full cached FITS")
    assert irsa_client.fetch_aperture_cutout(BASE, RA, DEC, 0.1) == b"full cached FITS"
    assert len(calls) == 1


def test_partial_read_retries_then_falls_back_without_poisoning_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    attempts, delays = [], []

    def failed(*args):
        attempts.append(args)
        raise TimeoutError("S3 read timed out")

    monkeypatch.setattr(s3_cutouts, "read_cutout", failed)
    monkeypatch.setattr(irsa_client.time, "sleep", delays.append)
    monkeypatch.setattr(irsa_client, "_download", lambda url: b"IRSA FITS")
    assert irsa_client.fetch_aperture_cutout(BASE, RA, DEC, 0.04) == b"IRSA FITS"
    assert len(attempts) == config.RETRIES
    assert delays == [config.RETRY_BACKOFF * i for i in range(1, config.RETRIES)]
    assert not list(tmp_path.glob("aperture_s3_*.bin"))


@pytest.mark.parametrize("kind", ["http", "filesystem"])
def test_missing_mirror_object_falls_back_immediately(monkeypatch, tmp_path, kind):
    from aiohttp import ClientResponseError

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    attempts = []

    def missing(*args):
        attempts.append(args)
        if kind == "filesystem":
            raise FileNotFoundError("Not found")
        raise ClientResponseError(SimpleNamespace(real_url=BASE), (), status=404, message="Not found")

    monkeypatch.setattr(s3_cutouts, "read_cutout", missing)
    monkeypatch.setattr(irsa_client.time, "sleep", lambda _: pytest.fail("permanent failure retried"))
    monkeypatch.setattr(irsa_client, "fetch_cutout", lambda *args: None)
    assert irsa_client.fetch_aperture_cutout(BASE, RA, DEC, 0.04) is None
    assert len(attempts) == 1


@pytest.mark.parametrize("aperture_only", [False, True])
def test_full_s3_cache_from_spectral_branch_is_reused_without_downloads(monkeypatch, tmp_path, aperture_only):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    s3_url = s3_cutouts.mirror_url(BASE)
    key = "s3_" + irsa_client.cache.key_hash(s3_url)
    irsa_client.cache.set_bytes(key, b"full S3 FITS")
    monkeypatch.setattr(irsa_client, "_download", lambda *a: pytest.fail("HTTP download used"))
    monkeypatch.setattr(s3_cutouts, "read_cutout", lambda *a: pytest.fail("S3 download used"))
    fetch = irsa_client.fetch_aperture_cutout if aperture_only else irsa_client.fetch_cutout
    assert fetch(BASE, RA, DEC, 0.04) == b"full S3 FITS"
    assert irsa_client.cache.get_bytes(key) == b"full S3 FITS"


@pytest.mark.parametrize("source_mapping", [False, True])
def test_batch_selects_aperture_flow_and_retains_order_and_failures(monkeypatch, source_mapping):
    metas = [{"base_url": "first"}, {"base_url": "second"}]
    if source_mapping:
        metas = [{"image": {"irsa": m["base_url"]}} for m in metas]
    monkeypatch.setattr(irsa_client, "fetch_cutout", lambda *args: pytest.fail("full download used"))
    monkeypatch.setattr(irsa_client, "fetch_aperture_cutout",
                        lambda url, *args: b"partial" if url == "first" else None)
    assert irsa_client.fetch_all(metas, RA, DEC, 0.04, aperture_only=True) == [
        (metas[0], b"partial"), (metas[1], None)]
