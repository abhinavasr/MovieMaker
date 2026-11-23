FROM python:3.12-slim

# ffmpeg is the only system dependency: storyboard mode joins the per-shot
# clips with it, and lays an uploaded soundtrack over the result.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Runs as the host user (see compose) so the bind-mounted /data does not fill
# up with root-owned files the operator cannot delete.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 STUDIO_DATA_DIR=/data
EXPOSE 8120

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8120/healthz', timeout=8).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8120", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
