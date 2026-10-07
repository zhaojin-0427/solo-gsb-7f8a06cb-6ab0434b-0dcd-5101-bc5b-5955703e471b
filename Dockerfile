FROM python:3.11-slim

# FFmpeg + fonts-free image (the app draws frames itself)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY scripts ./scripts

RUN mkdir -p /srv/data/assets /srv/data/exports /srv/data/tmp
ENV DATA_DIR=/srv/data \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# single worker: in-process decode cache + SQLite connection
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
