# Signaling server, pure Python. The server carries signaling only, so the
# image needs nothing beyond Pydantic and the websockets transport library.
FROM python:3.12-slim

WORKDIR /app

# Runtime dependencies (matches [project.dependencies] in pyproject.toml).
RUN pip install --no-cache-dir "pydantic>=2.0" "websockets>=13.0"

# Server code and the browser client.
COPY voip ./voip
COPY client ./client

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# Run as a non-root user.
RUN useradd --create-home voip
USER voip

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/')" || exit 1

CMD ["python", "-m", "voip", "--host", "0.0.0.0", "--port", "8080", "--static-dir", "/app/client"]
