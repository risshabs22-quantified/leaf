# One container serves the API and the frontend. No GPU needed: models run on Token Factory.
FROM python:3.12-slim
LABEL org.opencontainers.image.source=https://github.com/risshabs22-quantified/leaf \
      org.opencontainers.image.licenses=MIT

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# Platform data through the Worker bridge (cloudflare/src/index.ts). Baked into the image, not
# only passed by the Worker: a deploy restarts the running container with the environment the
# previous Worker version gave it, so a new variable would be missing until the next cold
# start (seen on the M1 deploy). `make docker-run` clears it to use local SQLite.
ENV LEAF_PLATFORM_URL=http://leaf-platform.internal
WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend ./backend
RUN pip install .

COPY frontend ./frontend

RUN useradd --create-home --uid 10001 leaf && chown -R leaf /app
USER leaf

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
