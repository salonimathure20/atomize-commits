# Multi-stage build.
#
# Stage 1 installs dependencies into their own directory. Stage 2 copies that
# directory into a clean image. The compilers and caches used during install
# never reach the final image, which keeps it small and shrinks the amount of
# software an attacker could find inside a running container.

# ---------- stage 1: build ----------
FROM python:3.12-slim AS builder

WORKDIR /build

# Copy only the requirements first. Docker caches each layer, and this one is
# reused on every rebuild where the dependency list has not changed — so
# editing application code does not trigger a reinstall.
COPY service/requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---------- stage 2: runtime ----------
FROM python:3.12-slim

# A container that runs as root gives an attacker root if they escape the
# process. Create an unprivileged user and stay there.
RUN useradd --create-home --uid 10001 splitter

COPY --from=builder /install /usr/local

WORKDIR /app
COPY --chown=splitter:splitter engine/splitter ./engine/splitter
COPY --chown=splitter:splitter service/app.py ./service/app.py

USER splitter

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/engine \
    PORT=8000 \
    SPLITTER_LLM=none

EXPOSE 8000

# Docker restarts the container when this fails. Kubernetes ignores it and
# uses the probes in the manifests instead.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# gunicorn, not flask's development server: it handles concurrent requests,
# recycles workers, and is built for production traffic.
#   --workers 2        two processes; raise it on a bigger machine
#   --threads 4        each handles several requests while waiting on the model
#   --timeout 120      model calls can be slow
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--chdir", "/app/service", \
     "app:app"]
