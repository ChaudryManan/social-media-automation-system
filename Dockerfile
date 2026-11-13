FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend ./backend

RUN pip install --no-cache-dir \
        "celery==5.5.3" \
        "fastapi==0.115.12" \
        "psycopg[binary]==3.2.9" \
        "redis==6.2.0" \
        "uvicorn==0.34.2" \
    && pip install --no-cache-dir --no-deps .
