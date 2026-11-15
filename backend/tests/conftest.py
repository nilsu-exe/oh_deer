import os
import sys
import tempfile
from pathlib import Path

# Make `import app` work and keep test cache files out of the project folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SPHEREX_CACHE_DIR", tempfile.mkdtemp(prefix="spherex-test-cache-"))


# Each test models an independent archive; identical synthetic URLs can contain
# different FITS data, so persistent render entries must not leak between tests.
import pytest


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, 'CACHE_DIR', tmp_path / 'cache')
