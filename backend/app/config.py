"""Central configuration. Everything tunable lives here (or in env vars)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(os.environ.get("SPHEREX_CACHE_DIR", BASE_DIR / ".cache"))

IRSA_BASE = "https://irsa.ipac.caltech.edu"
SPHEREX_S3_BASE = "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com"

# --- Network behaviour -------------------------------------------------------
# IRSA's server digests a limited number of parallel calls; 10 worked well in
# the IRSA tutorial.
MAX_PARALLEL_DOWNLOADS = int(os.environ.get("SPHEREX_MAX_PARALLEL", "10"))
HTTP_TIMEOUT = 120          # seconds; SPHEREx reads can be slow
RETRIES = 3
RETRY_BACKOFF = 2.0         # seconds * attempt number
LOG_LEVEL = os.environ.get("SPHEREX_LOG_LEVEL", "INFO").upper()
LOG_HEARTBEAT_SECONDS = float(os.environ.get("SPHEREX_LOG_HEARTBEAT", "10"))

# TAP results are cached only this long so newly ingested weekly data shows up.
TAP_TTL_SECONDS = int(os.environ.get("SPHEREX_TAP_TTL", str(6 * 3600)))

# --- Imaging ----------------------------------------------------------------
# SPHEREx pixels are ~6.15 arcsec (derived from the IMAGE header PC matrix).
PIXEL_SCALE_ARCSEC = 6.15

# Detector grids are rotated relative to the sky, and the rotation changes
# between exposures. A detector-aligned square cutout of side S does not cover
# a north-up square of side S, so we request extra margin (sqrt(2) ~ 1.41 is
# the worst case) and reproject onto a common north-up grid.
CUTOUT_PAD = 1.45

# Epoch used to propagate proper motion for targets that have it in targets.json.
DEFAULT_EPOCH = 2026.0

# --- FLAGS bitmap -----------------------------------------------------------
# Bit numbers from the FLAGS header of the SPHEREx Level 2 spectral images.
FLAG_BIT_NAMES = {
    0: "TRANSIENT", 1: "OVERFLOW", 2: "SUR_ERROR", 6: "NONFUNC", 7: "DICHROIC",
    9: "MISSING_DATA", 10: "HOT", 11: "COLD", 12: "FULLSAMPLE", 14: "PHANMISS",
    15: "NONLINEAR", 17: "PERSIST", 19: "OUTLIER", 20: "CROSSTALK",
    21: "SOURCE", 22: "GHOST", 23: "GHOST_FPA", 24: "GHOST_EXT", 25: "STREAK",
    26: "BLOOM", 27: "SNOWBALL", 28: "HALO",
}
# Pixels carrying any of these bits are masked (set to NaN). We deliberately do
# NOT mask SOURCE (21): it only means "pixel is mapped to a known source".
# Persistence, blooms, halos and streaks are masked because they look like
# faint moving objects in a flipbook.
BAD_FLAG_BITS = [0, 1, 2, 6, 7, 9, 10, 11, 15, 17, 19, 25, 26, 27, 28]
