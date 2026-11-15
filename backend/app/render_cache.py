"""Persistent rendered responses, including every embedded PNG.

Discovery runs before lookup so new exposures invalidate results. Bump VERSION
when changing dependencies in ways that affect rendering; local source edits are
also fingerprinted automatically.
"""
from __future__ import annotations

from functools import wraps
import hashlib
import inspect
import json
import logging
from pathlib import Path
import threading
import time

from . import cache, config

VERSION = 1
PARTIAL_TTL = 60
log = logging.getLogger(__name__)
# Bounded lock storage; identical misses render once in the single API worker.
_LOCKS = [threading.Lock() for _ in range(64)]
_SOURCE = hashlib.sha256(b''.join(
    Path(__file__).with_name(name).read_bytes()
    for name in ('pipeline.py', 'processing.py', 'config.py', 'render_cache.py')
)).hexdigest()


def cached_render(func):
    signature = inspect.signature(func)

    @wraps(func)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        payload = json.dumps({
            'version': VERSION, 'source': _SOURCE, 'function': func.__name__,
            'arguments': bound.arguments, 'cutout_pad': config.CUTOUT_PAD,
            'bad_flags': config.BAD_FLAG_BITS,
        }, sort_keys=True, separators=(',', ':'), allow_nan=False)
        key = hashlib.sha256(payload.encode()).hexdigest()
        directory = config.CACHE_DIR / 'render'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'{key}.json'
        with _LOCKS[int(key[:8], 16) % len(_LOCKS)]:
            try:
                entry = json.loads(path.read_text())
                result = entry['result']
                expires = entry['expires']
                if isinstance(result, dict) and (expires is None or time.time() < expires):
                    log.info('render cache hit function=%s key=%s', func.__name__, key)
                    return result
            except (OSError, ValueError, KeyError, TypeError):
                pass
            log.info('render cache miss function=%s key=%s', func.__name__, key)
            result = func(*args, **kwargs)
            # Retain partial images too, but retry transient download/parse failures.
            partial = result['counts']['failed'] > 0
            entry = {'expires': time.time() + PARTIAL_TTL if partial else None,
                     'result': result}
            cache._atomic_write(path, json.dumps(entry).encode())
            return result

    return wrapped
