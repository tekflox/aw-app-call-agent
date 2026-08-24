FROM debian:bookworm-slim AS asterisk-build

ARG ASTERISK_VERSION=20.20.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl libedit-dev libjansson-dev \
        libsqlite3-dev libssl-dev libxml2-dev pkg-config uuid-dev \
    && curl -fsSL \
        "https://downloads.asterisk.org/pub/telephony/asterisk/releases/asterisk-${ASTERISK_VERSION}.tar.gz" \
        -o /asterisk.tar.gz \
    && mkdir /src && tar -xzf /asterisk.tar.gz -C /src --strip-components=1 \
    && cd /src \
    && ./configure --prefix=/usr --with-jansson-bundled --with-pjproject-bundled \
    && make menuselect.makeopts \
    && menuselect/menuselect --enable app_audiosocket --enable res_audiosocket \
         --enable func_uuid menuselect.makeopts \
    && make -j"$(nproc)" \
    && make DESTDIR=/stage install \
    && test -f /stage/usr/lib/asterisk/modules/app_audiosocket.so \
    && test -f /stage/usr/lib/asterisk/modules/func_uuid.so

FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates libedit2 libjansson4 libsqlite3-0 libssl3 \
        libxml2 libuuid1 libncurses6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=asterisk-build /stage/ /

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
