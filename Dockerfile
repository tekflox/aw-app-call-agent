FROM python:3.11-slim-bullseye

RUN apt-get update && apt-get install -y --no-install-recommends \
        asterisk ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY call_agent_app/ ./call_agent_app/
COPY ui/ ./ui/
COPY container/ ./container/
RUN chmod +x /app/container/entrypoint.sh && mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 AW_CALL_AGENT_DATA=/app/data PORT=9412 AW_APP_HOST=0.0.0.0
EXPOSE 9412/tcp 5060/udp 10000-10100/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9412/api/apps/call-agent/health',timeout=4)"
ENTRYPOINT ["/app/container/entrypoint.sh"]
