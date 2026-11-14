"""RUN THIS FIRST on a machine that can reach IRSA.

    cd backend
    python scripts/smoke_test.py

It checks, step by step, the assumptions the backend makes about IRSA:
  1. the TAP query returns SPHEREx images for WISE 0855,
  2. a cutout downloads and has the expected extensions,
  3. the WCS-WAVE lookup gives a sensible wavelength for the detector,
  4. the full frames pipeline produces PNG frames.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import irsa_client, pipeline, processing, targets  # noqa: E402

DET = 2
EXPECTED_RANGE = {1: (0.75, 1.09), 2: (1.10, 1.62), 3: (1.63, 2.41),
                  4: (2.42, 3.82), 5: (3.83, 4.41), 6: (4.42, 5.00)}


def main():
    t = targets.resolve("wise0855")
    ra, dec = t["ra"], t["dec"]
    print(f"[1] TAP query at RA={ra:.5f} Dec={dec:.5f} detector {DET} (can take ~20 s)...")
    t0 = time.time()
    images = irsa_client.query_images(ra, dec, DET)
    print(f"    {len(images)} images in {time.time() - t0:.1f}s")
    assert images, "No images found - check the query / IRSA availability"
    print(f"    first: {images[0]['base_url']}")
    print(f"    MJD range: {images[0]['mjd']:.2f} .. {images[-1]['mjd']:.2f}")

    print("[2] Downloading one cutout...")
    data = irsa_client.fetch_cutout(images[0]["base_url"], ra, dec, 0.15)
    assert data, "Cutout download failed"
    print(f"    {len(data) / 1024:.0f} kB")

    print("[3] Parsing cutout + wavelength lookup...")
    c = processing.parse_cutout(data, images[0])
    x, y = processing.pixel_of(c, ra, dec)
    wl = processing.sample(c.wave, x, y)
    lo, hi = EXPECTED_RANGE[DET]
    print(f"    image shape {c.image.shape}, wavelength at target {wl:.3f} um "
          f"(detector {DET} should be {lo}-{hi} um), version {c.version}")
    assert lo - 0.05 <= wl <= hi + 0.05, "Wavelength looks wrong - check _wavelength_maps()"

    print("[4] Full frames pipeline (fov 0.15 deg, 30 frames)...")
    t0 = time.time()
    res = pipeline.build_frames(ra, dec, 0.15, DET, max_frames=30)
    print(f"    {res['counts']} in {time.time() - t0:.1f}s")
    assert res["frames"], "No frames returned"
    print("OK - backend assumptions hold.")


if __name__ == "__main__":
    main()
