# =============================================================================
# AI Bot Live Quiz Arena — Backend Dockerfile (FastAPI on Port 8080)
# =============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

# Install runtime dependencies (curl for container healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies + official Supabase client for Linux containers
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt supabase

# Copy application source
COPY backend/ ./backend/
COPY data/ ./data/
COPY frontend/ ./frontend/
COPY server.py scraper.py supabase_schema.sql test_api.py ./

# Run as non-root user for production container security
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
