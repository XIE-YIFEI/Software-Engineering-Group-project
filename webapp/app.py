"""The FastAPI application: JSON API under /api/v1 (OpenAPI docs at /docs), media under /media, pages at /."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .security import AuthError, Forbidden, SYSTEM
from .settings import Settings, load_settings, use

HERE = Path(__file__).resolve().parent


def _retention_loop(settings: Settings, stop: threading.Event) -> None:
    """Daily purge of what the retention settings no longer allow (also on start-up)."""
    from .privacy import purge
    while not stop.is_set():
        try:
            conn = db.connect(settings.db_path)
            purge(conn, settings.media_dir, settings.corrections_dir, SYSTEM, dry_run=False)
            conn.close()
        except Exception as e:                                           # noqa: BLE001 - retry tomorrow
            print(f"retention purge failed: {e}")
        stop.wait(24 * 3600)


def create_app(settings: Optional[Settings] = None, start_worker: bool = True, retention: bool = True) -> FastAPI:
    settings = settings or load_settings()
    use(settings)
    settings.ensure_dirs()
    db.connect(settings.db_path).close()                                  # create the schema

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        worker = None
        if start_worker:
            from .worker import Worker
            worker = Worker(settings)
            worker.start()
        if retention:
            threading.Thread(target=_retention_loop, args=(settings, stop), daemon=True, name="secs-retention").start()
        yield
        stop.set()
        if worker is not None:
            worker.stop()

    app = FastAPI(title="SECS Vision API", version="1.0",
                  description="Malaysian licence-plate recognition and traffic monitoring: plate search, alerts, traffic "
                              "analytics, congestion forecast, guardhouse. Course project demo; the vehicle registry is "
                              "SIMULATED. Every call needs a login session; state-changing calls need the X-CSRF-Token "
                              "header.", lifespan=lifespan)

    def wants_html(request: Request) -> bool:
        return not request.url.path.startswith(("/api/", "/media/")) and "text/html" in request.headers.get("accept", "")

    @app.exception_handler(AuthError)
    async def _auth(request: Request, exc: AuthError):
        if wants_html(request):
            return RedirectResponse("/login?next=" + request.url.path, status_code=303)
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(PermissionError)
    async def _csrf(request: Request, exc: PermissionError):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(LookupError)
    async def _missing(request: Request, exc: LookupError):
        if isinstance(exc, (KeyError, IndexError)):               # a bug, not a missing record
            return JSONResponse({"detail": f"internal error: {type(exc).__name__} {exc}"}, status_code=500)
        return JSONResponse({"detail": str(exc).strip("'\"") or "not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def _invalid(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        msgs = [f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg')}" for e in exc.errors()]
        return JSONResponse({"detail": "; ".join(msgs) or "invalid request"}, status_code=422)

    from .api import admin, core, gate, plates, traffic
    for r in (core.router, plates.router, traffic.router, gate.router, admin.router):
        app.include_router(r, prefix="/api/v1")
    app.include_router(core.media, prefix="/media")
    from .web import pages
    app.include_router(pages.router)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    return app
