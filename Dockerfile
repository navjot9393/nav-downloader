# Nav Downloader: a web UI for a headless JDownloader 2. Standard library Python only.
FROM python:3.13-alpine@sha256:c50ccfb66e5cc97a610dcd0b9597de6ef5b52c96280b51997b353a0fc87a8361

# The published name, shown in the page's setup instructions (the release workflow sets it).
ARG IMAGE_NAME=nav-downloader

ENV JDWEB_PORT=30500 \
    JDWEB_DATA=/data \
    JD_API=http://127.0.0.1:3128 \
    JDWEB_IMAGE=${IMAGE_NAME} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY server.py setup_jd.py ./
COPY static ./static
RUN printf '#!/bin/sh\nexec python /app/setup_jd.py "$@"\n' > /usr/local/bin/setup-jd \
 && chmod 755 /usr/local/bin/setup-jd \
 && mkdir /data && chown 1000:1000 /data

# 1000:1000 matches jlesage/jdownloader-2's default user; change it with `user:`.
USER 1000:1000
VOLUME /data
EXPOSE 30500
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ['JDWEB_PORT'], timeout=3)"
CMD ["python", "/app/server.py"]
