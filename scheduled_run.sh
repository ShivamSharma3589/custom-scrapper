#!/bin/bash
# What cron runs. Kept as its own file so the crontab line stays readable.
#
# flock -n: if the previous scrape is still going, skip this one rather than
# start a second alongside it. Two browsers at once once turned a 54-minute
# run into 30 hours.
#
# -E 99 gives lock failure its own exit code, so it is not confused with
# scrape_job.py returning 1 because a retailer produced nothing.

. /app/.cron_env
cd /app || exit 1

flock -n -E 99 /app/output/.scrape_job.lock python /app/scrape_job.py
status=$?

if [ "$status" -eq 99 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S')  skipped: the previous run is still going"
fi

exit 0
