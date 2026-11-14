"""Request-correlated timings and progress, including while a worker is blocked."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import threading
import time
import uuid

from . import config


@dataclass
class RequestState:
    request_id: str
    started: float = field(default_factory=time.perf_counter)
    phase: str = "waiting for worker"
    phase_started: float = field(default_factory=time.perf_counter)
    progress: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


current_request: ContextVar[RequestState | None] = ContextVar("request", default=None)


def request_id() -> str:
    state = current_request.get()
    return state.request_id if state else "-"


class RequestFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id()
        return True


def configure_logging():
    # A dedicated handler keeps INFO visible even if uvicorn/another library has
    # already configured the root logger. Do not alter third-party logger levels.
    logger = logging.getLogger("app")
    logger.setLevel(config.LOG_LEVEL)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(RequestFilter())
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(handler)


def set_progress(message: str):
    state = current_request.get()
    if state:
        with state.lock:
            state.progress = message


def set_phase(name: str):
    state = current_request.get()
    if state:
        with state.lock:
            state.phase, state.phase_started, state.progress = name, time.perf_counter(), ""


@contextmanager
def stage(logger, name: str, details: str = ""):
    started = time.perf_counter()
    state = current_request.get()
    previous = None
    if state:
        with state.lock:
            previous = (state.phase, state.phase_started, state.progress)
            if state.phase == "waiting for worker":
                previous = ("request processing", started, "")
            state.phase, state.phase_started, state.progress = name, started, ""
    logger.info("%s started %s", name, details)
    try:
        yield
    except Exception as exc:
        logger.error("%s failed elapsed=%.2fs error=%s: %s",
                     name, time.perf_counter() - started, type(exc).__name__, exc)
        raise
    else:
        logger.info("%s finished elapsed=%.2fs", name, time.perf_counter() - started)
    finally:
        if state and previous:
            with state.lock:
                state.phase, state.phase_started, state.progress = previous


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)

        logger = logging.getLogger(__name__)
        state = RequestState(uuid.uuid4().hex[:8])
        token = current_request.set(state)
        status = 500
        query = scope.get("query_string", b"").decode("utf-8", errors="replace")
        logger.info("request started method=%s path=%s query=%s",
                    scope["method"], scope["path"], query)

        async def heartbeat():
            while True:
                await asyncio.sleep(config.LOG_HEARTBEAT_SECONDS)
                now = time.perf_counter()
                with state.lock:
                    phase, phase_started, progress = state.phase, state.phase_started, state.progress
                logger.info("request running elapsed=%.1fs stage=%s stage_elapsed=%.1fs %s",
                            now - state.started, phase, now - phase_started, progress)

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", []),
                           (b"x-request-id", state.request_id.encode())]}
            await send(message)

        task = asyncio.create_task(heartbeat()) if config.LOG_HEARTBEAT_SECONDS > 0 else None
        try:
            await self.app(scope, receive, send_with_id)
        except asyncio.CancelledError:
            logger.warning("request cancelled elapsed=%.2fs; running worker calls may still finish",
                           time.perf_counter() - state.started)
            raise
        except Exception:
            logger.exception("request failed elapsed=%.2fs", time.perf_counter() - state.started)
            raise
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            logger.info("request finished status=%d elapsed=%.2fs", status,
                        time.perf_counter() - state.started)
            current_request.reset(token)
