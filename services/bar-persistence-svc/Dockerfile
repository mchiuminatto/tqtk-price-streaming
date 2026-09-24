# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# bar-persistence-svc
# Sole writer of the bars table; idempotently upserts closed bars from bars.*.* to TimescaleDB.
#
# Build from the REPOSITORY ROOT, not from this directory:
#
#     docker build -f services/bar-persistence-svc/Dockerfile -t tqtk/bar-persistence-svc:dev .
#
# The context is the repo root because this is a uv workspace: the service
# resolves `tqtk-common` through the root pyproject.toml against the shared
# uv.lock. Only this service's source and `libs/tqtk-common` are copied in, so
# an unrelated service's change does not invalidate this image's layers.

# Base images are pinned by tag AND multi-arch index digest, so a re-pushed tag
# cannot silently change what gets built. Bump both together, e.g.:
#     docker buildx imagetools inspect python:3.12-slim-bookworm
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500

# uv ships as its own image. It has to be pinned as a named stage rather than
# an inline `COPY --from=${UV_IMAGE}`: a build arg is expanded in a FROM, but
# NOT in a COPY --from image reference.
FROM ${UV_IMAGE} AS uv

# --------------------------------------------------------------------------
# builder — resolve and install into a self-contained /app/.venv
# --------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

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
COPY services/bar-persistence-svc/pyproject.toml services/bar-persistence-svc/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package bar-persistence-svc

# Workspace source, copied after dependencies for the same caching reason.
COPY libs/tqtk-common/src libs/tqtk-common/src
COPY services/bar-persistence-svc/src services/bar-persistence-svc/src

# --no-editable builds real wheels into the venv rather than .pth files
# pointing back at /app/libs and /app/services, so the runtime stage below
# needs the venv alone and no source tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package bar-persistence-svc

# --------------------------------------------------------------------------
# runtime — no uv, no build tooling, no source tree, non-root
# --------------------------------------------------------------------------
# Identical base to the builder: the venv's interpreter is a symlink into the
# image's own Python, so the two stages must agree on it.
FROM ${PYTHON_IMAGE} AS runtime

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

# Forward reference: the `bar_persistence_svc.__main__` entrypoint lands with the
# service-runtime contract (tasks 3.1-3.4) and this service's own tasks. The
# image builds without it; it will not start until that module exists.
CMD ["python", "-m", "bar_persistence_svc"]
