FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend ./backend

RUN pip install --no-cache-dir \
        "alembic==1.16.1" \
        "celery==5.5.3" \
        "fastapi==0.115.12" \
        "psycopg[binary]==3.2.9" \
        "pydantic==2.11.4" \
        "pydantic-settings==2.9.1" \
        "redis==6.2.0" \
        "SQLAlchemy==2.0.41" \
        "uvicorn==0.34.2" \
    && pip install --no-cache-dir --no-deps .
