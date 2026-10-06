FROM python:3.12-slim

LABEL org.opencontainers.image.version="0025"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY *.py ./
COPY METHODOLOGY.md METHODOLOGY_v0025.md README.md CHANGELOG_v0025.md AUDIT_v0025.md ./

RUN mkdir -p /data

CMD ["python", "main.py"]
