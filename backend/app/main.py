from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.routes import api
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.database import base as dbbase
from app.modules import models  # noqa: F401  (registers tables)

log = logging.getLogger("stocksense")


def create_app() -> FastAPI:
    s = get_settings()
    s.validate()
    app = FastAPI(title="StockSense API", version="0.1.0",
                  docs_url="/docs" if s.environment != "production" else None)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=False,
                       allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def secure_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                             "Referrer-Policy": "no-referrer"})
        if s.environment == "production":
            resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return resp

    @app.exception_handler(AppError)
    async def app_error(_: Request, e: AppError):
        return JSONResponse(status_code=e.status_code,
                            content={"error": {"code": e.code, "message": e.message, "details": e.details}})

    @app.exception_handler(IntegrityError)
    async def integrity(_: Request, e: IntegrityError):
        log.warning("integrity error: %s", e.orig)
        return JSONResponse(status_code=409, content={"error": {"code": "conflict",
                            "message": "The change conflicts with existing data (duplicate or invalid reference).",
                            "details": {}}})

    @app.exception_handler(Exception)
    async def unhandled(_: Request, e: Exception):
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error",
                            "message": "Something went wrong.", "details": {}}})  # no stack trace to clients

    @app.get("/health", tags=["ops"])
    def health():
        with dbbase.engine.connect() as c:
            c.execute(text("SELECT 1"))
        return {"status": "ok"}

    app.include_router(api)
    if s.environment != "production":  # production uses Alembic migrations
        dbbase.Base.metadata.create_all(dbbase.engine)
    return app


app = create_app()  # fails fast with a clear message if SECRET_KEY is missing
