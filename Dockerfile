# syntax=docker/dockerfile:1
# Thermaestro's core, for compose.yaml. docs/docker.md says how to run it.

FROM python:3.13-slim-trixie@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-trixie@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c
# A fixed user, so the files in the state volume stay this user's across rebuilds.
# The state directory is the user's alone, as Thermaestro requires.
RUN groupadd --system --gid 10001 thermaestro \
 && useradd --system --uid 10001 --gid thermaestro --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin thermaestro \
 && install -d -o thermaestro -g thermaestro -m 0700 /data \
 && install -d -m 0755 /config
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    CONFIGURATION_DIRECTORY=/config \
    STATE_DIRECTORY=/data \
    PYTHONUNBUFFERED=1
USER thermaestro:thermaestro
EXPOSE 8080 8443
# No curl in the image; Python asks instead. It assumes the default web port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]
CMD ["thermaestro", "run"]
