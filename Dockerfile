# Python 3.13 to match the machine this was developed and tested on.
FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code change does not reinstall everything.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Downloads Chromium and its system libraries. Boots and John Lewis need a
# real browser; without this they fetch nothing.
RUN scrapling install

# app/ becomes /app, so the code keeps the same layout it has on disk.
COPY app/ /app/

RUN mkdir -p /app/output
CMD ["python", "/app/scrape_job.py"]
