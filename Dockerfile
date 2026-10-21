# syntax=docker/dockerfile:1
# Thermaestro's core, for compose.yaml. docs/docker.md says how to run it.

FROM python:3.13-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-trixie@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f
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
