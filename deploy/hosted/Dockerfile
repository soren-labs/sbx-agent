FROM node:22-slim AS frontend
WORKDIR /console
COPY console/package.json console/package-lock.json ./
RUN npm ci
COPY console/ ./
ENV VITE_HOSTED=1
RUN npm run build

FROM python:3.12-slim AS api
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
RUN useradd --create-home --uid 10001 sbx
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY broker/ ./broker/
COPY control/ ./control/
COPY runtime/ ./runtime/
COPY src/ ./src/
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app XDG_STATE_HOME=/state
USER sbx
EXPOSE 8000
CMD ["uvicorn", "control.hosted_server:create_hosted_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]

FROM api AS mock
USER root
COPY tests/fakes/hosted_codex.py ./tests/fakes/hosted_codex.py
COPY deploy/hosted/mock_server.py ./deploy/hosted/mock_server.py
COPY --from=frontend /console/dist ./console/dist/
RUN mkdir -p /state && chown sbx:sbx /state
USER sbx
CMD ["python", "deploy/hosted/mock_server.py", "--host", "0.0.0.0", "--port", "8000", "--state", "/state"]
