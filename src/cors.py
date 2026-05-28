"""
CORS middleware configuration.

In production, set the CORS_ALLOW_ORIGINS environment variable to a
comma-separated list of allowed origins (e.g. "https://warkopsaja.com").

Leaving CORS_ALLOW_ORIGINS unset (or "*") keeps the wildcard behaviour
that is useful during local development.

⚠️  WARNING: allow_origins=["*"] combined with allow_credentials=True is
rejected by browsers. We therefore only enable credentials when origins
are explicitly restricted.
"""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def setup_cors(app: FastAPI) -> None:
    """
    Attach CORSMiddleware to *app* using settings from the environment.

    Reads:
        CORS_ALLOW_ORIGINS – comma-separated list of allowed origins.
                             Defaults to "*" (all) when not set.
    """
    raw = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()

    if raw == "*":
        allow_origins = ["*"]
        allow_credentials = False   # browsers block credentials + wildcard
    else:
        allow_origins = [o.strip() for o in raw.split(",") if o.strip()]
        allow_credentials = True

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=["GET", "POST"],  # only what we actually use
        allow_headers=["Content-Type", "Authorization"],
    )
