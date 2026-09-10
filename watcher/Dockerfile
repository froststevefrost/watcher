FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
# Neutral default only — the real value should be set via TZ in .env /
# compose.yaml so the 07:00-16:00 polling window matches each deployment's
# local time. Leaving this hardcoded to one timezone would silently give
# wrong polling hours to anyone who forgets to set TZ.
ENV TZ=UTC

RUN useradd -u 1000 -m watcher
WORKDIR /app

COPY --chown=watcher:watcher requirements.txt .

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=watcher:watcher watcher.py .

USER 1000:1000
CMD ["python", "-u", "watcher.py"]
