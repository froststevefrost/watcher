FROM python:3.14.7-alpine3.24 AS builder

RUN apk add --no-cache \
     gcc musl-dev libffi-dev openssl-dev cargo \
     tzdata ca-certificates

WORKDIR /src

COPY requirements.txt .

RUN pip wheel \
    --no-cache-dir \
    --wheel-dir=/wheels \
    -r requirements.txt

# ----------  Runtime stage ----------
FROM python:3.14.7-alpine3.24
ENV PYTHONUNBUFFERED=1 TZ=UTC

RUN apk add --no-cache tzdata ca-certificates && \
     adduser -D -u 1000 watcher

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY --from=builder /src/requirements.txt .

RUN pip install \
    --no-cache-dir \
    --no-index \
    --no-deps \
    /wheels/*.whl && \
    pip uninstall -y pip setuptools wheel && \
    rm -rf /wheels

COPY --chown=watcher:watcher watcher.py .

USER 1000:1000

CMD ["python", "-u", "watcher.py"]
