FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN groupadd --system overseer && useradd --system --gid overseer --home-dir /app overseer
RUN printf '%s\n' '#!/bin/sh' 'exec python -m feed.simulator' > /usr/local/bin/overseer-simulator \
    && chmod 0755 /usr/local/bin/overseer-simulator
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
CMD ["sh", "-c", "python -m uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
