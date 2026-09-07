# syntax=docker/dockerfile:1.7
#
# aggregation-svc
# Per-(provider, symbol, timeframe) actor model building live OHLCV bars across 8 timeframes.
#
# Build from the REPOSITORY ROOT, not from this directory:
#
#     docker build -f services/aggregation-svc/Dockerfile -t tqtk/aggregation-svc:dev .
#
# The context is the repo root because this is a uv workspace: the service
# resolves `tqtk-common` through the root pyproject.toml against the shared
# uv.lock. Only this service's source and `libs/tqtk-common` are copied in, so
# an unrelated service's change does not invalidate this image's layers.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.12.10

# uv ships as its own image. It has to be pinned as a named stage rather than
# an inline `COPY --from=ghcr.io/astral-sh/uv:${UV_VERSION}`: a build arg is
# expanded in a FROM, but NOT in a COPY --from image reference.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --------------------------------------------------------------------------
# builder — resolve and install into a self-contained /app/.venv
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency metadata only. This layer is invalidated by a dependency change,
# not by a source edit, so the install below stays cached across code changes.
# Only two of the workspace's members need to be present on disk; `--frozen`
# resolves the rest from uv.lock without them.
COPY pyproject.toml uv.lock ./
COPY libs/tqtk-common/pyproject.toml libs/tqtk-common/
COPY services/aggregation-svc/pyproject.toml services/aggregation-svc/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package aggregation-svc

# Workspace source, copied after dependencies for the same caching reason.
COPY libs/tqtk-common/src libs/tqtk-common/src
COPY services/aggregation-svc/src services/aggregation-svc/src

# --no-editable builds real wheels into the venv rather than .pth files
# pointing back at /app/libs and /app/services, so the runtime stage below
# needs the venv alone and no source tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package aggregation-svc

# --------------------------------------------------------------------------
# runtime — no uv, no build tooling, no source tree, non-root
# --------------------------------------------------------------------------
# Identical base to the builder: the venv's interpreter is a symlink into the
# image's own Python, so the two stages must agree on it.
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}"

RUN groupadd --system --gid 1001 tqtk \
 && useradd --system --uid 1001 --gid tqtk --no-create-home tqtk

WORKDIR /app

COPY --from=builder --chown=tqtk:tqtk /app/.venv /app/.venv

USER tqtk

# /health, /ready and /metrics, per the service-runtime contract.
EXPOSE 8000

# Forward reference: the `aggregation_svc.__main__` entrypoint lands with the
# service-runtime contract (tasks 3.1-3.4) and this service's own tasks. The
# image builds without it; it will not start until that module exists.
CMD ["python", "-m", "aggregation_svc"]
