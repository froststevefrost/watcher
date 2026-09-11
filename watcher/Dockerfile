FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1
# Neutral default only — real value comes from TZ in .env / env_file.
ENV TZ=UTC

# Pull whatever security patches exist for the base image's own packages
# (musl, busybox, openssl) at build time, not just at image-publish time.
RUN apk update && apk upgrade --no-cache \
    && apk add --no-cache tzdata ca-certificates

RUN adduser -D -u 1000 watcher
WORKDIR /app

COPY --chown=watcher:watcher requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip setuptools wheel

COPY --chown=watcher:watcher watcher.py .

USER 1000:1000
CMD ["python", "-u", "watcher.py"]
