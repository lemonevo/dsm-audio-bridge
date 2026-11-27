FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MUSIC_DIR=/music \
    PORT=5050

RUN sed -i 's#https://dl-cdn.alpinelinux.org#https://mirrors.aliyun.com#g' /etc/apk/repositories \
 && apk add --no-cache ffmpeg \
 && rm -rf /usr/local/lib/python3.12/ensurepip \
           /usr/local/lib/python3.12/idlelib \
           /usr/local/lib/python3.12/lib2to3 \
           /usr/local/lib/python3.12/pydoc_data \
           /usr/local/lib/python3.12/tkinter \
           /usr/local/lib/python3.12/turtledemo \
           /usr/local/lib/python3.12/site-packages/pip \
           /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
           /usr/local/lib/python3.12/site-packages/setuptools \
           /usr/local/lib/python3.12/site-packages/setuptools-*.dist-info \
           /usr/local/lib/python3.12/site-packages/wheel \
           /usr/local/lib/python3.12/site-packages/wheel-*.dist-info \
           /root/.cache

WORKDIR /app
COPY probe_server.py /app/probe_server.py

RUN addgroup -g 1000 app \
 && adduser -u 1000 -G app -D -H -s /sbin/nologin app \
 && mkdir -p /app/cache /app/logs \
 && chown -R 1000:1000 /app

USER 1000:1000
EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5050/webapi/query.cgi?api=SYNO.API.Info&version=1&method=query', timeout=4)"

CMD ["python3", "probe_server.py"]