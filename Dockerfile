# Python 3.13 to match the machine this was developed and tested on.
FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# cron runs the schedule, util-linux provides flock (which stops two runs
# overlapping), and the rest are what Chromium needs to start.
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron util-linux ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Requirements first, so a code change does not reinstall everything.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Downloads Chromium and its system libraries. Boots, John Lewis, ASOS and
# Amazon need a real browser; without this they fetch nothing.
RUN scrapling install

COPY . .

RUN chmod +x entrypoint.sh scheduled_run.sh && mkdir -p /app/output
ENTRYPOINT ["/app/entrypoint.sh"]
