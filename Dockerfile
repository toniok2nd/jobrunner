FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/srv/jobrunner

RUN apt-get update && apt-get install -y --no-install-recommends \
      redis-server supervisor curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/jobrunner

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY jobrunner ./jobrunner
COPY docker/supervisord.main.conf /etc/supervisor/supervisord.conf
COPY supervisord.conf /etc/supervisor/conf.d/jobrunner.conf
COPY supervisord.beat.conf /etc/supervisor/conf.d/jobrunner-beat.conf
COPY docker/entrypoint.sh /entrypoint.sh
COPY jobs/examples /opt/jobrunner-seed/jobs

RUN chmod +x /entrypoint.sh && mkdir -p /srv/jobrunner/jobs /srv/jobrunner/data

# NOTE: no default secrets. JOBRUNNER_SECRET_KEY and JOBRUNNER_ADMIN_PASSWORD
# are generated randomly at first boot and persisted in the data volume
# (set them explicitly to control them).
ENV JOBRUNNER_JOBS_DIR=/srv/jobrunner/jobs \
    JOBRUNNER_DATA_DIR=/srv/jobrunner/data \
    JOBRUNNER_ADMIN_USER=admin \
    JOBRUNNER_ENABLE_BEAT=true

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
