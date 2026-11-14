"""SQLite mirror of the SPHEREx discovery columns used by this application.

Snapshots are authoritative only for their declared scope: a saved point query
or an entire detector. Full-sky snapshots use a 3-D R-tree and spherical polygon
tests, avoiding RA wrap and polar problems. Replacement is transactional.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time

import numpy as np

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY, scope_key TEXT UNIQUE NOT NULL,
    scope TEXT NOT NULL, detector INTEGER NOT NULL, updated REAL NOT NULL,
    source TEXT NOT NULL, row_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    base_url TEXT NOT NULL, mjd REAL NOT NULL, det INTEGER NOT NULL,
    bandpass TEXT NOT NULL, normals TEXT,
    UNIQUE(snapshot_id, base_url)
);
CREATE INDEX IF NOT EXISTS image_snapshot ON images(snapshot_id, det, mjd);
CREATE VIRTUAL TABLE IF NOT EXISTS footprints USING rtree(
    id, min_x, max_x, min_y, max_y, min_z, max_z
);
"""


def point_key(ra, dec, det):
    # Match the precision of the existing TAP cache; no larger sky area is implied.
    return f"point_{round(ra % 360, 4) % 360}_{round(dec, 4)}_{det or 'all'}"


def _open(write=False):
    path = Path(config.DISCOVERY_MIRROR_PATH)
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(_SCHEMA)
    else:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def _unit(ra, dec):
    ra, dec = np.deg2rad([ra, dec])
    return np.array([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)])


def _polygon(footprint):
    """Convex STC-S POLYGON -> inward normals and conservative Cartesian bounds."""
    tokens = str(footprint).split()
    if not tokens or tokens.pop(0).upper() != "POLYGON":
        raise ValueError("Expected an STC-S POLYGON footprint")
    if tokens and tokens[0].upper() == "ICRS":
        tokens.pop(0)
    coords = np.asarray([float(v) for v in tokens], dtype=float)
    if len(coords) < 6 or len(coords) % 2 or not np.isfinite(coords).all():
        raise ValueError("Invalid polygon coordinates")
    coords = coords.reshape(-1, 2)
    if np.any(np.abs(coords[:, 1]) > 90):
        raise ValueError("Polygon declination is outside [-90, 90]")
    vertices = np.array([_unit(*v) for v in coords])
    if np.allclose(vertices[0], vertices[-1], atol=1e-14, rtol=0):
        vertices = vertices[:-1]
    center = vertices.sum(axis=0)
    length = np.linalg.norm(center)
    if length < 1e-10:
        raise ValueError("Polygon is not contained in a hemisphere")
    center /= length
    dots = vertices @ center
    if len(vertices) < 3 or dots.min() <= 0:
        raise ValueError("Expected a small, convex image footprint")
    normals = np.cross(vertices, np.roll(vertices, -1, axis=0))
    lengths = np.linalg.norm(normals, axis=1)
    if np.any(lengths < 1e-12):
        raise ValueError("Degenerate polygon edge")
    normals /= lengths[:, None]
    normals *= np.where(normals @ center >= 0, 1, -1)[:, None]
    if np.any(vertices @ normals.T < -1e-10):
        raise ValueError("Non-convex footprint")
    # The convex polygon is inside this <90-degree spherical cap. A cube with
    # half-width equal to its chord radius also contains every great-circle edge.
    radius = math.sqrt(max(0, 2 - 2 * float(dots.min()))) + 1e-10
    bounds = np.column_stack((center - radius, center + radius)).ravel().tolist()
    return json.dumps(normals.tolist()), bounds


