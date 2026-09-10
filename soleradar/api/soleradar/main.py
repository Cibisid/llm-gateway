"""App factory and entrypoint.

One process serves the API and, when it has been built, the compiled front end --
so `python -m soleradar` is the whole product, not just half of it.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import REPO_ROOT, SETTINGS
from .db import init_db
from .pipeline.scheduler import SCHEDULER, install_default_jobs
from .routes.api import router as api_router
from .sources import registry

log = logging.getLogger("soleradar")

WEB_DIST = REPO_ROOT / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    SETTINGS.ensure_dirs()
    init_db()
    result = registry.bootstrap()
    log.info("catalog ready: %s", result)
    install_default_jobs(SCHEDULER)
    SCHEDULER.start()
    try:
        yield
    finally:
        SCHEDULER.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="soleradar",
        version="0.1.0",
        summary="Sneaker release radar: what is dropping, what is hot, and why.",
        lifespan=lifespan,
    )
    # Vite's dev server runs on another port; without this the UI cannot call the API.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(api_router)

    if WEB_DIST.is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            # Client-side routing: unknown non-API paths return the shell.
            if full_path.startswith("api/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            candidate = WEB_DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def no_ui():
            return JSONResponse({
                "detail": "UI not built. Run `npm --prefix web install && npm --prefix web run build`, "
                          "or use the Vite dev server on :5173.",
                "api_docs": "/docs",
            })

    return app


app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run("soleradar.main:app", host=SETTINGS.host, port=SETTINGS.port, reload=False)


if __name__ == "__main__":
    main()
