"""Warm the disk cache for the five challenge targets so the demo is instant.

    cd backend
    python scripts/prefetch_targets.py            # frames, all 6 detectors
    python scripts/prefetch_targets.py --spectra  # also the spectra (slower)

The cache keys depend on ra/dec/fov, so prefetch with the same fov the frontend
uses by default (--fov, default 0.15).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import pipeline, targets  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fov", type=float, default=0.15)
    ap.add_argument("--spectra", action="store_true")
    args = ap.parse_args()

    for t in targets.load_targets():
        try:
            info = targets.resolve(t["id"])
        except LookupError as exc:
            print(f"SKIP {t['name']}: {exc}")
            continue
        ra, dec = info["ra"], info["dec"]
        print(f"== {t['name']}  RA={ra:.5f} Dec={dec:.5f}")
        for det in range(1, 7):
            res = pipeline.build_frames(ra, dec, args.fov, det)
            print(f"   detector {det}: {res['counts']}")
        if args.spectra:
            res = pipeline.build_spectrum(ra, dec)
            print(f"   spectrum: {res['counts']}")


if __name__ == "__main__":
    main()
