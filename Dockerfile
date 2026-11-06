FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

COPY certs/ /usr/local/share/ca-certificates/
RUN update-ca-certificates

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt .

RUN uv pip install --system --no-cache --requirement requirements.txt

RUN useradd --uid 10001 --no-create-home app \
    && mkdir data \
    && chown app:app data

COPY backend/ backend/
COPY frontend/ frontend/
COPY input/ input/

USER 10001

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
