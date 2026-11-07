FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim

# ffmpeg and ffprobe for videos, tzdata for TIMEZONE
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# The dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY debeloglav ./debeloglav

RUN useradd --create-home --uid 1000 debeloglav && mkdir -p data && chown debeloglav data
USER debeloglav

CMD ["python", "-m", "debeloglav"]
