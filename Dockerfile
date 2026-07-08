FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app.py schema.sql seed.py backfill_heatmap_demo.py ./
COPY templates/ templates/
COPY static/ static/

# DB lives on a mounted volume at /app/data so it survives image rebuilds.
# Seeding at build time populates the image; on first container start Docker
# initializes the (empty) named volume from this path, and subsequent rebuilds
# leave the volume — and its data — untouched.
#
# /app/golden holds a pristine snapshot of the demo DB + seeded uploads. It
# lives OUTSIDE the volumes, so it always reflects the freshly-built state;
# the app restores from it on every logout when DEMO_RESET_ON_LOGOUT=1.
ENV DB_PATH=/app/data/arcconnect.db
RUN mkdir -p /app/data && python seed.py && python backfill_heatmap_demo.py \
    && mkdir -p /app/golden \
    && cp /app/data/arcconnect.db /app/golden/arcconnect.db \
    && cp -a static/uploads /app/golden/uploads
ENV GOLDEN_DB_PATH=/app/golden/arcconnect.db
ENV GOLDEN_UPLOADS_DIR=/app/golden/uploads

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 5005

ENV FLASK_APP=app.py
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5005/healthz').read()" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:5005", "--workers", "2", "--threads", "2", "app:app"]
