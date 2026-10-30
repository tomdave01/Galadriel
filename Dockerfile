FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN groupadd --system overseer && useradd --system --gid overseer --home-dir /app overseer
COPY agents ./agents
COPY api ./api
COPY engine ./engine
COPY feed ./feed
COPY generator ./generator
COPY llm ./llm
COPY outputs ./outputs
COPY web ./web
COPY telemetry.py .

EXPOSE 8000
USER overseer
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
