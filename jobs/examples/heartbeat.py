"""Scheduled heartbeat: runs every minute to demonstrate cron scheduling.
Delete this file if you don't want it."""
from jobrunner.dsl import task

schedule = "* * * * *"
description = "Writes one log line per minute (scheduler demo)"


@task
def beat(ctx):
    ctx.log("tick")
