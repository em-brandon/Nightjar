FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hub/ ./hub/
COPY seed.py alembic.ini ./
COPY migrations/ ./migrations/

# Run unprivileged. Nothing here needs root.
RUN useradd --create-home --uid 10001 nightjar && chown -R nightjar /app
USER nightjar

EXPOSE 8000
CMD ["uvicorn", "hub.main:app", "--host", "0.0.0.0", "--port", "8000"]
