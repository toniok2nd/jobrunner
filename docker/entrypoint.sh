#!/bin/sh
set -e

JOBS_DIR="${JOBRUNNER_JOBS_DIR:-/srv/jobrunner/jobs}"
DATA_DIR="${JOBRUNNER_DATA_DIR:-/srv/jobrunner/data}"

mkdir -p "$JOBS_DIR" "$DATA_DIR/logs"

# Seed example jobs on first boot (only if the jobs volume is empty)
if [ -z "$(ls -A "$JOBS_DIR" 2>/dev/null)" ]; then
    cp -a /opt/jobrunner-seed/jobs/. "$JOBS_DIR/" 2>/dev/null || true
fi

# --- secrets bootstrap (secure by default) ------------------------------------
# Never run with the shipped placeholder secrets. If the operator did not
# provide them (or left the insecure defaults), generate strong random values,
# persist them in the data volume, and reuse them on every subsequent boot.

SECRET_FILE="$DATA_DIR/.secret_key"
if [ -z "${JOBRUNNER_SECRET_KEY:-}" ] || [ "$JOBRUNNER_SECRET_KEY" = "change-me" ]; then
    if [ -f "$SECRET_FILE" ]; then
        export JOBRUNNER_SECRET_KEY="$(cat "$SECRET_FILE")"
    else
        export JOBRUNNER_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
        printf '%s' "$JOBRUNNER_SECRET_KEY" > "$SECRET_FILE"
        chmod 600 "$SECRET_FILE"
        echo "==> Generated a random JOBRUNNER_SECRET_KEY (persisted in $SECRET_FILE)"
        echo "    Set the env var explicitly to control it."
    fi
fi

PW_FILE="$DATA_DIR/.admin_password"
if [ -z "${JOBRUNNER_ADMIN_PASSWORD:-}" ] || [ "$JOBRUNNER_ADMIN_PASSWORD" = "admin" ]; then
    if [ -f "$PW_FILE" ]; then
        export JOBRUNNER_ADMIN_PASSWORD="$(cat "$PW_FILE")"
    else
        export JOBRUNNER_ADMIN_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(16))')"
        printf '%s' "$JOBRUNNER_ADMIN_PASSWORD" > "$PW_FILE"
        chmod 600 "$PW_FILE"
        echo ""
        echo "=============================================================="
        echo "  Admin credentials (generated at first boot):"
        echo "    user:     ${JOBRUNNER_ADMIN_USER:-admin}"
        echo "    password: $JOBRUNNER_ADMIN_PASSWORD"
        echo "  (persisted in $PW_FILE inside the data volume — set"
        echo "   JOBRUNNER_ADMIN_PASSWORD to choose your own)"
        echo "=============================================================="
        echo ""
    fi
fi

# Disable the cron scheduler unless explicitly enabled
if [ "${JOBRUNNER_ENABLE_BEAT:-true}" != "true" ]; then
    rm -f /etc/supervisor/conf.d/jobrunner-beat.conf
fi

# Idempotent: create tables + seed admin user
python -c "from jobrunner.db import init_db; init_db()"

exec supervisord -n -c /etc/supervisor/supervisord.conf