def replace_snapshot(rows, *, det=None, ra=None, dec=None, updated=None, source="tap"):
    """Atomically import a complete detector or point result. Failed imports roll back.

    All-sky imports require a footprint for every row. Callers must reject TAP
    errors/overflow before importing; a truncated query is not a complete snapshot.
    """
    if det is not None and det not in range(1, 7):
        raise ValueError("Detector must be 1-6")
    if (ra is None) != (dec is None):
        raise ValueError("A point snapshot needs both RA and declination")
    scope = "all_sky" if ra is None else "point"
    key = f"all_sky_{det or 'all'}" if ra is None else point_key(ra, dec, det)
    updated = time.time() if updated is None else float(updated)
    if not math.isfinite(updated):
        raise ValueError("Invalid snapshot timestamp")
    with closing(_open(write=True)) as db, db:
        db.execute("INSERT OR IGNORE INTO snapshots VALUES(NULL, ?, ?, ?, ?, ?, 0)",
                   (key, scope, det or 0, updated, source))
        snapshot_id = db.execute("SELECT id FROM snapshots WHERE scope_key=?", (key,)).fetchone()[0]
        db.execute("DELETE FROM footprints WHERE id IN (SELECT id FROM images WHERE snapshot_id=?)",
                   (snapshot_id,))
        db.execute("DELETE FROM images WHERE snapshot_id=?", (snapshot_id,))
        count = 0
        for row in rows:
            url = str(row.get("base_url") or row["image"]["irsa"])
            mjd, detector = float(row["mjd"]), int(row["det"])
            if not url.startswith(config.IRSA_BASE + "/ibe/data/spherex/") or not math.isfinite(mjd):
                raise ValueError("Invalid image URL or MJD")
            if detector not in range(1, 7) or (det and detector != det):
                raise ValueError("Image detector does not match the snapshot")
            if str(row["bandpass"]).strip() != f"SPHEREx-D{detector}":
                raise ValueError("Image bandpass does not match its detector")
            normals, bounds = _polygon(row["footprint"]) if scope == "all_sky" else (None, None)
            cursor = db.execute(
                "INSERT INTO images VALUES(NULL, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, url, mjd, detector, str(row["bandpass"]), normals))
            if bounds is not None:
                db.execute("INSERT INTO footprints VALUES(?, ?, ?, ?, ?, ?, ?)",
                           (cursor.lastrowid, *bounds))
            count += 1
        db.execute("UPDATE snapshots SET updated=?, source=?, row_count=? WHERE id=?",
                   (updated, source, count, snapshot_id))
    return count


class MirrorImages(list):
    def __init__(self, images, snapshots):
        super().__init__(images)
        dates = [s["updated"] for s in snapshots]
        oldest = min(dates)
        self.discovery = {
            "source": "local_mirror",
            "scope": snapshots[0]["scope"] if len({s["scope"] for s in snapshots}) == 1 else "mixed",
            "oldest_snapshot_utc": datetime.fromtimestamp(oldest, timezone.utc).isoformat(),
            "age_seconds": max(0, round(time.time() - oldest, 1)),
            "stale": time.time() - oldest > config.TAP_TTL_SECONDS,
        }


def query_images(ra, dec, det=None):
    """Return local images, or None if the requested scope has not been mirrored.

    Combining six detector snapshots is allowed; combining fewer is not. An
    authoritative empty result is distinct from an unavailable/incomplete mirror.
    """
    if not Path(config.DISCOVERY_MIRROR_PATH).exists():
        return None
    point = _unit(ra, dec)
    out, snapshots = {}, []
    with closing(_open()) as db:
        # Keep all six detector reads on one consistent SQLite snapshot.
        db.execute("BEGIN")
        for detector in ([det] if det else range(1, 7)):
            keys = (point_key(ra, dec, detector), point_key(ra, dec, None),
                    f"all_sky_{detector}", "all_sky_all")
            snap = db.execute(
                "SELECT * FROM snapshots WHERE scope_key IN (?, ?, ?, ?) ORDER BY updated DESC LIMIT 1",
                keys).fetchone()
            if snap is None:
                return None
            snapshots.append(snap)
            if snap["scope"] == "point":
                rows = db.execute("SELECT * FROM images WHERE snapshot_id=? AND det=?",
                                  (snap["id"], detector))
            else:
                x, y, z = point.tolist()
                rows = db.execute("""
                    SELECT i.* FROM footprints f JOIN images i ON i.id=f.id
                    WHERE f.min_x<=? AND f.max_x>=? AND f.min_y<=? AND f.max_y>=?
                      AND f.min_z<=? AND f.max_z>=? AND i.snapshot_id=? AND i.det=?
                """, (x, x, y, y, z, z, snap["id"], detector))
            for row in rows:
                if row["normals"] and np.any(np.asarray(json.loads(row["normals"])) @ point < -1e-12):
                    continue
                out[row["base_url"]] = {k: row[k] for k in ("base_url", "mjd", "det", "bandpass")}
    return MirrorImages(sorted(out.values(), key=lambda r: (r["mjd"], r["base_url"])), snapshots)


def seed_cache(directory):
    """Import point caches with their original dates; never broaden their sky scope."""
    imported, count = 0, 0
    for path in sorted(Path(directory).glob("tap_*.json")):
        match = re.fullmatch(r"tap_(-?[\d.]+)_(-?[\d.]+)_(all|[1-6])\.json", path.name)
        if match is None:
            continue
        ra, dec, detector = match.groups()
        with path.open() as f:
            rows = json.load(f)
        count += replace_snapshot(rows, ra=float(ra), dec=float(dec),
                                  det=None if detector == "all" else int(detector),
                                  updated=path.stat().st_mtime, source="saved_tap_query")
        imported += 1
    return {"snapshots_imported": imported, "images_imported": count}


def status():
    if not Path(config.DISCOVERY_MIRROR_PATH).exists():
        return {"available": False, "snapshots": [], "images": 0}
    with closing(_open()) as db:
        rows = [dict(r) for r in db.execute("SELECT scope_key,scope,detector,updated,source,row_count FROM snapshots")]
    return {"available": True, "snapshots": rows, "images": sum(r["row_count"] for r in rows)}
