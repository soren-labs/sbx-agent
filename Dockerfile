# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.11.21 AS uv

FROM python:3.12-slim-bookworm

ARG SBX_VERSION=dev
ARG SBX_GIT_SHA=unknown

LABEL org.opencontainers.image.title="SBX Agent" \
      org.opencontainers.image.description="SBX Agent control plane and worker runtime" \
      org.opencontainers.image.source="https://github.com/soren-labs/sbx-agent" \
      org.opencontainers.image.version="${SBX_VERSION}" \
      org.opencontainers.image.revision="${SBX_GIT_SHA}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    SBX_DATA_DIR=/var/lib/sbx \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY control ./control
COPY protocol ./protocol
COPY runtime ./runtime
COPY src ./src
COPY resources ./resources
COPY docs/specs/unified/harnesses/manifests.json ./docs/specs/unified/harnesses/manifests.json

RUN uv sync --frozen --no-dev \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin sbx \
    && mkdir -p /var/lib/sbx \
    && chown -R sbx:sbx /var/lib/sbx /app

USER sbx

EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD curl --fail --silent http://127.0.0.1:8800/readyz >/dev/null || exit 1

CMD ["python", "-m", "control.composition", "serve", "--host", "0.0.0.0", "--port", "8800"]
