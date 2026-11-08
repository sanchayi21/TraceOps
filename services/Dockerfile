# One image for all three demo services. Build context is the repo root so
# the `services.*` package imports work exactly as they do on the host.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY services/requirements.txt /app/services/requirements.txt
RUN pip install --no-cache-dir -r /app/services/requirements.txt

COPY services /app/services

# SERVICE_MODULE and PORT are set per service in compose.yaml
CMD ["sh", "-c", "uvicorn services.${SERVICE_MODULE}.main:app --host 0.0.0.0 --port ${PORT}"]
