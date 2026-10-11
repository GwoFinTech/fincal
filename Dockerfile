FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim
WORKDIR /app
# Build-time commit, baked into the image so the running service can attest the
# revision it actually is (Issue #79). Only `app/` is copied, so the VERSION
# file `scripts/deploy.sh` writes next to the source tree never reaches the
# container: reading it made /api/admin/health report "dev" on every deploy.
# The default keeps a plain `docker build` (and CI) working.
ARG GIT_COMMIT=dev
ENV FINCAL_GIT_COMMIT=$GIT_COMMIT
COPY --from=builder /app/.venv /app/.venv
COPY app/ app/
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
