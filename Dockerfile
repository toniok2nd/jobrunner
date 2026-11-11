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

ENV JOBRUNNER_JOBS_DIR=/srv/jobrunner/jobs \
    JOBRUNNER_DATA_DIR=/srv/jobrunner/data \
    JOBRUNNER_SECRET_KEY=change-me \
    JOBRUNNER_ADMIN_USER=admin \
    JOBRUNNER_ADMIN_PASSWORD=admin \
    JOBRUNNER_ENABLE_BEAT=true

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
