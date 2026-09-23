# syntax=docker/dockerfile:1.7
#
# calibration-seeder
# One-shot job: seeds the Redis calibration store from tools/calibration_seed/tables.py and exits.
#
# Build from the REPOSITORY ROOT, like every image in this repo:
#
#     docker build -f tools/calibration_seed/Dockerfile -t tqtk/calibration-seeder:dev .
#
# The root .dockerignore excludes tools/ entirely, so this build uses its own ignore file
# (Dockerfile.dockerignore beside this file, which BuildKit reads in place of the root one): it
# admits only the seeder's runtime code - no tests, no sample data, no workspace members.
#
# Not a uv workspace member and not a service: it needs one dependency and serves no runtime
# endpoints, so it installs `redis` directly rather than syncing the workspace.

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim-bookworm

# Keep in step with the `redis` version resolved in uv.lock, which the service reads with.
ARG REDIS_PY_VERSION=8.1.0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir "redis==${REDIS_PY_VERSION}" \
 && groupadd --system --gid 1001 tqtk \
 && useradd --system --uid 1001 --gid tqtk --no-create-home tqtk

WORKDIR /app

COPY tools/__init__.py tools/
COPY tools/calibration_seed tools/calibration_seed

USER tqtk

CMD ["python", "-m", "tools.calibration_seed"]
