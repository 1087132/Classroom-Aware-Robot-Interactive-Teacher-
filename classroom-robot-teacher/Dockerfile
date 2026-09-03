# CAT - Classroom-Aware Interactive Robot Teacher
# Runs the full app (server + frontends + analytics + neural voice) in one container.
FROM python:3.12-slim

WORKDIR /app

# libsndfile is needed by soundfile (neural voice), curl for the model download
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# data/ (SQLite DB, uploads, TTS model) lives in a volume for persistence
VOLUME ["/app/data"]
EXPOSE 8000

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
