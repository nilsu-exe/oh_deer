import os
import sys
import tempfile
from pathlib import Path

# Make `import app` work and keep test cache files out of the project folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SPHEREX_CACHE_DIR", tempfile.mkdtemp(prefix="spherex-test-cache-"))
