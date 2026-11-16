FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY camviewer ./camviewer
COPY web ./web

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin camviewer \
    && mkdir /data && chown 10001:10001 /data
USER 10001

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    DATA_DIR=/data \
    WEB_DIR=/app/web

EXPOSE 8080
CMD ["python", "-m", "camviewer.main"]
