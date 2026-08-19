"""HTTP wrapper around the analysis engine.

The service is deliberately stateless: it never clones a repository, never
writes to disk, and holds nothing between requests. A caller sends a diff and
(optionally) the current contents of the changed files; the service sends back
a proposed split. That property is what makes it trivial to containerise and
to scale horizontally — any replica can answer any request.

It also never commits anything. Applying commits requires the caller's working
tree and their git credentials, so that stays entirely client-side. The
service only advises.

Endpoints
    GET  /health   liveness, for Docker and Kubernetes probes
    GET  /ready    readiness, reports whether a model is configured
    POST /analyze  the actual work
"""

from __future__ import annotations

import logging
import os
import sys
import time

from flask import Flask, g, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

from splitter import __version__
from splitter.core import analyze
from splitter.features import mapping_source
from splitter.llm import LLMError, provider_from_env

MAX_DIFF_BYTES = int(os.environ.get("MAX_DIFF_BYTES", 2_000_000))
MAX_FILES = int(os.environ.get("MAX_FILES", 500))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("splitter.service")

app = Flask(__name__)


@app.before_request
def _start_timer():
    g.started = time.monotonic()


@app.after_request
def _log_request(response):
    elapsed = (time.monotonic() - getattr(g, "started", time.monotonic())) * 1000
    log.info("%s %s -> %s in %.0fms",
             request.method, request.path, response.status_code, elapsed)
    return response


@app.get("/health")
def health():
    """Is the process alive? Cheap, no dependencies touched."""
    return jsonify(status="ok", version=__version__)


@app.get("/ready")
def ready():
    """Can we serve real traffic?

    A missing model is not fatal — the engine falls back to heuristic
    messages — so this reports the model's state without failing.
    """
    try:
        provider = provider_from_env()
        model = {"configured": provider is not None,
                 "provider": provider.name if provider else None}
    except LLMError as exc:
        model = {"configured": False, "error": str(exc)}
    return jsonify(status="ok", version=__version__, model=model)


@app.post("/analyze")
def analyze_endpoint():
    """Split a diff into proposed commits.

    Request body:
        diff      required, the output of `git diff`
        files     optional, {path: full current contents}. Improves symbol
                  detection; without it the analysis still works but is coarser.
        co_change optional, [[path_a, path_b, strength], ...] mined from the
                  caller's git log
        messages  optional bool, default true. Set false to skip the model.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="request body must be a JSON object"), 400

    diff_text = payload.get("diff")
    if not isinstance(diff_text, str) or not diff_text.strip():
        return jsonify(error="'diff' is required and must be a non-empty string"), 400
    if len(diff_text.encode()) > MAX_DIFF_BYTES:
        return jsonify(error=f"diff exceeds {MAX_DIFF_BYTES} bytes"), 413

    files = payload.get("files") or {}
    if not isinstance(files, dict):
        return jsonify(error="'files' must be an object of path -> contents"), 400
    if len(files) > MAX_FILES:
        return jsonify(error=f"too many files (limit {MAX_FILES})"), 413

    co_change = {}
    for entry in payload.get("co_change") or []:
        try:
            a, b, strength = entry
            co_change[tuple(sorted((str(a), str(b))))] = float(strength)
        except (TypeError, ValueError):
            return jsonify(error="'co_change' entries must be [path, path, number]"), 400

    provider = None
    warnings = []
    if payload.get("messages", True):
        try:
            provider = provider_from_env()
        except LLMError as exc:
            warnings.append(str(exc))

    try:
        result = analyze(
            diff_text,
            source=mapping_source({str(k): str(v) for k, v in files.items()}),
            co_change=co_change,
            provider=provider,
        )
    except Exception:
        # Never leak a stack trace to a caller; it may contain file contents.
        log.exception("analysis failed")
        return jsonify(error="analysis failed"), 500

    body = result.to_dict()
    body["warnings"] = warnings + body["warnings"]
    return jsonify(body)


@app.errorhandler(404)
def not_found(_):
    return jsonify(error="not found"), 404


if __name__ == "__main__":
    # Development only. Production uses gunicorn; see the Dockerfile.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
