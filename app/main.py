from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import integrations, internal, reminders
from app.api.auth import auth_router, session_router
from app.api.routes import router
from app.core.config import settings
from app.core.database import engine
from app.models import models  # noqa: F401

settings.validate()

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.storage_path.mkdir(parents=True, exist_ok=True)
    yield
    await engine.dispose()


docs = {} if settings.enable_docs else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="Dayla API", version="0.1.0", lifespan=lifespan, **docs)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if settings.cookie_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


def _same_origin_or_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    origin = origin.rstrip("/")
    if origin in settings.cors_origins:
        return True
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    return urlsplit(origin).netloc == host


@app.middleware("http")
async def reject_cross_site_writes(request: Request, call_next):
    if request.method in UNSAFE_METHODS and not _same_origin_or_allowed(request):
        return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    return await call_next(request)


if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )


app.include_router(router)
app.include_router(auth_router)
app.include_router(session_router)
app.include_router(integrations.router)
app.include_router(reminders.router)
app.include_router(internal.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
