FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV TZ=America/New_York

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
