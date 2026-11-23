FROM python:3.12-slim-bookworm

# Pango/HarfBuzz for WeasyPrint, plus decent fonts for reading.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libharfbuzz-subset0 \
        fonts-dejavu-core fonts-liberation fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/

ENV OUTPUT_DIR=/output
EXPOSE 8765
CMD ["gunicorn", "--bind", "0.0.0.0:8765", "--workers", "2", "--threads", "2", "--timeout", "120", "app.main:app"]
