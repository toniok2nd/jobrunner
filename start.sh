docker run -d --name jobrunner -p 8080:8080 \
  -e JOBRUNNER_ADMIN_PASSWORD=secret \
  -e JOBRUNNER_SECRET_KEY=$(openssl rand -hex 32) \
  -v jobrunner-jobs:/srv/jobrunner/jobs \
  -v jobrunner-data:/srv/jobrunner/data \
  jobrunner              
