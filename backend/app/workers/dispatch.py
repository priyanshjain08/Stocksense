"""Run forecast jobs via Celery when ASYNC_FORECASTS=1, otherwise inline (dev/tests)."""
from __future__ import annotations

import os


def dispatch_forecast(db, run_id) -> None:
    if os.environ.get("ASYNC_FORECASTS") == "1":
        from app.workers.tasks import run_forecast_task
        run_forecast_task.delay(str(run_id))
    else:
        from app.modules.forecasting.service import execute_run
        try:
            execute_run(db, run_id)
        except Exception:  # failure is stored on the run (status=failed, error=...)
            pass
