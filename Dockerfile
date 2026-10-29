FROM ghcr.io/astral-sh/uv:python3.14-alpine

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Fix runtime cache directory for non-root users
ENV UV_CACHE_DIR=/tmp/uv_cache

RUN apk add --no-cache gcc musl-dev postgresql-dev python3-dev libpq

# Pre-create virtualenv and runtime cache folder with appropriate permissions
RUN mkdir -p /app/.venv /tmp/uv_cache && chmod 777 /tmp/uv_cache

# Install dependencies into /app/.venv
RUN --mount=type=cache,target=/tmp/uv_cache \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy application source code
COPY pyproject.toml uv.lock run_robot.py ./
COPY app/ app/
COPY .config/ .config/

# Sync project code
RUN --mount=type=cache,target=/tmp/uv_cache \
    uv sync --locked --no-dev

# Ensure non-root user (1000:1000) owns the application and virtual environment
RUN chown -R 1000:1000 /app

# Place executables in PATH
ENV PATH="/app/.venv/bin:$PATH"

# Reset the entrypoint, don't invoke `uv`
ENTRYPOINT []
