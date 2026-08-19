"""HTTP wrapper around the analysis engine.

The service is stateless: it never clones a repository, writes to disk, or
holds anything between requests. Any replica can answer any request, which is
what makes it straightforward to scale. It also never commits — applying
commits needs the caller's working tree and git credentials, so that stays
client-side.

Interactive documentation is generated from the schemas and served at /docs.

Endpoints
    GET  /health   liveness, for container and Kubernetes probes
    GET  /ready    readiness, reports whether a model is configured
    POST /analyze  the actual work
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

from schemas import (  # noqa: E402
    AnalyzeRequest,
    AnalyzeResponse,
    ErrorResponse,
    HealthResponse,
    ModelStatus,
    ReadyResponse,
)
from splitter import __version__  # noqa: E402
from splitter.core import analyze  # noqa: E402
from splitter.features import mapping_source  # noqa: E402
from splitter.llm import LLMError, provider_from_env  # noqa: E402

MAX_DIFF_BYTES = int(os.environ.get("MAX_DIFF_BYTES", 2_000_000))
MAX_FILES = int(os.environ.get("MAX_FILES", 500))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("splitter.service")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("commit-splitter %s starting", __version__)
    yield
    log.info("shutting down")


app = FastAPI(
    title="Commit Splitter",
    version=__version__,
    summary="Splits a tangled diff into separate, well-described commits.",
    description=(
        "Send a diff and get back a proposed set of atomic commits, ordered so "
        "that nothing is used before it exists, each with a Conventional "
        "Commits message.\n\n"
        "The service is stateless and never commits anything itself."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    elapsed = (time.monotonic() - started) * 1000
    log.info("%s %s -> %s in %.0fms",
             request.method, request.url.path, response.status_code, elapsed)
    return response


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception):
    """Never let a stack trace reach a caller; it may contain their source."""
    log.exception("unhandled error", exc_info=exc)
    return JSONResponse(status_code=500, content={"error": "analysis failed"})


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    tags=["operations"],
)
async def health() -> HealthResponse:
    """Cheap check that the process is alive. Touches no dependencies."""
    return HealthResponse(status="ok", version=__version__)


@app.get(
    "/ready",
    response_model=ReadyResponse,
    summary="Readiness probe",
    tags=["operations"],
)
async def ready() -> ReadyResponse:
    """Report whether we can serve real traffic.

    A missing model is not fatal — the engine falls back to heuristic
    messages — so this reports the model's state without failing.
    """
    try:
        provider = provider_from_env()
        model = ModelStatus(
            configured=provider is not None,
            provider=provider.name if provider else None,
        )
    except LLMError as exc:
        model = ModelStatus(configured=False, error=str(exc))
    return ReadyResponse(status="ok", version=__version__, model=model)


@app.post(
    "/analyze",
    response_model=AnalyzeResponse,
    summary="Split a diff into proposed commits",
    tags=["analysis"],
    responses={
        413: {"model": ErrorResponse, "description": "Diff or file set too large"},
        422: {"description": "Request body failed validation"},
        500: {"model": ErrorResponse, "description": "Analysis failed"},
    },
)
async def analyze_endpoint(payload: AnalyzeRequest) -> AnalyzeResponse:
    """Group a working tree's changes into atomic commits.

    Shape and types are validated before this function runs. Size limits are
    checked here so they can return 413 rather than a validation error.
    """
    if len(payload.diff.encode()) > MAX_DIFF_BYTES:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"error": f"diff exceeds {MAX_DIFF_BYTES} bytes"},
        )
    if len(payload.files) > MAX_FILES:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"error": f"too many files (limit {MAX_FILES})"},
        )

    warnings: list[str] = []
    provider = None
    if payload.messages:
        try:
            provider = provider_from_env()
        except LLMError as exc:
            warnings.append(str(exc))

    # The analysis is CPU-bound and the model call blocks on the network.
    # Running it in a worker thread keeps the event loop free to accept other
    # requests instead of stalling the whole process behind one slow call.
    result = await run_in_threadpool(
        analyze,
        payload.diff,
        source=mapping_source(payload.files),
        co_change=payload.co_change_map(),
        provider=provider,
    )

    body = result.to_dict()
    body["warnings"] = warnings + body["warnings"]
    return AnalyzeResponse.model_validate(body)


if __name__ == "__main__":
    # Development only. The container runs uvicorn directly; see the Dockerfile.
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=bool(os.environ.get("RELOAD")),
    )
