"""Tiny disk cache for JSON blobs and raw bytes (downloaded cutouts)."""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import config


def key_hash(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


def _path(name: str) -> Path:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return config.CACHE_DIR / name


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _fresh(path: Path, ttl: Optional[float]) -> bool:
    if not path.exists():
        return False
    return ttl is None or (time.time() - path.stat().st_mtime) <= ttl


def get_json(key: str, ttl: Optional[float] = None) -> Optional[Any]:
    p = _path(f"{key}.json")
    if not _fresh(p, ttl):
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def set_json(key: str, obj: Any) -> None:
    _atomic_write(_path(f"{key}.json"), json.dumps(obj).encode())


def get_bytes(key: str) -> Optional[bytes]:
    p = _path(f"{key}.bin")
    return p.read_bytes() if p.exists() else None


def set_bytes(key: str, data: bytes) -> None:
    _atomic_write(_path(f"{key}.bin"), data)
