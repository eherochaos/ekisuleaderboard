FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV EIKETSU_ENV_ROOT=/app

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY assets ./assets
COPY frontend ./frontend
COPY src ./src

RUN pip install --no-cache-dir ".[server]"

EXPOSE 8000

CMD ["uvicorn", "eiketsu_env.server_app:app", "--host", "0.0.0.0", "--port", "8000"]
