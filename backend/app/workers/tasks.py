"""Celery app and tasks. Start with: celery -A app.workers.tasks worker -l info  (and `beat` for the schedule)."""
from __future__ import annotations

import uuid

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings
from app.database.base import SessionLocal

celery_app = Celery("stocksense", broker=get_settings().redis_url, backend=get_settings().redis_url)
celery_app.conf.beat_schedule = {"nightly-forecast-refresh": {"task": "stocksense.refresh_all_forecasts",
                                                              "schedule": crontab(hour=2, minute=0)}}


@celery_app.task(name="stocksense.run_forecast", bind=True, max_retries=2)
def run_forecast_task(self, run_id: str):
    from app.modules.forecasting.service import execute_run
    db = SessionLocal()
    try:
        execute_run(db, uuid.UUID(run_id))
    finally:
        db.close()


@celery_app.task(name="stocksense.refresh_all_forecasts")
def refresh_all_forecasts():
    from sqlalchemy import select
    from app.modules.models import Branch, ForecastRun
    db = SessionLocal()
    try:
        for b in db.scalars(select(Branch).where(Branch.status == "active")):
            run = ForecastRun(organization_id=b.organization_id, branch_id=b.id, horizon_days=30, granularity="daily",
                              configuration={"history_days": 180})
            db.add(run)
            db.commit()
            run_forecast_task.delay(str(run.id))
    finally:
        db.close()
