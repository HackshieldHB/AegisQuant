# AegisQuant — Production Docker image
# Multi-asset AI trading system. Use .env for secrets.

FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# App dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application
COPY . .
RUN mkdir -p logs Data/Models

# No root
RUN useradd -m -u 1000 aegis && chown -R aegis:aegis /app
USER aegis

ENV PYTHONUNBUFFERED=1
ENV AEGIS_ENVIRONMENT=VPS

# Default: run async engine (recommended for production)
CMD ["python", "Main_Async.py"]
