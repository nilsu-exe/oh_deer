"""Build/refresh the local SPHEREx discovery mirror (metadata only).

    python scripts/sync_discovery.py --seed-cache --status
    python scripts/sync_discovery.py --sync --det 2
    python scripts/sync_discovery.py --sync --timeout 600

Full-sky exports are committed one detector at a time. An HTTP failure, overflow,
invalid footprint, or interruption leaves that detector's previous snapshot intact.
Re-running skips recently completed detectors; --force refreshes them again.
This mirrors the discovery query used by this app, not an arbitrary ADQL server.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, discovery_mirror, tap_metadata  # noqa: E402


def sync_detector(det, *, maxrec=1000000, max_bytes=512 * 1024 * 1024):
    started = time.time()
    query = f"""
        SELECT a.uri AS uri, p.time_bounds_lower AS mjd,
               p.energy_bandpassname AS bandpass, p.poly AS footprint
        FROM spherex.artifact a
        JOIN spherex.plane p ON a.planeid = p.planeid
        WHERE p.energy_bandpassname = 'SPHEREx-D{int(det)}'
    """
    table = tap_metadata.query_table(query, maxrec=maxrec, max_bytes=max_bytes)
    if not {"uri", "mjd", "bandpass", "footprint"}.issubset(table.colnames):
        raise ValueError("TAP export is missing required discovery columns")

    def rows():
        for row in table:
            uri = str(row["uri"]).lstrip("/")
            yield {"base_url": config.IRSA_BASE + "/" + uri,
                   "mjd": row["mjd"], "det": det, "bandpass": row["bandpass"],
                   "footprint": row["footprint"]}

    return discovery_mirror.replace_snapshot(rows(), det=det, updated=started, source="tap_full_sky")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed-cache", action="store_true", help="Import existing point query JSON files")
    parser.add_argument("--cache-dir", type=Path, default=config.CACHE_DIR)
    parser.add_argument("--db", type=Path, default=config.DISCOVERY_MIRROR_PATH)
    parser.add_argument("--sync", action="store_true", help="Export full-sky footprint metadata from TAP")
    parser.add_argument("--det", type=int, nargs="+", choices=range(1, 7), default=list(range(1, 7)))
    parser.add_argument("--timeout", type=float, default=300, help="Maximum seconds for each detector export")
    parser.add_argument("--maxrec", type=int, default=1000000, help="Reject overflows; raise this for larger exports")
    parser.add_argument("--max-mb", type=int, default=512, help="Maximum downloaded metadata size per detector")
    parser.add_argument("--attempts", type=int, default=3, help="Attempts per detector for transient HTTP failures")
    parser.add_argument("--retry-delay", type=float, default=5, help="Seconds between failed HTTP attempts")
    parser.add_argument("--force", action="store_true", help="Refresh even recently completed full-sky snapshots")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if not (args.seed_cache or args.sync or args.status):
        parser.error("Choose --seed-cache, --sync, or --status")
    if args.timeout <= 0 or args.maxrec <= 0 or args.max_mb <= 0 or args.attempts <= 0 or args.retry_delay < 0:
        parser.error("Timeout and export limits must be positive")
    config.DISCOVERY_MIRROR_PATH = args.db
    config.TAP_HTTP_TIMEOUT = args.timeout
    if args.seed_cache:
        print(json.dumps(discovery_mirror.seed_cache(args.cache_dir)), flush=True)
    failures = 0
    if args.sync:
        recent = {s["detector"] for s in discovery_mirror.status()["snapshots"]
                  if s["scope"] == "all_sky" and time.time() - s["updated"] < config.TAP_TTL_SECONDS}
        for det in dict.fromkeys(args.det):
            if det in recent and not args.force:
                print(f"D{det}: recent complete snapshot, skipped", flush=True)
                continue
            print(f"D{det}: exporting full-sky metadata (timeout {args.timeout:g}s)...", flush=True)
            started = time.perf_counter()
            try:
                for attempt in range(1, args.attempts + 1):
                    try:
                        count = sync_detector(det, maxrec=args.maxrec, max_bytes=args.max_mb * 1024 * 1024)
                        break
                    except requests.RequestException as exc:
                        status = exc.response.status_code if exc.response is not None else None
                        if attempt == args.attempts or (status is not None and status < 500 and status != 429):
                            raise
                        print(f"D{det}: HTTP attempt {attempt}/{args.attempts} failed; retry in {args.retry_delay:g}s",
                              file=sys.stderr, flush=True)
                        time.sleep(args.retry_delay)
                print(f"D{det}: committed {count} images in {time.perf_counter() - started:.1f}s", flush=True)
            except Exception as exc:
                failures += 1
                print(f"D{det}: failed; previous snapshot retained: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
    if args.status:
        state = discovery_mirror.status()
        for snap in state["snapshots"]:
            snap["snapshot_utc"] = datetime.fromtimestamp(snap["updated"], timezone.utc).isoformat()
        print(json.dumps(state, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
