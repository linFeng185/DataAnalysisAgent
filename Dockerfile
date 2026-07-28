# syntax=docker/dockerfile:1.7

FROM python:3.14-slim AS builder

ARG INSTALL_EXTRAS="connectors,documents,structured"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip wheel --wheel-dir=/wheels ".[${INSTALL_EXTRAS}]"


FROM python:3.14-slim AS runtime

ARG INSTALL_EXTRAS="connectors,documents,structured"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels "data-analysis-agent[${INSTALL_EXTRAS}]" \
    && rm -rf /wheels

COPY --chown=app:app src ./src
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app config ./config
COPY --chown=app:app skills ./skills
COPY --chown=app:app docs ./docs

RUN mkdir -p /app/data/cache/datasource /app/data/chroma /app/data/skills /app/logs \
    && chown -R app:app /app/data /app/logs /home/app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"]

STOPSIGNAL SIGTERM

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
