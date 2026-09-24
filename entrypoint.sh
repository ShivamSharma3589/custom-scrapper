#!/bin/bash
# Runs the scraper on a schedule inside the container.
#
#   CRON_SCHEDULE   when to run, cron syntax      (default: every 30 minutes)
#   RUN_ON_START    "true" to scrape immediately  (default: no)
#   TZ              timezone for the schedule     (default: UTC)
set -e

SCHEDULE="${CRON_SCHEDULE:-*/30 * * * *}"
mkdir -p /app/output

# cron starts with an almost empty environment, so anything passed with
# `docker run -e` would be invisible to the job. Save it here and have the
# job source it first. shlex.quote keeps passwords with quotes or spaces
# intact, which a plain sed cannot.
python - > /app/.cron_env <<'PY'
import os, shlex
skip = {"PWD", "SHLVL", "_", "OLDPWD", "HOME"}
for key, value in os.environ.items():
    if key not in skip and key.isidentifier():
        print(f"export {key}={shlex.quote(value)}")
PY
chmod 600 /app/.cron_env

echo "${SCHEDULE} /app/scheduled_run.sh >> /app/output/cron.log 2>&1" > /tmp/scrape-cron
crontab /tmp/scrape-cron

echo "scraper container ready"
echo "  schedule : ${SCHEDULE}   (TZ=${TZ:-UTC})"
echo "  output   : /app/output"
crontab -l | sed 's/^/  cron     : /'

if [ "${RUN_ON_START}" = "true" ]; then
  echo "RUN_ON_START is set, scraping now"
  /app/scheduled_run.sh >> /app/output/cron.log 2>&1 &
fi

# Follow the log so `docker logs` shows what the scraper is doing.
touch /app/output/cron.log
tail -F /app/output/cron.log &

exec cron -f
