"""In-process sliding-window rate limits (one backend instance; move to Redis before scaling out)."""

import time

from fastapi import HTTPException, Request

_hits: dict[str, list[float]] = {}


def _recent(key: str, window: float, now: float) -> list[float]:
    if len(_hits) > 50_000:
        for stale in [k for k, stamps in _hits.items() if not stamps or stamps[-1] < now - 3600]:
            del _hits[stale]
    stamps = [stamp for stamp in _hits.get(key, []) if stamp > now - window]
    _hits[key] = stamps
    return stamps


def is_limited(key: str, limit: int, window: float) -> bool:
    """True if `key` already has `limit` hits within `window` seconds (does not record a hit)."""
    return len(_recent(key, window, time.monotonic())) >= limit


def record(key: str) -> None:
    _hits.setdefault(key, []).append(time.monotonic())


def reset(key: str) -> None:
    _hits.pop(key, None)


def hit(key: str, limit: int, window: float, detail: str = "Слишком много запросов, попробуйте позже") -> None:
    """Record a hit for `key`, or raise 429 if the limit is already reached."""
    if is_limited(key, limit, window):
        raise HTTPException(status_code=429, detail=detail, headers={"Retry-After": str(int(window))})
    record(key)


def client_ip(request: Request) -> str:
    # uvicorn --proxy-headers sets request.client from X-Forwarded-For, which the bundled nginx
    # overwrites with the real client address (see frontend/nginx.conf.template).
    return request.client.host if request.client else "unknown"
