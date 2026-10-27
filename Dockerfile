FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY agents ./agents
COPY api ./api
COPY engine ./engine
COPY generator ./generator
COPY llm ./llm
COPY outputs ./outputs
COPY web ./web
COPY telemetry.py .

EXPOSE 8000
CMD ["sh", "-c", "python -m uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
