#!/bin/sh
set -e

JOBS_DIR="${JOBRUNNER_JOBS_DIR:-/srv/jobrunner/jobs}"
DATA_DIR="${JOBRUNNER_DATA_DIR:-/srv/jobrunner/data}"

mkdir -p "$JOBS_DIR" "$DATA_DIR/logs"

# Seed example jobs on first boot (only if the jobs volume is empty)
if [ -z "$(ls -A "$JOBS_DIR" 2>/dev/null)" ]; then
    cp -a /opt/jobrunner-seed/jobs/. "$JOBS_DIR/" 2>/dev/null || true
fi

# Disable the cron scheduler unless explicitly enabled
if [ "${JOBRUNNER_ENABLE_BEAT:-true}" != "true" ]; then
    rm -f /etc/supervisor/conf.d/jobrunner-beat.conf
fi

# Idempotent: create tables + seed admin user
python -c "from jobrunner.db import init_db; init_db()"

exec supervisord -n -c /etc/supervisor/supervisord.conf
